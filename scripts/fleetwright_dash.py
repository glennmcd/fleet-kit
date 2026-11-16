#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""A terminal dashboard of the fleet: one panel per unit, with its state and its latest 5 journal lines.

    uv run --script /opt/fleetwright/scripts/fleetwright_dash.py      q quits

Panels: each author (``fleetwright-agent@agentNN``), each reviewer (``fleetwright-review@reviewNN``), the
allocator and the watchdog. An agent's or reviewer's panel also shows its assignment from
``/var/lib/fleetwright/slots/<slot>``. A failed unit is marked FAILED.

It only reads: the journal (as a member of ``systemd-journal``), ``systemctl is-active`` and the slots
directory. It never reads ``/etc/fleetwright``, so it finds the slots from the journal rather than the
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
    assignment: str = ""
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

    def _panel(self, unit: str) -> Panel:
        if unit not in self.panels:
            self.panels[unit] = Panel(unit)
            self.panels = {u: self.panels[u] for u in sorted(self.panels, key=_order)}
        return self.panels[unit]

    def add(self, unit: str, text: str) -> None:
        self._panel(unit).lines.append(text)

    def feed(self, raw: str) -> None:
        """One line of ``journalctl --output=json``; anything unreadable is skipped."""
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        found = entry_line(entry) if isinstance(entry, dict) else None
        if found:
            self.add(*found)

    def refresh(self, systemctl: Systemctl, slots_dir: Path) -> None:
        for panel in self.panels.values():
            panel.state = systemctl(["is-active", panel.unit])[1].strip() or "unknown"
            panel.assignment = ""
            if panel.slot:
                with contextlib.suppress(OSError):
                    panel.assignment = (slots_dir / panel.slot).read_text(encoding="utf-8").strip()


def _panel_rows(panel: Panel, width: int) -> list[str]:
    state = "FAILED" if panel.state == "failed" else panel.state
    head = f"{panel.title}  {state}" + (f"  {panel.assignment}" if panel.assignment else "")
    rows = [f"── {head} ".ljust(width, "─")[:width]]
    body = list(panel.lines)
    rows += [f"  {line}"[:width] for line in body] + [""] * (LINES - len(body))
    return rows


def render(dash: Dashboard, width: int, height: int) -> list[str]:
    """The screen as text rows, no wider than ``width`` and no taller than ``height``."""
    columns = max(1, min(len(dash.panels), width // PANEL_WIDTH)) if dash.panels else 1
    col_width = max(1, (width - (columns - 1)) // columns)
    blocks = [_panel_rows(p, col_width) for p in dash.panels.values()]
    screen: list[str] = []
    for start in range(0, len(blocks), columns):
        group = blocks[start : start + columns]
        for i in range(LINES + 1):
            screen.append(" ".join(b[i].ljust(col_width) for b in group).rstrip()[:width])
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
