#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Stop the line: halt every agent on this machine with one command.

    fleetwright_stop.py stop "why"     create the flag file; every agent's next tool call is refused
    fleetwright_stop.py clear          remove it; agents can work again
    fleetwright_stop.py status         say whether the line is stopped, and why

The flag file is ``FLEETWRIGHT_STOP_FILE`` (default ``/var/lib/fleetwright/STOP``). The hook that reads it is
``hooks/stop_line.py``. It stops new tool calls; it does not kill a command already running, so pair
it with stopping the agents' service units (docs/control-plane.md). The directory belongs to the
fleetwright-admin group (``fleetwright_slot.py init``) and is not writable by the agents' users, or an agent could
clear its own stop. Any member of fleetwright-admin can stop and clear the line, without root.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_STOP_FILE = "/var/lib/fleetwright/STOP"


def flag_path() -> Path:
    return Path(os.environ.get("FLEETWRIGHT_STOP_FILE") or DEFAULT_STOP_FILE)


def stop(reason: str, path: Path | None = None) -> Path:
    """Write the flag as a new file renamed into place.

    The directory belongs to fleetwright-admin, but a flag another admin wrote is that admin's file. Renaming
    needs only the directory, so any admin can replace it; mode 664 lets the group read and fix it.
    """
    path = path or flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    fd, tmp = tempfile.mkstemp(prefix=".STOP.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{reason.strip() or 'stopped by the operator'} ({stamp})\n")
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
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
    parser = argparse.ArgumentParser(prog="fleetwright_stop")
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
