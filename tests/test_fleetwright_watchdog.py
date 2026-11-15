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


def run(
    fleet: Path,
    gh: FakeGitHub | None = None,
    systemd: FakeSystemd | None = None,
    journal: Mapping[str, float | None] | None = None,
) -> int:
    last = journal or {}
    return fleetwright_watchdog.main(
        ["--config", str(fleet / "fleetwright.json"), "--fleetwright-dir", str(fleet)],
        gh=gh or FakeGitHub(),
        systemctl=systemd or FakeSystemd(),
        journal=lambda unit: last.get(unit, NOW - 60),
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
