#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Check a slot as its agent would run: same user, secrets and sandbox. Read-only; prints no secret.

Run by ``fleet-check-agent@<slot>`` or ``fleet-check-review@<slot>``, never by hand, so a member of
fleet-admin can check a slot without reading its tokens:

    systemctl start fleet-check-review@review01
    journalctl -u fleet-check-review@review01 -o cat -n 30

It reports, one line each: the slot's name and role agree; the unattended switch is on; the tools are on
the PATH; a model key is set (its name, never its value); ``gh`` is signed in, as ``FLEET_REVIEWER_LOGIN``
on a review slot; the clone is there and clean; both plugins are installed; the home is closed to other
users; the sandbox keeps the slot out of ``/var/lib/fleet`` and ``/etc/fleet``; the line is not stopped.
Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

FLEET_DIR = "/var/lib/fleet"
ENV_DIR = "/etc/fleet"
STOP_FILE = f"{FLEET_DIR}/STOP"
TOOLS = ("uv", "claude", "gh", "git", "rtk")
PLUGINS = ("fleet-kit", "cc-practices")
MODEL_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
ROLES = {"author": re.compile(r"agent[0-9]{2}"), "review": re.compile(r"review[0-9]{2}")}

Result = tuple[str, bool, str]


@dataclass
class Machine:
    env: Mapping[str, str]
    run: Callable[[Sequence[str]], tuple[int, str]]
    which: Callable[[str], str | None]
    exists: Callable[[str], bool]
    mode: Callable[[str], int | None]
    writable: Callable[[str], bool]
    listable: Callable[[str], bool]


def _slot(m: Machine) -> Result:
    slot, role = m.env.get("FLEET_SLOT", ""), m.env.get("FLEET_ROLE", "")
    pattern = ROLES.get(role)
    if pattern is None or not pattern.fullmatch(slot):
        return "slot", False, f"FLEET_SLOT {slot!r} and FLEET_ROLE {role!r} do not agree"
    return "slot", True, f"{slot}, {role}"


def _unattended(m: Machine) -> Result:
    if m.env.get("CC_PRACTICES_UNATTENDED") == "1":
        return "unattended", True, "CC_PRACTICES_UNATTENDED=1"
    return (
        "unattended",
        False,
        "CC_PRACTICES_UNATTENDED is not 1: cc-practices will wait for a person",
    )


def _tools(m: Machine) -> Result:
    missing = [t for t in TOOLS if m.which(t) is None]
    if missing:
        return "tools", False, f"not on PATH: {', '.join(missing)}"
    return "tools", True, ", ".join(TOOLS)


def _model_key(m: Machine) -> Result:
    found = [k for k in MODEL_KEYS if m.env.get(k)]
    if found:
        return "model key", True, f"{found[0]} is set"
    return "model key", False, f"none of {', '.join(MODEL_KEYS)} is set (agent.env)"


def _github(m: Machine) -> Result:
    review = m.env.get("FLEET_ROLE") == "review"
    expected = m.env.get("FLEET_REVIEWER_LOGIN", "")
    if not m.env.get("GH_TOKEN"):
        source = "reviewer.env" if review else "agent.env"
        return "github", False, f"GH_TOKEN is not set ({source})"
    if review and not expected:
        return "github", False, "FLEET_REVIEWER_LOGIN is not set (reviewer.env)"
    code, out = m.run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip()
    if code != 0 or not login:
        return "github", False, "GitHub refused the token, or could not be reached"
    if review and login != expected:
        return "github", False, f"signed in as {login}, not {expected}: the runner exits 64"
    return "github", True, f"signed in as {login}"


def _clone(m: Machine) -> Result:
    clone = m.env.get("FLEET_CLONE", "")
    if not clone or not m.exists(f"{clone}/.git"):
        return "clone", False, f"no clone at {clone or '(FLEET_CLONE unset)'}"
    code, url = m.run(["git", "-C", clone, "remote", "get-url", "origin"])
    if code != 0:
        return "clone", False, f"{clone} has no origin"
    code, porcelain = m.run(["git", "-C", clone, "status", "--porcelain"])
    if code != 0:
        return "clone", False, f"git status failed in {clone}"
    if porcelain.strip():
        return "clone", False, f"{clone} has uncommitted changes: the runner exits 70"
    return "clone", True, f"{clone}, clean, origin {url.strip()}"


def _plugins(m: Machine) -> Result:
    code, out = m.run(["claude", "plugin", "list"])
    if code != 0:
        return "plugins", False, "`claude plugin list` failed"
    missing = [p for p in PLUGINS if p not in out]
    if missing:
        return "plugins", False, f"not installed: {', '.join(missing)}"
    return "plugins", True, ", ".join(PLUGINS)


def _home(m: Machine) -> Result:
    home = os.path.dirname(m.env.get("FLEET_CLONE", "").rstrip("/"))
    mode = m.mode(home) if home else None
    if mode is None:
        return "home", False, f"cannot read the mode of {home or '(no home)'}"
    # Group bits show the fleet-admin ACL's mask; only "other" must be closed.
    if mode & 0o007:
        return "home", False, f"{home} is mode {mode:o}: other users can enter it"
    return "home", True, f"{home} is closed to other users"


def _sandbox(m: Machine) -> Result:
    if m.writable(FLEET_DIR):
        return "sandbox", False, f"the slot can write {FLEET_DIR}: it could clear the stop flag"
    if m.listable(ENV_DIR):
        return "sandbox", False, f"the slot can read {ENV_DIR}: it could read every token"
    return "sandbox", True, f"cannot write {FLEET_DIR} or read {ENV_DIR}"


def _stop(m: Machine) -> Result:
    if m.exists(STOP_FILE):
        return "stop flag", False, "the line is stopped: no agent starts (fleet_stop.py clear)"
    return "stop flag", True, "the line is running"


CHECKS = (_slot, _unattended, _tools, _model_key, _github, _clone, _plugins, _home, _sandbox, _stop)


def run_checks(m: Machine) -> list[Result]:
    return [check(m) for check in CHECKS]


def report(m: Machine) -> int:
    results = run_checks(m)
    for name, ok, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    slot = m.env.get("FLEET_SLOT", "slot")
    print(f"{slot}: every check passed" if not failed else f"{slot}: {failed} check(s) failed")
    return 0 if not failed else 1


# --- the real machine -----------------------------------------------------------------------------


def _run(command: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(command), capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    # stderr is dropped: a tool's error text could quote what it was given.
    return done.returncode, done.stdout


def _mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _writable(path: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".fleet-check."):
            return True
    except OSError:
        return False


def _listable(path: str) -> bool:
    try:
        os.listdir(path)
    except OSError:
        return False
    return True


def main() -> int:
    machine = Machine(
        env=os.environ,
        run=_run,
        which=shutil.which,
        exists=os.path.lexists,
        mode=_mode,
        writable=_writable,
        listable=_listable,
    )
    return report(machine)


if __name__ == "__main__":
    sys.exit(main())
