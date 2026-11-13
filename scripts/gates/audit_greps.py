"""Gate: patterns that must not appear in new code, each tied to the incident that produced it.

Incident (Marquardt): "warnings switched off rather than fixed". An agent that hit a type-checker or linter
error silenced it on that line, and the silenced line was sometimes where the bug was.

With `--base <commit>` only lines the change adds are checked (the pull-request mode). With `--all` the whole
tree is scanned, which is the weekly sweep for silenced warnings already in the code.

A rule is `{"id", "pattern", "paths", "incident", "message", "allow_if"}`. `incident` is required: a rule
nobody can explain gets deleted. `allow_if` is a regex that makes a match acceptable (default: an issue
reference on the same line, so a suppression always points at the work that will remove it).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from gates.common import ISSUE_REF, Finding, added_lines, list_files, matches, read_lines, run_gate

INCIDENT = "Marquardt: warnings were silenced instead of fixed (Guardrails, 'growing pains')"
CODE = ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs"]
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "id": "type-ignore",
        "pattern": r"#\s*type\s*:\s*ignore",
        "paths": ["**/*.py"],
        "message": "`# type: ignore` hides a type error; fix it or cite the issue",
    },
    {
        "id": "noqa",
        "pattern": r"#\s*noqa\b",
        "paths": ["**/*.py"],
        "message": "`# noqa` hides a lint error; fix it or cite the issue",
    },
    {
        "id": "ts-suppress",
        "pattern": r"@ts-(?:ignore|nocheck|expect-error)\b",
        "paths": ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
        "message": "TypeScript suppression hides a type error; fix it or cite the issue",
    },
    {
        "id": "lint-suppress",
        "pattern": r"(?:eslint-disable|biome-ignore|oxlint-disable)",
        "paths": CODE,
        "message": "lint suppression hides an error; fix it or cite the issue",
    },
    {
        "id": "bare-except",
        "pattern": r"^\s*except\s*:",
        "paths": ["**/*.py"],
        "message": "bare `except:` swallows every error, including the one being hunted",
    },
    {
        "id": "pragma-no-cover",
        "pattern": r"#\s*pragma\s*:\s*no\s+cover",
        "paths": ["**/*.py"],
        "message": "`pragma: no cover` hides untested code; test it or cite the issue",
    },
]


def rules_from(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = config.get("audit", {})
    disabled = set(section.get("disable", []))
    rules = [r for r in DEFAULT_RULES if r["id"] not in disabled] + list(section.get("rules", []))
    for r in rules:
        r.setdefault("incident", INCIDENT if r in DEFAULT_RULES else "")
    return rules


def validate(rules: list[dict[str, Any]]) -> list[Finding]:
    found = []
    for r in rules:
        if not r.get("incident"):
            found.append(
                Finding(
                    ".fleet-gates.json", 1, "audit-rule", f"rule `{r.get('id')}` cites no incident"
                )
            )
        if "id" not in r or "pattern" not in r:
            found.append(
                Finding(
                    ".fleet-gates.json", 1, "audit-rule", "every rule needs an id and a pattern"
                )
            )
    return found


def scan(
    path: str, line_no: int, text: str, rules: list[dict[str, Any]], excluded: list[str]
) -> list[Finding]:
    if matches(path, excluded):
        return []
    found = []
    for r in rules:
        if not matches(path, r.get("paths", ["**"])):
            continue
        if not re.search(r["pattern"], text, re.IGNORECASE):
            continue
        allow = re.compile(r["allow_if"]) if "allow_if" in r else ISSUE_REF
        if allow.search(text):
            continue
        found.append(
            Finding(path, line_no, r["id"], f"{r.get('message', r['pattern'])} ({r['incident']})")
        )
    return found


def check(root: Path, config: dict[str, Any], args: argparse.Namespace) -> list[Finding]:
    rules = rules_from(config)
    found = validate(rules)
    excluded = config.get("audit", {}).get("exclude", [])
    if getattr(args, "all", False) or not args.base:
        for f in list_files(root, ["**"], excluded):
            for i, text in enumerate(read_lines(root / f), start=1):
                found.extend(scan(f, i, text, rules, excluded))
    else:
        for path, i, text in added_lines(root, args.base):
            found.extend(scan(path, i, text, rules, excluded))
    return found


def _extra(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true", help="scan the whole tree, not just the diff")


def main(argv: list[str] | None = None) -> int:
    return run_gate("audit_greps", check, argv, _extra)


if __name__ == "__main__":
    sys.exit(main())
