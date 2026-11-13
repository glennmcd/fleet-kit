#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""What a slot's service unit runs: check the slot is fit to start, then become a headless agent.

The unit (``fleet-agent@.service``, from ``scripts/fleet_slot.py``) sets ``FLEET_SLOT`` and
``FLEET_CLONE``. In order, this script:

1. refuses to start while the stop flag exists (``scripts/fleet_stop.py``);
2. reads the slot's assignment, an issue number in ``/var/lib/fleet/slots/<slot>``. No file means the
   slot is idle, which is not a failure;
3. refuses a clone with uncommitted changes. A previous run left them; discarding them is a person's
   decision, so the slot stays down until someone looks;
4. fetches, and puts the clone on the branch ``issue/<n>``: the local one if it exists, the pushed one
   if another slot started it, else a new one from ``origin/main``;
5. replaces itself with ``claude -p``, so systemd watches and stops the agent itself.

The prompt is fixed apart from the issue number, which is checked to be a number, so nothing from the
assignment file reaches the model or a command line as text.

Exit codes: 0 started or idle, 64 the unit's environment is wrong, 65 the assignment is not an issue
number, 70 the clone is not ready, 75 the fleet is stopped.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import fleet_stop

BAD_CONFIG = 64
BAD_ASSIGNMENT = 65
NOT_READY = 70
STOPPED = 75

DEFAULT_SLOTS_DIR = "/var/lib/fleet/slots"
BASE = "origin/main"
SLOT_RE = re.compile(r"agent[0-9]{2}")
ISSUE_RE = re.compile(r"[1-9][0-9]{0,8}")

Git = Callable[[Sequence[str]], tuple[int, str]]
Launch = Callable[[Sequence[str]], int]


def prompt(issue: int) -> str:
    return (
        f"Work on GitHub issue #{issue} of this repository. Read it with `gh issue view {issue}`. "
        "Its Done when list is your approval and your definition of finished. "
        f"You are on the branch issue/{issue}; commit there, push it, and open a pull request that "
        "closes the issue, or continue the one that is open. "
        "Stop when the pull request's checks pass and it is ready for review, or write a handoff "
        "if you cannot finish."
    )


def _say(message: str) -> None:
    print(f"fleet_agent: {message}", file=sys.stderr)


def read_assignment(path: Path) -> int | None:
    """The assigned issue number, or None when the slot is idle. Raises ValueError on anything else."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    if not ISSUE_RE.fullmatch(text):
        raise ValueError(f"{path} does not hold an issue number")
    return int(text)


def checkout(issue: int, git: Git) -> str | None:
    """Put the clone on the issue's branch. Returns what went wrong, or None."""
    code, changes = git(["status", "--porcelain"])
    if code != 0:
        return "git status failed in the clone"
    if changes.strip():
        return "the clone has uncommitted changes from an earlier run; not touching them"
    if git(["fetch", "origin"])[0] != 0:
        return "git fetch failed"
    branch = f"issue/{issue}"
    if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])[0] == 0:
        switch = ["switch", branch]
    elif git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])[0] == 0:
        switch = ["switch", "-c", branch, "--track", f"origin/{branch}"]
    else:
        switch = ["switch", "-c", branch, BASE]
    if git(switch)[0] != 0:
        return f"could not switch to {branch}"
    return None


def run(env: Mapping[str, str], git: Git, launch: Launch) -> int:
    slot = env.get("FLEET_SLOT", "")
    clone = env.get("FLEET_CLONE", "")
    if not SLOT_RE.fullmatch(slot) or not clone:
        _say("FLEET_SLOT (agentNN) and FLEET_CLONE must be set by the service unit")
        return BAD_CONFIG

    stop_file = Path(env.get("FLEET_STOP_FILE") or fleet_stop.DEFAULT_STOP_FILE)
    note = fleet_stop.status(stop_file)
    if note is not None:
        _say(f"the fleet is stopped, not starting: {note}")
        return STOPPED

    try:
        issue = read_assignment(Path(env.get("FLEET_SLOTS_DIR") or DEFAULT_SLOTS_DIR) / slot)
    except ValueError as exc:
        _say(str(exc))
        return BAD_ASSIGNMENT
    if issue is None:
        print(f"fleet_agent: {slot} has no assignment; idle")
        return 0

    problem = checkout(issue, git)
    if problem:
        _say(f"{slot}: {problem}")
        return NOT_READY
    return launch(["claude", "-p", prompt(issue), "--dangerously-skip-permissions"])


def main() -> int:
    clone = os.environ.get("FLEET_CLONE", "")

    def git(args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(
            ["git", "-C", clone, *args], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout

    def launch(argv: Sequence[str]) -> int:
        os.chdir(clone)
        os.execvp(argv[0], list(argv))  # does not return

    return run(os.environ, git, launch)


if __name__ == "__main__":
    sys.exit(main())
