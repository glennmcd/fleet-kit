#!/usr/bin/env python3
# Vendored from https://github.com/alexander-marquardt/agent-fleet-kit (MIT, (c) Alexander Marquardt). Unchanged.
"""Sum token usage per Claude Code session, counting each request once.

Claude Code keeps one JSONL transcript per session under ~/.claude/projects/.
A request can appear on several records (one per content block), so usage is
counted once per ``requestId``; counting every record over-counts by about a
third.

    python3 scripts/session_tokens.py            # the 10 most recent sessions
    python3 scripts/session_tokens.py --last 50

Divide the totals by the work they produced (merged pull requests, changed
lines) rather than by hour: that is the number that shows whether a change
to the workflow helped.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict

KINDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def session_totals(path: str) -> tuple[int, dict[str, int]]:
    """(requests, token totals by kind) for one transcript."""
    seen: set[str] = set()
    totals: dict[str, int] = defaultdict(int)
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            usage, request_id = message.get("usage"), record.get("requestId")
            if not usage or not request_id or request_id in seen:
                continue
            seen.add(request_id)
            for kind in KINDS:
                totals[kind] += usage.get(kind) or 0
    return len(seen), dict(totals)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--last", type=int, default=10, help="how many recent sessions")
    parser.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    args = parser.parse_args()
    paths = sorted(glob.glob(f"{args.root}/**/*.jsonl", recursive=True), key=os.path.getmtime)
    for path in paths[-args.last:]:
        requests, totals = session_totals(path)
        if requests:
            # Claude Code names each transcript <session-uuid>.jsonl; [:36] is the uuid.
            print(os.path.basename(path)[:36], "calls", requests, totals)


if __name__ == "__main__":
    main()
