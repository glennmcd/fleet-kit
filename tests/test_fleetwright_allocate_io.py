"""One allocator pass against a fake GitHub and a fake systemd: what it reads, writes and starts."""

from __future__ import annotations

import base64
import json
import random
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fleetwright_allocate
import pytest

REPO = "o/r"
SHA = "a" * 40
FIXTURES = Path(__file__).parent / "fixtures" / "allocate"
NOW = datetime(
    2026, 11, 15, 18, 6, 33, tzinfo=UTC
).timestamp()  # the pass that gave issue 46 out again
DONE = "## Done when\n\n- [ ] it works\n"


def marker(sha: str, verdict: str) -> str:
    return f'<!-- fleetwright-verdict v1 {{"sha": "{sha}", "verdict": "{verdict}"}} -->\n\nfindings'


class FakeGitHub:
    def __init__(
        self,
        issues: list[dict[str, Any]] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        comments: dict[int, list[dict[str, Any]]] | None = None,
        reviewers: tuple[str, ...] = ("g26work",),
        closed: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = issues or []
        self.closed = closed or []
        self.pulls = pulls or []
        self.comments = comments or {}
        self.reviewers = list(reviewers)
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.pulls)
        path = args[-1]
        if path.startswith(f"repos/{REPO}/pulls?state=closed"):
            return json.dumps(self.closed)
        if path.endswith("/contents/.fleetwright-gates.json"):
            body = json.dumps({"verdict": {"reviewers": self.reviewers}}).encode()
            return json.dumps({"content": base64.b64encode(body).decode()})
        if path.endswith("/comments"):
            number = int(path.split("/")[-2])
            return json.dumps([self.comments.get(number, [])])
        raise AssertionError(f"unexpected gh call {args}")

    def writes(self) -> list[list[str]]:
        reads = ({"issue", "list"}, {"pr", "list"})
        return [c for c in self.calls if set(c[:2]) not in reads and c[0] != "api"]


class FakeSystemd:
    """`systemctl` as the allocator sees it: an exit code and stdout."""

    def __init__(self, states: dict[str, str] | None = None, failing: tuple[str, ...] = ()) -> None:
        self.states = states or {}
        self.failing = set(failing)
        self.started: list[str] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        if args[0] == "is-active":
            state = self.states.get(args[1], "inactive")
            return (0 if state == "active" else 3), state + "\n"
        if args[0] == "start":
            if args[-1] in self.failing:
                return 1, ""
            self.started.append(args[-1])
            return 0, ""
        raise AssertionError(f"unexpected systemctl call {list(args)}")


def issue(n: int, lane: str = "feature") -> dict[str, Any]:
    return {
        "number": n,
        "labels": [{"name": "agent-ready"}, {"name": f"lane:{lane}"}],
        "body": DONE,
        "createdAt": f"2026-11-{n:02d}T00:00:00Z",
        "assignees": [],
    }


def pr(n: int, issue_no: int, checks: str = "SUCCESS", **kw: Any) -> dict[str, Any]:
    return {
        "number": n,
        "headRefName": f"issue/{issue_no}",
        "headRefOid": SHA,
        "isDraft": False,
        "isCrossRepository": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "backend", "status": "COMPLETED", "conclusion": checks}],
        "createdAt": f"2026-11-{n:02d}T00:00:00Z",
        **kw,
    }


def comment(i: int, login: str, body: str) -> dict[str, Any]:
    t = f"2026-11-13T10:00:{i:02d}Z"
    return {"id": i, "user": {"login": login}, "body": body, "created_at": t, "updated_at": t}


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    (tmp_path / "slots").mkdir()
    (tmp_path / "fleetwright.json").write_text(
        json.dumps(
            {
                "repo": REPO,
                "slots": ["agent01", "agent02"],
                "review_slots": ["review01"],
                "lanes": {"feature": 1},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def run(fleet: Path, gh: FakeGitHub, systemd: FakeSystemd, *extra: str) -> int:
    return fleetwright_allocate.main(
        ["--config", str(fleet / "fleetwright.json"), "--fleetwright-dir", str(fleet), *extra],
        gh=gh,
        systemctl=systemd,
        rng=random.Random(1),
        now=lambda: NOW,
    )


@pytest.fixture(autouse=True)
def no_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEETWRIGHT_STOP_FILE", str(tmp_path / "STOP"))


def test_a_pass_writes_assignments_and_starts_the_units(fleet: Path) -> None:
    gh = FakeGitHub(issues=[issue(3)], pulls=[pr(40, 12)])
    systemd = FakeSystemd()
    assert run(fleet, gh, systemd) == 0
    assert (fleet / "slots" / "review01").read_text(encoding="utf-8") == "review 40\n"
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"
    assert systemd.started == ["fleetwright-review@review01", "fleetwright-agent@agent01"]
    state = json.loads((fleet / "state.json").read_text(encoding="utf-8"))
    assert state == {"runs": {"3": 1}, "reviewed": {"40": [SHA]}}


def test_it_never_writes_to_github(fleet: Path) -> None:
    gh = FakeGitHub(issues=[issue(3)], pulls=[pr(40, 12), pr(41, 13, checks="FAILURE")])
    run(fleet, gh, FakeSystemd())
    assert gh.writes() == []
    for call in gh.calls:
        if call[0] == "api":
            assert "-X" not in call and "-f" not in call


def test_a_verdict_from_a_listed_reviewer_at_head_is_read(fleet: Path) -> None:
    gh = FakeGitHub(
        pulls=[pr(40, 12), pr(41, 13)],
        comments={
            40: [comment(1, "g26work", marker(SHA, "request_changes"))],
            41: [comment(2, "g26work", marker(SHA, "approve"))],
        },
    )
    systemd = FakeSystemd()
    run(fleet, gh, systemd)
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "fix 12\n"
    assert not (fleet / "slots" / "review01").exists()  # 41 is approved and waits for a merge


def test_a_verdict_from_anyone_else_does_not_count(fleet: Path) -> None:
    gh = FakeGitHub(
        pulls=[pr(40, 12)], comments={40: [comment(1, "glennmcd", marker(SHA, "approve"))]}
    )
    run(fleet, gh, FakeSystemd())
    assert (fleet / "slots" / "review01").read_text(encoding="utf-8") == "review 40\n"


def test_comments_are_read_only_for_green_prs(fleet: Path) -> None:
    gh = FakeGitHub(pulls=[pr(40, 12, checks="FAILURE")])
    run(fleet, gh, FakeSystemd())
    assert not any(c[-1].endswith("/comments") for c in gh.calls)


def test_dry_run_prints_and_changes_nothing(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fleet / "slots" / "agent02").write_text("issue 9\n", encoding="utf-8")  # finished earlier
    gh = FakeGitHub(issues=[issue(3)])
    systemd = FakeSystemd()
    assert run(fleet, gh, systemd, "--dry-run") == 0
    assert systemd.started == []
    assert not (fleet / "state.json").exists()
    assert (fleet / "slots" / "agent02").read_text(encoding="utf-8") == "issue 9\n"
    assert "would assign agent01: issue 3" in capsys.readouterr().out


def test_a_finished_slot_is_cleared_and_reused(fleet: Path) -> None:
    (fleet / "slots" / "agent01").write_text("issue 9\n", encoding="utf-8")
    systemd = FakeSystemd()
    run(fleet, FakeGitHub(issues=[issue(3)]), systemd)
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"


def test_busy_and_failed_slots_keep_their_work(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fleet / "slots" / "agent01").write_text("issue 3\n", encoding="utf-8")
    (fleet / "slots" / "agent02").write_text("issue 4\n", encoding="utf-8")
    systemd = FakeSystemd(
        {"fleetwright-agent@agent01": "active", "fleetwright-agent@agent02": "failed"}
    )
    run(fleet, FakeGitHub(issues=[issue(3), issue(4), issue(5)]), systemd)
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"
    assert (fleet / "slots" / "agent02").read_text(encoding="utf-8") == "issue 4\n"
    assert systemd.started == []
    assert "agent02 failed" in capsys.readouterr().out


def test_a_stopped_fleet_reads_and_starts_nothing(fleet: Path, tmp_path: Path) -> None:
    (tmp_path / "STOP").write_text("main is red\n", encoding="utf-8")
    gh = FakeGitHub(issues=[issue(3)])
    systemd = FakeSystemd()
    assert run(fleet, gh, systemd) == 0
    assert gh.calls == [] and systemd.started == []


def test_a_bad_config_is_an_error_not_a_traceback(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fleet / "fleetwright.json").write_text(
        '{"repo": "o/r", "slots": ["root"], "lanes": {"x": 1}}', encoding="utf-8"
    )
    assert run(fleet, FakeGitHub(), FakeSystemd()) == 2
    assert "agentNN" in capsys.readouterr().err


def test_a_branch_that_is_not_an_issue_branch_is_ignored(fleet: Path) -> None:
    other = pr(40, 12)
    other["headRefName"] = "release-please--branches--main"
    systemd = FakeSystemd()
    run(fleet, FakeGitHub(pulls=[other]), systemd)
    assert systemd.started == []


def test_state_carries_over_between_passes(fleet: Path) -> None:
    gh = FakeGitHub(pulls=[pr(40, 12)])
    run(fleet, gh, FakeSystemd())
    (fleet / "slots" / "review01").unlink()
    systemd = FakeSystemd()
    run(
        fleet, gh, systemd
    )  # the review ran but posted nothing: the same head is not reviewed again
    assert systemd.started == []


def closed_pulls() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(
        (FIXTURES / "closed_pulls.json").read_text(encoding="utf-8")
    )
    return [{**p, "head": {**p["head"], "repo": {"full_name": REPO}}} for p in data]


def test_an_issue_whose_pr_just_merged_is_not_given_out(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue 46's PR 51 merged five seconds ago; the issue listing still shows 46 open.
    gh = FakeGitHub(issues=[issue(46), issue(52)], closed=closed_pulls())
    systemd = FakeSystemd()
    run(fleet, gh, systemd)
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 52\n"
    assert not (fleet / "slots" / "agent02").exists()
    assert "#46" in capsys.readouterr().out


def test_a_merge_older_than_the_window_does_not_block_a_reopened_issue(fleet: Path) -> None:
    gh = FakeGitHub(issues=[issue(46)], closed=closed_pulls())
    systemd = FakeSystemd()
    hour = 60 * 60
    fleetwright_allocate.main(
        ["--config", str(fleet / "fleetwright.json"), "--fleetwright-dir", str(fleet)],
        gh=gh,
        systemctl=systemd,
        rng=random.Random(1),
        now=lambda: NOW + hour,
    )
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 46\n"


def test_a_pr_closed_without_merging_does_not_block_its_issue(fleet: Path) -> None:
    closed = closed_pulls()
    closed[1] = {**closed[1], "merged_at": None}  # PR 51, issue 46
    run(fleet, FakeGitHub(issues=[issue(46)], closed=closed), FakeSystemd())
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 46\n"


def test_the_closed_pr_read_is_one_page_sorted_by_last_update(fleet: Path) -> None:
    gh = FakeGitHub(issues=[issue(3)])
    run(fleet, gh, FakeSystemd())
    reads = [c for c in gh.calls if c[0] == "api" and "pulls?" in c[-1]]
    assert reads == [
        ["api", f"repos/{REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=50"]
    ]


def test_a_closed_pr_page_full_of_recent_merges_stops_the_pass(fleet: Path) -> None:
    recent = closed_pulls()[1]
    gh = FakeGitHub(issues=[issue(3)], closed=[recent] * 50)
    systemd = FakeSystemd()
    assert run(fleet, gh, systemd) == 1
    assert systemd.started == []


# --- failure paths --------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["", "unknown", "maintenance"])
def test_a_slot_whose_state_cannot_be_read_stops_the_pass(fleet: Path, answer: str) -> None:
    (fleet / "slots" / "agent01").write_text("issue 3\n", encoding="utf-8")
    systemd = FakeSystemd({"fleetwright-agent@agent01": answer})
    assert run(fleet, FakeGitHub(issues=[issue(3), issue(4)]), systemd) == 1
    assert systemd.started == []
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"
    assert not (fleet / "state.json").exists()


@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading"])
def test_a_slot_on_its_way_up_or_down_is_busy(fleet: Path, state: str) -> None:
    (fleet / "slots" / "agent01").write_text("issue 3\n", encoding="utf-8")
    systemd = FakeSystemd({"fleetwright-agent@agent01": state})
    run(fleet, FakeGitHub(issues=[issue(3)]), systemd)
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"
    assert systemd.started == []


def test_a_unit_that_fails_to_start_gives_its_work_back(fleet: Path) -> None:
    gh = FakeGitHub(issues=[issue(3)], pulls=[pr(40, 12)])
    systemd = FakeSystemd(failing=("fleetwright-review@review01", "fleetwright-agent@agent01"))
    assert run(fleet, gh, systemd) == 1
    assert not (fleet / "slots" / "review01").exists()
    assert not (fleet / "slots" / "agent01").exists()
    state = json.loads((fleet / "state.json").read_text(encoding="utf-8"))
    assert state == {"runs": {}, "reviewed": {}}
    retry = FakeSystemd()
    run(fleet, gh, retry)  # the head was not reviewed, so the next pass tries again
    assert "fleetwright-review@review01" in retry.started


def test_a_pr_from_a_fork_is_never_given_to_a_slot(fleet: Path) -> None:
    fork = pr(40, 12, isCrossRepository=True)
    gh = FakeGitHub(
        issues=[issue(12)], pulls=[fork, pr(41, 13, checks="FAILURE", isCrossRepository=True)]
    )
    systemd = FakeSystemd()
    run(fleet, gh, systemd)
    assert systemd.started == ["fleetwright-agent@agent01"]
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 12\n"


def test_a_draft_pr_keeps_its_issue_in_flight(fleet: Path) -> None:
    systemd = FakeSystemd()
    run(fleet, FakeGitHub(issues=[issue(12)], pulls=[pr(40, 12, isDraft=True)]), systemd)
    assert systemd.started == []


def test_a_listing_that_hits_its_limit_stops_the_pass(fleet: Path) -> None:
    many = [pr(i, i, isDraft=True) for i in range(1, 101)]
    systemd = FakeSystemd()
    assert run(fleet, FakeGitHub(issues=[issue(200)], pulls=many), systemd) == 1
    assert systemd.started == []


class BrokenGitHub(FakeGitHub):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def __call__(self, args: Sequence[str]) -> str:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 404"),
        subprocess.TimeoutExpired(["gh", "api"], 60),
        FileNotFoundError("gh"),
    ],
)
def test_a_github_failure_ends_the_pass_cleanly(
    fleet: Path, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    systemd = FakeSystemd()
    assert run(fleet, BrokenGitHub(error), systemd) == 1
    assert systemd.started == []
    assert "fleetwright_allocate:" in capsys.readouterr().err


def test_a_gh_too_old_to_slurp_says_so_in_one_line(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    usage = "unknown flag: --slurp\n\nUsage:  gh api <endpoint> [flags]\n\nFlags:\n      --cache\n"
    error = subprocess.CalledProcessError(1, ["gh", "api", "--paginate"], stderr=usage)
    assert run(fleet, BrokenGitHub(error), FakeSystemd()) == 1
    err = capsys.readouterr().err
    assert "unknown flag: --slurp" in err and "2.48" in err and "fleetwright_check" in err
    assert "Usage:" not in err and len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("reply", ["not json", "{}", '{"content": "!!"}'])
def test_an_unreadable_reply_ends_the_pass_cleanly(fleet: Path, reply: str) -> None:
    class Odd(FakeGitHub):
        def __call__(self, args: Sequence[str]) -> str:
            return reply

    assert run(fleet, Odd(), FakeSystemd()) == 1


def test_a_corrupt_state_file_ends_the_pass_cleanly(fleet: Path) -> None:
    (fleet / "state.json").write_text("{not json", encoding="utf-8")
    systemd = FakeSystemd()
    assert run(fleet, FakeGitHub(issues=[issue(3)]), systemd) == 1
    assert systemd.started == []


# --- handoffs -------------------------------------------------------------------------------------

BLOCKED = "## Handoff\n\n**Status**: blocked\n\n**Decision needed**\n- which queue? I pick SQS\n"
PARTLY = "## Handoff\n\n**Status**: partly done\n\n**Decision needed**\n- None\n"


def ran_before(fleet: Path, *issues: int) -> None:
    runs = {str(n): 1 for n in issues}
    (fleet / "state.json").write_text(json.dumps({"runs": runs, "reviewed": {}}), encoding="utf-8")


def test_an_issue_whose_last_comment_is_a_blocked_handoff_waits(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran_before(fleet, 3)
    gh = FakeGitHub(
        issues=[issue(3), issue(4)],
        comments={3: [comment(1, "fleet-bot", PARTLY), comment(2, "fleet-bot", BLOCKED)]},
    )
    assert run(fleet, gh, FakeSystemd()) == 0
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 4\n"
    assert not (fleet / "slots" / "agent02").exists()
    assert "#3" in capsys.readouterr().out


def test_a_reply_after_the_blocked_handoff_releases_the_issue(fleet: Path) -> None:
    ran_before(fleet, 3)
    gh = FakeGitHub(
        issues=[issue(3)],
        comments={3: [comment(1, "fleet-bot", BLOCKED), comment(2, "owner", "Use SQS.")]},
    )
    run(fleet, gh, FakeSystemd())
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"


def test_a_partly_done_handoff_goes_out_again(fleet: Path) -> None:
    ran_before(fleet, 3)
    gh = FakeGitHub(issues=[issue(3)], comments={3: [comment(1, "fleet-bot", PARTLY)]})
    run(fleet, gh, FakeSystemd())
    assert (fleet / "slots" / "agent01").read_text(encoding="utf-8") == "issue 3\n"


def test_handoffs_are_read_only_for_issues_the_fleet_ran_with_no_open_pr(fleet: Path) -> None:
    ran_before(fleet, 3, 5)
    gh = FakeGitHub(issues=[issue(3), issue(4), issue(5)], pulls=[pr(40, 5, checks="FAILURE")])
    run(fleet, gh, FakeSystemd())
    read = [c[-1] for c in gh.calls if c[-1].endswith("/comments")]
    assert read == [f"repos/{REPO}/issues/3/comments"]
