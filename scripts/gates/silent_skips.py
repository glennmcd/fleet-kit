"""Gate: a skipped, expected-to-fail or focused test fails the build unless it names an issue.

Incident (Marquardt, "Guardrails for coding agents"): when something a test needed was missing, the test
quietly skipped itself, so a broken feature looked like a passing run. A skip is allowed only with an
issue reference (`#123` or an issues/pull URL) on the same line, the line above or the next three lines.
`.only` is never allowed: it silently drops every other test in the file.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Any

from gates.common import ISSUE_REF, Finding, list_files, read_lines, run_gate

RULE = "silent-skip"
DEFAULT_INCLUDE = [
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    "**/tests/**/*.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/tests/**/*.ts",
    "**/e2e/**/*.ts",
]
PY_SKIP_NAMES = {
    "skip",
    "skipif",
    "skipunless",
    "xfail",
    "importorskip",
    "skiptest",
    "skipped",
    "expectedfailure",
    "add_marker",
}
TS_SKIP = re.compile(
    r"""(?x)
    \b(?:it|test|describe|suite|bench)\s*(?:\.\s*\w+\s*)*\.\s*(?:skip|skipIf|fixme|todo|fails|failing)\b
  | \bx(?:it|describe|test)\b\s*[.(]
  | \[\s*['"](?:skip|skipIf|fixme|only)['"]\s*\]
  | \.\s*(?:skip|fixme)\s*\(
    """
)
TS_ONLY = re.compile(r"""(?:\.\s*only\b|\[\s*['"]only['"]\s*\])""")


def _has_issue(lines: list[str], line_no: int) -> bool:
    window = lines[max(0, line_no - 2) : line_no + 3]
    return any(ISSUE_REF.search(w) for w in window)


def _py_findings(path: str, lines: list[str]) -> list[Finding]:
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return [Finding(path, 1, RULE, "cannot parse the file, so its skips cannot be checked")]
    found: list[Finding] = []
    for node in ast.walk(tree):
        name = ""
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # getattr(pytest.mark, "skip"), pytest.mark.__getattr__("skip")
            name = node.value
        if name.lower() not in PY_SKIP_NAMES:
            continue
        if isinstance(node, ast.Constant) and not _is_getattr_arg(tree, node):
            continue
        if isinstance(node, ast.Name) and not _is_call_or_decorator(tree, node):
            continue
        lineno = getattr(node, "lineno", 1)
        if not _has_issue(lines, lineno):
            found.append(Finding(path, lineno, RULE, f"`{name}` without an issue reference"))
    return found


def _is_getattr_arg(tree: ast.AST, const: ast.Constant) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"getattr", "__getattr__"}
        and const in n.args
        for n in ast.walk(tree)
    )


def _is_call_or_decorator(tree: ast.AST, name: ast.Name) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.func is name:
            return True
        if (
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and name in n.decorator_list
        ):
            return True
        if isinstance(n, ast.Raise) and n.exc is name:
            return True
    return False


def _ts_findings(path: str, lines: list[str]) -> list[Finding]:
    found: list[Finding] = []
    for i, raw in enumerate(lines, start=1):
        text = raw.strip()
        if text.startswith(("//", "*", "/*")):
            continue
        if TS_ONLY.search(text):
            found.append(Finding(path, i, RULE, "`.only` drops every other test; never allowed"))
        elif TS_SKIP.search(text) and not _has_issue(lines, i):
            found.append(
                Finding(path, i, RULE, "skipped or disabled test without an issue reference")
            )
    return found


def check_text(path: str, text: str) -> list[Finding]:
    lines = text.replace("\r\n", "\n").split("\n")
    return _py_findings(path, lines) if path.endswith(".py") else _ts_findings(path, lines)


def check(
    root: Path, config: dict[str, Any], args: argparse.Namespace | None = None
) -> list[Finding]:
    section = config.get("silent_skips", {})
    files = list_files(root, section.get("include", DEFAULT_INCLUDE), section.get("exclude", []))
    found: list[Finding] = []
    for f in files:
        found.extend(check_text(f, "\n".join(read_lines(root / f))))
    return found


def main(argv: list[str] | None = None) -> int:
    return run_gate("silent_skips", check, argv)


if __name__ == "__main__":
    sys.exit(main())
