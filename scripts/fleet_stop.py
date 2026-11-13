#!/usr/bin/env python3
"""Stop the line: halt every agent on this machine with one command.

    fleet_stop.py stop "why"     create the flag file; every agent's next tool call is refused
    fleet_stop.py clear          remove it; agents can work again
    fleet_stop.py status         say whether the line is stopped, and why

The flag file is ``FLEET_STOP_FILE`` (default ``/var/lib/fleet/STOP``). The hook that reads it is
``hooks/stop_line.py``. It stops new tool calls; it does not kill a command already running, so pair
it with stopping the agents' service units (docs/control-plane.md). The directory must be owned by the
operator and not writable by the agents' users, or an agent could clear its own stop.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

DEFAULT_STOP_FILE = "/var/lib/fleet/STOP"


def flag_path() -> Path:
    return Path(os.environ.get("FLEET_STOP_FILE") or DEFAULT_STOP_FILE)


def stop(reason: str, path: Path | None = None) -> Path:
    path = path or flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    path.write_text(f"{reason.strip() or 'stopped by the operator'} ({stamp})\n", encoding="utf-8")
    return path


def clear(path: Path | None = None) -> bool:
    """True if the line was stopped."""
    path = path or flag_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def status(path: Path | None = None) -> str | None:
    """The stop note, or None when the line is running."""
    path = path or flag_path()
    if not os.path.lexists(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or "stopped"
    except OSError:
        return "stopped (flag file unreadable)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet_stop")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("stop")
    p.add_argument("reason", nargs="*")
    sub.add_parser("clear")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "stop":
        print(f"stopped: {stop(' '.join(args.reason))}")
        return 0
    if args.command == "clear":
        print("cleared" if clear() else "was not stopped")
        return 0
    note = status()
    print(f"STOPPED: {note}" if note is not None else "running")
    return 1 if note is not None else 0


if __name__ == "__main__":
    sys.exit(main())
