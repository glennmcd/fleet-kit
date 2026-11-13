"""Gate: "if you touch X you also owe Y", enforced by a script instead of remembered.

Incident (Marquardt): a rule an agent has to remember is followed most of the time; an obligation a script
checks is followed every time. Each rule cites the incident that made it necessary.

Config (`.fleet-gates.json`):

    "obligations": [
      {"id": "src-needs-tests", "when": ["backend/src/**/*.py"], "also": ["backend/tests/**"],
       "except": ["**/__init__.py"], "incident": "#12: a route shipped without a test"}
    ]

A change that touches any path matching `when` (and not `except`) must also touch a path matching `also`.
Run with `--base <commit>`; the changed files are those of base...HEAD. A rename counts as touching both
its old and its new path, and so does a deletion, so moving a file out of a rule's reach does not escape it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from gates.common import Finding, changed_files, matches, norm_path, run_gate

RULE = "obligation"


def touched(changes: list[tuple[str, str, str | None]]) -> set[str]:
    paths: set[str] = set()
    for _status, path, old in changes:
        paths.add(norm_path(path))
        if old:
            paths.add(norm_path(old))
    return paths


def evaluate(rules: list[dict[str, Any]], paths: set[str]) -> list[Finding]:
    found = []
    for r in rules:
        rid = r.get("id", "?")
        if not r.get("incident"):
            found.append(
                Finding(".fleet-gates.json", 1, RULE, f"obligation `{rid}` cites no incident")
            )
            continue
        triggers = sorted(
            p for p in paths if matches(p, r["when"]) and not matches(p, r.get("except", []))
        )
        if triggers and not any(matches(p, r["also"]) for p in paths):
            owed = ", ".join(r["also"])
            found.append(
                Finding(
                    triggers[0],
                    1,
                    rule=f"{RULE}:{rid}",
                    message=f"changing this also needs a change in {owed} ({r['incident']})",
                )
            )
    return found


def check(root: Path, config: dict[str, Any], args: argparse.Namespace) -> list[Finding]:
    rules = config.get("obligations", [])
    if not rules:
        return []
    if not args.base:
        return [
            Finding(
                ".fleet-gates.json",
                1,
                RULE,
                "obligations need --base <commit> to know what changed",
            )
        ]
    return evaluate(rules, touched(changed_files(root, args.base)))


def main(argv: list[str] | None = None) -> int:
    return run_gate("obligations", check, argv)


if __name__ == "__main__":
    sys.exit(main())
