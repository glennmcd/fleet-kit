#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""What a slot's service unit runs: check the slot is fit to start, then become a headless agent.

The units (``fleetwright-agent@.service`` and ``fleetwright-review@.service``, from ``scripts/fleetwright_slot.py``) set
``FLEETWRIGHT_SLOT``, ``FLEETWRIGHT_ROLE`` and ``FLEETWRIGHT_CLONE``. In order, this script:

1. checks the slot's name and role agree: ``agentNN`` is an author, ``reviewNN`` a reviewer;
2. refuses to start while the stop flag exists (``scripts/fleetwright_stop.py``);
3. for a reviewer, checks that ``gh`` is signed in as ``FLEETWRIGHT_REVIEWER_LOGIN``, so a verdict can never be
   posted as the author's account (for example when ``reviewer.env`` sets ``GITHUB_TOKEN``, which ``gh``
   ranks below the author's ``GH_TOKEN``);
4. reads the slot's assignment from ``/var/lib/fleetwright/slots/<slot>``, written by ``scripts/fleetwright_allocate.py``:
   ``issue <n>`` or ``fix <n>`` for an author, ``review <pr>`` for a reviewer. No file means the slot is idle,
   which is not a failure;
5. deals with uncommitted changes an earlier run left, which happens when the stop flag ends a run before
   it commits. On the branch of the issue this run works on, they are kept and the agent is told to carry on
   from them; anything else is stashed (``git stash list`` in the clone) and the run starts clean. Nothing
   is discarded, and if the stash fails the slot stays down until someone looks;
6. fetches, and puts the clone where the work is: an author on the branch ``issue/<n>`` (the local one,
   brought up to the pushed one; the pushed one if another slot started it; else a new one from
   ``origin/main``), a reviewer on the pull request's head commit, detached;
7. starts ``claude -p`` with its events streamed, through ``scripts/fleetwright_stream.py``, which prints one line
   per step to the journal and exits with the agent's exit code.

Each prompt is fixed apart from a number that is checked to be a number, so nothing from the assignment
file reaches the model or a command line as text.

Exit codes before the agent starts: 0 idle, 64 the unit's environment is wrong, 65 the assignment is not for
this slot, 70 the clone is not ready, 75 the fleet is stopped. After it starts: the agent's own exit code, or
128 plus the signal that ended it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import fleetwright_stop
import fleetwright_stream

BAD_CONFIG = 64
BAD_ASSIGNMENT = 65
NOT_READY = 70
STOPPED = 75

DEFAULT_SLOTS_DIR = "/var/lib/fleetwright/slots"
BASE = "origin/main"
ROLES = {"author": re.compile(r"agent[0-9]{2}"), "review": re.compile(r"review[0-9]{2}")}
ASSIGNMENT_RE = re.compile(r"(issue|fix|review) ([1-9][0-9]{0,8})")

Git = Callable[[Sequence[str]], tuple[int, str]]
Launch = Callable[[Sequence[str]], int]
WhoAmI = Callable[[], str | None]


# Fixed text, added to an author's prompt when the clone holds a stopped run's work on the same issue.
RESUME_NOTE = (
    " A run on this issue was stopped before it committed, and its uncommitted work is in the clone. "
    "Read `git status` and `git diff`, keep what is right, and carry on from there."
)


def issue_prompt(issue: int, resumed: bool = False) -> str:
    return (
        f"Work on GitHub issue #{issue} of this repository. Read it with `gh issue view {issue} --comments`; "
        "if an earlier run on it posted a handoff comment, carry on from the latest one. "
        "Its Done when list is your approval and your definition of finished. "
        f"You are on the branch issue/{issue}; commit there, push it, and open a pull request that "
        "closes the issue, or continue the one that is open. "
        "Stop when every check except `reviewer-verdict` passes, or write a handoff if you cannot "
        "finish. `reviewer-verdict` stays red until the fleet's reviewer posts its verdict: that is "
        "not yours to pass. Do not post review verdicts and do not run the review-pr skill."
    ) + (RESUME_NOTE if resumed else "")


def fix_prompt(issue: int, resumed: bool = False) -> str:
    return (
        f"The pull request from the branch issue/{issue}, for GitHub issue #{issue}, needs a fix round: "
        "its checks fail, it conflicts with main, or the reviewer requested changes. You are on that "
        f"branch. Read `gh pr view issue/{issue} --comments` and `gh pr checks issue/{issue}`, and the "
        "issue's Done when list. Fix what they report, merge main if it conflicts, and push to the same "
        "branch. Stop when every check except `reviewer-verdict` passes, or write a handoff if you "
        "cannot finish. `reviewer-verdict` stays red until the fleet's reviewer posts its verdict: that "
        "is not yours to pass. Do not post review verdicts and do not run the review-pr skill."
    ) + (RESUME_NOTE if resumed else "")


def review_prompt(pr: int) -> str:
    return (
        f"Run the cc-practices review-pr skill on pull request #{pr} of this repository. The clone is "
        "checked out at the pull request's head commit. You are the reviewer, not the author: do not "
        "change the pull request's code, push, or merge. Finish when the verdict is posted, or say why "
        "it could not be."
    )


def _say(message: str) -> None:
    print(f"fleetwright_agent: {message}", file=sys.stderr)


def read_assignment(path: Path) -> tuple[str, int] | None:
    """The assignment, or None when the slot is idle. Raises ValueError on anything else."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    m = ASSIGNMENT_RE.fullmatch(text)
    if not m:
        raise ValueError(f"{path} does not hold 'issue <n>', 'fix <n>' or 'review <n>'")
    return m.group(1), int(m.group(2))


def settle_leftovers(git: Git, branch: str | None) -> tuple[str | None, bool]:
    """Uncommitted changes an earlier run left: kept when they are on ``branch``, else stashed.

    Returns what went wrong, or None, and whether the changes were kept for this run.
    """
    code, changes = git(["status", "--porcelain"])
    if code != 0:
        return "git status failed in the clone", False
    if not changes.strip():
        return None, False
    code, out = git(["symbolic-ref", "--short", "-q", "HEAD"])
    head = out.strip() if code == 0 else ""
    if branch is not None and head == branch:
        print(f"fleetwright_agent: keeping uncommitted work left on {branch} by a stopped run")
        return None, True
    where = head or "a detached head"
    stash = [
        "stash",
        "push",
        "--include-untracked",
        "-m",
        f"fleetwright: left on {where} by a stopped run",
    ]
    if git(stash)[0] != 0:
        return (
            "the clone has uncommitted changes from an earlier run and git stash failed; not touching them",
            False,
        )
    print(
        f"fleetwright_agent: stashed uncommitted work left on {where} by an earlier run (git stash list)"
    )
    return None, False


def checkout_issue(issue: int, git: Git) -> tuple[str | None, bool]:
    """Put the clone on the issue's branch. Returns what went wrong, or None, and whether a stopped run's
    work on this issue was kept."""
    branch = f"issue/{issue}"
    problem, resumed = settle_leftovers(git, branch)
    if problem:
        return problem, False
    if git(["fetch", "origin"])[0] != 0:
        return "git fetch failed", resumed
    local = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])[0] == 0
    pushed = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])[0] == 0
    if local:
        switch = ["switch", branch]
    elif pushed:
        switch = ["switch", "-c", branch, "--track", f"origin/{branch}"]
    else:
        switch = ["switch", "-c", branch, BASE]
    if git(switch)[0] != 0:
        return f"could not switch to {branch}", resumed
    if local and pushed and git(["merge", "--ff-only", f"origin/{branch}"])[0] != 0:
        return (
            f"the local {branch} has diverged from the pushed one; not choosing between them",
            resumed,
        )
    return None, resumed


def checkout_review(pr: int, git: Git) -> str | None:
    """Put the clone on the pull request's head commit, detached. Returns what went wrong, or None."""
    problem, _ = settle_leftovers(git, None)
    if problem:
        return problem
    ref = f"refs/remotes/origin/pr/{pr}"
    if git(["fetch", "origin", f"+refs/pull/{pr}/head:{ref}"])[0] != 0:
        return f"could not fetch pull request #{pr}"
    if git(["switch", "--detach", f"origin/pr/{pr}"])[0] != 0:
        return f"could not check out pull request #{pr}"
    return None


def _gh_login() -> str | None:
    try:
        done = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def run(env: Mapping[str, str], git: Git, launch: Launch, whoami: WhoAmI = _gh_login) -> int:
    slot = env.get("FLEETWRIGHT_SLOT", "")
    role = env.get("FLEETWRIGHT_ROLE", "")
    clone = env.get("FLEETWRIGHT_CLONE", "")
    if role not in ROLES or not ROLES[role].fullmatch(slot) or not clone:
        _say(
            "FLEETWRIGHT_SLOT, FLEETWRIGHT_ROLE and FLEETWRIGHT_CLONE must be set by the service unit, and agree: "
            "an agentNN slot is an author, a reviewNN slot a reviewer"
        )
        return BAD_CONFIG

    stop_file = Path(env.get("FLEETWRIGHT_STOP_FILE") or fleetwright_stop.DEFAULT_STOP_FILE)
    note = fleetwright_stop.status(stop_file)
    if note is not None:
        _say(f"the fleet is stopped, not starting: {note}")
        return STOPPED

    if role == "review":
        expected = env.get("FLEETWRIGHT_REVIEWER_LOGIN", "")
        if not expected:
            _say("FLEETWRIGHT_REVIEWER_LOGIN must be set in /etc/fleetwright/reviewer.env")
            return BAD_CONFIG
        login = whoami()
        if (login or "").lower() != expected.lower():
            _say(
                f"gh is signed in as {login or 'nobody'}, not the reviewer {expected}; not reviewing"
            )
            return BAD_CONFIG

    try:
        assignment = read_assignment(
            Path(env.get("FLEETWRIGHT_SLOTS_DIR") or DEFAULT_SLOTS_DIR) / slot
        )
    except ValueError as exc:
        _say(str(exc))
        return BAD_ASSIGNMENT
    if assignment is None:
        print(f"fleetwright_agent: {slot} has no assignment; idle")
        return 0
    kind, number = assignment
    if (kind == "review") != (role == "review"):
        _say(
            f"{slot} is a{'' if role == 'author' else ' review'} slot and cannot take '{kind} {number}'"
        )
        return BAD_ASSIGNMENT

    if kind == "review":
        problem = checkout_review(number, git)
        text = review_prompt(number)
    else:
        problem, resumed = checkout_issue(number, git)
        text = issue_prompt(number, resumed) if kind == "issue" else fix_prompt(number, resumed)
    if problem:
        _say(f"{slot}: {problem}")
        return NOT_READY
    return launch(
        [
            "claude",
            "-p",
            text,
            "--dangerously-skip-permissions",
            # One JSON event per line, which fleetwright_stream turns into a line per step in the journal.
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    )


def launch_streaming(clone: str, env: Mapping[str, str]) -> Launch:
    """Run the agent in its clone, under fleetwright_stream, and return its exit code."""

    def launch(argv: Sequence[str]) -> int:
        os.chdir(clone)
        return fleetwright_stream.run(argv, env)

    return launch


def main() -> int:
    clone = os.environ.get("FLEETWRIGHT_CLONE", "")

    def git(args: Sequence[str]) -> tuple[int, str]:
        done = subprocess.run(
            ["git", "-C", clone, *args], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout

    return run(os.environ, git, launch_streaming(clone, os.environ))


if __name__ == "__main__":
    sys.exit(main())
