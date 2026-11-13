"""A slot's setup: the commands that create its user and clone, and the unit that runs it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import fleet_slot
import pytest
from fleet_slot import clone_path, setup_commands, unit_text, units, validate_repo, validate_slot

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/example/project.git"


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


def test_the_unit_binds_the_user_and_the_clone_to_the_same_slot() -> None:
    unit = unit_text()
    assert "User=%i" in unit
    assert "Environment=FLEET_CLONE=/home/%i/repo" in unit
    assert "Environment=FLEET_SLOT=%i" in unit
    assert "WorkingDirectory=/home/%i/repo" in unit
    assert clone_path("agent01") == "/home/agent01/repo"


def test_the_unit_runs_unattended_once_and_takes_secrets_from_a_file() -> None:
    unit = unit_text()
    assert "Environment=CC_PRACTICES_UNATTENDED=1" in unit
    assert "Restart=no" in unit
    assert "EnvironmentFile=/etc/fleet/agent.env" in unit
    assert "ExecStart=/usr/bin/env uv run --script /opt/fleet-kit/scripts/fleet_agent.py" in unit
    for secret in ("ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
        assert secret not in unit


def test_the_unit_lets_the_agent_write_only_its_home() -> None:
    unit = unit_text()
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/home/%i" in unit
    assert "NoNewPrivileges=yes" in unit
    assert "/var/lib/fleet" not in unit.split("ReadWritePaths=")[1].splitlines()[0]


def test_the_unit_names_the_kit_it_was_made_for() -> None:
    assert "/srv/kit/scripts/fleet_agent.py" in unit_text("/srv/kit")
    for bad in ("kit", "/opt/fleet kit", "/opt/kit;id", "/opt/kit\nUser=root", "/opt/%i"):
        with pytest.raises(ValueError):
            unit_text(bad)


def test_the_committed_units_are_the_generated_ones() -> None:
    committed = sorted(p.name for p in (ROOT / "systemd").iterdir())
    assert committed == sorted(units())
    for name, text in units().items():
        assert (ROOT / "systemd" / name).read_text(encoding="utf-8") == text, name


def test_each_slot_unit_names_its_role() -> None:
    assert "Environment=FLEET_ROLE=author" in units()["fleet-agent@.service"]
    assert "Environment=FLEET_ROLE=review" in units()["fleet-review@.service"]


def test_only_the_review_unit_reads_the_reviewer_token_and_reads_it_last() -> None:
    files = units()
    for name, text in files.items():
        assert ("reviewer.env" in text) == (name == "fleet-review@.service"), name
    review = files["fleet-review@.service"].splitlines()
    env_files = [line for line in review if line.startswith("EnvironmentFile=")]
    assert env_files == [
        "EnvironmentFile=/etc/fleet/agent.env",
        "EnvironmentFile=/etc/fleet/reviewer.env",
    ]


def test_the_review_unit_is_sandboxed_like_the_author_unit() -> None:
    files = units()
    for line in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/home/%i",
        "User=%i",
        "Restart=no",
    ):
        assert line in files["fleet-review@.service"].splitlines()


def test_the_allocator_runs_as_root_once_per_timer_tick_and_writes_only_fleet_state() -> None:
    service = units()["fleet-allocate.service"].splitlines()
    assert "Type=oneshot" in service and "User=root" in service
    assert "ReadWritePaths=/var/lib/fleet -/root/.cache -/root/.local/share/uv" in service
    assert "TimeoutStartSec=5min" in service
    assert "EnvironmentFile=-/etc/fleet/allocator.env" in service
    assert any(
        line.endswith("/scripts/fleet_allocate.py") and line.startswith("ExecStart=")
        for line in service
    )
    timer = units()["fleet-allocate.timer"].splitlines()
    assert "OnUnitInactiveSec=2min" in timer and "Unit=fleet-allocate.service" in timer


def test_setup_makes_the_stop_directory_root_owned_before_anything_else() -> None:
    first = setup_commands("agent01", REPO)[0]
    assert first == ["install", "-d", "-o", "root", "-g", "root", "-m", "755", "/var/lib/fleet"]


def test_setup_creates_the_user_and_clones_as_that_user() -> None:
    commands = setup_commands("agent07", REPO)
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


def test_setup_never_goes_through_a_shell() -> None:
    for command in setup_commands("agent01", REPO):
        assert command[0] not in {"sh", "bash", "eval"}
        assert "-c" not in command[:2]


def test_setup_refuses_a_bad_slot_or_repo() -> None:
    with pytest.raises(ValueError):
        setup_commands("root", REPO)
    with pytest.raises(ValueError):
        setup_commands("agent01", "--upload-pack=id")


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.ran: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> int:
        self.ran.append(list(command))
        return 1 if self.fail_on and self.fail_on in command else 0


def test_without_apply_it_prints_and_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    assert fleet_slot.main(["add", "agent01", "--repo", REPO], run=run, is_root=True) == 0
    assert run.ran == []
    out = capsys.readouterr().out
    assert "useradd --create-home --shell /bin/bash agent01" in out
    assert "fleet-agent@.service" in out


def test_apply_needs_root(capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    code = fleet_slot.main(["add", "agent01", "--repo", REPO, "--apply"], run=run, is_root=False)
    assert code != 0
    assert run.ran == []
    assert "root" in capsys.readouterr().err


def test_apply_stops_at_the_first_failure(tmp_path: Path) -> None:
    run = Recorder(fail_on="useradd")
    code = fleet_slot.main(
        ["add", "agent01", "--repo", REPO, "--apply", "--unit-dir", str(tmp_path)],
        run=run,
        is_root=True,
    )
    assert code != 0
    assert run.ran[-1][0] == "useradd"
    assert not any("clone" in c for c in run.ran)


def test_apply_writes_the_unit_and_reloads_systemd(tmp_path: Path) -> None:
    run = Recorder()
    code = fleet_slot.main(
        ["add", "agent01", "--repo", REPO, "--apply", "--unit-dir", str(tmp_path)],
        run=run,
        is_root=True,
    )
    assert code == 0
    for name, text in units().items():
        assert (tmp_path / name).read_text(encoding="utf-8") == text
    assert run.ran[-1] == ["systemctl", "daemon-reload"]


def test_a_review_slot_gets_its_own_user_and_clone() -> None:
    commands = setup_commands("review01", REPO)
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "review01"] in commands
    assert clone_path("review01") == "/home/review01/repo"


def test_the_unit_command_prints_one_unit_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert fleet_slot.main(["unit", "--name", "fleet-allocate.timer"]) == 0
    assert capsys.readouterr().out == units()["fleet-allocate.timer"]
    assert fleet_slot.main(["unit", "--name", "nope.service"]) == 2


def test_a_bad_slot_on_the_command_line_is_an_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fleet_slot.main(["add", "root", "--repo", REPO], run=Recorder(), is_root=True) != 0
    assert "agentNN" in capsys.readouterr().err


def test_setup_makes_the_secrets_directory_root_only() -> None:
    commands = setup_commands("agent01", REPO)
    assert ["install", "-d", "-o", "root", "-g", "root", "-m", "700", "/etc/fleet"] in commands


def test_a_slots_home_is_closed_to_the_other_slots() -> None:
    commands = setup_commands("review01", REPO)
    chmod = ["chmod", "700", "/home/review01"]
    assert chmod in commands
    assert (
        commands.index(chmod)
        == commands.index(["useradd", "--create-home", "--shell", "/bin/bash", "review01"]) + 1
    )


def test_slot_units_hide_devices_and_kernel_settings() -> None:
    for name in ("fleet-agent@.service", "fleet-review@.service"):
        lines = units()[name].splitlines()
        for setting in (
            "PrivateDevices=yes",
            "ProtectKernelTunables=yes",
            "ProtectControlGroups=yes",
        ):
            assert setting in lines, (name, setting)
