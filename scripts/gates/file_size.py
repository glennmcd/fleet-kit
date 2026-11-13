"""Gate: no source file grows past the line cap without a recorded exception.

Incident (Marquardt): "Agents love to keep appending to one file." A long file costs every later agent
context tokens and attention, so the cap is a cost control as much as a style rule.

Config (`.fleet-gates.json`):

    "file_size": {
      "cap": 800,
      "include": ["**/*.py", "**/*.ts"], "exclude": ["**/schema.d.ts"],
      "exceptions": {"path/to/file.py": {"max_lines": 950, "reason": "...", "issue": "#12"}}
    }

An exception is a ceiling that only goes down: the file may not grow past `max_lines`, and an exception
whose file no longer exceeds the cap is stale and fails, so it gets removed. The ratchet gate refuses a
change that raises the cap or an exception.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from gates.common import ISSUE_REF, Finding, list_files, norm_path, read_lines, run_gate

RULE = "file-size"
DEFAULT_CAP = 800
DEFAULT_INCLUDE = ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs", "**/*.css"]


def line_count(path: Path) -> int:
    lines = read_lines(path)
    return len(lines) - 1 if lines and lines[-1] == "" else len(lines)


def check(
    root: Path, config: dict[str, Any], args: argparse.Namespace | None = None
) -> list[Finding]:
    section = config.get("file_size", {})
    cap = int(section.get("cap", DEFAULT_CAP))
    exceptions = {norm_path(k): v for k, v in section.get("exceptions", {}).items()}
    files = list_files(root, section.get("include", DEFAULT_INCLUDE), section.get("exclude", []))
    found: list[Finding] = []
    for f in files:
        n = line_count(root / f)
        exc = exceptions.get(f)
        if exc is None:
            if n > cap:
                found.append(Finding(f, 1, RULE, f"{n} lines, over the cap of {cap}; split it"))
            continue
        ceiling = int(exc.get("max_lines", cap))
        if n > ceiling:
            found.append(Finding(f, 1, RULE, f"{n} lines, over its exception ceiling of {ceiling}"))
        elif n <= cap:
            found.append(
                Finding(f, 1, RULE, f"{n} lines is under the cap; remove the stale exception")
            )
    for path, exc in exceptions.items():
        if not (root / path).exists():
            found.append(
                Finding(path, 1, RULE, "exception for a file that does not exist; remove it")
            )
        if not exc.get("reason") or not ISSUE_REF.search(str(exc.get("issue", ""))):
            found.append(
                Finding(path, 1, RULE, "an exception needs a `reason` and an `issue` (#123)")
            )
        if "max_lines" not in exc:
            found.append(Finding(path, 1, RULE, "an exception needs a `max_lines` ceiling"))
    return found


def main(argv: list[str] | None = None) -> int:
    return run_gate("file_size", check, argv)


if __name__ == "__main__":
    sys.exit(main())
