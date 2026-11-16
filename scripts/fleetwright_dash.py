#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""A terminal dashboard of the fleet: one panel per unit, with its state and its latest 5 journal lines.

    uv run --script /opt/fleetwright/scripts/fleetwright_dash.py      q quits

Four keystrokes run a command; press the letter twice within a second:

    ss  stop the fleet       fleetwright_stop.py stop "from dashboard"
    cc  restart the fleet    fleetwright_stop.py clear
    pp  pause new work       systemctl stop fleetwright-allocate.timer
    rr  resume new work      systemctl start fleetwright-allocate.timer

Panels: each author (``fleetwright-agent@agentNN``), each reviewer (``fleetwright-review@reviewNN``), the
allocator and the watchdog. An agent's or reviewer's panel also shows its assignment from
``/var/lib/fleetwright/slots/<slot>``. A failed unit is marked FAILED, and a commands section under the panels lists the four commands. Each header ends with the time
of the unit's latest line, and says whether the unit starts on its own (see ``Dashboard.refresh``).

Apart from those four commands it only reads: the journal (as a member of ``systemd-journal``),
``systemctl is-active`` and the slots directory. It never reads ``/etc/fleetwright``, so it finds the slots from the journal rather than the
config, and a slot that has never logged has no panel until it does. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SLOTS_DIR = Path("/var/lib/fleetwright/slots")
ALLOCATE = "fleetwright-allocate.service"
WATCHDOG = "fleetwright-watchdog.service"
SLOT_UNIT = re.compile(r"fleetwright-(agent|review)@((?:agent|review)[0-9]{2})\.service")
LINES = 5
PANEL_WIDTH = 80  # a panel is at least this wide before panels go side by side
REFRESH_SECONDS = 2.0
# systemd's own lines about a unit starting and ending. They would fill a 5-line panel, so they are left
# out; its lines about failures and signals stay.
ROUTINE = re.compile(
    r"(Starting|Started|Finished) \S+\.service|\S+\.service: (Deactivated successfully|Consumed )"
)
BACKFILL = 200  # journal lines read per unit at start, to find the last 5 that are not routine
CONTROL = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x1f\x7f]")

Fields = Callable[[str], list[str]]  # journalctl -F <field>: every value the journal holds for it
Systemctl = Callable[[Sequence[str]], tuple[int, str]]


def is_panel_unit(unit: str) -> bool:
    return unit in (ALLOCATE, WATCHDOG) or bool(SLOT_UNIT.fullmatch(unit))


def _order(unit: str) -> tuple[int, str]:
    m = SLOT_UNIT.fullmatch(unit)
    if m:
        return (0 if m.group(1) == "agent" else 1), m.group(2)
    return (2 if unit == ALLOCATE else 3), unit


def discover(fields: Fields) -> list[str]:
    """The units to show: every slot unit the journal has seen, then the allocator and the watchdog."""
    seen = {u for name in ("UNIT", "_SYSTEMD_UNIT") for u in fields(name) if is_panel_unit(u)}
    return sorted(seen | {ALLOCATE, WATCHDOG}, key=_order)


def _text(message: Any) -> str:
    if isinstance(message, list):  # journald stores a message that is not valid UTF-8 as bytes
        message = bytes(b for b in message if isinstance(b, int) and 0 <= b < 256).decode(
            "utf-8", "replace"
        )
    return CONTROL.sub(" ", str(message or "")).strip()


def entry_line(entry: dict[str, Any]) -> tuple[str, str] | None:
    """The panel a journal entry belongs to and its text: a service's own line, or systemd's about it."""
    own = str(entry.get("_SYSTEMD_UNIT") or "")
    if is_panel_unit(own):
        return own, _text(entry.get("MESSAGE"))
    about = str(entry.get("UNIT") or "")
    if is_panel_unit(about):
        text = _text(entry.get("MESSAGE"))
        return None if ROUTINE.match(text) else (about, text)
    return None


@dataclass
class Panel:
    unit: str
    state: str = "unknown"
    mode: str = ""  # whether something will start the unit on its own: see Dashboard.refresh
    assignment: str = ""
    updated: float | None = None  # when its latest line was logged, seconds since the epoch
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LINES))

    @property
    def title(self) -> str:
        m = SLOT_UNIT.fullmatch(self.unit)
        return m.group(2) if m else ("allocator" if self.unit == ALLOCATE else "watchdog")

    @property
    def slot(self) -> str | None:
        m = SLOT_UNIT.fullmatch(self.unit)
        return m.group(2) if m else None


class Dashboard:
    def __init__(self, units: Iterable[str]) -> None:
        self.panels: dict[str, Panel] = {u: Panel(u) for u in units}
        self.stopped = False  # the stop flag, as the last refresh saw it
        self.note = ""  # the last keystroke command's result, shown in the commands header

    def _panel(self, unit: str) -> Panel:
        if unit not in self.panels:
            self.panels[unit] = Panel(unit)
            self.panels = {u: self.panels[u] for u in sorted(self.panels, key=_order)}
        return self.panels[unit]

    def add(self, unit: str, text: str, when: float | None = None) -> None:
        panel = self._panel(unit)
        panel.lines.append(text)
        if when is not None:
            panel.updated = when

    def feed(self, raw: str) -> None:
        """One line of ``journalctl --output=json``; anything unreadable is skipped."""
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        stamp = str(entry.get("__REALTIME_TIMESTAMP") or "")
        when = int(stamp) / 1_000_000 if stamp.isdigit() else None
        found = entry_line(entry)
        if found:
            self.add(*found, when)
            return
        # systemd's routine line about a unit: no text, but the unit did something, so its time moves.
        about = str(entry.get("UNIT") or "")
        if when is not None and is_panel_unit(about):
            self._panel(about).updated = when

    def refresh(self, systemctl: Systemctl, slots_dir: Path, stop_file: Path | None = None) -> None:
        """Each unit's state and assignment, and its mode: whether something will start it on its own.

        The allocator and the watchdog run a short pass and exit, so between passes they are inactive and
        their timer says whether another pass comes. A slot is started by the allocator: ``auto`` unless
        the allocator's timer is off, the fleet is stopped, or the unit failed and holds its work.
        """

        def active(unit: str) -> bool:
            return systemctl(["is-active", unit])[1].strip() == "active"

        stop = stop_file if stop_file is not None else slots_dir.parent / "STOP"
        stopped = self.stopped = stop.exists()
        timers = {u: active(u.removesuffix(".service") + ".timer") for u in (ALLOCATE, WATCHDOG)}
        for panel in self.panels.values():
            panel.state = systemctl(["is-active", panel.unit])[1].strip() or "unknown"
            panel.assignment = ""
            if panel.unit in timers:
                panel.mode = "timer on" if timers[panel.unit] else "timer off"
                continue
            panel.mode = (
                "held: reset-failed"
                if panel.state == "failed"
                else "fleet stopped"
                if stopped
                else "auto"
                if timers[ALLOCATE]
                else "allocator off"
            )
            if panel.slot:
                with contextlib.suppress(OSError):
                    panel.assignment = (slots_dir / panel.slot).read_text(encoding="utf-8").strip()


def _panel_rows(panel: Panel, width: int) -> list[str]:
    state = "FAILED" if panel.state == "failed" else panel.state
    if panel.mode:
        state += f" · {panel.mode}"
    head = (
        f"── {panel.title}  {state}" + (f"  {panel.assignment}" if panel.assignment else "") + " "
    )
    clock = (
        f" {time.strftime('%H:%M:%S', time.localtime(panel.updated))} ──"
        if panel.updated is not None
        else ""
    )
    if len(head) + len(clock) > width:
        clock = ""
    rows = [(head.ljust(width - len(clock), "─") + clock)[:width]]
    body = list(panel.lines)
    rows += [f"  {line}"[:width] for line in body] + [""] * (LINES - len(body))
    return rows


STOP_SCRIPT = "/opt/fleetwright/scripts/fleetwright_stop.py"


@dataclass(frozen=True)
class Action:
    argv: tuple[str, ...]
    label: str

    @property
    def text(self) -> str:
        return " ".join(f'"{a}"' if " " in a else a for a in self.argv)


# The only commands the dashboard runs, each on its letter pressed twice. Fixed here: nothing typed reaches
# them. They run as the dashboard's user, which fleetwright-admin and the polkit rule allow, without a shell.
ACTIONS: dict[str, Action] = {
    "ss": Action(("python3", STOP_SCRIPT, "stop", "from dashboard"), "stop the fleet"),
    "cc": Action(("python3", STOP_SCRIPT, "clear"), "restart the fleet"),
    "pp": Action(("systemctl", "stop", "fleetwright-allocate.timer"), "pause new work"),
    "rr": Action(("systemctl", "start", "fleetwright-allocate.timer"), "resume new work"),
}
DOUBLE_PRESS_SECONDS = 1.0


class Keys:
    """Turns keystrokes into a command: the same letter twice within a second. Anything else starts over."""

    def __init__(self) -> None:
        self.pending = ""
        self.at = 0.0

    def press(self, key: str, now: float) -> str | None:
        if key * 2 not in ACTIONS:
            self.pending = ""
            return None
        if self.pending == key and now - self.at <= DOUBLE_PRESS_SECONDS:
            self.pending = ""
            return key * 2
        self.pending, self.at = key, now
        return None


def run_action(key: str, run: Callable[[Sequence[str]], tuple[int, str]]) -> str:
    """Runs one command and returns a line saying how it went."""
    action = ACTIONS[key]
    code, out = run(action.argv)
    first = next((line.strip() for line in out.splitlines() if line.strip()), "")
    result = "ok" if code == 0 else f"failed (exit {code})"
    return f"{key} {action.label}: {result}" + (f": {first}" if first else "")


def commands(dash: Dashboard) -> list[str]:
    """The commands section: each failed unit's own commands first, as text, then the four keystrokes."""
    first: list[str] = []
    for panel in dash.panels.values():
        if panel.state == "failed":
            first += [
                f"{panel.title} failed and holds its work:",
                f"  journalctl -u {panel.unit.removesuffix('.service')} -o cat -n 50",
                f"  systemctl reset-failed {panel.unit.removesuffix('.service')}",
            ]
    return first + [f"{key}: {a.text}   --| {a.label}" for key, a in ACTIONS.items()]


def render(dash: Dashboard, width: int, height: int) -> list[str]:
    """The screen as text rows, no wider than ``width`` and no taller than ``height``.

    The panels come first; the commands section follows in whatever room is left.
    """
    columns = max(1, min(len(dash.panels), width // PANEL_WIDTH)) if dash.panels else 1
    col_width = max(1, (width - (columns - 1)) // columns)
    blocks = [_panel_rows(p, col_width) for p in dash.panels.values()]
    screen: list[str] = []
    for start in range(0, len(blocks), columns):
        group = blocks[start : start + columns]
        for i in range(LINES + 1):
            screen.append(" ".join(b[i].ljust(col_width) for b in group).rstrip()[:width])
    if len(screen) + 2 <= height:
        title = "── commands  (press the letter twice) " + (f"── {dash.note} " if dash.note else "")
        screen.append(title.ljust(width, "─")[:width])
        screen += [f"  {line}"[:width] for line in commands(dash)]
    return screen[:height]


# --- the terminal and the processes it reads ------------------------------------------------------


def follow_command() -> list[str]:
    units = ["fleetwright-agent@*", "fleetwright-review@*", ALLOCATE, WATCHDOG]
    argv = ["journalctl", "--follow", "--lines=0", "--output=json", "--no-pager"]
    for unit in units:
        argv += ["--unit", unit]
    return argv


def _run(argv: Sequence[str]) -> tuple[int, str]:
    done = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=30)
    return done.returncode, done.stdout


def _run_action(argv: Sequence[str]) -> tuple[int, str]:
    """A keystroke command, with what it printed on either stream."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _fields(name: str) -> list[str]:
    return _run(["journalctl", "--field", name])[1].split()


def _systemctl(args: Sequence[str]) -> tuple[int, str]:
    return _run(["systemctl", *args])


def _backfill(dash: Dashboard) -> None:
    for unit in list(dash.panels):
        out = _run(
            ["journalctl", "--unit", unit, f"--lines={BACKFILL}", "--output=json", "--no-pager"]
        )
        for raw in out[1].splitlines():
            dash.feed(raw)


def _draw(screen: Any, dash: Dashboard) -> None:
    import curses  # here, not at the top: the model and its tests need no terminal

    height, width = screen.getmaxyx()
    screen.erase()
    for y, row in enumerate(render(dash, width - 1, height)):
        attr = curses.A_BOLD if row.startswith("──") else curses.A_NORMAL
        if "FAILED" in row and row.startswith("──"):
            attr |= curses.A_REVERSE
        with contextlib.suppress(curses.error):  # the bottom-right cell cannot be written
            screen.addstr(y, 0, row, attr)
    screen.refresh()


def _loop(screen: Any) -> int:
    import curses

    curses.curs_set(0)
    screen.nodelay(True)
    dash = Dashboard(discover(_fields))
    _backfill(dash)
    follow = subprocess.Popen(follow_command(), stdout=subprocess.PIPE, text=True, bufsize=1)
    assert follow.stdout is not None
    os.set_blocking(follow.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(follow.stdout, selectors.EVENT_READ)
    last = 0.0
    keys = Keys()
    try:
        while True:
            if time.monotonic() - last >= REFRESH_SECONDS:
                dash.refresh(_systemctl, SLOTS_DIR)
                last = time.monotonic()
            for _key, _mask in selector.select(timeout=0.25):
                for raw in follow.stdout.readlines():
                    dash.feed(raw)
            _draw(screen, dash)
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            if 0 <= key < 256:
                chosen = keys.press(chr(key), time.monotonic())
                if chosen:
                    dash.note = run_action(chosen, _run_action)
                    last = 0.0  # show the command's effect at once
    finally:
        follow.terminate()


def main() -> int:
    if not sys.stdout.isatty():
        print("fleetwright_dash: needs a terminal", file=sys.stderr)
        return 2
    import curses

    try:
        return int(curses.wrapper(_loop))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
