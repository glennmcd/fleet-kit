"""One watchdog pass against a fake GitHub, a fake systemd and a fake journal."""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import fleetwright_stop
import fleetwright_watchdog
import pytest

REPO = "o/r"
SHA = "a" * 40
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
RECENT = "2027-01-15T07:50:00Z"
OLD = "2027-01-15T04:00:00Z"


def marker(sha: str, verdict: str) -> str:
    return f'<!-- fleetwright-verdict v1 {{"sha": "{sha}", "verdict": "{verdict}"}} -->\n\nfindings'


class FakeGitHub:
    def __init__(
        self,
        pulls: list[dict[str, Any]] | None = None,
        main: list[dict[str, Any]] | None = None,
        comments: dict[int, list[dict[str, Any]]] | None = None,
        fail: str = "",
    ) -> None:
        self.pulls = pulls or []
        self.main = main if main is not None else [check("backend")]
        self.comments = comments or {}
        self.fail = fail
        self.activity: list[dict[str, Any]] = []  # GitHub's repository activity log
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        self.calls.append(args)
        path = args[-1]
        if self.fail and self.fail in " ".join(args):
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 502\nusage")
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.pulls)
        if "/check-runs" in path:
            return json.dumps({"check_runs": self.main})
        if path.endswith("/contents/.fleetwright-gates.json"):
            body = json.dumps({"verdict": {"reviewers": ["g26work"]}}).encode()
            return json.dumps({"content": base64.b64encode(body).decode()})
        if path.endswith("/comments"):
            return json.dumps([self.comments.get(int(path.split("/")[-2]), [])])
        if "/activity?" in path:
            return json.dumps([self.activity])
        raise AssertionError(f"unexpected gh call {args}")


class FakeSystemd:
    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states = {"fleetwright-allocate.timer": "active", **(states or {})}
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        assert args[0] == "is-active", f"the watchdog only reads systemd, not {list(args)}"
        state = self.states.get(args[1], "inactive")
        return (0 if state == "active" else 3), state + "\n"


def check(name: str, conclusion: str | None = "success") -> dict[str, Any]:
    status = "completed" if conclusion else "in_progress"
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": SHA}


def pr(
    n: int, issue_no: int, checks: str = "SUCCESS", updated: str = OLD, **kw: Any
) -> dict[str, Any]:
    return {
        "number": n,
        "headRefName": f"issue/{issue_no}",
        "headRefOid": SHA,
        "isDraft": False,
        "isCrossRepository": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "backend", "status": "COMPLETED", "conclusion": checks}],
        "createdAt": OLD,
        "updatedAt": updated,
        **kw,
    }


def verdict(sha: str, kind: str) -> dict[str, Any]:
    t = "2027-01-15T04:00:00Z"
    return {
        "id": 1,
        "user": {"login": "g26work"},
        "body": marker(sha, kind),
        "created_at": t,
        "updated_at": t,
    }


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    monkeypatch.setenv("FLEETWRIGHT_STOP_FILE", str(tmp_path / "STOP"))
    return tmp_path


class FakeJournal:
    """journalctl -o json: entries matched by SYSLOG_IDENTIFIER= and _UID=, read after a cursor."""

    def __init__(self, entries: list[dict[str, Any]] | None = None, fail: str = "") -> None:
        self.entries = entries or []
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> list[dict[str, Any]]:
        args = list(args)
        self.calls.append(args)
        if self.fail and any(self.fail in a for a in args):
            raise OSError("journalctl failed")
        matches = [a.split("=", 1) for a in args if "=" in a and not a.startswith("-")]
        found = [e for e in self.entries if all(e.get(k) == v for k, v in matches)]
        if "--after-cursor" in args:
            cursor = args[args.index("--after-cursor") + 1]
            cursors = [e["__CURSOR"] for e in found]
            found = found[cursors.index(cursor) + 1 :] if cursor in cursors else found
        return found


def run(
    fleet: Path,
    gh: FakeGitHub | None = None,
    systemd: FakeSystemd | None = None,
    journal: Mapping[str, float | None] | None = None,
    entries: FakeJournal | None = None,
) -> int:
    last = journal or {}
    return fleetwright_watchdog.main(
        [
            "--config",
            str(fleet / "fleetwright.json"),
            "--fleetwright-dir",
            str(fleet),
            "--state",
            str(fleet / "watchdog.json"),
        ],
        gh=gh or FakeGitHub(),
        systemctl=systemd or FakeSystemd(),
        journal=lambda unit: last.get(unit, NOW - 60),
        journal_entries=entries or FakeJournal(),
        now=lambda: NOW,
    )


def lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_a_quiet_fleet_is_clean(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(fleet) == 0
    assert lines(capsys) == ["watchdog: clean"]
    assert fleetwright_stop.status(fleet / "STOP") is None


def test_it_only_reads_github(fleet: Path) -> None:
    gh = FakeGitHub(pulls=[pr(7, 3)])
    run(fleet, gh=gh)
    assert all(c[0] == "api" or c[:2] == ["pr", "list"] for c in gh.calls)
    assert not any(flag in c for c in gh.calls for flag in ("-X", "--method", "-f", "-F"))


# --- failed units ---------------------------------------------------------------------------------


def test_a_failed_slot_is_caught(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(fleet, systemd=FakeSystemd({"fleetwright-agent@agent02": "failed"})) == 0
    out = lines(capsys)
    assert out[0].startswith("caught failed-unit: fleetwright-agent@agent02 failed")
    assert "reset-failed" in out[0]
    assert out[-1] == "watchdog: caught 1"


def test_a_failed_allocator_and_a_stopped_timer_are_caught(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    systemd = FakeSystemd(
        {"fleetwright-allocate.service": "failed", "fleetwright-allocate.timer": "inactive"}
    )
    assert run(fleet, systemd=systemd) == 0
    out = lines(capsys)
    assert any(
        ln.startswith("caught failed-unit: fleetwright-allocate.service failed") for ln in out
    )
    assert any(
        ln.startswith("caught failed-unit: fleetwright-allocate.timer is inactive") for ln in out
    )
    assert out[-1] == "watchdog: caught 2"


def test_a_unit_state_it_cannot_read_is_missed(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(fleet, systemd=FakeSystemd({"fleetwright-review@review01": ""})) == 1
    out = lines(capsys)
    assert out[0].startswith("missed failed-unit: cannot tell whether fleetwright-review@review01")
    assert out[-1] == "watchdog: missed 1"


# --- stalled agents -------------------------------------------------------------------------------


def test_a_running_agent_that_logged_recently_is_fine(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    systemd = FakeSystemd({"fleetwright-agent@agent01": "active"})
    assert run(fleet, systemd=systemd, journal={"fleetwright-agent@agent01": NOW - 14 * 60}) == 0
    assert lines(capsys) == ["watchdog: clean"]


def test_a_running_agent_with_no_journal_line_for_too_long_is_caught(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fleet / "slots" / "agent01").write_text("issue 12\n", encoding="utf-8")
    systemd = FakeSystemd(
        {"fleetwright-agent@agent01": "active", "fleetwright-review@review01": "activating"}
    )
    journal = {"fleetwright-agent@agent01": NOW - 23 * 60, "fleetwright-review@review01": NOW - 5}
    assert run(fleet, systemd=systemd, journal=journal) == 0
    out = lines(capsys)
    assert out[0] == (
        "caught stalled: fleetwright-agent@agent01 (issue 12) has logged nothing for 23 min "
        "(limit 15); `systemctl stop fleetwright-agent@agent01` frees the slot"
    )
    assert out[-1] == "watchdog: caught 1"


def test_an_idle_slot_is_never_stalled(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(fleet, journal={"fleetwright-agent@agent01": NOW - 9999 * 60}) == 0
    assert lines(capsys) == ["watchdog: clean"]


def test_the_stall_limit_comes_from_the_config(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = json.loads((fleet / "fleetwright.json").read_text(encoding="utf-8"))
    raw["watchdog"] = {"stall_minutes": 30}
    (fleet / "fleetwright.json").write_text(json.dumps(raw), encoding="utf-8")
    systemd = FakeSystemd({"fleetwright-agent@agent01": "active"})
    assert run(fleet, systemd=systemd, journal={"fleetwright-agent@agent01": NOW - 23 * 60}) == 0
    assert lines(capsys) == ["watchdog: clean"]


def test_a_journal_it_cannot_read_is_missed(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    systemd = FakeSystemd({"fleetwright-agent@agent01": "active"})
    assert run(fleet, systemd=systemd, journal={"fleetwright-agent@agent01": None}) == 1
    out = lines(capsys)
    assert out[0].startswith("missed stalled: no journal line for fleetwright-agent@agent01")
    assert "systemd-journal" in out[0]


# --- a red main -----------------------------------------------------------------------------------


def test_a_red_main_is_caught_and_stops_the_line(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(main=[check("backend"), check("frontend", "failure"), check("e2e", None)])
    assert run(fleet, gh=gh) == 0
    out = lines(capsys)
    assert out[0] == "caught red-main: main is red at aaaaaaa (frontend); the line is now stopped"
    note = fleetwright_stop.status(fleet / "STOP")
    assert note is not None and note.startswith("watchdog: main is red at aaaaaaa (frontend)")


def test_a_line_already_stopped_keeps_its_note(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleetwright_stop.stop("the operator's reason", fleet / "STOP")
    assert run(fleet, gh=FakeGitHub(main=[check("backend", "timed_out")])) == 0
    assert "the line was already stopped" in lines(capsys)[0]
    note = fleetwright_stop.status(fleet / "STOP")
    assert note is not None and note.startswith("the operator's reason")


def recorded_runs() -> list[dict[str, Any]]:
    path = Path(__file__).parent / "fixtures" / "watchdog" / "check_runs_rerun.json"
    runs: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["check_runs"]
    return runs


def test_a_check_that_failed_and_then_passed_is_not_red(fleet: Path) -> None:
    # The drill check failed, then a newer run passed in its own check suite; GitHub lists both.
    assert run(fleet, gh=FakeGitHub(main=recorded_runs())) == 0
    assert fleetwright_stop.status(fleet / "STOP") is None


def test_a_check_that_passed_and_then_failed_is_red(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = recorded_runs()
    for r in runs:
        if r["name"] == "drill":
            r["conclusion"] = "failure" if r["conclusion"] == "success" else "success"
    assert run(fleet, gh=FakeGitHub(main=runs)) == 0
    assert lines(capsys)[0].startswith("caught red-main: main is red at 2d08681 (drill)")


@pytest.mark.parametrize("conclusion", ["success", "neutral", "skipped", "cancelled", None])
def test_a_main_that_is_not_red_does_not_stop_the_line(fleet: Path, conclusion: str | None) -> None:
    assert run(fleet, gh=FakeGitHub(main=[check("backend", conclusion)])) == 0
    assert fleetwright_stop.status(fleet / "STOP") is None


def test_a_main_it_cannot_read_is_missed_and_does_not_stop_the_line(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(fleet, gh=FakeGitHub(fail="check-runs")) == 1
    out = lines(capsys)
    assert out[0].startswith("missed red-main: gh api repos/o/r/commits/HEAD/check-runs")
    assert "HTTP 502" in out[0] and "usage" not in out[0]
    assert fleetwright_stop.status(fleet / "STOP") is None


# --- stuck pull requests --------------------------------------------------------------------------


def test_a_red_pr_nobody_holds_is_stuck(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(fleet, gh=FakeGitHub(pulls=[pr(7, 3, checks="FAILURE")])) == 0
    out = lines(capsys)
    assert out[0] == (
        "caught stuck-pr: PR #7 (issue #3) has not moved for 240 min (limit 120): needs a fix round"
    )


def test_a_green_pr_with_no_verdict_is_stuck_waiting_for_a_review(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(fleet, gh=FakeGitHub(pulls=[pr(7, 3)]))
    assert lines(capsys)[0].endswith("needs a review")


def test_an_approved_green_pr_is_stuck_waiting_for_a_person(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(pulls=[pr(7, 3)], comments={7: [verdict(SHA, "approve")]})
    run(fleet, gh=gh)
    assert lines(capsys)[0].endswith("approved, waiting for a person to merge")


def test_a_pr_that_moved_recently_is_not_stuck(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(fleet, gh=FakeGitHub(pulls=[pr(7, 3, checks="FAILURE", updated=RECENT)]))
    assert lines(capsys) == ["watchdog: clean"]


@pytest.mark.parametrize(
    ("slot", "unit", "work"),
    [
        ("agent01", "fleetwright-agent@agent01", "fix 3"),
        ("review01", "fleetwright-review@review01", "review 7"),
    ],
)
def test_a_pr_a_running_slot_holds_is_not_stuck(
    fleet: Path, capsys: pytest.CaptureFixture[str], slot: str, unit: str, work: str
) -> None:
    (fleet / "slots" / slot).write_text(work + "\n", encoding="utf-8")
    run(
        fleet,
        gh=FakeGitHub(pulls=[pr(7, 3, checks="FAILURE")]),
        systemd=FakeSystemd({unit: "active"}),
    )
    assert lines(capsys) == ["watchdog: clean"]


def test_drafts_and_other_peoples_prs_are_left_alone(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = [pr(7, 3, isDraft=True), pr(8, 4, headRefName="feature/x")]
    run(fleet, gh=FakeGitHub(pulls=pulls))
    assert lines(capsys) == ["watchdog: clean"]


def test_pull_requests_it_cannot_read_are_missed_and_the_other_checks_still_run(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(fail="pr list", main=[check("backend", "failure")])
    assert run(fleet, gh=gh, systemd=FakeSystemd({"fleetwright-agent@agent02": "failed"})) == 1
    out = lines(capsys)
    assert [ln.split(":")[0] for ln in out[:-1]] == [
        "caught failed-unit",
        "caught red-main",
        "missed stuck-pr",
    ]
    assert out[-1] == "watchdog: caught 2, missed 1"


# --- the command ----------------------------------------------------------------------------------


def test_a_bad_config_is_refused(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (fleet / "fleetwright.json").write_text(
        '{"repo": "o/r", "watchdog": {"stall_minutes": 0}}', "utf-8"
    )
    assert run(fleet) == 2
    assert "fleetwright_watchdog" in capsys.readouterr().err


def test_the_real_journal_reader_parses_the_last_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["journalctl", "-u", "fleetwright-agent@agent01"]
        return subprocess.CompletedProcess(cmd, 0, '{"__REALTIME_TIMESTAMP": "1800000000500000"}\n')

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fleetwright_watchdog._journal("fleetwright-agent@agent01") == 1_800_000_000.5


@pytest.mark.parametrize("stdout", ["", "-- No entries --\n", '{"MESSAGE": "x"}\n'])
def test_the_real_journal_reader_answers_none_when_there_is_nothing(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout)
    )
    assert fleetwright_watchdog._journal("fleetwright-agent@agent01") is None


# --- wrong-branch: a push that is not the run's own ---------------------------------------------

BOT = "g26work-fleet-authors[bot]"
T = NOW - 600  # ten minutes ago


def with_app_bot(fleet: Path, **extra: Any) -> None:
    path = fleet / "fleetwright.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["watchdog"] = {"app_bot": BOT, **extra}
    path.write_text(json.dumps(raw), encoding="utf-8")


def stamp(t: float) -> str:
    return str(int(t * 1_000_000))


def token_line(
    kind: str,
    slot: str,
    assignment: str | None,
    t: float,
    invocation: str,
    uid: str = "0",
) -> dict[str, Any]:
    held = f'assignment="{assignment}"' if assignment else "assignment=none"
    if kind == "renewed":
        message = f"token renewed slot={slot} {held} mode=write invocation={invocation} id=sha256:x"
        unit, inv = "fleetwright-token-refresh.service", "refresh-run"
    else:
        message = f"token {kind} slot={slot} {held} mode=write id=sha256:x"
        unit, inv = f"fleetwright-agent@{slot}.service", invocation
    return {
        "SYSLOG_IDENTIFIER": "fleetwright-token",
        "_UID": uid,
        "_SYSTEMD_UNIT": unit,
        "_SYSTEMD_INVOCATION_ID": inv,
        "MESSAGE": message,
        "__REALTIME_TIMESTAMP": stamp(t),
        "__CURSOR": f"t-{invocation}-{kind}-{t}",
    }


def push_line(slot: str, ref: str, t: float, invocation: str, n: int = 0) -> dict[str, Any]:
    return {
        "SYSLOG_IDENTIFIER": "fleetwright-push",
        "_UID": "1001",
        "_SYSTEMD_UNIT": f"fleetwright-agent@{slot}.service",
        "_SYSTEMD_INVOCATION_ID": invocation,
        "MESSAGE": f"ref={ref} sha={'b' * 40}",
        "__REALTIME_TIMESTAMP": stamp(t),
        "__CURSOR": f"p-{n}",
    }


def activity(
    ref: str, t: float, kind: str = "push", actor: str = BOT, n: int = 1
) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "id": n,
        "ref": ref,
        "timestamp": datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "activity_type": kind,
        "actor": {"login": actor},
    }


def two_runs() -> list[dict[str, Any]]:
    """agent01 on issue 12 and agent02 on a fix round for issue 14, both started 30 minutes ago."""
    return [
        token_line("minted", "agent01", "issue 12", NOW - 1800, "inv1"),
        token_line("minted", "agent02", "fix 14", NOW - 1800, "inv2"),
    ]


def wrong(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [ln for ln in lines(capsys) if "wrong-branch" in ln]


@pytest.mark.parametrize(
    ("slot", "ref", "invocation"),
    [("agent01", "refs/heads/issue/12", "inv1"), ("agent02", "refs/heads/issue/14", "inv2")],
)
def test_a_run_that_pushes_its_own_branch_is_clean(
    fleet: Path, capsys: pytest.CaptureFixture[str], slot: str, ref: str, invocation: str
) -> None:
    journal = FakeJournal([*two_runs(), push_line(slot, ref, T, invocation)])
    gh = FakeGitHub()
    gh.activity = [activity(ref, T)]
    with_app_bot(fleet)
    assert run(fleet, gh=gh, entries=journal) == 0
    assert wrong(capsys) == []


def test_a_push_to_another_slots_branch_is_caught_from_the_pre_push_log(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = FakeJournal([*two_runs(), push_line("agent02", "refs/heads/issue/12", T, "inv2")])
    assert run(fleet, entries=journal) == 0
    assert wrong(capsys) == [
        "caught wrong-branch: agent02 (fix 14) pushed refs/heads/issue/12 at 2027-01-15 07:50 UTC "
        "(its pre-push log); `systemctl stop fleetwright-agent@agent02` ends the run"
    ]


def test_a_token_line_a_slot_wrote_itself_is_not_believed(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # agent02 logs a fake mint for issue 12 from its own user; only root's lines count.
    forged = token_line("minted", "agent02", "issue 12", NOW - 900, "inv2", uid="1002")
    journal = FakeJournal(
        [*two_runs(), forged, push_line("agent02", "refs/heads/issue/12", T, "inv2")]
    )
    run(fleet, entries=journal)
    assert len(wrong(capsys)) == 1
    assert journal.calls[0][:2] == ["SYSLOG_IDENTIFIER=fleetwright-token", "_UID=0"]


def test_a_renewal_names_the_run_whose_mint_is_out_of_reach(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = FakeJournal(
        [
            token_line("renewed", "agent01", "issue 12", NOW - 1200, "inv9"),
            push_line("agent01", "refs/heads/issue/12", T, "inv9"),
        ]
    )
    run(fleet, entries=journal)
    assert wrong(capsys) == []


def test_a_push_from_a_run_with_no_token_line_is_missed(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = FakeJournal([push_line("agent03", "refs/heads/issue/3", T, "unknown")])
    assert run(fleet, entries=journal) == 1
    (line,) = wrong(capsys)
    assert line.startswith("missed wrong-branch: agent03 pushed refs/heads/issue/3")


def test_github_shows_a_push_no_run_held(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub()
    gh.activity = [activity("refs/heads/issue/31", T)]
    with_app_bot(fleet)
    run(fleet, gh=gh, entries=FakeJournal(two_runs()))
    assert wrong(capsys) == [
        "caught wrong-branch: the App pushed refs/heads/issue/31 at 2027-01-15 07:50 UTC "
        "while no slot held issue 31 (GitHub activity)"
    ]
    (call,) = [c for c in gh.calls if "/activity?" in c[-1]]
    assert call[-1] == (
        f"repos/{REPO}/activity?per_page=100&time_period=day&actor=g26work-fleet-authors%5Bbot%5D"
    )


@pytest.mark.parametrize(
    ("kind", "verb"),
    [
        ("push", "pushed"),
        ("force_push", "force-pushed"),
        ("branch_creation", "created"),
        ("branch_deletion", "deleted"),
    ],
)
def test_github_shows_a_ref_that_is_not_an_issue_branch(
    fleet: Path, capsys: pytest.CaptureFixture[str], kind: str, verb: str
) -> None:
    gh = FakeGitHub()
    gh.activity = [activity("refs/heads/scratch", T, kind=kind)]
    with_app_bot(fleet)
    run(fleet, gh=gh, entries=FakeJournal(two_runs()))
    assert wrong(capsys) == [
        f"caught wrong-branch: the App {verb} refs/heads/scratch at 2027-01-15 07:50 UTC, "
        "not an issue/<n> branch (GitHub activity)"
    ]


def test_a_push_just_after_the_run_ended_is_within_the_slack(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ended = [*two_runs(), token_line("revoked", "agent01", "issue 12", T - 60, "inv1")]
    gh = FakeGitHub()
    gh.activity = [activity("refs/heads/issue/12", T)]  # 60 s after the revoke; slack is 120 s
    with_app_bot(fleet)
    run(fleet, gh=gh, entries=FakeJournal(ended))
    assert wrong(capsys) == []
    gh.activity = [activity("refs/heads/issue/12", T + 600, n=2)]
    run(fleet, gh=gh, entries=FakeJournal(ended))
    assert len(wrong(capsys)) == 1


def test_other_actors_on_github_are_not_the_fleets(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    gh.activity = [activity("refs/heads/scratch", T, actor="glennmcd")]
    with_app_bot(fleet)
    run(fleet, gh=gh, entries=FakeJournal(two_runs()))
    assert wrong(capsys) == []


def test_each_push_is_reported_once(fleet: Path, capsys: pytest.CaptureFixture[str]) -> None:
    journal = FakeJournal([*two_runs(), push_line("agent02", "refs/heads/issue/12", T, "inv2")])
    gh = FakeGitHub()
    gh.activity = [activity("refs/heads/issue/31", T)]
    with_app_bot(fleet)
    run(fleet, gh=gh, entries=journal)
    assert len(wrong(capsys)) == 2
    run(fleet, gh=gh, entries=journal)
    assert wrong(capsys) == []
    pushes = [c for c in journal.calls if "SYSLOG_IDENTIFIER=fleetwright-push" in c]
    assert pushes[-1][-2:] == ["--after-cursor", "p-0"]


def test_a_first_pass_looks_back_a_day(fleet: Path) -> None:
    journal = FakeJournal()
    run(fleet, entries=journal)
    pushes = [c for c in journal.calls if "SYSLOG_IDENTIFIER=fleetwright-push" in c]
    assert pushes[0][-2:] == ["--since", f"@{int(NOW - 24 * 3600)}"]


def test_without_an_app_bot_github_is_not_asked(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert run(fleet, gh=gh) == 0
    assert not any("/activity?" in c[-1] for c in gh.calls)
    assert lines(capsys) == ["watchdog: clean"]


def test_sources_it_cannot_read_are_missed_and_the_rest_still_runs(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(fail="/activity?")
    with_app_bot(fleet)
    assert run(fleet, gh=gh, entries=FakeJournal(fail="fleetwright-push")) == 1
    out = wrong(capsys)
    assert any(ln.startswith("missed wrong-branch: the pre-push log") for ln in out)
    assert any(ln.startswith("missed wrong-branch: GitHub activity") for ln in out)


def test_the_app_bot_must_be_a_bot_login(fleet: Path) -> None:
    with_app_bot(fleet)
    path = fleet / "fleetwright.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["watchdog"]["app_bot"] = "glennmcd"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert run(fleet) == 2


def test_the_real_entry_reader_parses_journalctl_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"MESSAGE": "a"}\nnot json\n{"MESSAGE": "b"}\n')

    monkeypatch.setattr(subprocess, "run", fake_run)
    got = fleetwright_watchdog._journal_entries(["SYSLOG_IDENTIFIER=fleetwright-push"])
    assert got == [{"MESSAGE": "a"}, {"MESSAGE": "b"}]
    assert seen[0][:4] == ["journalctl", "-o", "json", "--no-pager"]
