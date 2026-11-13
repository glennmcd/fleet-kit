"""Gate: a block of code copied verbatim from elsewhere in the repository blocks the merge.

Incident (Marquardt): "an agent that needed something close to existing code often copied the block and
changed it, instead of finding and reusing what was there." A fix to one copy then left the others broken.

How: each file is reduced to its significant lines (whitespace collapsed, blank lines, comment-only lines,
imports and trivial lines such as `}` dropped), and every run of `min_lines` significant lines is hashed.
A hash found in more places than the baseline allows is a new duplicate. So indentation, trailing spaces,
line endings, blank lines and inserted comments do not hide a copy.

Known limits, on purpose, because the check is a line hash and not a parser: a copy with renamed variables
or reordered lines is not found, and neither is one with a real code line inserted into it. Review catches
those.

Config (`.fleet-gates.json`):

    "duplication": {"min_lines": 8, "min_chars": 160, "include": [...], "exclude": [...],
                    "baseline": ".fleet-gates/duplication-baseline.json"}

Existing duplicates go into the baseline once (`--write-baseline`) so only new copies fail. The baseline
may only shrink; the ratchet gate refuses a change that grows it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from gates.common import Finding, list_files, read_lines, run_gate

RULE = "duplicate-code"
DEFAULT_INCLUDE = ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs"]
DEFAULT_EXCLUDE = [
    "**/tests/**",
    "**/test_*.py",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*.d.ts",
    "**/migrations/**",
]
DEFAULT_BASELINE = ".fleet-gates/duplication-baseline.json"
COMMENT = re.compile(r"^(#|//|/\*|\*|\*/)")
IMPORT = re.compile(r"^(import\b|from\s+\S+\s+import\b|export\s+\*|export\s*\{.*\}\s*from\b)")
TRIVIAL = re.compile(
    r"^[\s\W]{0,3}$|^(else:?|try:|finally:|pass|return|break|continue|\)\s*[:;,]?|\}\s*[);,]*|\]\s*[);,]*)$"
)

Occurrence = tuple[str, int, int]  # file, first original line, last original line


def significant(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, raw in enumerate(lines, start=1):
        text = " ".join(raw.split())
        if not text or COMMENT.match(text) or IMPORT.match(text) or TRIVIAL.match(text):
            continue
        out.append((i, text))
    return out


def windows(
    path: str, lines: list[str], min_lines: int, min_chars: int
) -> list[tuple[str, Occurrence]]:
    sig = significant(lines)
    found = []
    for k in range(len(sig) - min_lines + 1):
        chunk = sig[k : k + min_lines]
        body = "\n".join(t for _, t in chunk)
        if len(body) < min_chars:
            continue
        digest = hashlib.sha1(body.encode()).hexdigest()[:16]
        found.append((digest, (path, chunk[0][0], chunk[-1][0])))
    return found


def collect(root: Path, section: dict[str, Any]) -> dict[str, list[Occurrence]]:
    include = section.get("include", DEFAULT_INCLUDE)
    exclude = section.get("exclude", DEFAULT_EXCLUDE)
    min_lines = int(section.get("min_lines", 8))
    min_chars = int(section.get("min_chars", 160))
    seen: dict[str, list[Occurrence]] = defaultdict(list)
    for f in list_files(root, include, exclude):
        for digest, occ in windows(f, read_lines(root / f), min_lines, min_chars):
            seen[digest].append(occ)
    return {h: o for h, o in seen.items() if len(o) > 1}


def load_baseline(root: Path, section: dict[str, Any]) -> dict[str, int]:
    file = root / section.get("baseline", DEFAULT_BASELINE)
    return (
        {str(k): int(v) for k, v in json.loads(file.read_text()).items()} if file.exists() else {}
    )


def check(
    root: Path, config: dict[str, Any], args: argparse.Namespace | None = None
) -> list[Finding]:
    section = config.get("duplication", {})
    dups = collect(root, section)
    if args is not None and getattr(args, "write_baseline", False):
        target = root / section.get("baseline", DEFAULT_BASELINE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({h: len(o) for h, o in sorted(dups.items())}, indent=1) + "\n")
        return []
    baseline = load_baseline(root, section)
    spans: dict[str, list[tuple[int, int, set[str]]]] = defaultdict(list)
    for digest, occs in dups.items():
        if len(occs) <= baseline.get(digest, 1):
            continue
        for path, first, last in occs:
            others = {f"{p}:{a}" for p, a, _ in occs if (p, a) != (path, first)}
            spans[path].append((first, last, others))
    found = []
    for path, items in spans.items():
        items.sort(key=lambda s: (s[0], s[1]))
        cur_a, cur_b, cur_o = items[0]
        for a, b, o in [*items[1:], (10**9, 10**9, set())]:
            if a <= cur_b + 1:
                cur_b, cur_o = max(cur_b, b), cur_o | o
                continue
            where = ", ".join(sorted(cur_o)[:3])
            found.append(
                Finding(
                    path,
                    cur_a,
                    RULE,
                    f"lines {cur_a}-{cur_b} duplicate code also at {where}; reuse it",
                )
            )
            cur_a, cur_b, cur_o = a, b, o
    return found


def _extra(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--write-baseline", action="store_true", help="record current duplicates and exit 0"
    )


def main(argv: list[str] | None = None) -> int:
    return run_gate("duplication", check, argv, _extra)


if __name__ == "__main__":
    sys.exit(main())
