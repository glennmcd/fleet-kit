#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""The watchdog: one pass, with no AI, that looks for what the fleet cannot see about itself.

    fleetwright_watchdog.py                    one pass (the fleetwright-watchdog timer runs it every 5 minutes)

It runs four checks and prints one line for each thing it finds:

- failed-unit: a slot's unit or the allocator's service has failed, or the allocator's timer is not running.
  A failed slot holds its work until a person looks (``systemctl reset-failed``).
- stalled: a running slot whose unit has written no journal line for ``stall_minutes``. An agent writes a
  line per step (``fleetwright_stream.py``), so silence means a hung call, not a long think.
- red-main: a check on the default branch's head commit failed. This one acts: it sets the stop flag
  (``fleetwright_stop.py``), since every agent would branch from a broken main. A person clears it. A line that
  is already stopped keeps its note.
- stuck-pr: an open pull request from an ``issue/<n>`` branch that has not changed for
  ``stuck_pr_minutes`` and that no running slot holds. The line says what it waits for: a fix round, a
  review, its checks, or a person to merge it.
- wrong-branch: a push that is not the run's own. Every author slot pushes as the same GitHub App, so GitHub
  cannot tell them apart; two sources can. The slots' pre-push hook (``git-hooks/pre-push``) logs each ref a
  run pushes, and the root-written token lines (``fleetwright_token.py``) say which assignment that run held:
  a push to any branch but ``issue/<n>`` of its own issue is caught. GitHub's activity log lists every ref the
  App changed, which no agent can hide: a change to a non-issue branch, or to an issue no run held at the time,
  is caught. It detects; it does not prevent, and an agent that skips the hook on purpose is caught only when
  no run held the branch it pushed. Its read position is kept in
  ``/var/lib/fleetwright-allocator/watchdog.json``, so each push is reported once.

Each line starts with ``caught`` (a problem) or ``missed`` (a check could not be made, so a problem may
have gone unseen). The last line is the run's result: ``watchdog: clean``, or the counts. Read them with
``journalctl -u fleetwright-watchdog -o cat``. Exit 0 when every check was made, 1 when one was missed, 2 for a
bad config.

Apart from the stop flag and its read position it changes nothing: it starts and stops no unit and writes
nothing to GitHub. It reads the allocator's config, ``/etc/fleetwright/fleetwright.json``, with one optional
object::

    "watchdog": {"stall_minutes": 15, "stuck_pr_minutes": 120, "app_bot": "my-app[bot]",
                 "wrong_branch_slack_seconds": 120, "wrong_branch_first_lookback_hours": 24}

Without ``app_bot`` the GitHub side of wrong-branch does not run; the pre-push side still does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import fleetwright_allocate
import fleetwright_stop
from fleetwright_allocate import Assignment, Config, Gh, PassError, Systemctl

RED = {"failure", "timed_out", "startup_failure"}
ALLOCATOR_SERVICE = "fleetwright-allocate.service"
ALLOCATOR_TIMER = "fleetwright-allocate.timer"

DEFAULT_STATE = "/var/lib/fleetwright-allocator/watchdog.json"
TOKEN_ID = "fleetwright-token"  # the minter's journal lines (fleetwright_token.py), written as root
PUSH_ID = (
    "fleetwright-push"  # the author clones' pre-push hook (git-hooks/pre-push), written as the slot
)
BOT_RE = re.compile(r"[A-Za-z0-9-]{1,40}\[bot\]")
AGENT_UNIT_RE = re.compile(r"fleetwright-agent@(agent[0-9]{2})\.service")
TOKEN_LINE_RE = re.compile(
    r'token (minted|renewed|revoked) slot=(agent[0-9]{2}) assignment=(?:"((?:issue|fix|review) [1-9][0-9]*)"|none)'
)
INVOCATION_RE = re.compile(r"\binvocation=([0-9A-Za-z-]+)")
PUSH_RE = re.compile(r"\bref=(\S+)")
ISSUE_REF_RE = re.compile(r"refs/heads/issue/([1-9][0-9]*)")
VERBS = {
    "push": "pushed",
    "force_push": "force-pushed",
    "branch_creation": "created",
    "branch_deletion": "deleted",
}

Journal = Callable[[str], float | None]
JournalEntries = Callable[[Sequence[str]], list[dict[str, Any]]]
Clock = Callable[[], float]
Finding = tuple[str, str, str]  # "caught" or "missed", the check's name, what was found


@dataclass(frozen=True)
class Settings:
    stall_minutes: int = 15
    stuck_pr_minutes: int = 120
    # The authors' GitHub App's bot login: the GitHub side of wrong-branch looks only at its pushes.
    app_bot: str | None = None
    wrong_branch_slack_seconds: int = 120
    wrong_branch_first_lookback_hours: int = 24


def parse_settings(raw: Any) -> Settings:
    block = raw.get("watchdog", {}) if isinstance(raw, dict) else {}
    if not isinstance(block, dict):
        raise ValueError("watchdog must be a JSON object")
    values: dict[str, Any] = {}
    for key, default in (
        ("stall_minutes", 15),
        ("stuck_pr_minutes", 120),
        ("wrong_branch_slack_seconds", 120),
        ("wrong_branch_first_lookback_hours", 24),
    ):
        value = block.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"watchdog.{key} must be a positive whole number")
        values[key] = value
    bot = block.get("app_bot")
    if bot is not None and (not isinstance(bot, str) or not BOT_RE.fullmatch(bot)):
        raise ValueError("watchdog.app_bot must be the App's bot login, such as my-app[bot]")
    return Settings(app_bot=bot, **values)


def _journal(unit: str) -> float | None:
    """When the unit last wrote to the journal, in seconds since the epoch; None if that cannot be read."""
    try:
        done = subprocess.run(
            ["journalctl", "-u", unit, "-n", "1", "-o", "json", "--no-pager"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        last = done.stdout.strip().splitlines()[-1]
        return int(json.loads(last)["__REALTIME_TIMESTAMP"]) / 1_000_000
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError, KeyError, TypeError):
        return None


def _journal_entries(args: Sequence[str]) -> list[dict[str, Any]]:
    """journalctl's JSON entries for the given matches and options, oldest first."""
    done = subprocess.run(
        ["journalctl", "-o", "json", "--no-pager", "-q", *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if done.returncode != 0 and not done.stdout.strip():
        raise OSError(f"journalctl failed ({done.returncode})")
    entries = []
    for line in done.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _why(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        # gh's first stderr line is the error; the rest is often its usage text.
        err = next((ln.strip() for ln in str(exc.stderr or "").splitlines() if ln.strip()), "")
        return f"{' '.join(map(str, list(exc.cmd)[:3]))} failed: {err[:300]}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{' '.join(map(str, list(exc.cmd)[:3]))} timed out"
    return f"{type(exc).__name__}: {exc}"


def check_units(
    cfg: Config, slots_dir: Path, systemctl: Systemctl
) -> tuple[list[Finding], dict[str, Assignment | None]]:
    """Failed units, and the running slots' units with what each holds."""
    found: list[Finding] = []
    running: dict[str, Assignment | None] = {}
    for name in (*cfg.author_slots, *cfg.review_slots):
        unit = fleetwright_allocate.unit_for(name)
        answer = systemctl(["is-active", unit])[1].strip()
        if answer == "failed":
            found.append(
                (
                    "caught",
                    "failed-unit",
                    f"{unit} failed and holds its work: `journalctl -u {unit} -o cat -n 50`, "
                    f"then `systemctl reset-failed {unit}`",
                )
            )
        elif answer in fleetwright_allocate.BUSY:
            running[unit] = fleetwright_allocate.read_assignment(slots_dir / name)
        elif answer != "inactive":
            found.append(
                (
                    "missed",
                    "failed-unit",
                    f"cannot tell whether {unit} is running ({answer or 'no answer'})",
                )
            )
    if systemctl(["is-active", ALLOCATOR_SERVICE])[1].strip() == "failed":
        found.append(
            (
                "caught",
                "failed-unit",
                f"{ALLOCATOR_SERVICE} failed: `journalctl -u fleetwright-allocate -o cat -n 50`",
            )
        )
    timer = systemctl(["is-active", ALLOCATOR_TIMER])[1].strip()
    if timer != "active":
        found.append(
            (
                "caught",
                "failed-unit",
                f"{ALLOCATOR_TIMER} is {timer or 'unknown'}: no slot gets new work; "
                f"`systemctl start {ALLOCATOR_TIMER}`",
            )
        )
    return found, running


def check_stalls(
    running: dict[str, Assignment | None], journal: Journal, now: float, limit: int
) -> list[Finding]:
    found: list[Finding] = []
    for unit, work in running.items():
        last = journal(unit)
        if last is None:
            found.append(
                (
                    "missed",
                    "stalled",
                    f"no journal line for {unit} can be read (is the watchdog's user in systemd-journal?)",
                )
            )
            continue
        quiet = int((now - last) // 60)
        if quiet >= limit:
            holding = f" ({work.text()})" if work else ""
            found.append(
                (
                    "caught",
                    "stalled",
                    f"{unit}{holding} has logged nothing for {quiet} min (limit {limit}); "
                    f"`systemctl stop {unit}` frees the slot",
                )
            )
    return found


def check_main(repo: str, gh: Gh, stop_file: Path) -> list[Finding]:
    """A failed check on the default branch's head stops the line."""
    data = json.loads(gh(["api", f"repos/{repo}/commits/HEAD/check-runs?per_page=100"]))
    runs = data["check_runs"]
    # A check run again in a new check suite (a manual run, a workflow started again) is listed beside its
    # earlier runs; only the newest run of each check says whether it is red now.
    newest: dict[str, dict[str, Any]] = {}
    for r in runs:
        name = str(r.get("name") or "?")
        key = (str(r.get("started_at") or ""), int(r.get("id") or 0))
        held = newest.get(name)
        if held is None or key > (str(held.get("started_at") or ""), int(held.get("id") or 0)):
            newest[name] = r
    red = sorted(name for name, r in newest.items() if r.get("conclusion") in RED)
    if not red:
        return []
    sha = next((str(r.get("head_sha") or "") for r in runs), "")[:7]
    what = f"main is red at {sha} ({', '.join(red)})"
    if fleetwright_stop.status(stop_file) is not None:
        return [("caught", "red-main", f"{what}; the line was already stopped")]
    fleetwright_stop.stop(f"watchdog: {what}", stop_file)
    return [("caught", "red-main", f"{what}; the line is now stopped")]


def _waits_for(p: fleetwright_allocate.Pull) -> str:
    if p.conflicting or p.ci == "fail" or p.verdict == "request_changes":
        return "needs a fix round"
    if p.ci == "running":
        return "its checks have not finished"
    if p.verdict == "approve":
        return "approved, waiting for a person to merge"
    return "needs a review"


def check_pulls(
    cfg: Config, gh: Gh, running: dict[str, Assignment | None], now: float, limit: int
) -> list[Finding]:
    reviewers = fleetwright_allocate.fetch_reviewers(cfg.repo, gh)
    pulls = fleetwright_allocate.fetch_pulls(cfg, reviewers, gh)
    held = [a for a in running.values() if a]
    held_issues = {a.number for a in held if a.kind in ("issue", "fix")}
    held_pulls = {a.number for a in held if a.kind == "review"}
    found: list[Finding] = []
    for p in sorted(pulls, key=lambda p: p.number):
        if p.draft or p.number in held_pulls or p.issue in held_issues:
            continue
        moved = datetime.fromisoformat(p.updated_at).timestamp()
        quiet = int((now - moved) // 60)
        if quiet >= limit:
            found.append(
                (
                    "caught",
                    "stuck-pr",
                    f"PR #{p.number} (issue #{p.issue}) has not moved for {quiet} min "
                    f"(limit {limit}): {_waits_for(p)}",
                )
            )
    return found


# --- wrong-branch ---------------------------------------------------------------------------------


@dataclass
class Run:
    """One author run, as the root-written token lines tell it."""

    slot: str
    assignment: str | None  # "issue 12", "fix 14"
    start: float
    end: float | None = None  # when its token was revoked; None while it runs


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def _at(entry: dict[str, Any]) -> float:
    return int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000


def _text(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def token_runs(entries: Sequence[dict[str, Any]]) -> dict[str, Run]:
    """Author runs by systemd invocation id. Only lines journald stamped as root count: a slot can write a
    line that reads like the minter's, but not one with _UID=0."""
    runs: dict[str, Run] = {}
    for e in entries:
        if _text(e, "_UID") != "0" or _text(e, "SYSLOG_IDENTIFIER") != TOKEN_ID:
            continue
        m = TOKEN_LINE_RE.match(_text(e, "MESSAGE"))
        if m is None:
            continue
        kind, slot, assignment = m.groups()
        if kind == "renewed":
            # Written by the refresh unit, which names the run it renewed for.
            found = INVOCATION_RE.search(_text(e, "MESSAGE"))
            invocation = found.group(1) if found else ""
        else:
            unit = AGENT_UNIT_RE.fullmatch(_text(e, "_SYSTEMD_UNIT"))
            if unit is None or unit.group(1) != slot:
                continue  # a check unit's token, or a line that names another slot
            invocation = _text(e, "_SYSTEMD_INVOCATION_ID")
        if not invocation or invocation == "-":
            continue
        t = _at(e)
        run = runs.setdefault(invocation, Run(slot, assignment, t))
        run.start = min(run.start, t)
        run.assignment = run.assignment or assignment
        if kind == "revoked":
            run.end = t
    return runs


def _branch(assignment: str | None) -> str | None:
    if not assignment:
        return None
    kind, number = assignment.split()
    return f"refs/heads/issue/{number}" if kind in ("issue", "fix") else None


def check_pushes(pushes: Sequence[dict[str, Any]], runs: dict[str, Run]) -> list[Finding]:
    """Each ref an author run pushed, from its pre-push hook, against the run's own assignment."""
    found: list[Finding] = []
    for e in pushes:
        unit = AGENT_UNIT_RE.fullmatch(_text(e, "_SYSTEMD_UNIT"))
        ref = PUSH_RE.search(_text(e, "MESSAGE"))
        if unit is None or ref is None:
            continue
        slot, pushed, when = unit.group(1), ref.group(1), _when(_at(e))
        run = runs.get(_text(e, "_SYSTEMD_INVOCATION_ID"))
        if run is None:
            found.append(
                (
                    "missed",
                    "wrong-branch",
                    f"{slot} pushed {pushed} at {when} in a run with no token line",
                )
            )
        elif pushed != _branch(run.assignment):
            found.append(
                (
                    "caught",
                    "wrong-branch",
                    f"{slot} ({run.assignment or 'no assignment'}) pushed {pushed} at {when} "
                    f"(its pre-push log); `systemctl stop fleetwright-agent@{slot}` ends the run",
                )
            )
    return found


def check_activity(
    items: Sequence[dict[str, Any]], runs: dict[str, Run], slack: int, now: float
) -> list[Finding]:
    """Ref changes GitHub saw from the App, against the runs that held each issue at the time."""
    found: list[Finding] = []
    for item in items:
        ref = str(item.get("ref") or "")
        t = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")).timestamp()
        verb = VERBS.get(str(item.get("activity_type") or ""), "changed")
        what = f"the App {verb} {ref} at {_when(t)}"
        issue = ISSUE_REF_RE.fullmatch(ref)
        if issue is None:
            found.append(
                ("caught", "wrong-branch", f"{what}, not an issue/<n> branch (GitHub activity)")
            )
            continue
        n = issue.group(1)
        held = any(
            run.assignment in (f"issue {n}", f"fix {n}")
            and run.start - slack <= t <= (run.end if run.end is not None else now) + slack
            for run in runs.values()
        )
        if not held:
            found.append(
                ("caught", "wrong-branch", f"{what} while no slot held issue {n} (GitHub activity)")
            )
    return found


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _new_activity(
    pages: Any, bot: str, state: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The App's entries GitHub listed since the last pass, and the read position after them."""
    items = [i for page in pages for i in page if isinstance(i, dict)]
    mine = [i for i in items if (i.get("actor") or {}).get("login") == bot]
    last = state.get("activity", {}) if isinstance(state.get("activity"), dict) else {}
    last_t = str(last.get("timestamp", ""))
    last_ids = {x for x in last.get("ids", []) if isinstance(x, int)}

    def ident(i: dict[str, Any]) -> int:
        value = i.get("id")
        return value if isinstance(value, int) else 0

    fresh = [
        i
        for i in mine
        if str(i["timestamp"]) > last_t
        or (str(i["timestamp"]) == last_t and ident(i) not in last_ids)
    ]
    newest = max((str(i["timestamp"]) for i in mine), default=last_t)
    # Entries that share the newest second are told apart by id, so none is reported twice.
    at_newest = {ident(i) for i in mine if str(i["timestamp"]) == newest}
    position = {
        "timestamp": newest,
        "ids": sorted(at_newest | (last_ids if newest == last_t else set())),
    }
    return sorted(fresh, key=lambda i: str(i["timestamp"])), position


def check_wrong_branch(
    cfg: Config,
    settings: Settings,
    gh: Gh,
    entries: JournalEntries,
    state_path: Path,
    now: float,
) -> list[Finding]:
    """Pushes that are not the run's own: from the slots' pre-push hook, and from GitHub's activity log."""
    state = _load_state(state_path)
    first = f"@{int(now - settings.wrong_branch_first_lookback_hours * 3600)}"
    # A running slot gets a renewed line every 20 minutes, so an hour more than the window finds every run.
    since = f"@{int(now - settings.wrong_branch_first_lookback_hours * 3600 - 3600)}"
    found: list[Finding] = []
    try:
        runs = token_runs(entries([f"SYSLOG_IDENTIFIER={TOKEN_ID}", "_UID=0", "--since", since]))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        return [("missed", "wrong-branch", f"the token log cannot be read: {_why(exc)}")]
    cursor = state.get("push_cursor")
    window = ["--after-cursor", str(cursor)] if cursor else ["--since", first]
    try:
        pushes = entries([f"SYSLOG_IDENTIFIER={PUSH_ID}", *window])
        found += check_pushes(pushes, runs)
        if pushes:
            state["push_cursor"] = _text(pushes[-1], "__CURSOR") or cursor
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        found.append(("missed", "wrong-branch", f"the pre-push log cannot be read: {_why(exc)}"))
    if settings.app_bot:
        actor = urllib.parse.quote(settings.app_bot, safe="")
        path = f"repos/{cfg.repo}/activity?per_page=100&time_period=day&actor={actor}"
        try:
            pages = json.loads(gh(["api", "--paginate", "--slurp", path]))
            fresh, state["activity"] = _new_activity(pages, settings.app_bot, state)
            found += check_activity(fresh, runs, settings.wrong_branch_slack_seconds, now)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            found.append(("missed", "wrong-branch", f"GitHub activity cannot be read: {_why(exc)}"))
    try:
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        found.append(("missed", "wrong-branch", f"cannot save the read position: {_why(exc)}"))
    return found


def run_pass(
    cfg: Config,
    settings: Settings,
    fleetwright_dir: Path,
    gh: Gh,
    systemctl: Systemctl,
    journal: Journal,
    now: float,
    stop_file: Path,
    entries: JournalEntries = _journal_entries,
    state_path: Path = Path(DEFAULT_STATE),
) -> int:
    found: list[Finding] = []
    running: dict[str, Assignment | None] = {}
    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("stalled", lambda: check_stalls(running, journal, now, settings.stall_minutes)),
        ("red-main", lambda: check_main(cfg.repo, gh, stop_file)),
        ("stuck-pr", lambda: check_pulls(cfg, gh, running, now, settings.stuck_pr_minutes)),
        ("wrong-branch", lambda: check_wrong_branch(cfg, settings, gh, entries, state_path, now)),
    ]
    errors = (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        PassError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    )
    try:
        units, running_now = check_units(cfg, fleetwright_dir / "slots", systemctl)
        found += units
        running.update(running_now)
    except errors as exc:
        found.append(("missed", "failed-unit", _why(exc)))
    for name, check in checks:
        try:
            found += check()
        except errors as exc:
            found.append(("missed", name, _why(exc)))
    for outcome, name, detail in found:
        print(f"{outcome} {name}: {detail}")
    caught = sum(1 for f in found if f[0] == "caught")
    missed = len(found) - caught
    counts = [f"{label} {n}" for label, n in (("caught", caught), ("missed", missed)) if n]
    print(f"watchdog: {', '.join(counts) or 'clean'}")
    return 1 if missed else 0


def main(
    argv: list[str] | None = None,
    gh: Gh = fleetwright_allocate._gh,
    systemctl: Systemctl = fleetwright_allocate._systemctl,
    journal: Journal = _journal,
    now: Clock = time.time,
    journal_entries: JournalEntries = _journal_entries,
) -> int:
    parser = argparse.ArgumentParser(prog="fleetwright_watchdog")
    parser.add_argument(
        "--config",
        default=os.environ.get("FLEETWRIGHT_CONFIG") or fleetwright_allocate.DEFAULT_CONFIG,
    )
    parser.add_argument("--fleetwright-dir", default=fleetwright_allocate.FLEETWRIGHT_DIR)
    parser.add_argument(
        "--state", default=DEFAULT_STATE, help="where wrong-branch keeps its read position"
    )
    args = parser.parse_args(argv)
    try:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        settings = parse_settings(raw)
        cfg = fleetwright_allocate.parse_config(raw)
    except (OSError, ValueError) as exc:
        print(f"fleetwright_watchdog: {args.config}: {exc}", file=sys.stderr)
        return 2
    stop_file = Path(os.environ.get("FLEETWRIGHT_STOP_FILE") or fleetwright_stop.DEFAULT_STOP_FILE)
    return run_pass(
        cfg,
        settings,
        Path(args.fleetwright_dir),
        gh,
        systemctl,
        journal,
        now(),
        stop_file,
        journal_entries,
        Path(args.state),
    )


if __name__ == "__main__":
    sys.exit(main())
