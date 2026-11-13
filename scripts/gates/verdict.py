"""Gate: a pull request merges only when the reviewer approved its current head commit.

Incident (Marquardt): "The author's confidence carries no weight." Review has to be independent, adversarial,
and tied to the exact commit that will merge, so a push after approval cannot ride on the old verdict.

The reviewer posts a PR comment with a marker on a line of its own:

    <!-- fleet-verdict v1 {"sha": "<40 hex>", "verdict": "approve"} -->

`verdict` is `approve` or `request_changes`. This script reads the PR's comments, decides whether the head is
approved, and publishes the answer as a check run named `reviewer-verdict` on the head SHA. A new push is a new
SHA with no verdict, so the check fails until the new head is reviewed. Require the check, from GitHub Actions,
in the branch ruleset.

A verdict counts only if all of these hold, so nothing a PR author or a quoted reply can write passes it:
  - the comment's author is in `verdict.reviewers` (none configured: nothing passes);
  - the marker is a line of its own, outside code fences, quotes and indented blocks, and is the only one in
    the comment, with the exact `v1` shape and a full lowercase 40-character `sha` equal to the live head;
  - the comment was never edited;
  - no later verdict at the same head says otherwise: the newest wins.
A draft PR never passes. A head branch starting with a `verdict.exempt_branches` prefix (release-please's) passes.

Config (`.fleet-gates.json`):  "verdict": {"reviewers": ["login"], "exempt_branches": ["release-please--"]}

Run: `python -m gates.verdict --repo owner/name --pr 7` with `GH_TOKEN` set. It never reads PR code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gates.common import load_config

CHECK_NAME = "reviewer-verdict"
VERDICTS = {"approve", "request_changes"}
MARKER = re.compile(r"^<!--\s*fleet-verdict\s+v1\s+(\{.*\})\s*-->[ \t]*$")
FENCE = re.compile(r"^ {0,3}(```|~~~)")
FULL_SHA = re.compile(r"[0-9a-f]{40}")

Gh = Callable[..., Any]


@dataclass(frozen=True)
class Comment:
    id: int
    login: str
    body: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Verdict:
    sha: str
    verdict: str


@dataclass(frozen=True)
class Decision:
    conclusion: str  # "success" or "failure"
    title: str
    summary: str


def _time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def parse_verdict(body: str | None) -> Verdict | None:
    """The one verdict a comment carries, or None if it carries none, several, or a malformed one."""
    if not body:
        return None
    markers: list[str] = []
    fence = ""
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = FENCE.match(line)
        if m:
            fence = "" if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        found = MARKER.match(line)
        if found:
            markers.append(found.group(1))
    if len(markers) != 1:
        return None
    try:
        data = json.loads(markers[0])
    except ValueError:
        return None
    sha, kind = data.get("sha"), data.get("verdict")
    if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha) or kind not in VERDICTS:
        return None
    return Verdict(sha=sha, verdict=kind)


def decide(
    head_sha: str,
    comments: list[Comment],
    *,
    reviewers: list[str],
    draft: bool = False,
    head_ref: str = "",
    exempt_prefixes: tuple[str, ...] | list[str] = (),
) -> Decision:
    short = head_sha[:7]
    if draft:
        return Decision("failure", "Draft pull request", "A draft is not reviewed or merged.")
    if head_ref and any(p and head_ref.startswith(p) for p in exempt_prefixes):
        return Decision("success", "Exempt branch", f"`{head_ref}` is exempt from review.")
    allowed = {r.lower() for r in reviewers if r}
    if not allowed:
        return Decision(
            "failure", "No reviewers configured", "Set `verdict.reviewers`; nothing passes without."
        )
    valid: list[tuple[datetime, int, Verdict]] = []
    for c in comments:
        created, updated = _time(c.created_at), _time(c.updated_at)
        if not c.login or c.login.lower() not in allowed or created is None or updated is None:
            continue
        if updated > created:  # an edited comment is not trusted
            continue
        v = parse_verdict(c.body)
        if v is not None:
            valid.append((created, c.id, v))
    valid.sort(key=lambda item: (item[0], item[1]))
    at_head = [v for _, _, v in valid if v.sha == head_sha]
    if at_head:
        latest = at_head[-1]
        if latest.verdict == "approve":
            return Decision("success", "Approved", f"The reviewer approved head {short}.")
        return Decision(
            "failure", "Changes requested", f"The reviewer requested changes at head {short}."
        )
    hint = f" The latest verdict is for {valid[-1][2].sha[:7]}, an earlier commit." if valid else ""
    return Decision(
        "failure", "No verdict at head", f"No verdict for head {short}.{hint} Run the review again."
    )


def gh_api(method: str, path: str, fields: dict[str, str] | None = None) -> Any:
    """Call the GitHub API with the `gh` CLI (authenticated by GH_TOKEN) and parse the JSON reply."""
    cmd = ["gh", "api", "-X", method, path]
    paged = method == "GET" and path.endswith("/comments")
    if paged:
        cmd += ["--paginate", "--slurp"]
    for key, value in (fields or {}).items():
        cmd += ["-f", f"{key}={value}"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8")
    data = json.loads(out) if out.strip() else {}
    return [item for page in data for item in page] if paged else data


def run(repo: str, pr: int, config: dict[str, Any], gh: Gh = gh_api) -> Decision:
    section = config.get("verdict", {})
    pull = gh("GET", f"repos/{repo}/pulls/{pr}")
    head_sha = pull["head"]["sha"]
    raw = gh("GET", f"repos/{repo}/issues/{pr}/comments")
    comments = [
        Comment(
            id=int(c["id"]),
            login=(c.get("user") or {}).get("login") or "",
            body=c.get("body") or "",
            created_at=c.get("created_at") or "",
            updated_at=c.get("updated_at") or "",
        )
        for c in raw
    ]
    decision = decide(
        head_sha,
        comments,
        reviewers=section.get("reviewers", []),
        draft=bool(pull.get("draft")),
        head_ref=pull["head"].get("ref", ""),
        exempt_prefixes=section.get("exempt_branches", []),
    )
    fields = {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": decision.conclusion,
        "output[title]": decision.title,
        "output[summary]": decision.summary,
    }
    existing = gh("GET", f"repos/{repo}/commits/{head_sha}/check-runs?check_name={CHECK_NAME}")
    runs = [r for r in existing.get("check_runs", []) if r.get("name") == CHECK_NAME]
    if runs:
        gh("PATCH", f"repos/{repo}/check-runs/{runs[0]['id']}", fields)
    else:
        gh("POST", f"repos/{repo}/check-runs", fields)
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verdict")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    from pathlib import Path

    config = load_config(Path(args.root).resolve(), args.config, required=True)
    decision = run(args.repo, args.pr, config)
    print(f"verdict: {decision.conclusion}: {decision.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
