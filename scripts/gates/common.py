"""Shared helpers for the CI gates: findings, config, path matching and git diffs.

Every gate is a standard-library script that prints one `path:line: [rule] message` per finding and
exits 1 if there is any. The target repository keeps its settings in `.fleet-gates.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_NAME = ".fleet-gates.json"
# Never scanned: the kit's own checkout in CI, dependency and VCS directories.
ALWAYS_SKIP_DIRS = {".git", ".fleet-kit", "node_modules", ".venv", "__pycache__"}
ISSUE_REF = re.compile(r"(?:#|/issues/|/pull/)\d+")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def norm_path(path: str) -> str:
    """Spell a path one way: forward slashes, no `./`, no `a/../b`, no leading slash."""
    p = os.path.normpath(path.replace("\\", "/")).replace("\\", "/")
    return p.lstrip("/")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` do not. Case-sensitive, like git."""
    pattern = norm_path(pattern)
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns: Iterable[str]) -> bool:
    path = norm_path(path)
    return any(glob_to_regex(p).match(path) for p in patterns)


def load_config(root: Path, config: str | None = None) -> dict[str, Any]:
    file = Path(config) if config else root / CONFIG_NAME
    if not file.is_absolute() and config:
        file = root / file
    if not file.exists():
        return {}
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{file}: expected a JSON object")
    return data


def _walk(root: Path) -> Iterator[str]:
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ALWAYS_SKIP_DIRS)
        for f in files:
            yield norm_path(os.path.relpath(os.path.join(d, f), root))


def list_files(root: Path, include: list[str], exclude: list[str]) -> list[str]:
    """Files under root that match include and not exclude, as normalized relative paths."""
    names: Iterator[str]
    if (root / ".git").exists():
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", "replace")
        names = (n for n in out.split("\0") if n)
    else:
        names = _walk(root)
    result = []
    for name in names:
        n = norm_path(name)
        if any(part in ALWAYS_SKIP_DIRS for part in n.split("/")):
            continue
        if matches(n, include) and not matches(n, exclude) and (root / n).is_file():
            result.append(n)
    return sorted(result)


def read_lines(path: Path) -> list[str]:
    """Lines without terminators; CRLF, LF and a lone CR all end a line."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")


def changed_files(root: Path, base: str) -> list[tuple[str, str, str | None]]:
    """(status letter, path, old path for a rename) for base...HEAD."""
    result: list[tuple[str, str, str | None]] = []
    diff = git(root, "diff", "--name-status", "-M", "-z", f"{base}...HEAD")
    fields = [f for f in diff.split("\0") if f]
    i = 0
    while i < len(fields):
        status = fields[i][0]
        if status in "RC":
            result.append((status, norm_path(fields[i + 2]), norm_path(fields[i + 1])))
            i += 3
        else:
            result.append((status, norm_path(fields[i + 1]), None))
            i += 2
    return result


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(root: Path, base: str) -> Iterator[tuple[str, int, str]]:
    """(path, line number, text) for every line base...HEAD adds."""
    path = ""
    line_no = 0
    for raw in git(root, "diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...HEAD").split(
        "\n"
    ):
        if raw.startswith("+++ "):
            path = norm_path(raw[6:]) if raw.startswith("+++ b/") else ""
        elif (m := _HUNK.match(raw)) is not None:
            line_no = int(m.group(1))
        elif raw.startswith("+") and path:
            yield path, line_no, raw[1:].rstrip("\r")
            line_no += 1


def run_gate(
    name: str,
    check: Callable[[Path, dict[str, Any], argparse.Namespace], list[Finding]],
    argv: list[str] | None = None,
    extra_args: Callable[[argparse.ArgumentParser], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=name)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default=None)
    parser.add_argument("--base", default=None, help="base commit for diff-based gates")
    if extra_args:
        extra_args(parser)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    config = load_config(root, args.config)
    findings = sorted(check(root, config, args))
    for f in findings:
        print(f)
    print(f"{name}: {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0
