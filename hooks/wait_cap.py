#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
# Vendored from https://github.com/alexander-marquardt/agent-fleet-kit (MIT, (c) Alexander Marquardt).
# Changes: this header only. See NOTICE.md.
"""PreToolUse hook: a sub-agent never waits in the foreground past the prompt cache.

The prompt cache expires a few minutes after an agent's last model call (five
minutes by default). A Bash call that blocks longer than that, waiting for CI or
a slow test, lets it expire, and the next call re-writes the agent's whole
context at full price. On the fleet this kit comes from, that was about a third
of all agent usage.

For a sub-agent's Bash call (the hook input carries ``agent_id``) that is not
``run_in_background``, the hook refuses:

* a ``timeout`` above ``WAIT_CAP_SECS`` plus a margin for start-up;
* a ``sleep`` longer than ``WAIT_CAP_SECS``.

The refusal says what to do instead: start the long command in the background,
then poll it with foreground calls that each end within the cap.

``WAIT_CAP_SECS`` defaults to 270, under a five-minute cache. If your sessions
use a longer cache lifetime, raise it. ``WAIT_CAP_MAIN=1`` also judges the main
session.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

WAIT_CAP_SECS = int(os.environ.get("WAIT_CAP_SECS", "270"))
TIMEOUT_CAP_MS = (WAIT_CAP_SECS + 20) * 1000
JUDGE_MAIN = os.environ.get("WAIT_CAP_MAIN") == "1"
DURATION = re.compile(r"(\d+(?:\.\d+)?)([smh]?)")
UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}
# The words after which the next word is a command.
COMMAND_START = {"&&", "||", ";", "|", "&", "(", "{", "then", "do", "else", "time"}


def longest_sleep(command: str) -> float:
    """The longest ``sleep`` run as a command in *command*, in seconds (0 when none)."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unbalanced quote: nothing to read
        return 0
    longest = 0.0
    for i, word in enumerate(words[:-1]):
        before = words[i - 1] if i else ";"
        match = DURATION.fullmatch(words[i + 1])
        if word == "sleep" and before in COMMAND_START and match:
            longest = max(longest, float(match[1]) * UNIT[match[2]])
    return longest


def refusal(tool_input: dict) -> str | None:
    """Why this Bash call would idle past the cache, else None."""
    if tool_input.get("run_in_background") in (True, "true"):
        return None
    timeout = tool_input.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > TIMEOUT_CAP_MS:
        return f"its timeout is {int(timeout)} ms, over the {TIMEOUT_CAP_MS} ms cap"
    slept = longest_sleep(str(tool_input.get("command", "")))
    if slept > WAIT_CAP_SECS:
        return f"it sleeps {slept:g} s, over the {WAIT_CAP_SECS} s cap"
    return None


def decide(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash" or not (payload.get("agent_id") or JUDGE_MAIN):
        return None
    why = refusal(payload.get("tool_input") or {})
    if why is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Refused: {why}. The prompt cache expires a few minutes after your last "
                "call, and the call after re-writes your whole context. Start the long "
                "command with run_in_background, then check on it with foreground calls "
                f"that each end within {WAIT_CAP_SECS} s, for example "
                f"`for i in $(seq {WAIT_CAP_SECS // 10}); do grep -q DONE out.log && break; sleep 10; done`."
            ),
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        out = decide(payload) if isinstance(payload, dict) else None
    except Exception as exc:  # fail open
        print(f"wait_cap: {exc}", file=sys.stderr)
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
