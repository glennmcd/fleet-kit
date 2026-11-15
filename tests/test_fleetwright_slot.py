"""Slot and machine setup, and the units, polkit rule and sshd file they install."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import fleetwright_slot
import pytest
from fleetwright_slot import (
    ADMIN_UNITS_RE,
    clone_path,
    init_commands,
    machine_files,
    polkit_rule,
    setup_commands,
    unit_text,
    units,
    validate_repo,
    validate_slot,
)

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/example/project.git"
EMAIL = "1234+example@users.noreply.github.com"


@pytest.mark.parametrize("name", ["agent01", "agent12", "agent99", "review01", "review12"])
def test_a_slot_is_agent_or_review_and_two_digits(name: str) -> None:
    assert validate_slot(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "root",
        "agent1",
        "agent001",
        "Agent01",
        "agent01 ",
        "agent01;id",
        "../agent01",
        "agent0a",
    ],
)
def test_any_other_slot_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        validate_slot(name)


@pytest.mark.parametrize(
    "repo",
    [
        "--upload-pack=touch /tmp/x",  # an option, not a repository
        "-u",
        "git@github.com:example/project.git",
        "http://github.com/example/project.git",
        "https://github.com/example/project.git; id",
        "https://github.com/example/pro ject",
        "file:///etc",
        "",
    ],
)
def test_a_repo_that_is_not_a_plain_https_url_is_refused(repo: str) -> None:
    with pytest.raises(ValueError):
        validate_repo(repo)


# --- units ----------------------------------------------------------------------------------------


def test_the_unit_binds_the_user_and_the_clone_to_the_same_slot() -> None:
    unit = unit_text()
    assert "User=%i" in unit
    assert "Environment=FLEETWRIGHT_CLONE=/home/%i/repo" in unit
    assert "Environment=FLEETWRIGHT_SLOT=%i" in unit
    assert "WorkingDirectory=/home/%i/repo" in unit
    assert clone_path("agent01") == "/home/agent01/repo"


def test_the_unit_runs_unattended_once_and_takes_secrets_from_a_file() -> None:
    unit = unit_text()
    assert "Environment=CC_PRACTICES_UNATTENDED=1" in unit
    assert "Restart=no" in unit
    assert "EnvironmentFile=/etc/fleetwright/agent.env" in unit
    assert (
        "ExecStart=/usr/bin/env uv run --script /opt/fleetwright/scripts/fleetwright_agent.py"
        in unit
    )
    for secret in ("ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        assert secret not in unit


def test_the_unit_lets_the_agent_write_only_its_home() -> None:
    unit = unit_text()
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/home/%i" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "/var/lib/fleetwright" not in unit.split("ReadWritePaths=")[1].splitlines()[0]


def test_the_unit_names_the_kit_it_was_made_for() -> None:
    assert "/srv/kit/scripts/fleetwright_agent.py" in unit_text("/srv/kit")
    for bad in ("kit", "/opt/fleetwright kit", "/opt/kit;id", "/opt/kit\nUser=root", "/opt/%i"):
        with pytest.raises(ValueError):
            unit_text(bad)


def test_the_committed_units_are_the_generated_ones() -> None:
    committed = sorted(p.name for p in (ROOT / "systemd").iterdir())
    assert committed == sorted(units())
    for name, text in units().items():
        assert (ROOT / "systemd" / name).read_text(encoding="utf-8") == text, name


def test_each_slot_unit_names_its_role() -> None:
    assert "Environment=FLEETWRIGHT_ROLE=author" in units()["fleetwright-agent@.service"]
    assert "Environment=FLEETWRIGHT_ROLE=review" in units()["fleetwright-review@.service"]


def test_only_the_review_units_read_the_reviewer_token_and_read_it_last() -> None:
    files = units()
    readers = {"fleetwright-review@.service", "fleetwright-check-review@.service"}
    for name, text in files.items():
        assert ("reviewer.env" in text) == (name in readers), name
    for name in readers:
        env_files = [ln for ln in files[name].splitlines() if ln.startswith("EnvironmentFile=")]
        assert env_files == [
            "EnvironmentFile=/etc/fleetwright/agent.env",
            "EnvironmentFile=/etc/fleetwright/reviewer.env",
        ], name


def test_the_review_unit_is_sandboxed_like_the_author_unit() -> None:
    files = units()
    for line in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/home/%i",
        "User=%i",
        "Restart=no",
    ):
        assert line in files["fleetwright-review@.service"].splitlines()


def test_slot_units_hide_devices_and_kernel_settings() -> None:
    for name in ("fleetwright-agent@.service", "fleetwright-review@.service"):
        lines = units()[name].splitlines()
        for setting in (
            "PrivateDevices=yes",
            "ProtectKernelTunables=yes",
            "ProtectControlGroups=yes",
        ):
            assert setting in lines, (name, setting)


def test_each_role_runs_in_its_own_slice_under_the_fleets() -> None:
    files = units()
    assert "Slice=fleetwright-agent.slice" in files["fleetwright-agent@.service"].splitlines()
    assert "Slice=fleetwright-review.slice" in files["fleetwright-review@.service"].splitlines()
    for name in ("fleetwright.slice", "fleetwright-agent.slice", "fleetwright-review.slice"):
        assert "[Slice]" in files[name], name
        # Limits come later, in a drop-in, once the first runs show what they should be.
        settings = [ln for ln in files[name].splitlines() if not ln.startswith("#")]
        assert not any(ln.startswith(("MemoryMax=", "CPUQuota=")) for ln in settings)


def test_a_check_unit_runs_as_the_slot_with_the_slots_own_secrets_and_sandbox() -> None:
    for role, check, real in (
        ("author", "fleetwright-check-agent@.service", "fleetwright-agent@.service"),
        ("review", "fleetwright-check-review@.service", "fleetwright-review@.service"),
    ):
        lines = units()[check].splitlines()
        real_lines = units()[real].splitlines()
        assert "Type=oneshot" in lines and f"Environment=FLEETWRIGHT_ROLE={role}" in lines
        assert any(
            ln.endswith("/scripts/fleetwright_check.py")
            for ln in lines
            if ln.startswith("ExecStart=")
        )
        for line in real_lines:
            if line.startswith(
                (
                    "User=",
                    "WorkingDirectory=",
                    "Environment=",
                    "EnvironmentFile=",
                    "ReadWritePaths=",
                    "ProtectSystem=",
                    "NoNewPrivileges=",
                    "PrivateTmp=",
                )
            ):
                assert line in lines, (check, line)


def test_the_allocator_runs_as_its_own_user_not_root() -> None:
    service = units()["fleetwright-allocate.service"].splitlines()
    assert "Type=oneshot" in service
    assert "User=fleetwright-allocator" in service
    assert not any(ln == "User=root" or "/root/" in ln for ln in service)
    assert "ReadWritePaths=/var/lib/fleetwright /var/lib/fleetwright-allocator" in service
    assert "ProtectHome=yes" in service
    # Group-writable files, so the admins can read and fix what a pass leaves.
    assert "UMask=0002" in service
    assert "TimeoutStartSec=5min" in service
    assert "EnvironmentFile=-/etc/fleetwright/allocator.env" in service
    assert any(
        line.startswith("ExecStart=") and line.endswith("/scripts/fleetwright_allocate.py")
        for line in service
    )
    timer = units()["fleetwright-allocate.timer"].splitlines()
    assert "OnUnitInactiveSec=2min" in timer and "Unit=fleetwright-allocate.service" in timer


def test_the_allocator_gets_its_root_only_config_from_systemd() -> None:
    # /etc/fleetwright is root's; the allocator is not. systemd reads the file as root and passes
    # a copy.
    for name in ("fleetwright-allocate.service", "fleetwright-allocate-dry-run.service"):
        lines = units()[name].splitlines()
        assert "LoadCredential=fleetwright.json:/etc/fleetwright/fleetwright.json" in lines, name
        assert "Environment=FLEETWRIGHT_CONFIG=%d/fleetwright.json" in lines, name
        assert "Environment=FLEETWRIGHT_CONFIG=/etc/fleetwright/fleetwright.json" not in lines, name


def test_the_dry_run_is_a_unit_so_admins_never_hold_the_token() -> None:
    real = units()["fleetwright-allocate.service"].splitlines()
    dry = units()["fleetwright-allocate-dry-run.service"].splitlines()
    assert any(
        ln.startswith("ExecStart=") and ln.endswith("fleetwright_allocate.py --dry-run")
        for ln in dry
    )
    for line in real:
        if line.startswith(("User=", "EnvironmentFile=", "ReadWritePaths=", "ProtectSystem=")):
            assert line in dry, line


def test_the_watchdog_runs_as_the_allocators_user_and_can_read_the_journal() -> None:
    service = units()["fleetwright-watchdog.service"].splitlines()
    assert "Type=oneshot" in service
    assert "User=fleetwright-allocator" in service
    # Reading other units' journal lines needs this group; the allocator's user is not in it.
    assert "SupplementaryGroups=systemd-journal" in service
    assert "LoadCredential=fleetwright.json:/etc/fleetwright/fleetwright.json" in service
    assert "Environment=FLEETWRIGHT_CONFIG=%d/fleetwright.json" in service
    # It writes the stop flag and nothing else.
    assert "ReadWritePaths=/var/lib/fleetwright /var/lib/fleetwright-allocator" in service
    assert "ProtectSystem=strict" in service and "ProtectHome=yes" in service
    assert "UMask=0002" in service
    assert "EnvironmentFile=-/etc/fleetwright/allocator.env" in service
    assert any(
        line.startswith("ExecStart=") and line.endswith("/scripts/fleetwright_watchdog.py")
        for line in service
    )
    timer = units()["fleetwright-watchdog.timer"].splitlines()
    assert "OnUnitInactiveSec=5min" in timer and "Unit=fleetwright-watchdog.service" in timer


def test_the_report_unit_reads_every_slots_transcripts_and_nothing_leaves() -> None:
    service = units()["fleetwright-report.service"].splitlines()
    assert "Type=oneshot" in service
    # Not root: the allocator's user, given only the right to read past file modes.
    assert "User=fleetwright-allocator" in service
    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in service
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in service
    assert "NoNewPrivileges=yes" in service
    # It can read everything, so it gets no secrets, no network and no way to write what it read.
    assert not any(line.startswith(("EnvironmentFile=", "LoadCredential=")) for line in service)
    assert "InaccessiblePaths=/etc/fleetwright" in service
    assert "PrivateNetwork=yes" in service
    assert "ProtectSystem=strict" in service and "ProtectHome=read-only" in service
    assert "ReadWritePaths=/var/lib/fleetwright-allocator" in service  # uv's cache, nothing else
    assert "Environment=UV_OFFLINE=1" in service
    exec_start = next(line for line in service if line.startswith("ExecStart="))
    # Fixed arguments: an admin starts the unit but cannot point it at another file.
    assert exec_start.endswith(
        "/scripts/fleetwright_report.py"
        " --root /home/agent[0-9][0-9]/.claude/projects"
        " --root /home/review[0-9][0-9]/.claude/projects"
        " --prs /var/lib/fleetwright/prs.json --json"
    )
    assert "%" not in exec_start


# --- polkit and sshd ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit",
    [
        "fleetwright-agent@agent01.service",
        "fleetwright-review@review01.service",
        "fleetwright-check-agent@agent12.service",
        "fleetwright-check-review@review03.service",
        "fleetwright-allocate.service",
        "fleetwright-allocate.timer",
        "fleetwright-allocate-dry-run.service",
        "fleetwright-watchdog.service",
        "fleetwright-watchdog.timer",
        "fleetwright-report.service",
    ],
)
def test_admins_may_manage_the_fleets_units(unit: str) -> None:
    assert re.fullmatch(ADMIN_UNITS_RE, unit)


@pytest.mark.parametrize(
    "unit",
    [
        "ssh.service",
        "polkit.service",
        "fleetwright-agent@root.service",
        "fleetwright-agent@review01.service",  # an author unit on a reviewer's user
        "fleetwright-review@agent01.service",
        "fleetwright-check-agent@review01.service",
        "fleetwright-agent@agent01.service.d",
        "fleetwright-agent@agent1.service",
        "fleetwright-agent.slice",
        "xfleetwright-allocate.service",
        "fleetwright-allocate.serviceX",
        "fleetwright-watchdog@agent01.service",
        "fleetwright-watchdog.serviceX",
        "fleetwright-report@agent01.service",
        "fleetwright-report.timer",
    ],
)
def test_admins_may_not_manage_any_other_unit(unit: str) -> None:
    assert not re.fullmatch(ADMIN_UNITS_RE, unit)


def test_the_polkit_rule_grants_only_fleet_admin_only_these_verbs() -> None:
    rule = polkit_rule()
    assert f"/^{ADMIN_UNITS_RE}$/" in rule
    assert 'subject.isInGroup("fleetwright-admin")' in rule
    assert '"org.freedesktop.systemd1.manage-units"' in rule
    assert '["start", "stop", "restart", "reset-failed"]' in rule
    # Unit files and daemon reloads stay root's.
    assert "manage-unit-files" not in rule and "reload-daemon" not in rule
    assert rule.count("polkit.Result.YES") == 1


def test_nobody_logs_in_over_ssh_as_a_slot() -> None:
    files = machine_files()
    assert (
        "DenyGroups fleetwright-agent fleetwright-review"
        in files["/etc/ssh/sshd_config.d/50-fleetwright.conf"].splitlines()
    )


def test_the_machine_files_are_the_units_the_rule_and_the_sshd_file() -> None:
    files = machine_files()
    for name in units():
        assert f"/etc/systemd/system/{name}" in files
    assert files["/etc/polkit-1/rules.d/50-fleetwright.rules"] == polkit_rule()


# --- init: the machine, once ----------------------------------------------------------------------


def test_init_creates_the_three_groups_first() -> None:
    commands = init_commands([])
    assert commands[:3] == [
        ["groupadd", "-f", "fleetwright-admin"],
        ["groupadd", "-f", "fleetwright-agent"],
        ["groupadd", "-f", "fleetwright-review"],
    ]


def test_init_makes_the_allocator_a_system_user_in_fleet_admin_with_no_login() -> None:
    commands = init_commands([])
    useradd = next(c for c in commands if c[0] == "useradd" and c[-1] == "fleetwright-allocator")
    assert "--system" in useradd and "/usr/sbin/nologin" in useradd
    assert useradd[useradd.index("--home-dir") + 1] == "/var/lib/fleetwright-allocator"
    assert ["usermod", "-aG", "fleetwright-admin", "fleetwright-allocator"] in commands


def test_init_makes_a_claude_login_in_fleet_admin_and_never_in_sudo() -> None:
    commands = init_commands([])
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "claude"] in commands
    assert ["usermod", "-aG", "fleetwright-admin,systemd-journal", "claude"] in commands
    for c in commands:
        assert not any(g in ("sudo", "wheel", "adm") for part in c for g in part.split(","))


def test_init_adds_each_named_admin() -> None:
    commands = init_commands(["glenn"])
    assert ["usermod", "-aG", "fleetwright-admin,systemd-journal", "glenn"] in commands


@pytest.mark.parametrize("bad", ["root", "Glenn", "-g", "a b", "x;id", "", "fleetwright-allocator"])
def test_init_refuses_a_bad_admin_name(bad: str) -> None:
    with pytest.raises(ValueError):
        init_commands([bad])


def test_init_keeps_secrets_root_only_and_gives_the_fleet_dir_to_fleet_admin() -> None:
    commands = init_commands([])
    assert [
        "install",
        "-d",
        "-o",
        "root",
        "-g",
        "root",
        "-m",
        "700",
        "/etc/fleetwright",
    ] in commands
    fleet = [
        "install",
        "-d",
        "-o",
        "root",
        "-g",
        "fleetwright-admin",
        "-m",
        "2775",
        "/var/lib/fleetwright",
    ]
    slots = [
        "install",
        "-d",
        "-o",
        "root",
        "-g",
        "fleetwright-admin",
        "-m",
        "2775",
        "/var/lib/fleetwright/slots",
    ]
    chown = ["chown", "-R", "root:fleetwright-admin", "/var/lib/fleetwright"]
    chmod = ["chmod", "-R", "g+rwX", "/var/lib/fleetwright"]
    setgid = ["chmod", "2775", "/var/lib/fleetwright", "/var/lib/fleetwright/slots"]
    for c in (fleet, slots, chown, chmod, setgid):
        assert c in commands, c
    # Files a root-run allocator left behind join the group, and the setgid bit is set last.
    assert (
        commands.index(fleet)
        < commands.index(chown)
        < commands.index(chmod)
        < commands.index(setgid)
    )
    assert not any(
        "/etc/fleetwright" in part for c in commands if c[0] in ("chown", "chmod") for part in c
    )


def test_init_checks_sshd_and_reloads_after_writing() -> None:
    commands = init_commands([])
    assert commands[-3:] == [
        ["sshd", "-t"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "try-reload-or-restart", "ssh.service"],
    ]


def test_init_never_goes_through_a_shell() -> None:
    for command in init_commands(["glenn"]):
        assert command[0] not in {"sh", "bash", "eval"}
        assert "-c" not in command[:2]


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.ran: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> int:
        self.ran.append(list(command))
        return 1 if self.fail_on and self.fail_on in command else 0


def test_init_without_apply_prints_and_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    assert fleetwright_slot.main(["init", "--admin", "glenn"], run=run, is_root=True) == 0
    assert run.ran == []
    out = capsys.readouterr().out
    assert "groupadd -f fleetwright-admin" in out
    assert "/etc/polkit-1/rules.d/50-fleetwright.rules" in out
    assert "DenyGroups fleetwright-agent fleetwright-review" in out


def test_init_apply_needs_root(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    assert fleetwright_slot.main(["init", "--apply"], run=run, is_root=False) != 0
    assert run.ran == []
    assert "root" in capsys.readouterr().err


def test_init_apply_writes_every_file_before_checking_sshd(tmp_path: Path) -> None:
    run = Recorder()
    code = fleetwright_slot.main(
        ["init", "--apply", "--dest", str(tmp_path)], run=run, is_root=True
    )
    assert code == 0
    for path, text in machine_files().items():
        assert (tmp_path / path.lstrip("/")).read_text(encoding="utf-8") == text, path
    assert run.ran[-3:] == [
        ["sshd", "-t"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "try-reload-or-restart", "ssh.service"],
    ]


def test_init_apply_stops_before_writing_when_a_command_fails(tmp_path: Path) -> None:
    run = Recorder(fail_on="groupadd")
    code = fleetwright_slot.main(
        ["init", "--apply", "--dest", str(tmp_path)], run=run, is_root=True
    )
    assert code != 0
    assert len(run.ran) == 1
    assert not (tmp_path / "etc").exists()


def test_init_apply_does_not_reload_ssh_when_its_config_is_bad(tmp_path: Path) -> None:
    run = Recorder(fail_on="-t")
    code = fleetwright_slot.main(
        ["init", "--apply", "--dest", str(tmp_path)], run=run, is_root=True
    )
    assert code != 0
    assert ["systemctl", "try-reload-or-restart", "ssh.service"] not in run.ran


# --- add: one slot --------------------------------------------------------------------------------


def test_setup_creates_the_user_and_clones_as_that_user() -> None:
    commands = setup_commands("agent07", REPO, EMAIL)
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "agent07"] in commands
    clone = next(c for c in commands if "clone" in c)
    assert clone == [
        "runuser",
        "-u",
        "agent07",
        "--",
        "git",
        "clone",
        "--",
        REPO,
        "/home/agent07/repo",
    ]
    assert commands.index(clone) > commands.index(
        ["useradd", "--create-home", "--shell", "/bin/bash", "agent07"]
    )


def test_setup_puts_each_slot_in_its_roles_group() -> None:
    assert ["usermod", "-aG", "fleetwright-agent", "agent01"] in setup_commands(
        "agent01", REPO, EMAIL
    )
    assert ["usermod", "-aG", "fleetwright-review", "review01"] in setup_commands("review01", REPO)


def test_setup_leaves_the_machine_wide_directories_to_init() -> None:
    # Running add again must not take /var/lib/fleetwright back from fleetwright-admin.
    for command in setup_commands("agent01", REPO, EMAIL):
        assert not any(
            p.startswith(("/var/lib/fleetwright", "/etc/fleetwright")) for p in command
        ), command


def test_a_slots_home_is_closed_to_the_other_slots_and_readable_by_admins() -> None:
    commands = setup_commands("review01", REPO)
    useradd = ["useradd", "--create-home", "--shell", "/bin/bash", "review01"]
    chmod = ["chmod", "700", "/home/review01"]
    acl = ["setfacl", "-m", "g:fleetwright-admin:rx", "/home/review01"]
    assert chmod in commands and acl in commands
    # chmod after setfacl would set the ACL mask to nothing and shut the admins out again.
    assert commands.index(useradd) < commands.index(chmod) < commands.index(acl)


def test_setup_never_goes_through_a_shell() -> None:
    for command in setup_commands("agent01", REPO, EMAIL):
        assert command[0] not in {"sh", "bash", "eval"}
        assert "-c" not in command[:2]


def test_setup_refuses_a_bad_slot_or_repo() -> None:
    with pytest.raises(ValueError):
        setup_commands("root", REPO)
    with pytest.raises(ValueError):
        setup_commands("agent01", "--upload-pack=id")


def test_without_apply_it_prints_and_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    assert (
        fleetwright_slot.main(
            ["add", "agent01", "--repo", REPO, "--git-email", EMAIL], run=run, is_root=True
        )
        == 0
    )
    assert run.ran == []
    out = capsys.readouterr().out
    assert "useradd --create-home --shell /bin/bash agent01" in out


def test_apply_needs_root(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    code = fleetwright_slot.main(
        ["add", "agent01", "--repo", REPO, "--git-email", EMAIL, "--apply"], run=run, is_root=False
    )
    assert code != 0
    assert run.ran == []
    assert "root" in capsys.readouterr().err


def test_apply_stops_at_the_first_failure() -> None:
    run = Recorder(fail_on="useradd")
    code = fleetwright_slot.main(
        ["add", "agent01", "--repo", REPO, "--git-email", EMAIL, "--apply"], run=run, is_root=True
    )
    assert code != 0
    assert run.ran[-1][0] == "useradd"
    assert not any("clone" in c for c in run.ran)


def test_a_missing_group_points_at_init(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder(fail_on="usermod")
    code = fleetwright_slot.main(
        ["add", "agent01", "--repo", REPO, "--git-email", EMAIL, "--apply"], run=run, is_root=True
    )
    assert code != 0
    assert "fleetwright_slot.py init" in capsys.readouterr().err


def test_add_writes_no_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run = Recorder()
    assert (
        fleetwright_slot.main(
            ["add", "agent01", "--repo", REPO, "--git-email", EMAIL, "--apply"],
            run=run,
            is_root=True,
        )
        == 0
    )
    assert list(tmp_path.iterdir()) == []
    assert ["systemctl", "daemon-reload"] not in run.ran


def test_a_review_slot_gets_its_own_user_and_clone() -> None:
    commands = setup_commands("review01", REPO)
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "review01"] in commands
    assert clone_path("review01") == "/home/review01/repo"


def test_an_author_slot_commits_as_itself_at_the_address_given() -> None:
    commands = setup_commands("agent03", REPO, EMAIL)
    git = ["runuser", "-u", "agent03", "--", "git", "-C", "/home/agent03/repo", "config"]
    name = [*git, "user.name", "agent03 (fleet)"]
    email = [*git, "user.email", EMAIL]
    assert name in commands and email in commands
    clone = next(c for c in commands if "clone" in c)
    assert commands.index(clone) < commands.index(name)


def test_an_author_slot_without_an_address_is_refused() -> None:
    # Without one, an agent's first commit fails and it borrows whatever identity it can find.
    with pytest.raises(ValueError, match="--git-email"):
        setup_commands("agent01", REPO)


def test_a_review_slot_needs_no_address_but_takes_one() -> None:
    assert not any("user.email" in c for c in setup_commands("review01", REPO))
    assert any(c[-2:] == ["user.email", EMAIL] for c in setup_commands("review01", REPO, EMAIL))


@pytest.mark.parametrize(
    "bad", ["", "--upload-pack=id", "no-at-sign", "a b@example.com", "a@example.com\nb", "@x.org"]
)
def test_setup_refuses_a_bad_address(bad: str) -> None:
    with pytest.raises(ValueError, match="git-email"):
        setup_commands("agent01", REPO, bad)


def test_add_without_an_address_for_an_author_is_an_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = Recorder()
    assert (
        fleetwright_slot.main(["add", "agent01", "--repo", REPO, "--apply"], run=run, is_root=True)
        != 0
    )
    assert run.ran == []
    assert "--git-email" in capsys.readouterr().err


def test_add_prints_the_identity_it_sets(capsys: pytest.CaptureFixture[str]) -> None:
    fleetwright_slot.main(["add", "agent02", "--repo", REPO, "--git-email", EMAIL], run=Recorder())
    out = capsys.readouterr().out
    assert "config user.name 'agent02 (fleet)'" in out
    assert f"config user.email {EMAIL}" in out


def test_the_unit_command_prints_one_unit_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert fleetwright_slot.main(["unit", "--name", "fleetwright-allocate.timer"]) == 0
    assert capsys.readouterr().out == units()["fleetwright-allocate.timer"]
    assert fleetwright_slot.main(["unit", "--name", "nope.service"]) == 2


def test_a_bad_slot_on_the_command_line_is_an_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fleetwright_slot.main(["add", "root", "--repo", REPO], run=Recorder(), is_root=True) != 0
    assert "agentNN" in capsys.readouterr().err
