#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""PreToolUse hook: an agent writes only inside its own clone.

Two agents that share a checkout overwrite each other's work, and an agent that edits ``~/.claude`` or a
neighbour's clone changes the fleet, not its task. This hook refuses a write whose target is outside
the agent's clone.

It acts only on a fleet agent: ``FLEET_CLONE`` must be set (the agent's service unit does that). The
agent may also write to the directories in ``FLEET_ALLOW_WRITE`` (a ``os.pathsep``-separated list) and to
the system temp directory. Anything else is refused, and so is any write whose target cannot be worked out
(an unset variable, a brace expansion, a process substitution).

What it checks:

* ``Edit``, ``Write``, ``MultiEdit`` and ``NotebookEdit``: the target path, with symlinks resolved.
* ``Bash``: redirections (``>``, ``>>``, ``tee``) and the targets of common writers (``rm``, ``mv``, ``cp``,
  ``touch``, ``mkdir``, ``sed -i``, ``dd of=``, ``tar -x``, ``curl -o``, ``git clone`` and more), following
  ``cd``, ``sudo``/``env`` wrappers, ``bash -c`` and ``eval`` strings, ``$(...)`` and ``find -exec``.
* The agent's own safety settings, even inside its clone: ``.claude/settings*.json``, ``.claude/hooks`` and
  ``.git/hooks``, because an agent that can edit them can switch the guards off.

What it cannot see: code that writes from inside a program (``python -c``, ``node -e``, a build script, a test
that writes), and reads. This hook is a guard against mistakes. The boundary that holds is the agent's OS user:
the clone is the only directory that user can write (docs/control-plane.md). It fails closed: a crash refuses
the call.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from typing import Any

FILE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
PROTECTED = (
    re.compile(r"(^|/)\.claude/settings[^/]*\.json$"),
    re.compile(r"(^|/)\.claude/hooks(/|$)"),
    re.compile(r"(^|/)\.git/hooks(/|$)"),
)
GLOB_CHARS = "*?["
MAX_DEPTH = 4

# every non-option argument is a target
ALL_ARGS = {
    "touch",
    "mkdir",
    "rm",
    "rmdir",
    "truncate",
    "tee",
    "unlink",
    "shred",
    "mkfifo",
    "mktemp",
    "mv",
}
# the first non-option argument is a mode or owner, the rest are targets
SKIP_FIRST = {"chmod", "chown", "chgrp"}
# the last non-option argument is the destination (or -t DIR)
LAST_ARG = {"cp", "install", "ln", "rsync"}
SHELLS = {"sh", "bash", "zsh", "dash", "ash", "ksh"}
WRAPPERS = {
    "sudo",
    "env",
    "nohup",
    "time",
    "command",
    "exec",
    "builtin",
    "nice",
    "setsid",
    "timeout",
    "stdbuf",
    "doas",
}
SEPARATOR_CHARS = set(";&|()")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class Unknown(Exception):
    """A write target that cannot be worked out, which is refused."""


def real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def zones() -> list[str]:
    out = []
    clone = os.environ.get("FLEET_CLONE")
    if clone:
        out.append(real(clone))
    for extra in os.environ.get("FLEET_ALLOW_WRITE", "").split(os.pathsep):
        if extra.strip():
            out.append(real(extra.strip()))
    out.append(real(tempfile.gettempdir()))
    return out


def inside(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:  # different drives
            continue
    return False


def resolve(raw: str, cwd: str | None) -> str:
    """The real absolute path a write to *raw* would reach, or Unknown."""
    if raw == os.devnull or raw == "/dev/null":
        return os.devnull
    if any(c in raw for c in "${},`") or "$" in raw:
        raise Unknown(f"cannot tell where {raw!r} points (shell expansion)")
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        if cwd is None:
            raise Unknown(f"the working directory is not known, so {raw!r} cannot be placed")
        path = os.path.join(cwd, path)
    cut = min((path.find(c) for c in GLOB_CHARS if c in path), default=-1)
    if (
        cut >= 0
    ):  # a wildcard: the directory before it must be inside, and nothing may climb after it
        rest = path[cut:].replace("\\", "/").split("/")
        if ".." in rest:
            raise Unknown(f"cannot tell where {raw!r} points (wildcard before '..')")
        path = os.path.dirname(path[:cut] + "x") or path[:cut]
    return real(path)


def check_target(raw: str, cwd: str | None, roots: list[str]) -> str | None:
    """A refusal message for a write to *raw*, or None when it is allowed."""
    try:
        path = resolve(raw, cwd)
    except Unknown as why:
        return str(why)
    if path == os.devnull:
        return None
    unix = path.replace("\\", "/")
    if any(p.search(unix) for p in PROTECTED):
        return f"{raw!r} is one of the agent's safety settings, which an agent may not change"
    if not inside(path, roots):
        return f"{raw!r} is outside this agent's clone"
    return None


# --- Bash --------------------------------------------------------------------------------------------


def strip_heredocs(command: str) -> str:
    out: list[str] = []
    wait: str | None = None
    for line in command.split("\n"):
        if wait is not None:
            if line.strip() == wait:
                wait = None
            continue
        out.append(line)
        found = HEREDOC.search(line)
        if found:
            wait = found.group(2)
    return "\n".join(out)


def split_statements(command: str) -> tuple[list[list[str]], list[str]]:
    """(statements as word lists, redirection targets), from one shell command line."""
    lexer = shlex.shlex(
        strip_heredocs(command).replace("\n", " ; "), posix=True, punctuation_chars=True
    )
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError as why:
        raise Unknown(f"cannot parse the command ({why})") from why
    statements: list[list[str]] = [[]]
    targets: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        punct = set(word) <= set(";&|()<>")
        if punct and ("<" in word or ">" in word) and "(" in word:
            raise Unknown("a process substitution, which cannot be checked")
        if punct and ">" in word:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if "&" in word and (nxt.isdigit() or nxt == "-"):
                i += 2  # '>&2': a file descriptor, not a file
                continue
            if not nxt or "(" in nxt:
                raise Unknown("a redirection whose target cannot be worked out")
            targets.append(nxt)
            i += 2
            continue
        if punct and "<" in word:
            i += 2  # input redirection, here-document or here-string: the next word is read, not written
            continue
        if punct and set(word) <= SEPARATOR_CHARS:
            if statements[-1]:
                statements.append([])
            i += 1
            continue
        statements[-1].append(word)
        i += 1
    return [s for s in statements if s], targets


def positional(args: list[str]) -> list[str]:
    out, done = [], False
    for a in args:
        if done:
            out.append(a)
        elif a == "--":
            done = True
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


def option_value(args: list[str], short: str, long: str) -> str | None:
    for i, a in enumerate(args):
        if a in (short, long):
            return args[i + 1] if i + 1 < len(args) else None
        if long and a.startswith(long + "="):
            return a.split("=", 1)[1]
        if (
            short
            and len(short) == 2
            and a.startswith(short)
            and len(a) > 2
            and not a.startswith("--")
        ):
            return a[2:]
    return None


# options of a wrapper that take a value, so the value is not mistaken for the command
WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group", "--host"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-S", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"},
}


def unwrap(words: list[str]) -> list[str]:
    """Drop leading VAR=value words and wrapper commands (sudo, env, nohup...)."""
    while words:
        if ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif os.path.basename(words[0]) in WRAPPERS:
            name = os.path.basename(words[0])
            words = words[1:]
            takes_value = WRAPPER_VALUE_OPTIONS.get(name, set())
            while words and (
                words[0].startswith("-") or ASSIGNMENT.match(words[0]) or words[0].isdigit()
            ):
                if name == "env" and words[0] in {"-C", "--chdir"}:
                    raise Unknown("env changes the working directory, which cannot be followed")
                skip = 2 if words[0] in takes_value else 1
                words = words[skip:]
            if name == "timeout" and words and re.fullmatch(r"[0-9.]+[smhd]?", words[0]):
                words = words[1:]
        else:
            break
    return words


Pair = tuple[str, "str | None"]


def writes_of(words: list[str], cwd: str | None, depth: int) -> tuple[list[Pair], str | None]:
    """([(raw target, cwd to resolve it against)], new working directory) for one statement."""
    words = unwrap(words)
    if not words:
        return [], cwd
    cmd, args = os.path.basename(words[0]), words[1:]
    out: list[str] = []
    if cmd in {"cd", "pushd"}:
        pos = positional(args)
        try:
            return [], (resolve(pos[0], cwd) if pos else None)
        except Unknown:
            return [], None
    if cmd in SHELLS or cmd == "eval":
        text = None
        if cmd == "eval":
            text = " ".join(args)
        else:
            for i, a in enumerate(args):
                if re.fullmatch(r"-[a-z]*c[a-z]*", a) and i + 1 < len(args):
                    text = args[i + 1]
                    break
        if text is not None:
            return collect(text, cwd, depth + 1), cwd
        return [], cwd
    if cmd in ALL_ARGS:
        out = positional(args)
        if cmd == "mktemp" and not out:
            out = []
    elif cmd in SKIP_FIRST:
        out = positional(args)[1:]
    elif cmd in LAST_ARG:
        into = option_value(args, "-t", "--target-directory")
        pos = positional(args)
        out = [into] if into else pos[-1:]
    elif cmd == "sed" and any(
        a == "--in-place" or a.startswith("--in-place=") or re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z]*", a)
        for a in args
    ):
        pos = positional(args)
        scripted = any(
            a in {"-e", "-f"} or a.startswith("--expression") or a.startswith("--file")
            for a in args
        )
        out = pos if scripted else pos[1:]
    elif cmd == "dd":
        out = [a[3:] for a in args if a.startswith("of=")]
    elif cmd == "tar":
        mode = next((a for a in args if a.startswith("-") and not a.startswith("--")), "")
        first = args[0] if args else ""
        extracts = (
            "x" in mode
            or ("x" in first and not first.startswith("-"))
            or "--extract" in args
            or "--get" in args
        )
        if extracts:
            out = [option_value(args, "-C", "--directory") or "."]
    elif cmd == "unzip":
        out = [option_value(args, "-d", "") or "."]
    elif cmd == "curl":
        dest = option_value(args, "-o", "--output")
        if dest:
            out = [dest]
        elif any(a in {"-O", "--remote-name"} for a in args):
            out = ["."]
    elif cmd == "wget":
        dest = option_value(args, "-O", "--output-document")
        if dest != "-":
            out = [dest] if dest else [option_value(args, "-P", "--directory-prefix") or "."]
    elif cmd == "git":
        where = option_value(args, "-C", "")
        if where:
            out.append(where)
        pos = positional([a for a in args if a != where])
        if pos[:1] == ["clone"]:
            rest = pos[1:]
            out.append(
                rest[1]
                if len(rest) > 1
                else (rest[0].rstrip("/").split("/")[-1].removesuffix(".git") if rest else ".")
            )
        if pos[:2] == ["worktree", "add"] and len(pos) > 2:
            out.append(pos[2])
    elif cmd == "find":
        return [(t, cwd) for t in find_writes(args, cwd, depth)], cwd
    elif cmd == "xargs":
        inner = unwrap([a for a in args if not a.startswith("-")])
        if inner and os.path.basename(inner[0]) in ALL_ARGS | SKIP_FIRST | LAST_ARG | {
            "sed",
            "dd",
            "tar",
        }:
            raise Unknown(f"xargs {inner[0]}: the targets are not known until it runs")
    return [(t, cwd) for t in out], cwd


def find_writes(args: list[str], cwd: str | None, depth: int) -> list[str]:
    """find's starting points when it deletes or executes, plus what -exec runs on them."""
    exprs = [
        i
        for i, a in enumerate(args)
        if a.startswith("-") and a not in {"-H", "-L", "-P"} or a in {"(", "!"}
    ]
    start = exprs[0] if exprs else len(args)
    roots = [a for a in args[:start] if not a.startswith("-")] or ["."]
    acts = {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprintf",
        "-fprint0",
        "-fls",
    }
    if not any(a in acts for a in args):
        return []
    out = list(roots)
    for i, a in enumerate(args):
        if a in {"-fprint", "-fprintf", "-fprint0", "-fls"} and i + 1 < len(args):
            out.append(args[i + 1])
        if a in {"-exec", "-execdir", "-ok", "-okdir"}:
            body = []
            for b in args[i + 1 :]:
                if b in {";", "+", "\\;"}:
                    break
                body.append(roots[0] if b == "{}" else b)
            more, _ = writes_of(body, cwd, depth + 1)
            out += [t for t, _base in more]
    return out


def collect(command: str, cwd: str | None, depth: int = 0) -> list[Pair]:
    """Every (raw path, cwd) a shell command line would write, following cd. Raises Unknown."""
    if depth >= MAX_DEPTH:
        raise Unknown("shell commands nested too deeply to check")
    text = strip_heredocs(command)
    if re.search(r"\$\([^()]*\(|\$\(\(", text):
        raise Unknown("command substitution too complex to check")
    found: list[Pair] = []
    for inner in SUBSTITUTION.findall(text):
        found += collect("".join(inner), cwd, depth + 1)
    statements, redirects = split_statements(SUBSTITUTION.sub(" X ", text))
    found += [(t, cwd) for t in redirects]
    here = cwd
    for words in statements:
        targets, here = writes_of(words, here, depth)
        found += targets
    return found


def bash_refusal(command: str, cwd: str | None, roots: list[str]) -> str | None:
    try:
        pairs = collect(command, cwd)
    except Unknown as why:
        return str(why)
    for raw, base in pairs:
        refusal = check_target(raw, base, roots)
        if refusal:
            return refusal
    return None


# --- the hook ----------------------------------------------------------------------------------------


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{reason}. Work only inside your own clone ({os.environ.get('FLEET_CLONE')}).",
        }
    }


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The hook's output for one tool call, or None to allow it."""
    if not os.environ.get("FLEET_CLONE"):
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    roots = zones()
    if tool in FILE_TOOLS:
        raw = tool_input.get(FILE_TOOLS[tool])
        if not isinstance(raw, str) or not raw:
            return deny(f"{tool} without a file path")
        refusal = check_target(raw, cwd, roots)
        return deny(refusal) if refusal else None
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return deny("Bash without a command")
        refusal = bash_refusal(command, cwd, roots)
        return deny(refusal) if refusal else None
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        out = decide(payload) if isinstance(payload, dict) else None
    except Exception as exc:  # fail closed: a safety guard never allows on its own bug
        out = deny(f"write_guard could not check this call ({exc})")
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
