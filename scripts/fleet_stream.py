#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Run a headless agent and put one short line per step in the journal.

``fleet_agent.py`` starts ``claude -p ... --output-format stream-json --verbose`` through ``run()``, which reads
the agent's events as they come and prints a summary of each to stdout, which systemd sends to the journal:

    start model=claude-opus-5-5 claude=2.1.311 session=...
    tool Bash: gh pr diff 43
      → ok
    tool Bash: uv run pytest -q
      → exit 1
    tool Edit: /home/agent01/repo/src/app.py
      → denied: write_guard: ... outside the clone
    say: The test fails because ...
    done success turns=23 time=6m12s cost=$0.84 in=12 out=9450 cache_read=812345 cache_write=40211

What is printed: the tool and its target (a command, a path, a pattern), never a file body or an edit; how a tool
call ended (ok, its exit code, an error, or why it was refused), never its output; the first line of what the
agent says; failing hooks; and the run's totals. Every line is cut short, and the values of the secrets in the
agent's environment are replaced with ``***`` before printing, as a backstop. The complete record stays in the
agent's transcript under its home, readable by root.

The runner stays the agent's parent and exits with its code, or 128 plus the signal that ended it. When systemd
stops the unit it signals every process in it: the runner waits for the agent to exit, then reports it.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

ARG_MAX = 160
SAY_MAX = 200
SECRET_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
SECRET_MIN = 8  # shorter values would mask ordinary words
EXIT_RE = re.compile(r"Exit code (\d+)")

# The argument that says what a tool call is about, by tool. Anything else shows the tool's name only.
TARGETS = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern",),
    "Task": ("description",),
    "Agent": ("description",),
    "Skill": ("skill",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
}


def secrets_from(env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(v for k in SECRET_VARS if len(v := env.get(k, "")) >= SECRET_MIN)


def mask(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _target(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    if name == "Grep":
        pattern, path = args.get("pattern"), args.get("path")
        if isinstance(pattern, str):
            return f"{pattern} in {path}" if isinstance(path, str) and path else pattern
        return ""
    for key in TARGETS.get(name, ()):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _duration(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}s"


class Summarizer:
    """Turns one event at a time into journal lines. Keeps which call each result belongs to."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self.secrets = tuple(secrets)
        self.denied: set[str] = set()

    def lines(self, raw: str) -> list[str]:
        """Each line is masked whole, before it is cut, so a cut cannot leave half a secret showing."""
        raw = mask(raw, self.secrets)
        if not raw.strip():
            return []
        try:
            event = json.loads(raw)
        except ValueError:
            return [f"unparsed: {_cut(raw, SAY_MAX)}"]
        if not isinstance(event, dict):
            return [f"unparsed: {_cut(raw, SAY_MAX)}"]
        kind = event.get("type")
        if kind == "system":
            return self._system(event)
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._user(event)
        if kind == "result":
            return [self._result(event)]
        return []

    def _system(self, e: dict[str, Any]) -> list[str]:
        sub = e.get("subtype")
        if sub == "init":
            return [
                f"start model={e.get('model', '?')} claude={e.get('claude_code_version', '?')} "
                f"session={e.get('session_id', '?')}"
            ]
        if sub == "hook_response" and e.get("exit_code") not in (0, None):
            return [f"hook {e.get('hook_name', '?')} exit {e.get('exit_code')}"]
        if sub == "permission_denied":
            tool_id = e.get("tool_use_id")
            if isinstance(tool_id, str):
                self.denied.add(tool_id)
            reason = e.get("message") or e.get("decision_reason") or "refused"
            return [f"  → denied: {_cut(_first_line(str(reason)), SAY_MAX)}"]
        return []

    def _assistant(self, e: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for block in _blocks(e):
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "?")
                target = _target(name, block.get("input"))
                out.append(f"tool {name}: {_cut(target, ARG_MAX)}" if target else f"tool {name}")
            elif block.get("type") == "text":
                text = _first_line(str(block.get("text") or ""))
                if text:
                    out.append(f"say: {_cut(text, SAY_MAX)}")
        return out

    def _user(self, e: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for block in _blocks(e):
            if block.get("type") != "tool_result":
                continue
            if block.get("tool_use_id") in self.denied:
                continue  # already said why, on the denial
            if not block.get("is_error"):
                out.append("  → ok")
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else ""
            match = EXIT_RE.match(text)
            out.append(f"  → exit {match.group(1)}" if match else "  → error")
        return out

    def _result(self, e: dict[str, Any]) -> str:
        raw_usage = e.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        cost = e.get("total_cost_usd")
        cost_text = f"{cost:.2f}" if isinstance(cost, (int, float)) else "?"
        return (
            f"done {e.get('subtype', '?')} turns={e.get('num_turns', '?')} "
            f"time={_duration(e.get('duration_ms'))} cost=${cost_text} "
            f"in={usage.get('input_tokens', '?')} out={usage.get('output_tokens', '?')} "
            f"cache_read={usage.get('cache_read_input_tokens', '?')} "
            f"cache_write={usage.get('cache_creation_input_tokens', '?')}"
        )


def _blocks(e: dict[str, Any]) -> list[dict[str, Any]]:
    message = e.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def summarize_lines(raw: Iterable[str], secrets: Sequence[str] = ()) -> list[str]:
    s = Summarizer(secrets)
    return [line for r in raw for line in s.lines(r)]


def run(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Start the agent, print a line per step as it goes, and return its exit code."""
    summarizer = Summarizer(secrets_from(env))
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(f"fleet_stream: could not start {argv[0]}: {exc.strerror}", file=sys.stderr)
        return 127
    stopping: list[int] = []

    def note_stop(signum: int, _frame: object) -> None:
        # The agent got the same signal from systemd; keep reading until it has gone.
        stopping.append(signum)

    previous = signal.signal(signal.SIGTERM, note_stop)
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            for line in summarizer.lines(raw):
                print(line, flush=True)
        code = proc.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)
    if code < 0:
        print(f"stopped by signal {-code}", flush=True)
        return 128 - code
    if stopping:
        print(f"stopped: the unit was asked to stop; the agent exited {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], {}))
