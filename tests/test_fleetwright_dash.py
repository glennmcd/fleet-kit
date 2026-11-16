"""The dashboard's model and layout, from a fake journal and fake systemctl."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import fleetwright_dash
import pytest
from fleetwright_dash import Dashboard, discover, entry_line, render

AGENT = "fleetwright-agent@agent01.service"
REVIEW = "fleetwright-review@review01.service"
ALLOCATE = "fleetwright-allocate.service"
WATCHDOG = "fleetwright-watchdog.service"


class FakeSystemctl:
    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states = states or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        state = self.states.get(args[-1], "inactive")
        return (0 if state == "active" else 3), state + "\n"


def fields(units: dict[str, list[str]]) -> fleetwright_dash.Fields:
    return lambda field: units.get(field, [])


def test_it_finds_every_agent_reviewer_the_allocator_and_the_watchdog() -> None:
    found = discover(
        fields(
            {
                "UNIT": [
                    "fleetwright-agent@agent02.service",
                    "fleetwright-review@review01.service",
                    "fleetwright-agent.slice",
                    "fleetwright-watchdog.timer",
                    "fleetwright-report.service",
                    "fleetwright-allocate-dry-run.service",
                    "ssh.service",
                ],
                "_SYSTEMD_UNIT": [
                    "fleetwright-agent@agent01.service",
                    "fleetwright-check-agent@agent01.service",
                    "fleetwright-agent@agent02.service",
                ],
            }
        )
    )
    assert found == [
        "fleetwright-agent@agent01.service",
        "fleetwright-agent@agent02.service",
        REVIEW,
        ALLOCATE,
        WATCHDOG,
    ]


def test_the_allocator_and_watchdog_have_panels_before_they_have_logged() -> None:
    assert discover(fields({})) == [ALLOCATE, WATCHDOG]


def test_a_service_line_and_systemds_line_about_it_both_reach_its_panel() -> None:
    assert entry_line({"_SYSTEMD_UNIT": AGENT, "MESSAGE": "tool Bash: git status"}) == (
        AGENT,
        "tool Bash: git status",
    )
    started = {"_SYSTEMD_UNIT": "init.scope", "UNIT": AGENT, "MESSAGE": "Started fleetwright-agent"}
    assert entry_line(started) == (AGENT, "Started fleetwright-agent")


def test_lines_of_other_units_are_ignored() -> None:
    assert entry_line({"_SYSTEMD_UNIT": "ssh.service", "MESSAGE": "x"}) is None
    assert entry_line({"UNIT": "fleetwright-check-agent@agent01.service", "MESSAGE": "x"}) is None
    assert entry_line({"MESSAGE": "no unit"}) is None


def test_a_message_journald_stored_as_bytes_is_decoded_and_control_characters_dropped() -> None:
    raw = list(b"ok \x1b[31mred\x1b[0m\tdone")
    unit, text = entry_line({"_SYSTEMD_UNIT": AGENT, "MESSAGE": raw}) or ("", "")
    assert unit == AGENT
    assert "\x1b" not in text and "\t" not in text
    assert "red" in text and "done" in text


def test_a_panel_keeps_only_the_latest_five_lines() -> None:
    dash = Dashboard([AGENT])
    for i in range(8):
        dash.add(AGENT, f"line {i}")
    assert list(dash.panels[AGENT].lines) == [f"line {i}" for i in range(3, 8)]


def test_a_unit_first_seen_while_following_gets_its_own_panel() -> None:
    dash = Dashboard([ALLOCATE, WATCHDOG])
    dash.add("fleetwright-agent@agent03.service", "Started")
    assert list(dash.panels) == ["fleetwright-agent@agent03.service", ALLOCATE, WATCHDOG]


def test_refresh_reads_each_units_state_and_each_slots_assignment(tmp_path: Path) -> None:
    (tmp_path / "agent01").write_text("issue 55\n", encoding="utf-8")
    systemctl = FakeSystemctl({AGENT: "active", REVIEW: "failed"})
    dash = Dashboard([AGENT, REVIEW, ALLOCATE])
    dash.refresh(systemctl, tmp_path)
    assert dash.panels[AGENT].state == "active"
    assert dash.panels[AGENT].assignment == "issue 55"
    assert dash.panels[REVIEW].state == "failed"
    assert dash.panels[REVIEW].assignment == ""
    assert dash.panels[ALLOCATE].assignment == ""


def test_it_only_ever_asks_systemctl_and_changes_nothing(tmp_path: Path) -> None:
    systemctl = FakeSystemctl()
    Dashboard([AGENT, REVIEW, ALLOCATE, WATCHDOG]).refresh(systemctl, tmp_path)
    assert {c[0] for c in systemctl.calls} == {"is-active"}


def test_render_shows_every_panel_its_state_assignment_and_lines(tmp_path: Path) -> None:
    (tmp_path / "agent01").write_text("issue 55\n", encoding="utf-8")
    dash = Dashboard([AGENT, REVIEW, ALLOCATE, WATCHDOG])
    dash.refresh(FakeSystemctl({AGENT: "active", REVIEW: "failed"}), tmp_path)
    dash.add(AGENT, "tool Bash: bun run test")
    dash.add(WATCHDOG, "watchdog: clean")
    screen = "\n".join(render(dash, 80, 60))
    assert "agent01" in screen and "issue 55" in screen and "active" in screen
    assert "review01" in screen and "FAILED" in screen
    assert "allocator" in screen and "watchdog" in screen
    assert "tool Bash: bun run test" in screen and "watchdog: clean" in screen


def test_render_fits_the_terminal(tmp_path: Path) -> None:
    dash = Dashboard([f"fleetwright-agent@agent0{i}.service" for i in range(1, 5)] + [WATCHDOG])
    for unit in dash.panels:
        for i in range(5):
            dash.add(unit, "x" * 300 + str(i))
    for width, height in ((80, 24), (40, 10), (200, 50)):
        screen = render(dash, width, height)
        assert len(screen) <= height
        assert all(len(row) <= width for row in screen)


def test_a_wide_terminal_puts_panels_side_by_side() -> None:
    units = [f"fleetwright-agent@agent0{i}.service" for i in range(1, 5)]
    dash = Dashboard(units)
    narrow = render(dash, 80, 100)
    wide = render(dash, 170, 100)
    assert len(wide) < len(narrow)
    assert "agent01" in wide[0] and "agent02" in wide[0]


def test_the_follow_command_reads_only_the_fleets_units_from_now_on() -> None:
    argv = fleetwright_dash.follow_command()
    assert argv[:2] == ["journalctl", "--follow"]
    assert "--lines=0" in argv and "--output=json" in argv
    units = [argv[i + 1] for i, a in enumerate(argv) if a == "--unit"]
    assert units == ["fleetwright-agent@*", "fleetwright-review@*", ALLOCATE, WATCHDOG]


@pytest.mark.parametrize("line", ["", "not json", json.dumps({"MESSAGE": "x"})])
def test_an_unreadable_follow_line_is_skipped(line: str) -> None:
    dash = Dashboard([AGENT])
    dash.feed(line)
    assert list(dash.panels[AGENT].lines) == []


def test_a_follow_line_is_routed_to_its_panel() -> None:
    dash = Dashboard([AGENT])
    dash.feed(json.dumps({"_SYSTEMD_UNIT": AGENT, "MESSAGE": "say: done"}))
    assert list(dash.panels[AGENT].lines) == ["say: done"]


@pytest.mark.parametrize(
    "message",
    [
        "Starting fleetwright-agent@agent01.service - Fleet agent agent01...",
        "Started fleetwright-agent@agent01.service - Fleet agent agent01.",
        "Finished fleetwright-watchdog.service - Fleet watchdog: stalled agents",
        "fleetwright-agent@agent01.service: Deactivated successfully.",
        "fleetwright-agent@agent01.service: Consumed 3min 25.682s CPU time over 3min 3.805s",
    ],
)
def test_systemds_routine_lines_are_left_out_so_the_units_own_lines_show(message: str) -> None:
    assert entry_line({"_SYSTEMD_UNIT": "init.scope", "UNIT": AGENT, "MESSAGE": message}) is None


@pytest.mark.parametrize(
    "message",
    [
        "fleetwright-agent@agent03.service: Main process exited, code=exited, status=70/SOFTWARE",
        "fleetwright-agent@agent03.service: Failed with result 'exit-code'.",
        "fleetwright-agent@agent01.service: Sent signal SIGCONT to main process 496507 (uv)",
    ],
)
def test_systemds_lines_about_failures_and_signals_are_kept(message: str) -> None:
    assert entry_line({"UNIT": AGENT, "MESSAGE": message}) == (AGENT, message)


def test_a_units_own_line_is_kept_even_if_it_reads_like_systemds() -> None:
    assert entry_line({"_SYSTEMD_UNIT": AGENT, "MESSAGE": "Started the test run"}) is not None
