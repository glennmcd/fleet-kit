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

    python3 scripts/fleetwright_report.py --since 2026-11-01
    gh pr list --state merged --limit 2000 \\
        --json number,headRefName,mergedAt,additions,deletions > prs.json
    python3 scripts/fleetwright_report.py --prs prs.json --baseline 2026-W45

``--pr N`` (repeatable) narrows every number to the work on those pull requests: the
author's calls on each PR's branch (from ``--prs``) and the reviewer's calls, found by the
PR number in the fleet's fixed review prompt, since a reviewer works on a detached head.
To select an open PR, write the PR data with ``--state all``.

    python3 scripts/fleetwright_report.py --prs prs.json --pr 57 --pr 58

Prices are relative to fresh input (cache read 0.1, 5-minute cache write 1.25,
1-hour cache write 2, output 5); override them with ``--price kind=value``.
Standard library only.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
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
# The opening of fleetwright_agent.review_prompt; a test ties the two together. Anchored at the
# start of a transcript's first prompt, so a quote of it elsewhere never makes a run a review.
REVIEW_PROMPT_RE = re.compile(
    r"Run the cc-practices review-pr skill on pull request #([1-9][0-9]{0,8}) of this repository\."
)


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
    review_pr: int | None = None  # the PR a reviewer run was started on
    model: str = ""  # the model id the call ran on; empty in transcripts that don't record it

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
        model=str(message.get("model") or ""),
    )


def review_pr(text: str) -> int | None:
    """The PR number when *text* opens with the fleet's review prompt."""
    match = REVIEW_PROMPT_RE.match(text)
    return int(match.group(1)) if match else None


def _prompt_text(record: dict[str, Any]) -> str | None:
    """The text a person or the runner typed, or None for any other record."""
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    blocks = [b for b in content if isinstance(b, dict)]
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    texts = [b.get("text") for b in blocks if b.get("type") == "text"]
    return "".join(t for t in texts if isinstance(t, str)) or None


def _parent_transcript(path: str) -> str | None:
    """``<session>.jsonl`` for a subagent transcript under ``<session>/subagents/``."""
    folder = os.path.dirname(path)
    if os.path.basename(folder) != "subagents":
        return None
    return os.path.dirname(folder) + ".jsonl"


def read_requests(paths: Iterable[str]) -> list[Request]:
    """Every request in *paths*, once each, oldest first.

    A request appears on one record per content block, each with a copy of
    its usage; counting every record over-counts by about a third.
    """
    seen: set[str] = set()
    found: list[Request] = []
    reviews: dict[str, int | None] = {}
    for path in paths:
        first_prompt: str | None = None
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if first_prompt is None:
                    first_prompt = _prompt_text(record)
                request = _request(record, path)
                if request is None or request.request_id in seen:
                    continue
                seen.add(request.request_id)
                found.append(request)
        reviews[path] = review_pr(first_prompt) if first_prompt else None
    for request in found:
        parent = _parent_transcript(request.transcript)
        request.review_pr = reviews.get(request.transcript) or (
            reviews.get(parent) if parent else None
        )
    found.sort(key=lambda r: r.when)
    return found


def transcripts(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def find_transcripts(patterns: Iterable[str], project: str | None = None) -> list[str]:
    """Transcripts under each root; a root may be a glob pattern, such as /home/agent[0-9][0-9].

    ``project`` keeps the transcripts whose path below their root contains it.
    """
    found: set[str] = set()
    for pattern in patterns:
        for root in glob.glob(pattern):
            for path in transcripts(root):
                if not project or project in os.path.relpath(path, root):
                    found.add(path)
    return sorted(found)


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


def _pr_rows(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    return [row for row in rows if isinstance(row, dict)]


def read_pr_branches(path: str) -> dict[int, str]:
    """The head branch of every PR in the file, merged or not."""
    return {int(row["number"]): str(row.get("headRefName") or "") for row in _pr_rows(path)}


def read_prs(path: str, line_cap: int = LINE_CAP) -> list[PullRequest]:
    """Merged PRs from ``gh pr list --json number,headRefName,mergedAt,additions,deletions``."""
    prs = []
    for row in _pr_rows(path):
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


def select_prs(
    requests: list[Request],
    prs: list[PullRequest],
    branches: dict[int, str],
    wanted: set[int],
) -> tuple[list[Request], list[PullRequest], list[str]]:
    """The calls and merged PRs of the *wanted* PRs, and warnings about what could not be matched.

    An author's call belongs to a PR when it was made on that PR's head branch; a reviewer's when
    its run was started on that PR.
    """
    warnings = [
        f"PR #{n} is not in the PR data; only its reviewer's calls are counted"
        for n in sorted(wanted)
        if n not in branches
    ]
    keep = {branches[n] for n in wanted if n in branches and branches[n]}
    warnings += [
        f"PR #{n} also came from {branch}; that branch's author calls are counted here too"
        for n, branch in sorted(branches.items())
        if branch in keep and n not in wanted
    ]
    kept = [r for r in requests if r.branch in keep or r.review_pr in wanted]
    return kept, [p for p in prs if p.number in wanted], warnings


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


@dataclass
class _Marks:
    """Calls picked out across the whole set, before it is split into rows."""

    expired: set[str]
    firsts: set[str]  # each transcript's first call
    author_firsts: set[str]  # the first call of each author's own transcript

    @classmethod
    def of(cls, requests: list[Request], ttl: int) -> _Marks:
        grouped = by_transcript(requests).values()
        return cls(
            expiry_ids(requests, ttl),
            {calls[0].request_id for calls in grouped},
            {
                calls[0].request_id
                for calls in grouped
                if not calls[0].is_subagent and calls[0].review_pr is None
            },
        )


UNKNOWN_MODEL = "(unknown)"


def model_name(request: Request) -> str:
    return request.model or UNKNOWN_MODEL


def model_rate(model: str, rates: dict[str, float]) -> float | None:
    """Dollars per million fresh input tokens: an exact name, else the longest name inside *model*.

    So ``haiku=1`` prices every Haiku, and ``claude-haiku-5-5=0.8`` overrides it for one version.
    """
    if model in rates:
        return rates[model]
    for name in sorted(rates, key=len, reverse=True):
        if name in model:
            return rates[name]
    return None


def call_cost(
    request: Request, prices: dict[str, float], model_prices: dict[str, float]
) -> float | None:
    rate = model_rate(model_name(request), model_prices)
    return None if rate is None else request.weighted(prices) * rate / 1_000_000


def role(request: Request) -> str:
    """Who made the call: the author or reviewer run, or a subagent one of them started.

    ``--model`` (FLEETWRIGHT_MODEL) sets only the run's own model; a subagent runs on the model
    its agent definition names, so the two are told apart here.
    """
    who = "author" if request.review_pr is None else "reviewer"
    return f"{who} subagent" if request.is_subagent else who


def _spend(
    request: Request, prices: dict[str, float], model_prices: dict[str, float] | None
) -> float:
    """The call's dollars when priced, else its weighted tokens."""
    cost = call_cost(request, prices, model_prices) if model_prices else None
    return cost if cost is not None else request.weighted(prices)


def _by_model(
    calls: Iterable[Request], prices: dict[str, float], model_prices: dict[str, float] | None
) -> dict[str, float]:
    """Each model's dollars when priced, else its weighted tokens."""
    spend: dict[str, float] = defaultdict(float)
    for r in calls:
        spend[model_name(r)] += _spend(r, prices, model_prices)
    return spend


def _shares(spend: dict[str, float]) -> dict[str, float]:
    whole = sum(spend.values())
    ordered = sorted(spend.items(), key=lambda kv: -kv[1])
    return {name: value / whole for name, value in ordered} if whole else {}


def _median(values: Iterable[int]) -> int | None:
    found = list(values)
    return round(statistics.median(found)) if found else None


def summary_row(
    name: str,
    calls: list[Request],
    prs: list[PullRequest],
    prices: dict[str, float],
    marks: _Marks,
    ceiling: int = CEILING_TOKENS,
    model_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    weighted = sum(r.weighted(prices) for r in calls)
    spend = _by_model(calls, prices, model_prices)
    write_5m, write_1h = prices["cache_write_5m"], prices["cache_write_1h"]
    expiry = sum(
        r.cache_write_5m * write_5m + r.cache_write_1h * write_1h
        for r in calls
        if r.request_id in marks.expired
    )
    reads = sum(r.cache_read for r in calls)
    contexts = [r.context for r in calls]
    first = [r for r in calls if r.request_id in marks.firsts]
    merged, lines = len(prs), sum(p.lines for p in prs)
    raw = sum(r.raw for r in calls)
    return {
        "week": name,
        "requests": len(calls),
        "raw_tokens": raw,
        "weighted": round(weighted),
        "cost_usd": sum(spend.values()) if model_prices else None,
        "models": _shares(spend),
        "merged_prs": merged,
        "changed_lines": lines,
        "raw_per_pr": raw / merged if merged else None,
        "raw_per_line": raw / lines if lines else None,
        "weighted_per_pr": weighted / merged if merged else None,
        "weighted_per_line": weighted / lines if lines else None,
        "cache_read_share": reads / raw if raw else 0.0,
        "cache_read_spend_share": reads * prices["cache_read"] / weighted if weighted else 0.0,
        "expiry_share": expiry / weighted if weighted else 0.0,
        "median_context": _median(contexts) or 0,
        "p90_context": percentile(contexts, 0.9),
        "over_ceiling_share": (
            sum(c >= ceiling for c in contexts) / len(contexts) if contexts else 0.0
        ),
        "first_turn_read": _median(r.cache_read for r in first),
        "first_turn_write": _median(r.cache_write for r in first),
        "author_first_context": _median(
            r.context for r in calls if r.request_id in marks.author_firsts
        ),
    }


def week_rows(
    requests: list[Request],
    prs: list[PullRequest],
    prices: dict[str, float],
    ceiling: int = CEILING_TOKENS,
    ttl: int = CACHE_TTL_SECS,
    model_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One summary row per ISO week, oldest first."""
    weeks: dict[str, Week] = {}
    for request in requests:
        weeks.setdefault(request.week, Week(request.week)).requests.append(request)
    for pr in prs:
        weeks.setdefault(pr.week, Week(pr.week)).prs.append(pr)
    marks = _Marks.of(requests, ttl)
    return [
        summary_row(
            name, weeks[name].requests, weeks[name].prs, prices, marks, ceiling, model_prices
        )
        for name in sorted(weeks)
    ]


def total_row(
    requests: list[Request],
    prs: list[PullRequest],
    prices: dict[str, float],
    ceiling: int = CEILING_TOKENS,
    ttl: int = CACHE_TTL_SECS,
    model_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """The same summary over every week at once: medians and shares over all calls, not per week."""
    marks = _Marks.of(requests, ttl)
    return summary_row("Total", requests, prs, prices, marks, ceiling, model_prices)


def model_rows(
    requests: list[Request],
    prices: dict[str, float],
    model_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One row per model, biggest share first: of dollars when priced, else of weighted tokens."""
    return _grouped_rows(requests, prices, model_prices, ("model",), lambda r: (model_name(r),))


def role_rows(
    requests: list[Request],
    prices: dict[str, float],
    model_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One row per role and model, biggest share first."""
    return _grouped_rows(
        requests, prices, model_prices, ("role", "model"), lambda r: (role(r), model_name(r))
    )


def _grouped_rows(
    requests: list[Request],
    prices: dict[str, float],
    model_prices: dict[str, float] | None,
    names: tuple[str, ...],
    key: Callable[[Request], tuple[str, ...]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[Request]] = defaultdict(list)
    for request in requests:
        grouped[key(request)].append(request)
    spend = {k: sum(_spend(r, prices, model_prices) for r in calls) for k, calls in grouped.items()}
    whole = sum(spend.values())
    rows = []
    for k in sorted(spend, key=lambda k: -spend[k]):
        calls = grouped[k]
        rows.append(
            {
                **dict(zip(names, k, strict=True)),
                "calls": len(calls),
                "raw_tokens": sum(r.raw for r in calls),
                "weighted": round(sum(r.weighted(prices) for r in calls)),
                "cost_usd": spend[k] if model_prices else None,
                "share": spend[k] / whole if whole else 0.0,
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
    if kind == "int":
        return f"{round(value):,}"
    if kind == "usd":
        return f"${value:,.2f}"
    if kind == "models":
        return ", ".join(
            f"{name.removeprefix('claude-')} {100 * share:.0f}%" for name, share in value.items()
        )
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
    ("raw_tokens", "Raw tokens", ""),
    ("raw_per_line", "Raw/line", "int"),
    ("weighted", "Weighted tokens", ""),
    ("cost_usd", "Cost $", "usd"),
    ("models", "Models", "models"),
    ("weighted_index", "Index", ""),
    ("merged_prs", "Merged PRs", ""),
    ("weighted_per_pr_index", "Per PR idx", ""),
    ("weighted_per_line_index", "Per line idx", ""),
    ("cache_read_share", "Cache reads", "pct"),
    ("cache_read_spend_share", "Reads % of cost", "pct"),
    ("expiry_share", "Cache expiry", "pct"),
    ("median_context", "Median ctx", ""),
    ("over_ceiling_share", "Ctx >={ceiling}", "pct"),
    ("first_turn_write", "1st-turn write", ""),
    ("author_first_context", "Author 1st ctx", ""),
]
BRANCH_COLUMNS = [
    ("branch", "Branch", ""),
    ("weighted", "Weighted tokens", ""),
    ("pr", "PR", ""),
    ("changed_lines", "Lines", ""),
    ("weighted_per_line", "Per line", ""),
]
MODEL_COLUMNS = [
    ("model", "Model", ""),
    ("calls", "Calls", ""),
    ("raw_tokens", "Raw tokens", ""),
    ("weighted", "Weighted tokens", ""),
    ("cost_usd", "Cost $", "usd"),
    ("share", "Share", "pct"),
]
ROLE_COLUMNS = [("role", "Role", ""), *MODEL_COLUMNS]


def render(
    weeks: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    top: int,
    ceiling: int = CEILING_TOKENS,
    selected: list[int] | None = None,
    models: list[dict[str, Any]] | None = None,
    priced: bool = False,
    roles: list[dict[str, Any]] | None = None,
) -> str:
    label = f"{ceiling // 1000}k" if ceiling % 1000 == 0 else f"{ceiling:,}"

    def shown(table: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        return [
            (key, title.format(ceiling=label), kind)
            for key, title, kind in table
            if priced or key != "cost_usd"
        ]

    basis = "dollars" if priced else "weighted tokens"
    lines = []
    if selected:
        names = ", ".join(f"#{n}" for n in selected)
        lines += [f"Only PRs {names}: their authors' and reviewers' calls.", ""]
    lines += ["## Per week (index: 100 = baseline)", "", *_table(shown(WEEK_COLUMNS), weeks)]
    lines += ["", f"Models: each model's share of {basis}."]
    lines += ["", f"## Per model (share of {basis})", ""]
    lines += _table(shown(MODEL_COLUMNS), models or [])
    lines += ["", f"## Per role and model (share of {basis})", ""]
    lines += _table(shown(ROLE_COLUMNS), roles or [])
    lines += [
        "",
        "FLEETWRIGHT_MODEL sets the author's and reviewer's own model; subagents use their own.",
    ]
    lines += ["", f"## Top {top} branches by weighted tokens", ""]
    lines += _table(BRANCH_COLUMNS, branches[:top])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--root",
        action="append",
        help="a transcripts folder or a glob pattern of folders; repeatable "
        "(default: ~/.claude/projects)",
    )
    parser.add_argument("--project", help="only transcripts whose folder name contains this")
    parser.add_argument("--since", help="YYYY-MM-DD; ignore earlier calls")
    parser.add_argument("--prs", help="JSON from gh pr list --state merged --json ...")
    parser.add_argument(
        "--pr",
        type=int,
        action="append",
        default=[],
        help="count only this PR's author and reviewer calls; repeatable (needs --prs for authors)",
    )
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
    parser.add_argument(
        "--model-price",
        type=_model_price,
        action="append",
        default=[],
        metavar="MODEL=USD",
        help="dollars per million fresh input tokens, e.g. haiku=1 or claude-opus-5-5=5; matches "
        "a model id exactly or by part; repeatable. Given, the report adds dollars and every "
        "model it saw must be priced",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables")
    args = parser.parse_args(argv)
    model_prices: dict[str, float] | None = dict(args.model_price) or None

    prices = dict(PRICES)
    for item in args.price:
        kind, _, value = item.partition("=")
        if kind not in prices:
            parser.error(f"unknown price kind {kind!r}; one of {', '.join(prices)}")
        prices[kind] = float(value)

    roots = args.root or [os.path.expanduser("~/.claude/projects")]
    requests = read_requests(find_transcripts(roots, args.project))
    if args.since:
        since = datetime.fromisoformat(args.since).date()
        requests = [r for r in requests if r.when.date() >= since]
    if args.prs and not os.path.isfile(args.prs):
        print(
            f"fleetwright_report: no PR data at {args.prs}; write it with: "
            "gh pr list --state merged --limit 2000 "
            "--json number,headRefName,mergedAt,additions,deletions",
            file=sys.stderr,
        )
        return 2
    prs = read_prs(args.prs, args.line_cap) if args.prs else []
    if not requests:
        print(f"no requests found under {', '.join(roots)}", file=sys.stderr)
        return 1
    selected = sorted(set(args.pr))
    if selected:
        if not args.prs:
            print(
                "fleetwright_report: no --prs, so authors' calls can't be matched to a PR; "
                "counting reviewer calls only",
                file=sys.stderr,
            )
        pr_branches = read_pr_branches(args.prs) if args.prs else {}
        requests, prs, warnings = select_prs(requests, prs, pr_branches, set(selected))
        for warning in warnings if args.prs else []:
            print(f"fleetwright_report: {warning}", file=sys.stderr)
        if not requests:
            names = ", ".join(f"#{n}" for n in selected)
            print(f"no calls found for PRs {names}", file=sys.stderr)
            return 1

    if model_prices:
        unpriced = sorted({model_name(r) for r in requests} - _priced(requests, model_prices))
        if unpriced:
            print(
                "fleetwright_report: no --model-price for "
                f"{', '.join(unpriced)}; price every model or none",
                file=sys.stderr,
            )
            return 2

    weeks = week_rows(requests, prs, prices, args.ceiling, args.ttl, model_prices)
    total = total_row(requests, prs, prices, args.ceiling, args.ttl, model_prices)
    models = model_rows(requests, prices, model_prices)
    roles = role_rows(requests, prices, model_prices)
    baseline = [w for w in args.baseline.split(",") if w] or [weeks[0]["week"]]
    add_index([*weeks, total], baseline)
    total["weighted_index"] = None  # a total of many weeks against one week's average says nothing
    branches = branch_rows(requests, prs, prices)
    if args.json:
        report: dict[str, Any] = {
            "baseline": baseline,
            "weeks": weeks,
            "total": total,
            "models": models,
            "roles": roles,
            "branches": branches,
        }
        if selected:
            report["prs"] = selected
        if model_prices:
            report["model_prices"] = model_prices
        print(json.dumps(report, indent=2))
    else:
        print(
            render(
                [*weeks, total],
                branches,
                args.top,
                args.ceiling,
                selected,
                models,
                priced=bool(model_prices),
                roles=roles,
            )
        )
    return 0


def _model_price(text: str) -> tuple[str, float]:
    name, sep, value = text.partition("=")
    try:
        if not sep or not name:
            raise ValueError
        return name, float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not MODEL=USD, e.g. haiku=1") from None


def _priced(requests: Iterable[Request], model_prices: dict[str, float]) -> set[str]:
    names = {model_name(r) for r in requests}
    return {n for n in names if model_rate(n, model_prices) is not None}


if __name__ == "__main__":
    sys.exit(main())
