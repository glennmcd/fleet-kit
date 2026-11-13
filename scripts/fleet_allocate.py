#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""The allocator: one pass that gives free slots their next piece of work.

    fleet_allocate.py                    one pass (the fleet-allocate timer runs it every 2 minutes)
    fleet_allocate.py --dry-run          print what it would assign; write and start nothing

There are three kinds of work, all read from GitHub:

- review: an open pull request from an ``issue/<n>`` branch whose CI passed and which has no reviewer verdict
  at its head commit. Review slots (``reviewNN``) take these, oldest first. A red PR is never reviewed: it goes
  to a fix round, since a review of a red PR only repeats what CI says.
- fix: such a pull request with failing checks, a merge conflict, or a ``request_changes`` verdict at its head.
- issue: an open issue labelled ``agent-ready`` and ``lane:<name>``, with a filled-in *Done when* list, no
  assignee and no open pull request. The lane is drawn by weight among the lanes that have work; within a lane
  the oldest issue goes first.

Author slots (``agentNN``) take fix rounds and new issues. Two thirds of them (``in_flight_share``) are kept for
fix rounds, counting the slots already busy with one; a share left unused goes to the other kind, so no slot
idles while there is work. An issue is given out at most ``max_runs_per_issue`` times, counting its fix rounds;
after that its PR is left for a person. A head commit is reviewed once.

A slot is free when its unit is inactive. A finished slot's assignment file is removed before the pass; a
failed unit keeps its file and holds its work until a person looks (``systemctl reset-failed``). The pass does
nothing while the stop flag exists. It reads GitHub with the ``gh`` CLI and writes nothing there.

Configuration, ``/etc/fleet/fleet.json``::

    {"repo": "owner/name", "slots": ["agent01", "agent02"], "review_slots": ["review01"],
     "lanes": {"feature": 3, "fix": 2, "tooling": 1}}

Optional: ``in_flight_share`` (0.67), ``max_runs_per_issue`` (4), ``ready_label`` ("agent-ready"). Run counts
and reviewed heads are kept in ``/var/lib/fleet/state.json``. The reviewers whose verdicts count are read from
the repository's ``.fleet-gates.json``, as the merge check reads them.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import random
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fleet_stop
from gates.verdict import Comment, decide

DEFAULT_CONFIG = "/etc/fleet/fleet.json"
FLEET_DIR = "/var/lib/fleet"
AUTHOR_RE = re.compile(r"agent[0-9]{2}")
REVIEW_RE = re.compile(r"review[0-9]{2}")
LANE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH_RE = re.compile(r"issue/([1-9][0-9]{0,8})")
KINDS = ("issue", "fix", "review")
VERDICT_CHECK = "reviewer-verdict"
GOOD = {"SUCCESS", "NEUTRAL", "SKIPPED"}
DONE_HEADING = re.compile(r"^#{1,6}\s*done when\s*$", re.IGNORECASE)
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+\S")
HEADING = re.compile(r"^#{1,6}\s")


# --- the model ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    repo: str
    author_slots: tuple[str, ...]
    review_slots: tuple[str, ...]
    lanes: dict[str, int]
    in_flight_share: float = 0.67
    max_runs: int = 4
    ready_label: str = "agent-ready"


@dataclass(frozen=True)
class Issue:
    number: int
    labels: tuple[str, ...]
    body: str
    created_at: str
    assigned: bool = False


@dataclass(frozen=True)
class Pull:
    number: int
    head_ref: str
    head_sha: str
    draft: bool
    conflicting: bool
    ci: str  # "pass", "fail" or "running"
    verdict: str | None  # "approve", "request_changes" or None, at the head commit
    created_at: str

    @property
    def issue(self) -> int | None:
        m = BRANCH_RE.fullmatch(self.head_ref)
        return int(m.group(1)) if m else None


@dataclass(frozen=True)
class Assignment:
    kind: str  # "issue" or "fix": an issue number; "review": a pull request number
    number: int

    def text(self) -> str:
        return f"{self.kind} {self.number}"

    @property
    def issue_key(self) -> int | None:
        return self.number if self.kind in ("issue", "fix") else None


@dataclass(frozen=True)
class Slot:
    name: str
    role: str  # "author" or "review"
    state: str  # "free", "busy" or "failed"
    assignment: Assignment | None


@dataclass
class State:
    runs: dict[int, int] = field(default_factory=dict)  # issue number -> times given out
    reviewed: dict[int, list[str]] = field(default_factory=dict)  # PR number -> heads reviewed


@dataclass
class Plan:
    assignments: list[tuple[str, Assignment]]
    notes: list[str]
    state: State


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ValueError("the config must be a JSON object")
    repo = raw.get("repo")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise ValueError("repo must be owner/name")
    authors = raw.get("slots", [])
    reviews = raw.get("review_slots", [])
    if not isinstance(authors, list) or not all(
        isinstance(s, str) and AUTHOR_RE.fullmatch(s) for s in authors
    ):
        raise ValueError("slots must be a list of agentNN names")
    if not isinstance(reviews, list) or not all(
        isinstance(s, str) and REVIEW_RE.fullmatch(s) for s in reviews
    ):
        raise ValueError("review_slots must be a list of reviewNN names")
    if len(set(authors)) != len(authors) or len(set(reviews)) != len(reviews):
        raise ValueError("a slot is listed twice")
    lanes = raw.get("lanes")
    if (
        not isinstance(lanes, dict)
        or not lanes
        or not all(isinstance(k, str) and LANE_RE.fullmatch(k) for k in lanes)
        or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in lanes.values())
    ):
        raise ValueError("lanes must map lowercase names to positive whole weights")
    share = raw.get("in_flight_share", 0.67)
    if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 <= share <= 1:
        raise ValueError("in_flight_share must be between 0 and 1")
    max_runs = raw.get("max_runs_per_issue", 4)
    if not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1:
        raise ValueError("max_runs_per_issue must be a positive whole number")
    label = raw.get("ready_label", "agent-ready")
    if not isinstance(label, str) or not label:
        raise ValueError("ready_label must be a label name")
    return Config(repo, tuple(authors), tuple(reviews), dict(lanes), float(share), max_runs, label)


def record(state: State, a: Assignment, head_sha: str = "") -> None:
    """Count a run that started: an issue's run, or a review of one head."""
    if a.kind == "review":
        state.reviewed.setdefault(a.number, []).append(head_sha)
    else:
        state.runs[a.number] = state.runs.get(a.number, 0) + 1


def in_flight_slots(author_slots: int, share: float) -> int:
    return int(author_slots * share + 0.5)


def has_done_when(body: str) -> bool:
    """True when a *Done when* heading is followed, before the next heading, by a checkbox with text."""
    inside = False
    for line in body.replace("\r\n", "\n").split("\n"):
        if DONE_HEADING.match(line.strip()):
            inside = True
            continue
        if inside and HEADING.match(line):
            inside = False
        if inside and CHECKBOX.match(line):
            return True
    return False


def classify_ci(checks: Sequence[dict[str, Any]]) -> str:
    """ "fail", "running" or "pass" for a PR's checks, the reviewer's own check left out."""
    states: list[str] = []
    for c in checks:
        if c.get("name") == VERDICT_CHECK:
            continue
        if "context" in c and "name" not in c:  # a commit status
            state = str(c.get("state") or "").upper()
            states.append(
                "pass"
                if state == "SUCCESS"
                else "running"
                if state in ("PENDING", "EXPECTED")
                else "fail"
            )
        elif str(c.get("status") or "").upper() != "COMPLETED":
            states.append("running")
        else:
            states.append("pass" if str(c.get("conclusion") or "").upper() in GOOD else "fail")
    if "fail" in states:
        return "fail"
    if "running" in states or not states:
        return "running"
    return "pass"


def _lane(issue: Issue, cfg: Config) -> str | None:
    labels = set(issue.labels)
    return next((lane for lane in cfg.lanes if f"lane:{lane}" in labels), None)


def plan(
    cfg: Config,
    slots: Sequence[Slot],
    issues: Sequence[Issue],
    pulls: Sequence[Pull],
    state: State,
    rng: random.Random,
) -> Plan:
    state = copy.deepcopy(state)
    notes: list[str] = []
    held_issues = {s.assignment.issue_key for s in slots if s.state != "free" and s.assignment} - {
        None
    }
    held_pulls = {
        s.assignment.number
        for s in slots
        if s.state != "free" and s.assignment and s.assignment.kind == "review"
    }
    for s in slots:
        if s.state == "failed":
            notes.append(
                f"{s.name} failed; it holds {s.assignment.text() if s.assignment else 'nothing'} until a person resets it"
            )

    reviews: list[Pull] = []
    fixes: list[Pull] = []
    in_flight_issues: set[int] = set()
    for p in sorted(pulls, key=lambda p: (p.created_at, p.number)):
        n = p.issue
        if n is None:
            continue
        in_flight_issues.add(n)  # a draft too: its issue is not new work
        if p.draft:
            continue
        if p.verdict == "approve" and p.ci == "pass" and not p.conflicting:
            continue  # waits for a merge
        if p.conflicting or p.ci == "fail" or (p.ci == "pass" and p.verdict == "request_changes"):
            if n in held_issues:
                continue
            if state.runs.get(n, 0) >= cfg.max_runs:
                notes.append(
                    f"PR #{p.number} (issue #{n}) has had {cfg.max_runs} runs; it needs a person"
                )
                continue
            fixes.append(p)
        elif p.ci == "pass" and p.verdict is None:
            if p.number in held_pulls:
                continue
            if p.head_sha in state.reviewed.get(p.number, []):
                notes.append(
                    f"PR #{p.number} head {p.head_sha[:7]} was already reviewed with no verdict; it needs a person"
                )
                continue
            reviews.append(p)

    by_lane: dict[str, list[Issue]] = {lane: [] for lane in cfg.lanes}
    for i in sorted(issues, key=lambda i: (i.created_at, i.number)):
        if (
            cfg.ready_label not in i.labels
            or i.number in in_flight_issues
            or i.number in held_issues
        ):
            continue
        lane = _lane(i, cfg)
        why = (
            "has no known lane label"
            if lane is None
            else "is assigned to a person"
            if i.assigned
            else "has no filled-in Done when list"
            if not has_done_when(i.body)
            else f"has had {cfg.max_runs} runs"
            if state.runs.get(i.number, 0) >= cfg.max_runs
            else ""
        )
        if why:
            notes.append(f"issue #{i.number} {why}; skipped")
            continue
        assert lane is not None
        by_lane[lane].append(i)

    assignments: list[tuple[str, Assignment]] = []

    def give(slot: str, a: Assignment, sha: str = "") -> None:
        assignments.append((slot, a))
        record(state, a, sha)

    for slot in [s for s in slots if s.role == "review" and s.state == "free"]:
        if not reviews:
            break
        p = reviews.pop(0)
        give(slot.name, Assignment("review", p.number), p.head_sha)

    authors = [s for s in slots if s.role == "author"]
    free = [s.name for s in authors if s.state == "free"]
    busy_fix = sum(
        1 for s in authors if s.state != "free" and s.assignment and s.assignment.kind == "fix"
    )
    busy_new = sum(
        1 for s in authors if s.state != "free" and s.assignment and s.assignment.kind == "issue"
    )
    fix_share = in_flight_slots(len(authors), cfg.in_flight_share)
    fix_quota = max(0, fix_share - busy_fix)
    new_quota = max(0, len(authors) - fix_share - busy_new)

    def draw() -> Issue | None:
        lanes = [lane for lane in cfg.lanes if by_lane[lane]]
        if not lanes:
            return None
        lane = rng.choices(lanes, weights=[cfg.lanes[lane] for lane in lanes])[0]
        return by_lane[lane].pop(0)

    def take_fix() -> bool:
        if not fixes or not free:
            return False
        p = fixes.pop(0)
        assert p.issue is not None
        give(free.pop(0), Assignment("fix", p.issue))
        return True

    def take_new() -> bool:
        if not free:
            return False
        i = draw()
        if i is None:
            return False
        give(free.pop(0), Assignment("issue", i.number))
        return True

    for _ in range(fix_quota):
        take_fix()
    for _ in range(new_quota):
        take_new()
    while free and (take_fix() or take_new()):
        pass
    return Plan(assignments, notes, state)


# --- reading GitHub and the machine ---------------------------------------------------------------

Gh = Callable[[Sequence[str]], str]
Systemctl = Callable[[Sequence[str]], tuple[int, str]]

ISSUE_LIMIT = 200
PULL_LIMIT = 100
BUSY = {"active", "activating", "deactivating", "reloading", "refreshing"}


class PassError(Exception):
    """The pass cannot tell what is true, so it changes nothing."""


def _gh(args: Sequence[str]) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, check=True, text=True, timeout=60)
    return done.stdout


def _systemctl(args: Sequence[str]) -> tuple[int, str]:
    done = subprocess.run(
        ["systemctl", *args], capture_output=True, check=False, text=True, timeout=30
    )
    return done.returncode, done.stdout


def unit_for(slot: str) -> str:
    return f"fleet-review@{slot}" if REVIEW_RE.fullmatch(slot) else f"fleet-agent@{slot}"


def read_assignment(path: Path) -> Assignment | None:
    try:
        parts = path.read_text(encoding="utf-8", errors="replace").split()
    except FileNotFoundError:
        return None
    if len(parts) == 2 and parts[0] in KINDS and parts[1].isdigit() and int(parts[1]) > 0:
        return Assignment(parts[0], int(parts[1]))
    return None


def read_slots(cfg: Config, slots_dir: Path, systemctl: Systemctl) -> list[Slot]:
    out: list[Slot] = []
    for role, names in (("author", cfg.author_slots), ("review", cfg.review_slots)):
        for name in names:
            unit = unit_for(name)
            answer = systemctl(["is-active", unit])[1].strip()
            if answer == "inactive":
                state = "free"
            elif answer == "failed":
                state = "failed"
            elif answer in BUSY:
                state = "busy"
            else:  # a running slot read as free would get a second assignment
                raise PassError(f"cannot tell whether {unit} is running ({answer or 'no answer'})")
            out.append(Slot(name, role, state, read_assignment(slots_dir / name)))
    return out


def fetch_reviewers(repo: str, gh: Gh) -> list[str]:
    """The accounts whose verdicts count, from the repository's .fleet-gates.json on its default branch."""
    data = json.loads(gh(["api", f"repos/{repo}/contents/.fleet-gates.json"]))
    config = json.loads(base64.b64decode(data["content"], validate=False).decode("utf-8"))
    reviewers = config.get("verdict", {}).get("reviewers", [])
    return [r for r in reviewers if isinstance(r, str)]


def fetch_issues(cfg: Config, gh: Gh) -> list[Issue]:
    raw = json.loads(
        gh(
            [
                "issue",
                "list",
                "--repo",
                cfg.repo,
                "--state",
                "open",
                "--label",
                cfg.ready_label,
                "--limit",
                str(ISSUE_LIMIT),
                "--json",
                "number,labels,body,createdAt,assignees",
            ]
        )
    )
    if len(raw) >= ISSUE_LIMIT:
        raise PassError(f"{len(raw)} ready issues reach the listing limit; some would be missed")
    return [
        Issue(
            number=int(i["number"]),
            labels=tuple(str(label.get("name", "")) for label in i.get("labels") or []),
            body=str(i.get("body") or ""),
            created_at=str(i.get("createdAt") or ""),
            assigned=bool(i.get("assignees")),
        )
        for i in raw
    ]


def verdict_at_head(
    repo: str, number: int, head_sha: str, reviewers: list[str], gh: Gh
) -> str | None:
    pages = json.loads(
        gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments"])
    )
    comments = [
        Comment(
            id=int(c["id"]),
            login=str((c.get("user") or {}).get("login") or ""),
            body=str(c.get("body") or ""),
            created_at=str(c.get("created_at") or ""),
            updated_at=str(c.get("updated_at") or ""),
        )
        for page in pages
        for c in page
    ]
    title = decide(head_sha, comments, reviewers=reviewers).title
    return {"Approved": "approve", "Changes requested": "request_changes"}.get(title)


def fetch_pulls(cfg: Config, reviewers: list[str], gh: Gh) -> list[Pull]:
    fields = "number,headRefName,headRefOid,isDraft,isCrossRepository,mergeable,statusCheckRollup,createdAt"
    raw = json.loads(
        gh(
            [
                "pr",
                "list",
                "--repo",
                cfg.repo,
                "--state",
                "open",
                "--limit",
                str(PULL_LIMIT),
                "--json",
                fields,
            ]
        )
    )
    if len(raw) >= PULL_LIMIT:
        raise PassError(
            f"{len(raw)} open pull requests reach the listing limit; some would be missed"
        )
    pulls: list[Pull] = []
    for p in raw:
        ref = str(p.get("headRefName") or "")
        # A fork can name its branch anything; its code must never reach a slot that holds tokens.
        if not BRANCH_RE.fullmatch(ref) or p.get("isCrossRepository") is not False:
            continue
        draft = bool(p.get("isDraft"))
        sha = str(p.get("headRefOid") or "")
        ci = classify_ci(p.get("statusCheckRollup") or [])
        number = int(p["number"])
        wants_verdict = ci == "pass" and not draft
        verdict = verdict_at_head(cfg.repo, number, sha, reviewers, gh) if wants_verdict else None
        pulls.append(
            Pull(
                number=number,
                head_ref=ref,
                head_sha=sha,
                draft=draft,
                conflicting=p.get("mergeable") == "CONFLICTING",
                ci=ci,
                verdict=verdict,
                created_at=str(p.get("createdAt") or ""),
            )
        )
    return pulls


def load_state(path: Path) -> State:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State()
    if not isinstance(raw, dict):
        raise PassError(f"{path} is not a state file")
    return State(
        runs={int(k): int(v) for k, v in raw.get("runs", {}).items()},
        reviewed={int(k): [str(s) for s in v] for k, v in raw.get("reviewed", {}).items()},
    )


def save_state(path: Path, state: State) -> None:
    data = {
        "runs": {str(k): v for k, v in sorted(state.runs.items())},
        "reviewed": {str(k): v[-20:] for k, v in sorted(state.reviewed.items())},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    """One pass at a time. Released by the kernel if the pass dies."""
    if sys.platform == "win32":  # the fleet runs on Linux; tests pass their own lock
        yield
        return
    import fcntl

    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PassError("another pass is running") from None
        yield


def run_pass(
    cfg: Config,
    fleet_dir: Path,
    gh: Gh,
    systemctl: Systemctl,
    rng: random.Random,
    dry_run: bool,
    stop_file: Path,
) -> int:
    note = fleet_stop.status(stop_file)
    if note is not None:
        print(f"fleet_allocate: the fleet is stopped, assigning nothing: {note}")
        return 0
    # Every read happens before anything is written, so a failure leaves the machine as it was.
    slots_dir = fleet_dir / "slots"
    slots = read_slots(cfg, slots_dir, systemctl)
    state = load_state(fleet_dir / "state.json")
    reviewers = fetch_reviewers(cfg.repo, gh)
    pulls = fetch_pulls(cfg, reviewers, gh)
    issues = fetch_issues(cfg, gh)

    finished = [s for s in slots if s.state == "free" and s.assignment]
    for s in finished:
        print(f"{s.name} finished {s.assignment.text() if s.assignment else ''}")
    slots = [Slot(s.name, s.role, s.state, None) if s.state == "free" else s for s in slots]
    result = plan(cfg, slots, issues, pulls, state, rng)
    for line in result.notes:
        print(f"note: {line}")
    for slot, a in result.assignments:
        print(f"{'would assign' if dry_run else 'assign'} {slot}: {a.text()}")
    if dry_run:
        return 0

    for s in finished:
        (slots_dir / s.name).unlink(missing_ok=True)
    heads = {p.number: p.head_sha for p in pulls}
    failed = 0
    for slot, a in result.assignments:
        path = slots_dir / slot
        path.write_text(a.text() + "\n", encoding="utf-8")
        if systemctl(["start", "--no-block", unit_for(slot)])[0] != 0:
            # Give the work back: the next pass can hand it out again, and no run is counted.
            path.unlink(missing_ok=True)
            print(
                f"fleet_allocate: could not start {unit_for(slot)}; {a.text()} goes back",
                file=sys.stderr,
            )
            failed += 1
            continue
        record(state, a, heads.get(a.number, "") if a.kind == "review" else "")
    save_state(fleet_dir / "state.json", state)
    return 1 if failed else 0


def _command(cmd: Any) -> str:
    return " ".join(map(str, list(cmd)[:3])) if isinstance(cmd, (list, tuple)) else str(cmd)


def main(
    argv: list[str] | None = None,
    gh: Gh = _gh,
    systemctl: Systemctl = _systemctl,
    rng: random.Random | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="fleet_allocate")
    parser.add_argument("--config", default=os.environ.get("FLEET_CONFIG") or DEFAULT_CONFIG)
    parser.add_argument("--fleet-dir", default=FLEET_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = parse_config(json.loads(Path(args.config).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"fleet_allocate: {args.config}: {exc}", file=sys.stderr)
        return 2
    fleet_dir = Path(args.fleet_dir)
    stop_file = Path(os.environ.get("FLEET_STOP_FILE") or fleet_stop.DEFAULT_STOP_FILE)
    try:
        with _lock(fleet_dir / "allocate.lock"):
            return run_pass(
                cfg, fleet_dir, gh, systemctl, rng or random.Random(), args.dry_run, stop_file
            )
    except subprocess.CalledProcessError as exc:
        err = str(exc.stderr or "").strip()[:300]
        print(
            f"fleet_allocate: {_command(exc.cmd)} failed: {err}; nothing changed", file=sys.stderr
        )
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"fleet_allocate: {_command(exc.cmd)} timed out; nothing changed", file=sys.stderr)
        return 1
    except (PassError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # ValueError covers unreadable JSON and base64 from GitHub or the state file.
        print(f"fleet_allocate: {type(exc).__name__}: {exc}; nothing changed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
