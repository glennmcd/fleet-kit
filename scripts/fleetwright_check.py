#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Check a slot as its agent would run: same user, secrets and sandbox. Read-only; prints no secret.

Run by ``fleetwright-check-agent@<slot>`` or ``fleetwright-check-review@<slot>``, never by hand, so a member of
fleetwright-admin can check a slot without reading its tokens:

    systemctl start fleetwright-check-review@review01
    journalctl -u fleetwright-check-review@review01 -o cat -n 30

It reports, one line each: the slot's name and role agree; the unattended switch is on; the tools are on
the PATH, ``bwrap`` and ``socat`` included; bubblewrap can start a sandbox under the unit; ``gh`` has
``api --slurp`` (2.48 or later, which the allocator needs; Ubuntu's own package is older); a model key is set
(its name, never its value); ``gh`` is signed in, as ``FLEETWRIGHT_REVIEWER_LOGIN`` on a review slot; the
clone is there and clean; both plugins are installed; Claude Code's managed settings require the sandbox with
no way out and run the stop switch and write guard from files only root can change; the home is closed to
other users; the sandbox keeps the slot out of ``/var/lib/fleetwright`` and ``/etc/fleetwright``; the line is
not stopped.
Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FLEETWRIGHT_DIR = "/var/lib/fleetwright"
ENV_DIR = "/etc/fleetwright"
STOP_FILE = f"{FLEETWRIGHT_DIR}/STOP"
TOOLS = ("uv", "claude", "gh", "git", "rtk", "bwrap", "socat")
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
SAFETY_HOOKS = ("stop_line.py", "write_guard.py")
# A sandbox as Claude Code starts one: a user namespace over a read-only root.
BWRAP_PROBE = ("bwrap", "--unshare-user", "--ro-bind", "/", "/", "--dev", "/dev", "true")
PLUGINS = ("fleetwright", "cc-practices")
MODEL_KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
ROLES = {"author": re.compile(r"agent[0-9]{2}"), "review": re.compile(r"review[0-9]{2}")}
GH_VERSION_RE = re.compile(r"gh version ([0-9]+\.[0-9]+\.[0-9]+)")
GH_SLURP_RE = re.compile(r"^\s+--slurp\b", re.MULTILINE)

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
    read: Callable[[str], str | None]
    owner: Callable[[str], int | None]


def _slot(m: Machine) -> Result:
    slot, role = m.env.get("FLEETWRIGHT_SLOT", ""), m.env.get("FLEETWRIGHT_ROLE", "")
    pattern = ROLES.get(role)
    if pattern is None or not pattern.fullmatch(slot):
        return (
            "slot",
            False,
            f"FLEETWRIGHT_SLOT {slot!r} and FLEETWRIGHT_ROLE {role!r} do not agree",
        )
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


def _bubblewrap(m: Machine) -> Result:
    """Claude Code's sandbox needs bubblewrap to start under this unit's restrictions, as the slot."""
    code, _ = m.run(list(BWRAP_PROBE))
    if code != 0:
        return (
            "bubblewrap",
            False,
            "bwrap cannot start a sandbox here: on Ubuntu, AppArmor blocks unprivileged user namespaces "
            "until /etc/apparmor.d/bwrap allows them (docs/vm-setup.md)",
        )
    return "bubblewrap", True, "starts a sandbox under this unit"


def _root_only(m: Machine, path: str) -> bool:
    """Owned by root and writable by nobody else, so no slot can change it."""
    mode = m.mode(path)
    return m.owner(path) == 0 and mode is not None and not mode & 0o022


def _hook_scripts(settings: Mapping[str, Any]) -> dict[str, str]:
    """The script path each PreToolUse hook runs, by file name."""
    groups = settings.get("hooks", {}).get("PreToolUse", [])
    commands = [h.get("command", "") for g in groups for h in g.get("hooks", [])]
    return {c.split()[-1].rsplit("/", 1)[-1]: c.split()[-1] for c in commands if c.split()}


def _managed_settings(m: Machine) -> Result:
    """The settings no slot can override: the sandbox with no way out, and the safety hooks from root's kit."""
    name = "managed settings"
    text = m.read(MANAGED_SETTINGS)
    if text is None:
        return name, False, f"{MANAGED_SETTINGS} is missing: run `fleetwright_slot.py init --apply`"
    if not _root_only(m, MANAGED_SETTINGS):
        return name, False, f"{MANAGED_SETTINGS} is not root's alone: a slot could loosen it"
    try:
        settings = json.loads(text)
        sandbox = settings["sandbox"]
    except (ValueError, KeyError, TypeError):
        return name, False, f"{MANAGED_SETTINGS} is not valid JSON with a sandbox section"
    if not (
        sandbox.get("enabled") is True
        and sandbox.get("failIfUnavailable") is True
        and sandbox.get("allowUnsandboxedCommands") is False
    ):
        return (
            name,
            False,
            "the sandbox must be enabled, required (failIfUnavailable) and with no way out "
            "(allowUnsandboxedCommands false)",
        )
    scripts = _hook_scripts(settings)
    for hook in SAFETY_HOOKS:
        path = scripts.get(hook)
        if path is None:
            return name, False, f"{hook} is not a hook in {MANAGED_SETTINGS}"
        # The directory too: whoever can write it can replace the file.
        for p in (path, os.path.dirname(path)):
            if not _root_only(m, p):
                return name, False, f"{p} is not root's alone: a slot could switch {hook} off"
    return (
        name,
        True,
        f"{MANAGED_SETTINGS}: sandbox required, no way out, stop switch and write guard from root's kit",
    )


def _gh_version(m: Machine) -> Result:
    """The allocator reads paged comments with ``gh api --paginate --slurp``, which gh gained in 2.48."""
    code, out = m.run(["gh", "--version"])
    match = GH_VERSION_RE.search(out) if code == 0 else None
    if match is None:
        return "gh version", False, "`gh --version` failed"
    version = match.group(1)
    code, help_text = m.run(["gh", "api", "--help"])
    if code != 0 or not GH_SLURP_RE.search(help_text):
        return (
            "gh version",
            False,
            f"gh {version} has no `api --slurp` (needs 2.48 or later); the allocator fails once a PR is "
            "open. Install gh from GitHub's apt repository: https://cli.github.com/packages",
        )
    return "gh version", True, f"gh {version}, has api --slurp"


def _model_key(m: Machine) -> Result:
    found = [k for k in MODEL_KEYS if m.env.get(k)]
    if found:
        return "model key", True, f"{found[0]} is set"
    return "model key", False, f"none of {', '.join(MODEL_KEYS)} is set (agent.env)"


def _github(m: Machine) -> Result:
    review = m.env.get("FLEETWRIGHT_ROLE") == "review"
    expected = m.env.get("FLEETWRIGHT_REVIEWER_LOGIN", "")
    if not m.env.get("GH_TOKEN"):
        source = "reviewer.env" if review else "agent.env"
        return "github", False, f"GH_TOKEN is not set ({source})"
    if review and not expected:
        return "github", False, "FLEETWRIGHT_REVIEWER_LOGIN is not set (reviewer.env)"
    code, out = m.run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip()
    if code != 0 or not login:
        return "github", False, "GitHub refused the token, or could not be reached"
    if review and login != expected:
        return "github", False, f"signed in as {login}, not {expected}: the runner exits 64"
    return "github", True, f"signed in as {login}"


def _clone(m: Machine) -> Result:
    clone = m.env.get("FLEETWRIGHT_CLONE", "")
    if not clone or not m.exists(f"{clone}/.git"):
        return "clone", False, f"no clone at {clone or '(FLEETWRIGHT_CLONE unset)'}"
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
    home = os.path.dirname(m.env.get("FLEETWRIGHT_CLONE", "").rstrip("/"))
    mode = m.mode(home) if home else None
    if mode is None:
        return "home", False, f"cannot read the mode of {home or '(no home)'}"
    # Group bits show the fleetwright-admin ACL's mask; only "other" must be closed.
    if mode & 0o007:
        return "home", False, f"{home} is mode {mode:o}: other users can enter it"
    return "home", True, f"{home} is closed to other users"


def _sandbox(m: Machine) -> Result:
    if m.writable(FLEETWRIGHT_DIR):
        return (
            "sandbox",
            False,
            f"the slot can write {FLEETWRIGHT_DIR}: it could clear the stop flag",
        )
    if m.listable(ENV_DIR):
        return "sandbox", False, f"the slot can read {ENV_DIR}: it could read every token"
    return "sandbox", True, f"cannot write {FLEETWRIGHT_DIR} or read {ENV_DIR}"


def _stop(m: Machine) -> Result:
    if m.exists(STOP_FILE):
        return (
            "stop flag",
            False,
            "the line is stopped: no agent starts (fleetwright_stop.py clear)",
        )
    return "stop flag", True, "the line is running"


CHECKS = (
    _slot,
    _unattended,
    _tools,
    _bubblewrap,
    _gh_version,
    _model_key,
    _github,
    _clone,
    _plugins,
    _managed_settings,
    _home,
    _sandbox,
    _stop,
)


def run_checks(m: Machine) -> list[Result]:
    return [check(m) for check in CHECKS]


def report(m: Machine) -> int:
    results = run_checks(m)
    for name, ok, detail in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    slot = m.env.get("FLEETWRIGHT_SLOT", "slot")
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
        with tempfile.NamedTemporaryFile(dir=path, prefix=".fleetwright-check."):
            return True
    except OSError:
        return False


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _owner(path: str) -> int | None:
    try:
        return os.stat(path).st_uid
    except OSError:
        return None


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
        read=_read,
        owner=_owner,
    )
    return report(machine)


if __name__ == "__main__":
    sys.exit(main())
