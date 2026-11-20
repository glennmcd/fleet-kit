"""What each hook does on its own error: safety hooks refuse, cost hooks allow.

The invariant is in CLAUDE.md, under Invariants.

write_guard's case is ``test_run_as_a_process_it_fails_closed_on_garbage_input`` in
``tests/test_evasion_write_guard.py``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "stop_line_fail_mode", ROOT / "hooks" / "stop_line.py"
)
assert _spec and _spec.loader
stop_line = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stop_line)


def test_stop_line_fails_closed_when_it_cannot_check_the_stop_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise OSError("no such device")

    monkeypatch.setattr(stop_line, "decide", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name": "Read"}'))
    assert stop_line.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "no such device" in out["permissionDecisionReason"]


@pytest.mark.parametrize("name", ["context_ceiling", "wait_cap"])
def test_a_cost_hook_fails_open_on_garbage_input(name: str) -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / f"{name}.py")],
        input="{not json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0
    assert done.stdout == ""  # no decision: the call runs
    assert done.stderr.startswith(f"{name}: ")  # it took the error branch, not a silent allow
