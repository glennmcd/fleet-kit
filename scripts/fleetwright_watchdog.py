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

Each line starts with ``caught`` (a problem) or ``missed`` (a check could not be made, so a problem may
have gone unseen). The last line is the run's result: ``watchdog: clean``, or the counts. Read them with
``journalctl -u fleetwright-watchdog -o cat``. Exit 0 when every check was made, 1 when one was missed, 2 for a
bad config.

Apart from the stop flag it changes nothing: it starts and stops no unit and writes nothing to GitHub.
It reads the allocator's config, ``/etc/fleetwright/fleetwright.json``, with one optional object::

    "watchdog": {"stall_minutes": 15, "stuck_pr_minutes": 120}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
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

Journal = Callable[[str], float | None]
Clock = Callable[[], float]
Finding = tuple[str, str, str]  # "caught" or "missed", the check's name, what was found


@dataclass(frozen=True)
class Settings:
    stall_minutes: int = 15
    stuck_pr_minutes: int = 120


def parse_settings(raw: Any) -> Settings:
    block = raw.get("watchdog", {}) if isinstance(raw, dict) else {}
    if not isinstance(block, dict):
        raise ValueError("watchdog must be a JSON object")
    values = {}
    for key, default in (("stall_minutes", 15), ("stuck_pr_minutes", 120)):
        value = block.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"watchdog.{key} must be a positive whole number of minutes")
        values[key] = value
    return Settings(**values)


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
    red = sorted({str(r.get("name") or "?") for r in runs if r.get("conclusion") in RED})
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


def run_pass(
    cfg: Config,
    settings: Settings,
    fleetwright_dir: Path,
    gh: Gh,
    systemctl: Systemctl,
    journal: Journal,
    now: float,
    stop_file: Path,
) -> int:
    found: list[Finding] = []
    running: dict[str, Assignment | None] = {}
    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("stalled", lambda: check_stalls(running, journal, now, settings.stall_minutes)),
        ("red-main", lambda: check_main(cfg.repo, gh, stop_file)),
        ("stuck-pr", lambda: check_pulls(cfg, gh, running, now, settings.stuck_pr_minutes)),
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
) -> int:
    parser = argparse.ArgumentParser(prog="fleetwright_watchdog")
    parser.add_argument(
        "--config",
        default=os.environ.get("FLEETWRIGHT_CONFIG") or fleetwright_allocate.DEFAULT_CONFIG,
    )
    parser.add_argument("--fleetwright-dir", default=fleetwright_allocate.FLEETWRIGHT_DIR)
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
        cfg, settings, Path(args.fleetwright_dir), gh, systemctl, journal, now(), stop_file
    )


if __name__ == "__main__":
    sys.exit(main())
