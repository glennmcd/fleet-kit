"""The dashboard's model and layout, from a fake journal and fake systemctl."""

from __future__ import annotations

import json
import time
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


# --- the time of the latest line, and whether a unit starts on its own ----------------------------

WHEN = 1_763_262_148.0  # an instant; shown in local time, as the journal shows it


def test_a_panel_remembers_when_its_latest_line_was_logged() -> None:
    dash = Dashboard([AGENT])
    stamp = str(int(WHEN * 1_000_000))
    dash.feed(json.dumps({"_SYSTEMD_UNIT": AGENT, "MESSAGE": "x", "__REALTIME_TIMESTAMP": stamp}))
    assert dash.panels[AGENT].updated == WHEN


def test_the_header_shows_the_latest_lines_time_on_the_right() -> None:
    dash = Dashboard([WATCHDOG])
    dash.add(WATCHDOG, "watchdog: clean", WHEN)
    header = render(dash, 80, 10)[0]
    clock = time.strftime("%H:%M:%S", time.localtime(WHEN))
    assert header.rstrip("─ ").endswith(clock)
    assert len(header) == 80
    assert header.index("watchdog") < header.index(clock)


def test_a_panel_with_no_lines_has_no_time() -> None:
    header = render(Dashboard([WATCHDOG]), 80, 10)[0]
    assert ":" not in header.split("watchdog", 1)[1]


def refreshed(tmp_path: Path, states: dict[str, str], stopped: bool = False) -> Dashboard:
    slots = tmp_path / "slots"
    slots.mkdir(exist_ok=True)
    if stopped:
        (tmp_path / "STOP").write_text("why\n", encoding="utf-8")
    dash = Dashboard([AGENT, REVIEW, ALLOCATE, WATCHDOG])
    dash.refresh(FakeSystemctl(states), slots, tmp_path / "STOP")
    return dash


def test_a_timer_driven_unit_between_passes_says_its_timer_is_on(tmp_path: Path) -> None:
    dash = refreshed(
        tmp_path, {"fleetwright-allocate.timer": "active", "fleetwright-watchdog.timer": "active"}
    )
    assert dash.panels[WATCHDOG].state == "inactive"
    assert dash.panels[WATCHDOG].mode == "timer on"
    assert dash.panels[ALLOCATE].mode == "timer on"
    assert "inactive · timer on" in render(dash, 80, 60)[3 * 6]


def test_a_timer_that_is_off_is_shown(tmp_path: Path) -> None:
    dash = refreshed(tmp_path, {"fleetwright-allocate.timer": "active"})
    assert dash.panels[WATCHDOG].mode == "timer off"


def test_a_slot_is_auto_while_the_allocator_can_start_it(tmp_path: Path) -> None:
    dash = refreshed(tmp_path, {"fleetwright-allocate.timer": "active"})
    assert dash.panels[AGENT].mode == "auto"
    assert dash.panels[REVIEW].mode == "auto"


@pytest.mark.parametrize(
    ("states", "stopped", "mode"),
    [
        ({"fleetwright-allocate.timer": "inactive"}, False, "allocator off"),
        ({"fleetwright-allocate.timer": "active"}, True, "fleet stopped"),
        ({"fleetwright-allocate.timer": "active", AGENT: "failed"}, False, "held: reset-failed"),
        ({"fleetwright-allocate.timer": "active", AGENT: "failed"}, True, "held: reset-failed"),
    ],
)
def test_a_slot_says_why_nothing_will_start_it(
    tmp_path: Path, states: dict[str, str], stopped: bool, mode: str
) -> None:
    assert refreshed(tmp_path, states, stopped).panels[AGENT].mode == mode


def test_a_routine_line_moves_the_time_but_adds_no_text() -> None:
    # A quiet allocator pass logs only systemd's start and stop lines; its time should still move.
    dash = Dashboard([ALLOCATE])
    stamp = str(int(WHEN * 1_000_000))
    entry = {
        "_SYSTEMD_UNIT": "init.scope",
        "UNIT": ALLOCATE,
        "MESSAGE": "fleetwright-allocate.service: Deactivated successfully.",
        "__REALTIME_TIMESTAMP": stamp,
    }
    dash.feed(json.dumps(entry))
    assert dash.panels[ALLOCATE].updated == WHEN
    assert list(dash.panels[ALLOCATE].lines) == []


# --- the commands section -----------------------------------------------------------------------


STOP = "python3 /opt/fleetwright/scripts/fleetwright_stop.py"


def test_the_commands_section_shows_the_keystroke_commands_then_the_typed_ones() -> None:
    assert fleetwright_dash.commands(Dashboard([AGENT, WATCHDOG])) == [
        f'ss: {STOP} stop "from dashboard"   --| stop the fleet',
        f"cc: {STOP} clear   --| restart the fleet",
        "pp: systemctl stop fleetwright-allocate.timer   --| pause new work",
        "rr: systemctl start fleetwright-allocate.timer   --| resume new work",
        "  : systemctl start fleetwright-allocate-dry-run; "
        "journalctl -u fleetwright-allocate-dry-run -o cat -n 20   "
        "--| what the allocator would assign",
        "  : systemctl start fleetwright-check-agent@agentNN   --| check a slot "
        "(fleetwright-check-review@reviewNN)",
        "  : systemctl stop fleetwright-agent@agentNN   --| stop one agent; "
        "its next run keeps or stashes its work",
        "  : journalctl -u fleetwright-agent@agentNN -o cat -n 50   --| an agent's full log",
        "  : systemctl start fleetwright-report   --| the token report; "
        "docs/control-plane.md has how to read it",
    ]


def test_a_typed_command_has_no_keystroke() -> None:
    typed = [c for c in fleetwright_dash.commands(Dashboard([AGENT])) if c.startswith("  : ")]
    assert len(typed) == 5
    keys = fleetwright_dash.Keys()
    for letter in "adjtk":  # none of the typed commands' first letters runs anything
        keys.press(letter, 10.0)
        assert keys.press(letter, 10.1) is None


def test_each_keystroke_runs_exactly_its_command() -> None:
    argv = {key: list(action.argv) for key, action in fleetwright_dash.ACTIONS.items()}
    assert argv == {
        "ss": ["python3", "/opt/fleetwright/scripts/fleetwright_stop.py", "stop", "from dashboard"],
        "cc": ["python3", "/opt/fleetwright/scripts/fleetwright_stop.py", "clear"],
        "pp": ["systemctl", "stop", "fleetwright-allocate.timer"],
        "rr": ["systemctl", "start", "fleetwright-allocate.timer"],
    }


def test_a_failed_unit_gets_its_own_lines_above_the_commands(tmp_path: Path) -> None:
    dash = refreshed(tmp_path, {"fleetwright-allocate.timer": "active", AGENT: "failed"})
    lines = fleetwright_dash.commands(dash)
    assert "agent01" in lines[0] and "failed" in lines[0]
    assert "systemctl reset-failed fleetwright-agent@agent01" in "\n".join(lines[:3])
    assert "journalctl -u fleetwright-agent@agent01" in "\n".join(lines[:3])
    assert lines[3].startswith("ss: ")


@pytest.mark.parametrize("key", ["ss", "cc", "pp", "rr"])
def test_the_same_key_twice_within_a_second_picks_its_command(key: str) -> None:
    keys = fleetwright_dash.Keys()
    assert keys.press(key[0], 10.0) is None
    assert keys.press(key[1], 10.5) == key


def test_one_press_does_nothing() -> None:
    keys = fleetwright_dash.Keys()
    assert keys.press("s", 10.0) is None


def test_a_second_press_after_the_window_starts_over() -> None:
    keys = fleetwright_dash.Keys()
    keys.press("s", 10.0)
    assert keys.press("s", 11.5) is None
    assert keys.press("s", 11.8) == "ss"


def test_another_key_between_the_presses_starts_over() -> None:
    keys = fleetwright_dash.Keys()
    keys.press("s", 10.0)
    assert keys.press("c", 10.2) is None
    assert keys.press("s", 10.4) is None


def test_a_command_is_not_run_twice_by_a_third_press() -> None:
    keys = fleetwright_dash.Keys()
    keys.press("p", 10.0)
    assert keys.press("p", 10.1) == "pp"
    assert keys.press("p", 10.2) is None


@pytest.mark.parametrize("key", ["x", "q", "S"])
def test_keys_with_no_command_never_pick_one(key: str) -> None:
    keys = fleetwright_dash.Keys()
    keys.press(key, 10.0)
    assert keys.press(key, 10.1) is None


def test_running_a_command_reports_its_exit_and_first_line() -> None:
    ran: list[list[str]] = []

    def run(argv: Sequence[str]) -> tuple[int, str]:
        ran.append(list(argv))
        return 0, "stopped: /var/lib/fleetwright/STOP\nmore\n"

    note = fleetwright_dash.run_action("ss", run)
    assert ran == [list(fleetwright_dash.ACTIONS["ss"].argv)]
    assert note == "ss stop the fleet: ok: stopped: /var/lib/fleetwright/STOP"


def test_a_failed_command_says_so() -> None:
    note = fleetwright_dash.run_action("pp", lambda argv: (1, "Access denied"))
    assert note == "pp pause new work: failed (exit 1): Access denied"


def test_the_last_result_shows_in_the_commands_header() -> None:
    dash = Dashboard([AGENT])
    dash.note = "pp pause new work: ok"
    header = next(row for row in render(dash, 100, 60) if "commands" in row)
    assert "pp pause new work: ok" in header


def test_the_commands_follow_the_panels_when_there_is_room() -> None:
    dash = Dashboard([AGENT, WATCHDOG])
    screen = render(dash, 100, 60)
    heads = [i for i, row in enumerate(screen) if row.startswith("──")]
    assert "commands" in screen[heads[-1]]
    assert heads[-1] > heads[0]
    assert any("fleetwright_stop.py stop" in row for row in screen)


def test_the_panels_keep_priority_on_a_short_terminal() -> None:
    dash = Dashboard([AGENT, WATCHDOG])
    screen = render(dash, 100, 12)
    assert len(screen) == 12
    assert sum(row.startswith("── agent01") or row.startswith("── watchdog") for row in screen) == 2
