#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Token use per unit of work, from the transcripts Claude Code keeps on disk.

Extends agent-fleet-kit's ``session_tokens.py`` (which sums tokens per session)
into the measurements Alexander Marquardt's token article is built on:

* tokens per ISO week, raw and price-weighted, deduplicated by ``requestId``;
* tokens per merged PR and per changed line, joined to ``gh`` PR data by branch
  and by merge week (each PR capped at ``--line-cap`` changed lines);
* an index against a baseline: 100 = the baseline weeks' value;
* cache-expiry share: the cost of cache writes that follow an idle gap longer
  than the cache lifetime and re-write most of the context;
* context distribution: median, p90 and the share of requests over a ceiling;
* first-turn cost: cache read vs fresh write on each transcript's first call.

    python3 scripts/fleet_report.py --since 2026-11-01
    gh pr list --state merged --limit 2000 \\
        --json number,headRefName,mergedAt,additions,deletions > prs.json
    python3 scripts/fleet_report.py --prs prs.json --baseline 2026-W45

Prices are relative to fresh input (cache read 0.1, 5-minute cache write 1.25,
1-hour cache write 2, output 5); override them with ``--price kind=value``.
Standard library only.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

PRICES: dict[str, float] = {
    "input": 1.0,
    "cache_read": 0.1,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.0,
    "output": 5.0,
}
CACHE_TTL_SECS = 300
EXPIRY_WRITE_SHARE = 0.5
# The share-of-calls column counts contexts at or above this. 150k is context_ceiling's advisory
# level; its hard ceiling is 200k. The JSON key keeps its name, "over_ceiling_share".
CEILING_TOKENS = 150_000
LINE_CAP = 5_000
SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Request:
    """One model call, counted once."""

    request_id: str
    when: datetime
    transcript: str
    branch: str
    is_subagent: bool
    input: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    output: int

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def context(self) -> int:
        """Size of the prompt this call sent."""
        return self.input + self.cache_read + self.cache_write

    @property
    def raw(self) -> int:
        return self.context + self.output

    def weighted(self, prices: dict[str, float]) -> float:
        return (
            self.input * prices["input"]
            + self.cache_read * prices["cache_read"]
            + self.cache_write_5m * prices["cache_write_5m"]
            + self.cache_write_1h * prices["cache_write_1h"]
            + self.output * prices["output"]
        )

    @property
    def week(self) -> str:
        return iso_week(self.when)


def iso_week(when: datetime) -> str:
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _request(record: dict[str, Any], path: str) -> Request | None:
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    request_id, stamp = record.get("requestId"), record.get("timestamp")
    if not isinstance(message, dict) or not isinstance(usage, dict) or not request_id or not stamp:
        return None
    if message.get("model") == SYNTHETIC_MODEL:  # a reply Claude Code wrote itself; no call
        return None
    write = int(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation")
    write_1h = int(split.get("ephemeral_1h_input_tokens") or 0) if isinstance(split, dict) else 0
    return Request(
        request_id=str(request_id),
        when=parse_time(str(stamp)),
        transcript=path,
        branch=str(record.get("gitBranch") or ""),
        is_subagent=f"{os.sep}subagents{os.sep}" in path or "/subagents/" in path,
        input=int(usage.get("input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_5m=max(0, write - write_1h),
        cache_write_1h=write_1h,
        output=int(usage.get("output_tokens") or 0),
    )


def read_requests(paths: Iterable[str]) -> list[Request]:
    """Every request in *paths*, once each, oldest first.

    A request appears on one record per content block, each with a copy of
    its usage; counting every record over-counts by about a third.
    """
    seen: set[str] = set()
    found: list[Request] = []
    for path in paths:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                request = _request(record, path) if isinstance(record, dict) else None
                if request is None or request.request_id in seen:
                    continue
                seen.add(request.request_id)
                found.append(request)
    found.sort(key=lambda r: r.when)
    return found


def transcripts(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def by_transcript(requests: Iterable[Request]) -> dict[str, list[Request]]:
    grouped: dict[str, list[Request]] = defaultdict(list)
    for request in requests:
        grouped[request.transcript].append(request)
    return grouped


def expiry_ids(
    requests: Iterable[Request], ttl: int = CACHE_TTL_SECS, share: float = EXPIRY_WRITE_SHARE
) -> set[str]:
    """Requests that paid to re-write a cache that expired while the agent idled.

    Evidence, not just a gap: the call came more than *ttl* seconds after the
    previous call in the same transcript, AND at least *share* of its prompt
    was written fresh. A one-hour cache write is judged against an hour.
    """
    hits: set[str] = set()
    for calls in by_transcript(requests).values():
        for before, now in pairwise(calls):
            limit = 3600 if now.cache_write_1h else ttl
            gap = (now.when - before.when).total_seconds()
            if gap > limit and now.context and now.cache_write >= share * now.context:
                hits.add(now.request_id)
    return hits


@dataclass
class PullRequest:
    number: int
    branch: str
    week: str
    lines: int


def read_prs(path: str, line_cap: int = LINE_CAP) -> list[PullRequest]:
    """Merged PRs from ``gh pr list --json number,headRefName,mergedAt,additions,deletions``."""
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    prs = []
    for row in rows:
        if not row.get("mergedAt"):
            continue
        lines = int(row.get("additions") or 0) + int(row.get("deletions") or 0)
        prs.append(
            PullRequest(
                number=int(row["number"]),
                branch=str(row.get("headRefName") or ""),
                week=iso_week(parse_time(row["mergedAt"])),
                lines=min(lines, line_cap),
            )
        )
    return prs


@dataclass
class Week:
    week: str
    requests: list[Request] = field(default_factory=list)
    prs: list[PullRequest] = field(default_factory=list)


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def week_rows(
    requests: list[Request],
    prs: list[PullRequest],
    prices: dict[str, float],
    ceiling: int = CEILING_TOKENS,
    ttl: int = CACHE_TTL_SECS,
) -> list[dict[str, Any]]:
    """One summary row per ISO week, oldest first."""
    weeks: dict[str, Week] = {}
    for request in requests:
        weeks.setdefault(request.week, Week(request.week)).requests.append(request)
    for pr in prs:
        weeks.setdefault(pr.week, Week(pr.week)).prs.append(pr)
    expired = expiry_ids(requests, ttl)
    firsts = {calls[0].request_id for calls in by_transcript(requests).values()}
    rows = []
    for name in sorted(weeks):
        week = weeks[name]
        calls = week.requests
        weighted = sum(r.weighted(prices) for r in calls)
        write_5m, write_1h = prices["cache_write_5m"], prices["cache_write_1h"]
        expiry = sum(
            r.cache_write_5m * write_5m + r.cache_write_1h * write_1h
            for r in calls
            if r.request_id in expired
        )
        contexts = [r.context for r in calls]
        first = [r for r in calls if r.request_id in firsts]
        merged, lines = len(week.prs), sum(p.lines for p in week.prs)
        raw = sum(r.raw for r in calls)
        rows.append(
            {
                "week": name,
                "requests": len(calls),
                "raw_tokens": raw,
                "weighted": round(weighted),
                "merged_prs": merged,
                "changed_lines": lines,
                "raw_per_pr": raw / merged if merged else None,
                "weighted_per_pr": weighted / merged if merged else None,
                "weighted_per_line": weighted / lines if lines else None,
                "cache_read_share": (sum(r.cache_read for r in calls) / raw) if raw else 0.0,
                "expiry_share": expiry / weighted if weighted else 0.0,
                "median_context": round(statistics.median(contexts)) if contexts else 0,
                "p90_context": percentile(contexts, 0.9),
                "over_ceiling_share": (
                    sum(c >= ceiling for c in contexts) / len(contexts) if contexts else 0.0
                ),
                "first_turn_read": round(statistics.median(r.cache_read for r in first))
                if first
                else None,
                "first_turn_write": round(statistics.median(r.cache_write for r in first))
                if first
                else None,
            }
        )
    return rows


INDEXED = ("weighted", "weighted_per_pr", "weighted_per_line")


def add_index(rows: list[dict[str, Any]], baseline: list[str]) -> None:
    """Add ``<metric>_index`` columns: 100 = the baseline weeks taken together."""
    base = [r for r in rows if r["week"] in baseline]
    if not base:
        return
    weighted = sum(r["weighted"] for r in base)
    prs = sum(r["merged_prs"] for r in base)
    lines = sum(r["changed_lines"] for r in base)
    reference = {
        "weighted": weighted / len(base),
        "weighted_per_pr": weighted / prs if prs else None,
        "weighted_per_line": weighted / lines if lines else None,
    }
    for row in rows:
        for metric in INDEXED:
            value, ref = row[metric], reference[metric]
            row[f"{metric}_index"] = round(100 * value / ref) if value is not None and ref else None


def branch_rows(
    requests: list[Request], prs: list[PullRequest], prices: dict[str, float]
) -> list[dict[str, Any]]:
    """Weighted tokens per git branch, with the PR merged from it when there is one."""
    by_branch: dict[str, float] = defaultdict(float)
    for request in requests:
        by_branch[request.branch or "(none)"] += request.weighted(prices)
    merged = {p.branch: p for p in prs}
    rows = []
    for branch, weighted in sorted(by_branch.items(), key=lambda kv: -kv[1]):
        pr = merged.get(branch)
        rows.append(
            {
                "branch": branch,
                "weighted": round(weighted),
                "pr": pr.number if pr else None,
                "changed_lines": pr.lines if pr else None,
                "weighted_per_line": weighted / pr.lines if pr and pr.lines else None,
            }
        )
    return rows


def _fmt(value: Any, kind: str = "") -> str:
    if value is None:
        return "-"
    if kind == "pct":
        return f"{100 * value:.1f}%"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _table(headers: list[tuple[str, str, str]], rows: list[dict[str, Any]]) -> Iterator[str]:
    yield "| " + " | ".join(title for _, title, _ in headers) + " |"
    yield "|" + "---|" * len(headers)
    for row in rows:
        yield "| " + " | ".join(_fmt(row.get(key), kind) for key, _, kind in headers) + " |"


WEEK_COLUMNS = [
    ("week", "Week", ""),
    ("requests", "Calls", ""),
    ("weighted", "Weighted tokens", ""),
    ("weighted_index", "Index", ""),
    ("merged_prs", "Merged PRs", ""),
    ("weighted_per_pr_index", "Per PR idx", ""),
    ("weighted_per_line_index", "Per line idx", ""),
    ("cache_read_share", "Cache reads", "pct"),
    ("expiry_share", "Cache expiry", "pct"),
    ("median_context", "Median ctx", ""),
    ("over_ceiling_share", "Ctx >={ceiling}", "pct"),
    ("first_turn_write", "1st-turn write", ""),
]
BRANCH_COLUMNS = [
    ("branch", "Branch", ""),
    ("weighted", "Weighted tokens", ""),
    ("pr", "PR", ""),
    ("changed_lines", "Lines", ""),
    ("weighted_per_line", "Per line", ""),
]


def render(
    weeks: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    top: int,
    ceiling: int = CEILING_TOKENS,
) -> str:
    label = f"{ceiling // 1000}k" if ceiling % 1000 == 0 else f"{ceiling:,}"
    columns = [(key, title.format(ceiling=label), kind) for key, title, kind in WEEK_COLUMNS]
    lines = ["## Per week (index: 100 = baseline)", "", *_table(columns, weeks)]
    lines += ["", f"## Top {top} branches by weighted tokens", ""]
    lines += _table(BRANCH_COLUMNS, branches[:top])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    parser.add_argument("--project", help="only transcripts whose folder name contains this")
    parser.add_argument("--since", help="YYYY-MM-DD; ignore earlier calls")
    parser.add_argument("--prs", help="JSON from gh pr list --state merged --json ...")
    parser.add_argument("--baseline", default="", help="comma-separated ISO weeks, e.g. 2026-W45")
    parser.add_argument(
        "--ceiling",
        type=int,
        default=CEILING_TOKENS,
        help="context size for the share-of-calls column (default: the hook's 150k advisory)",
    )
    parser.add_argument("--ttl", type=int, default=CACHE_TTL_SECS, help="cache lifetime, seconds")
    parser.add_argument("--line-cap", type=int, default=LINE_CAP)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--price", action="append", default=[], help="kind=value override")
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables")
    args = parser.parse_args(argv)

    prices = dict(PRICES)
    for item in args.price:
        kind, _, value = item.partition("=")
        if kind not in prices:
            parser.error(f"unknown price kind {kind!r}; one of {', '.join(prices)}")
        prices[kind] = float(value)

    paths = transcripts(args.root)
    if args.project:
        paths = [p for p in paths if args.project in os.path.relpath(p, args.root)]
    requests = read_requests(paths)
    if args.since:
        since = datetime.fromisoformat(args.since).date()
        requests = [r for r in requests if r.when.date() >= since]
    prs = read_prs(args.prs, args.line_cap) if args.prs else []
    if not requests:
        print(f"no requests found under {args.root}", file=sys.stderr)
        return 1

    weeks = week_rows(requests, prs, prices, args.ceiling, args.ttl)
    baseline = [w for w in args.baseline.split(",") if w] or [weeks[0]["week"]]
    add_index(weeks, baseline)
    branches = branch_rows(requests, prs, prices)
    if args.json:
        print(json.dumps({"baseline": baseline, "weeks": weeks, "branches": branches}, indent=2))
    else:
        print(render(weeks, branches, args.top, args.ceiling))
    return 0


if __name__ == "__main__":
    sys.exit(main())
