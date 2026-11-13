#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""PreToolUse hook: stop the line. One flag file halts every agent on the machine.

When the flag file exists, every tool call of every agent is refused with the reason written in the
file. The agent can still answer in text, so it can say why it stopped, but it can do nothing. Delete
the file (``scripts/fleet_stop.py clear``) and the next tool call runs again.

The hook only acts on a fleet agent: ``FLEET_CLONE`` or ``FLEET_STOP_FILE`` must be set, which the
agent's service unit does. On a desktop where neither is set it does nothing, so installing the plugin
there never blocks anything.

The flag file is ``FLEET_STOP_FILE``, default ``/var/lib/fleet/STOP``. A safety hook fails closed: if
the file cannot be checked or the hook crashes, the call is refused.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

DEFAULT_STOP_FILE = "/var/lib/fleet/STOP"
REASON_BYTES = 500


def stop_file() -> str | None:
    """The flag file's path, or None when this is not a fleet agent."""
    explicit = os.environ.get("FLEET_STOP_FILE")
    if explicit:
        return explicit
    return DEFAULT_STOP_FILE if os.environ.get("FLEET_CLONE") else None


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide() -> dict[str, Any] | None:
    """The hook's output, or None to allow the call."""
    path = stop_file()
    if path is None or not os.path.lexists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            note = f.read(REASON_BYTES).strip()
    except OSError:
        note = ""  # unreadable still means stopped
    return deny(
        "The fleet is stopped. Do not work around this: make no further tool calls, and reply with "
        "a one-line status of where you stopped. " + (f"Reason: {note}" if note else "")
    )


def main() -> int:
    try:
        sys.stdin.read()  # the payload is not needed; drain it
        out = decide()
    except Exception as exc:  # fail closed
        out = deny(f"stop_line could not check the stop file ({exc}); refusing to run")
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
