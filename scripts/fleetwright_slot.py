#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Set up the fleet machine once, then one slot at a time.

    fleetwright_slot.py init [--admin you]                                              print what it would do
    sudo fleetwright_slot.py init --admin you --apply                                   the machine, once
    sudo fleetwright_slot.py add agent01 --repo https://github.com/owner/name.git --git-email ADDRESS --apply
                                                                                  an author slot
    sudo fleetwright_slot.py add review01 --repo https://github.com/owner/name.git --apply  a review slot
    fleetwright_slot.py unit [--name fleetwright-review@.service]                             print the units

Root is needed for these two commands and nothing after them. ``init`` creates three groups:
``fleetwright-admin`` (the operators, a ``claude`` login for the operator's agent, and the allocator's own user
``fleetwright-allocator``), ``fleetwright-agent`` (the author slots) and ``fleetwright-review`` (the review slots). It gives
``/var/lib/fleetwright`` to fleetwright-admin and installs the units, a polkit rule that lets fleetwright-admin start, stop and
reset the fleet's units and no others, and an sshd file that refuses logins as a slot. fleetwright-admin cannot
read the secrets in ``/etc/fleetwright`` or change the code or units that run as the slots: the check units
(``fleetwright-check-agent@``, ``fleetwright-check-review@``) and ``fleetwright-allocate-dry-run`` run as the slot or the
allocator with its secrets, and the admin reads their result in the journal.

A slot is an OS user whose home holds one clone, ``/home/<slot>/repo``. The user is the boundary: it can
write its home and nothing else. ``agentNN`` slots are authors: they work on issues and fix rounds.
``reviewNN`` slots are reviewers: they review pull requests and post verdicts as the reviewer's own GitHub
account. An author slot commits in its clone as ``agentNN (fleet)`` at the address given with
``--git-email``: the noreply address of the account whose token pushes, so GitHub links the commits to it
and the name says which slot wrote them. ``systemctl start fleetwright-agent@agent01`` runs
``scripts/fleetwright_agent.py`` as that user with ``FLEETWRIGHT_CLONE`` set, which turns on the stop switch and the
write guard (docs/control-plane.md).

Without ``--apply`` nothing is run: the commands and files are printed for the operator to read.
``--apply`` needs root, runs each command without a shell, and stops at the first one that fails. Both
are safe to run again: an existing user or clone is kept.

Secrets are not handled here. The units read them from files the operator writes by hand, owned by root
with mode 600: ``/etc/fleetwright/agent.env`` (the model key and the authors' GitHub token) for every unit,
``/etc/fleetwright/reviewer.env`` (the reviewer's ``GH_TOKEN`` and ``FLEETWRIGHT_REVIEWER_LOGIN``) for review slots
only, so an author's run never has the reviewer's token, and ``/etc/fleetwright/allocator.env`` (a read-only
``GH_TOKEN``) for the allocator.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

UNIT_DIR = "/etc/systemd/system"
POLKIT_RULE = "/etc/polkit-1/rules.d/50-fleetwright.rules"
SSHD_CONF = "/etc/ssh/sshd_config.d/50-fleetwright.conf"
DEFAULT_KIT = "/opt/fleetwright"
FLEETWRIGHT_DIR = "/var/lib/fleetwright"
ENV_DIR = "/etc/fleetwright"
AGENT_ENV = f"{ENV_DIR}/agent.env"
REVIEWER_ENV = f"{ENV_DIR}/reviewer.env"
ALLOCATOR_ENV = f"{ENV_DIR}/allocator.env"

ADMIN_GROUP = "fleetwright-admin"
AUTHOR_GROUP = "fleetwright-agent"
REVIEW_GROUP = "fleetwright-review"
ALLOCATOR_USER = "fleetwright-allocator"
ALLOCATOR_HOME = "/var/lib/fleetwright-allocator"
CLAUDE_USER = "claude"
JOURNAL_GROUP = "systemd-journal"

AUTHOR_RE = re.compile(r"agent[0-9]{2}")
REVIEW_RE = re.compile(r"review[0-9]{2}")
REPO_RE = re.compile(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)+")
KIT_RE = re.compile(r"(/[A-Za-z0-9._-]+)+")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")

# The units fleetwright-admin may start, stop, restart and reset, written once for Python and polkit's
# JavaScript. Each template accepts only its own kind of slot, so an author unit cannot run as a reviewer.
ADMIN_UNITS_RE = (
    r"(fleetwright-agent@agent[0-9]{2}\.service"
    r"|fleetwright-review@review[0-9]{2}\.service"
    r"|fleetwright-check-agent@agent[0-9]{2}\.service"
    r"|fleetwright-check-review@review[0-9]{2}\.service"
    r"|fleetwright-allocate\.service|fleetwright-allocate\.timer|fleetwright-allocate-dry-run\.service"
    r"|fleetwright-watchdog\.service|fleetwright-watchdog\.timer"
    r"|fleetwright-report\.service)"
)
ADMIN_VERBS = ("start", "stop", "restart", "reset-failed")

Run = Callable[[Sequence[str]], int]

HEADER = "# Generated by scripts/fleetwright_slot.py (`fleetwright_slot.py unit`); a test keeps this file equal to it.\n"


def validate_slot(name: str) -> str:
    if not (AUTHOR_RE.fullmatch(name) or REVIEW_RE.fullmatch(name)):
        raise ValueError(
            f"slot {name!r}: a slot is named agentNN (an author) or reviewNN (a reviewer), "
            "for example agent01"
        )
    return name


def validate_repo(repo: str) -> str:
    """A plain https URL. Anything else could be read by git as an option or a local path."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError(
            f"repo {repo!r}: give an https URL such as https://github.com/owner/name.git"
        )
    return repo


def validate_email(email: str) -> str:
    """A plain address. Not an option, and nothing git would read as more than one value."""
    if email.startswith("-") or not EMAIL_RE.fullmatch(email):
        raise ValueError(
            f"git-email {email!r}: give an address such as 123+you@users.noreply.github.com"
        )
    return email


def validate_admin(name: str) -> str:
    """An existing login to add to fleetwright-admin. Not root, the allocator or a slot."""
    if (
        not USER_RE.fullmatch(name)
        or name in ("root", ALLOCATOR_USER)
        or AUTHOR_RE.fullmatch(name)
        or REVIEW_RE.fullmatch(name)
    ):
        raise ValueError(f"admin {name!r}: give a login name, not root, {ALLOCATOR_USER} or a slot")
    return name


def clone_path(slot: str) -> str:
    return f"/home/{validate_slot(slot)}/repo"


def _slot_unit(kit: str, role: str, check: bool = False) -> str:
    """A slot's template unit, or its check. ``%i`` is the slot, so user and clone name the same slot."""
    review = role == "review"
    who = "reviewer" if review else "agent"
    if check:
        name = f"fleetwright-check-{'review@review01' if review else 'agent@agent01'}"
    else:
        name = "fleetwright-review@review01" if review else "fleetwright-agent@agent01"
    secrets = (
        f"# The model key from agent.env; the reviewer's GitHub token and expected login from reviewer.env,\n"
        f"# read second so its GH_TOKEN replaces the authors'. Owned by root, mode 600; never in this file.\n"
        f"EnvironmentFile={AGENT_ENV}\nEnvironmentFile={REVIEWER_ENV}"
        if review
        else "# Secrets (the model key, the GitHub token). Owned by root, mode 600; never in this file.\n"
        f"EnvironmentFile={AGENT_ENV}"
    )
    if check:
        intro = (
            f"# Checks slot %i as its agent would run: same user, secrets and sandbox. Prints no secret.\n"
            f"# As a member of fleetwright-admin: systemctl start {name}; journalctl -u {name} -o cat\n"
        )
        description = f"Fleet check of {who} %i"
        kind = "Type=oneshot"
        script = "fleetwright_check.py"
        slice_ = "fleetwright.slice"
        tail = "TimeoutStartSec=2min\n"
        install = ""
    else:
        intro = f"# The allocator starts it; by hand, as a member of fleetwright-admin: systemctl start {name}\n"
        description = f"Fleet {who} %i"
        kind = "Type=exec"
        script = "fleetwright_agent.py"
        slice_ = f"fleetwright-{'review' if review else 'agent'}.slice"
        tail = (
            "# One run per assignment: the allocator starts the unit again, not systemd.\n"
            "Restart=no\n"
            "TimeoutStopSec=30\n"
        )
        install = "\n[Install]\nWantedBy=multi-user.target\n"
    return f"""\
{HEADER}{intro}[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
{kind}
User=%i
Slice={slice_}
WorkingDirectory=/home/%i/repo
Environment=FLEETWRIGHT_SLOT=%i
Environment=FLEETWRIGHT_ROLE={role}
Environment=FLEETWRIGHT_CLONE=/home/%i/repo
Environment=CC_PRACTICES_UNATTENDED=1
Environment=GH_NO_UPDATE_NOTIFIER=1
Environment=PATH=/home/%i/.local/bin:/usr/local/bin:/usr/bin:/bin
{secrets}
ExecStart=/usr/bin/env uv run --script {kit}/scripts/{script}
{tail}# The slot's user can write its home and a private /tmp, and nothing else. Other homes are closed to it
# by their mode (700), not by this unit.
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/home/%i
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
{install}"""


def _allocate_service(kit: str, dry_run: bool = False) -> str:
    if dry_run:
        intro = (
            "# One allocator pass that prints what it would assign and changes nothing. It runs as the\n"
            "# allocator, with its token, so the admin who starts it never holds the token:\n"
            "#   systemctl start fleetwright-allocate-dry-run; journalctl -u fleetwright-allocate-dry-run -o cat -n 50\n"
        )
        description = "Fleet allocator, dry run: print what a pass would assign"
    else:
        intro = (
            "# One allocator pass; fleetwright-allocate.timer runs it. To see what it would do, without changing\n"
            "# anything: systemctl start fleetwright-allocate-dry-run\n"
        )
        description = "Fleet allocator: give free slots their next work"
    return f"""\
{HEADER}{intro}[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# Its own user in fleetwright-admin, not root: it writes {FLEETWRIGHT_DIR} and starts the slots' units through
# the polkit rule, and can do nothing a member of fleetwright-admin cannot.
User={ALLOCATOR_USER}
Slice=fleetwright.slice
# Files it writes stay group-writable, so the admins can read and fix them.
UMask=0002
# {ENV_DIR} is root's. systemd reads the config as root and gives the unit a private copy (%d).
LoadCredential=fleetwright.json:{ENV_DIR}/fleetwright.json
Environment=FLEETWRIGHT_CONFIG=%d/fleetwright.json
Environment=GH_NO_UPDATE_NOTIFIER=1
Environment=PATH={ALLOCATOR_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
# It only reads GitHub, but without allocator.env it holds the authors' token, which can write. Put a
# read-only token there (GH_TOKEN=...) so a bug in the allocator cannot change the repository.
EnvironmentFile={AGENT_ENV}
EnvironmentFile=-{ALLOCATOR_ENV}
ExecStart=/usr/bin/env uv run --script {kit}/scripts/fleetwright_allocate.py{" --dry-run" if dry_run else ""}
# A hung gh call must not hold the lock and stop every later pass.
TimeoutStartSec=5min
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={FLEETWRIGHT_DIR} {ALLOCATOR_HOME}
PrivateTmp=yes
"""


ALLOCATE_TIMER = f"""\
{HEADER}# Enable once /etc/fleetwright/fleetwright.json is written (root): systemctl enable --now fleetwright-allocate.timer
# Pause it as a member of fleetwright-admin: systemctl stop fleetwright-allocate.timer
[Unit]
Description=Run the fleet allocator every 2 minutes

[Timer]
OnBootSec=2min
# Counted from the end of the last pass, so two passes never overlap.
OnUnitInactiveSec=2min
Unit=fleetwright-allocate.service

[Install]
WantedBy=timers.target
"""


def _watchdog_service(kit: str) -> str:
    return f"""{HEADER}# One watchdog pass, with no AI; fleetwright-watchdog.timer runs it. Its lines start with caught or missed, and
# the last one is the run's result: journalctl -u fleetwright-watchdog -o cat -n 30
[Unit]
Description=Fleet watchdog: stalled agents, a red main, stuck pull requests, failed units
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# The allocator's user: it reads what the allocator reads and, when main is red, writes the stop flag.
User={ALLOCATOR_USER}
# To read the slots' journal lines, which tell a stalled agent from a working one.
SupplementaryGroups={JOURNAL_GROUP}
Slice=fleetwright.slice
UMask=0002
LoadCredential=fleetwright.json:{ENV_DIR}/fleetwright.json
Environment=FLEETWRIGHT_CONFIG=%d/fleetwright.json
Environment=GH_NO_UPDATE_NOTIFIER=1
Environment=PATH={ALLOCATOR_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
# It only reads GitHub: allocator.env's read-only token replaces the authors' when it is there.
EnvironmentFile={AGENT_ENV}
EnvironmentFile=-{ALLOCATOR_ENV}
ExecStart=/usr/bin/env uv run --script {kit}/scripts/fleetwright_watchdog.py
TimeoutStartSec=5min
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={FLEETWRIGHT_DIR} {ALLOCATOR_HOME}
PrivateTmp=yes
"""


def _report_service(kit: str) -> str:
    return f"""{HEADER}# The token report over every slot's transcripts, which are mode 600 and owned by the slot. As a member
# of fleetwright-admin, write the PR data first (the unit has no network), then start it and read its JSON:
#   gh pr list --repo <owner>/<name> --state merged --limit 2000 \\
#     --json number,headRefName,mergedAt,additions,deletions > {FLEETWRIGHT_DIR}/prs.json
#   c=$(journalctl -u fleetwright-report -n 0 --show-cursor -q | sed -n 's/^-- cursor: //p')
#   systemctl start fleetwright-report
#   journalctl -u fleetwright-report -o cat --after-cursor "$c" | grep -E '^([{{}}]| )' > report.json
[Unit]
Description=Fleet token report over the slots' transcripts

[Service]
Type=oneshot
# Not root: the allocator's user, with only the right to read past file modes, which the transcripts need.
User={ALLOCATOR_USER}
AmbientCapabilities=CAP_DAC_READ_SEARCH
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
NoNewPrivileges=yes
Slice=fleetwright.slice
Environment=PATH={ALLOCATOR_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=UV_OFFLINE=1
# Fixed arguments: whoever starts it cannot point it at another file. It prints totals per week and
# branch, never a transcript's text.
ExecStart=/usr/bin/env uv run --script {kit}/scripts/fleetwright_report.py --root /home/agent[0-9][0-9]/.claude/projects --root /home/review[0-9][0-9]/.claude/projects --prs {FLEETWRIGHT_DIR}/prs.json --json
TimeoutStartSec=5min
# It can read any file, so it gets no secrets and no network, cannot see {ENV_DIR}, and writes only
# uv's cache.
InaccessiblePaths={ENV_DIR}
PrivateNetwork=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={ALLOCATOR_HOME}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
"""


WATCHDOG_TIMER = f"""{HEADER}# Enable with the allocator's timer (root): systemctl enable --now fleetwright-watchdog.timer
# Pause it as a member of fleetwright-admin: systemctl stop fleetwright-watchdog.timer
[Unit]
Description=Run the fleet watchdog every 5 minutes

[Timer]
OnBootSec=3min
OnUnitInactiveSec=5min
Unit=fleetwright-watchdog.service

[Install]
WantedBy=timers.target
"""


def _slice(name: str, what: str) -> str:
    return f"""\
{HEADER}# {what}. `systemctl status {name}` shows them together. No limits yet: once the first runs
# show what they need, add MemoryMax= and CPUQuota= in a drop-in (systemctl edit {name}).
[Unit]
Description=Fleet: {what.lower()}

[Slice]
"""


def units(kit: str = DEFAULT_KIT) -> dict[str, str]:
    """Every unit file the fleet needs, by file name."""
    if not KIT_RE.fullmatch(kit):
        raise ValueError(f"kit {kit!r}: give an absolute path of letters, digits, '.', '_' and '-'")
    return {
        "fleetwright.slice": _slice("fleetwright.slice", "Every fleet unit"),
        "fleetwright-agent.slice": _slice("fleetwright-agent.slice", "The author slots"),
        "fleetwright-review.slice": _slice("fleetwright-review.slice", "The review slots"),
        "fleetwright-agent@.service": _slot_unit(kit, "author"),
        "fleetwright-review@.service": _slot_unit(kit, "review"),
        "fleetwright-check-agent@.service": _slot_unit(kit, "author", check=True),
        "fleetwright-check-review@.service": _slot_unit(kit, "review", check=True),
        "fleetwright-allocate.service": _allocate_service(kit),
        "fleetwright-allocate-dry-run.service": _allocate_service(kit, dry_run=True),
        "fleetwright-allocate.timer": ALLOCATE_TIMER,
        "fleetwright-watchdog.service": _watchdog_service(kit),
        "fleetwright-watchdog.timer": WATCHDOG_TIMER,
        "fleetwright-report.service": _report_service(kit),
    }


def unit_text(kit: str = DEFAULT_KIT) -> str:
    """The author slots' unit."""
    return units(kit)["fleetwright-agent@.service"]


def polkit_rule() -> str:
    verbs = ", ".join(f'"{v}"' for v in ADMIN_VERBS)
    return (
        "// Generated by scripts/fleetwright_slot.py (`fleetwright_slot.py init`).\n"
        f"// Lets {ADMIN_GROUP} start, stop, restart and reset the fleet's own units, and no others, without\n"
        "// root. Installing or enabling units and reloading systemd stay root's.\n"
        "polkit.addRule(function (action, subject) {\n"
        '    if (action.id !== "org.freedesktop.systemd1.manage-units"'
        f' || !subject.isInGroup("{ADMIN_GROUP}")) {{\n'
        "        return polkit.Result.NOT_HANDLED;\n"
        "    }\n"
        '    var unit = action.lookup("unit") || "";\n'
        '    var verb = action.lookup("verb") || "";\n'
        f"    if (/^{ADMIN_UNITS_RE}$/.test(unit) && [{verbs}].indexOf(verb) >= 0) {{\n"
        "        return polkit.Result.YES;\n"
        "    }\n"
        "    return polkit.Result.NOT_HANDLED;\n"
        "});\n"
    )


SSHD_TEXT = f"""\
# Generated by scripts/fleetwright_slot.py (`fleetwright_slot.py init`).
# Nobody logs in as a slot: its agent runs from systemd, and its tokens arrive only in the unit's environment.
DenyGroups {AUTHOR_GROUP} {REVIEW_GROUP}
"""


def machine_files(kit: str = DEFAULT_KIT) -> dict[str, str]:
    """Every file init writes, by absolute path."""
    files = {f"{UNIT_DIR}/{name}": text for name, text in units(kit).items()}
    files[POLKIT_RULE] = polkit_rule()
    files[SSHD_CONF] = SSHD_TEXT
    return files


# Run after the files are written: a bad sshd file must stop before ssh reloads it.
AFTER_FILES = [
    ["sshd", "-t"],
    ["systemctl", "daemon-reload"],
    ["systemctl", "try-reload-or-restart", "ssh.service"],
]


def init_commands(admins: Sequence[str]) -> list[list[str]]:
    """What root runs once on the machine, in order; the files are written before ``AFTER_FILES``."""
    names = [validate_admin(a) for a in admins]
    admin_groups = f"{ADMIN_GROUP},{JOURNAL_GROUP}"
    fleetwright_dir = ["install", "-d", "-o", "root", "-g", ADMIN_GROUP, "-m", "2775"]
    return [
        ["groupadd", "-f", ADMIN_GROUP],
        ["groupadd", "-f", AUTHOR_GROUP],
        ["groupadd", "-f", REVIEW_GROUP],
        [
            "useradd",
            "--system",
            "--create-home",
            "--home-dir",
            ALLOCATOR_HOME,
            "--shell",
            "/usr/sbin/nologin",
            ALLOCATOR_USER,
        ],
        ["usermod", "-aG", ADMIN_GROUP, ALLOCATOR_USER],
        # The operator's agent logs in as this user: fleetwright-admin, never sudo.
        ["useradd", "--create-home", "--shell", "/bin/bash", CLAUDE_USER],
        *(["usermod", "-aG", admin_groups, name] for name in [CLAUDE_USER, *names]),
        ["install", "-d", "-o", "root", "-g", "root", "-m", "700", ENV_DIR],
        [*fleetwright_dir, FLEETWRIGHT_DIR],
        [*fleetwright_dir, f"{FLEETWRIGHT_DIR}/slots"],
        # What a root-run allocator left behind (state, lock, assignments) joins the group.
        ["chown", "-R", f"root:{ADMIN_GROUP}", FLEETWRIGHT_DIR],
        ["chmod", "-R", "g+rwX", FLEETWRIGHT_DIR],
        ["chmod", "2775", FLEETWRIGHT_DIR, f"{FLEETWRIGHT_DIR}/slots"],
        *AFTER_FILES,
    ]


def setup_commands(slot: str, repo: str, email: str | None = None) -> list[list[str]]:
    """What root runs to create the slot, in order. Each is an argument list, never a shell string.

    ``email`` is the address the slot commits at; an author slot must have one, a review slot never commits.
    """
    validate_slot(slot)
    validate_repo(repo)
    if email is not None:
        validate_email(email)
    elif AUTHOR_RE.fullmatch(slot):
        raise ValueError(
            f"slot {slot}: an author slot commits; give the address it commits at with --git-email, "
            "the noreply address of the account whose token pushes"
        )
    clone = clone_path(slot)
    home = f"/home/{slot}"
    group = REVIEW_GROUP if REVIEW_RE.fullmatch(slot) else AUTHOR_GROUP
    as_slot = ["runuser", "-u", slot, "--"]
    return [
        ["useradd", "--create-home", "--shell", "/bin/bash", slot],
        ["usermod", "-aG", group, slot],
        # Distributions differ on the home's mode; a slot must not read another's clone or transcripts.
        ["chmod", "700", home],
        # The admins may read it, to look at a clone left for a person. After the chmod, which would
        # otherwise set the ACL mask to nothing.
        ["setfacl", "-m", f"g:{ADMIN_GROUP}:rx", home],
        [*as_slot, "git", "clone", "--", repo, clone],
        # Pushes use the token in the unit's environment, through gh; nothing is stored in the clone.
        [*as_slot, "git", "-C", clone, "config", "credential.helper", "!gh auth git-credential"],
        *(
            [
                [*as_slot, "git", "-C", clone, "config", "user.name", f"{slot} (fleet)"],
                [*as_slot, "git", "-C", clone, "config", "user.email", email],
            ]
            if email
            else []
        ),
    ]


def _exists(command: Sequence[str]) -> bool:
    """True when the command's result is already there, so a second --apply keeps it."""
    if command[0] == "useradd":
        home = (
            command[command.index("--home-dir") + 1]
            if "--home-dir" in command
            else f"/home/{command[-1]}"
        )
        return Path(home).is_dir()
    if "clone" in command:
        return Path(command[-1], ".git").is_dir()
    return False


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _run_all(commands: Sequence[Sequence[str]], run: Run) -> bool:
    for command in commands:
        if _exists(command):
            print(f"kept: {shlex.join(command)}")
            continue
        if run(command) != 0:
            hint = (
                " (is the group there? run `fleetwright_slot.py init --apply` first)"
                if command[0] == "usermod"
                else ""
            )
            print(
                f"fleetwright_slot: failed, stopping: {shlex.join(command)}{hint}", file=sys.stderr
            )
            return False
    return True


def init(admins: Sequence[str], kit: str, dest: str, apply: bool, run: Run, is_root: bool) -> int:
    commands = init_commands(admins)
    files = machine_files(kit)
    before, after = commands[: -len(AFTER_FILES)], commands[-len(AFTER_FILES) :]
    if not apply:
        print("# The fleet machine, once: run as root, or pass --apply.")
        for command in before:
            print(shlex.join(command))
        for path, text in files.items():
            print(f"\n# Written to {path}:")
            print(text, end="")
        print()
        for command in after:
            print(shlex.join(command))
        return 0
    if not is_root:
        print("fleetwright_slot: --apply must run as root (sudo)", file=sys.stderr)
        return 1
    if not _run_all(before, run):
        return 1
    for path, text in files.items():
        target = Path(dest) / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    if not _run_all(after, run):
        print(
            "fleetwright_slot: the files are written; fix the failure above and run init again",
            file=sys.stderr,
        )
        return 1
    print(
        f"the machine is ready. Members of {ADMIN_GROUP} must log in again to use the group. Next: "
        f"put the secrets in {ENV_DIR}, then `fleetwright_slot.py add <slot> --repo ... --apply` per slot"
    )
    return 0


def add(slot: str, repo: str, email: str | None, apply: bool, run: Run, is_root: bool) -> int:
    commands = setup_commands(slot, repo, email)
    unit = "fleetwright-review" if REVIEW_RE.fullmatch(slot) else "fleetwright-agent"
    check = "fleetwright-check-review" if REVIEW_RE.fullmatch(slot) else "fleetwright-check-agent"
    if not apply:
        print(
            f"# Slot {slot}: run as root, or pass --apply. Needs `fleetwright_slot.py init --apply` first."
        )
        for command in commands:
            print(shlex.join(command))
        return 0
    if not is_root:
        print("fleetwright_slot: --apply must run as root (sudo)", file=sys.stderr)
        return 1
    if not _run_all(commands, run):
        return 1
    print(
        f"slot {slot} is ready. Install its tools and plugins, then check it: systemctl start {check}@{slot}. "
        f"List it in {ENV_DIR}/fleetwright.json and the allocator starts {unit}@{slot}"
    )
    return 0


def main(argv: list[str] | None = None, run: Run = _run, is_root: bool | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleetwright_slot")
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser(
        "init", help="the machine, once: groups, users, directories, units, polkit, sshd"
    )
    i.add_argument(
        "--admin", action="append", default=[], help="an existing login to add to fleetwright-admin"
    )
    i.add_argument(
        "--kit", default=DEFAULT_KIT, help="where fleetwright is installed on the machine"
    )
    i.add_argument("--dest", default="/", help="write the files under this directory (for testing)")
    i.add_argument("--apply", action="store_true", help="run the commands instead of printing them")
    p = sub.add_parser("add", help="create a slot's user and clone")
    p.add_argument("slot")
    p.add_argument("--repo", required=True)
    p.add_argument(
        "--git-email",
        help="the address the slot commits at (needed for an author slot): "
        "the noreply address of the account whose token pushes",
    )
    p.add_argument("--apply", action="store_true", help="run the commands instead of printing them")
    u = sub.add_parser("unit", help="print the unit files")
    u.add_argument("--kit", default=DEFAULT_KIT)
    u.add_argument("--name", help="print only this unit file")
    args = parser.parse_args(argv)
    try:
        if args.command == "unit":
            files = units(args.kit)
            if args.name:
                if args.name not in files:
                    raise ValueError(f"no unit named {args.name!r}; one of {', '.join(files)}")
                files = {args.name: files[args.name]}
            print("\n".join(files.values()), end="")
            return 0
        root = _is_root() if is_root is None else is_root
        if args.command == "init":
            return init(args.admin, args.kit, args.dest, args.apply, run, root)
        return add(args.slot, args.repo, args.git_email, args.apply, run, root)
    except ValueError as exc:
        print(f"fleetwright_slot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
