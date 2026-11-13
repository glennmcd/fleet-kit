"""The stop switch: the flag file, the hook that reads it, and the command that sets it."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import fleet_stop
import pytest
from fleet_stop import clear, status, stop

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stop_line_hook", ROOT / "hooks" / "stop_line.py")
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


@pytest.fixture
def flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fleet" / "STOP"
    monkeypatch.setenv("FLEET_STOP_FILE", str(path))
    monkeypatch.delenv("FLEET_CLONE", raising=False)
    return path


def test_the_line_runs_until_it_is_stopped(flag: Path) -> None:
    assert hook.decide() is None
    stop("deploy gone wrong", flag)
    out = hook.decide()
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "deploy gone wrong" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert clear(flag) is True
    assert hook.decide() is None


def test_clearing_a_line_that_is_running_says_so(flag: Path) -> None:
    assert clear(flag) is False


def test_status_reports_the_reason(flag: Path) -> None:
    assert status(flag) is None
    stop("looping on issue 12", flag)
    assert "looping on issue 12" in (status(flag) or "")


def test_an_empty_flag_file_still_stops(flag: Path) -> None:
    flag.parent.mkdir(parents=True)
    flag.write_text("", encoding="utf-8")
    assert hook.decide() is not None


def test_a_directory_in_place_of_the_flag_file_still_stops(flag: Path) -> None:
    flag.mkdir(parents=True)  # unreadable as a file, but it exists
    assert hook.decide() is not None


def test_a_long_reason_is_cut_not_dumped(flag: Path) -> None:
    flag.parent.mkdir(parents=True)
    flag.write_text("x" * 100_000, encoding="utf-8")
    reason = hook.decide()["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) < 1_000


def test_a_machine_that_is_not_a_fleet_agent_is_never_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLEET_STOP_FILE", raising=False)
    monkeypatch.delenv("FLEET_CLONE", raising=False)
    assert hook.stop_file() is None
    assert hook.decide() is None


def test_a_clone_alone_switches_on_the_default_flag_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_STOP_FILE", raising=False)
    monkeypatch.setenv("FLEET_CLONE", "/x")
    assert hook.stop_file() == "/var/lib/fleet/STOP"


def run_hook(env: dict[str, str], stdin: str = "{}") -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "stop_line.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env={**clean, **env},
        timeout=30,
    )


def test_as_a_process_it_denies_every_tool_while_stopped(flag: Path) -> None:
    stop("halt", flag)
    done = run_hook({"FLEET_STOP_FILE": str(flag)}, json.dumps({"tool_name": "Read"}))
    assert done.returncode == 0
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_as_a_process_it_is_silent_while_running(flag: Path) -> None:
    done = run_hook({"FLEET_STOP_FILE": str(flag)})
    assert done.returncode == 0 and done.stdout == ""


def test_the_command_line_stops_clears_and_reports(flag: Path) -> None:
    assert fleet_stop.main(["status"]) == 0
    assert fleet_stop.main(["stop", "mid", "run"]) == 0
    assert fleet_stop.main(["status"]) == 1
    assert "mid run" in (status() or "")
    assert fleet_stop.main(["clear"]) == 0
    assert fleet_stop.main(["status"]) == 0
