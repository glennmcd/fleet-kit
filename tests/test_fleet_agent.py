"""What a slot's service unit runs: the checks before an agent starts, and how it is started."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import fleet_agent
import pytest
from fleet_agent import BAD_ASSIGNMENT, BAD_CONFIG, NOT_READY, STOPPED, run


class FakeGit:
    """Answers git by subcommand. `refs` are the refs that exist; `dirty` is the status output."""

    def __init__(self, refs: Sequence[str] = (), dirty: str = "", fail: str | None = None) -> None:
        self.refs = set(refs)
        self.dirty = dirty
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if self.fail and args[0] == self.fail:
            return 1, "boom"
        if args[0] == "status":
            return 0, self.dirty
        if args[0] == "rev-parse":
            return (0, "abc") if args[-1] in self.refs else (1, "")
        return 0, ""

    def ran(self, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == subcommand]


class Launch:
    def __init__(self) -> None:
        self.argv: list[str] | None = None

    def __call__(self, argv: Sequence[str]) -> int:
        self.argv = list(argv)
        return 0


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    slots = tmp_path / "slots"
    slots.mkdir()
    (slots / "agent01").write_text("12\n", encoding="utf-8")
    return {
        "FLEET_SLOT": "agent01",
        "FLEET_CLONE": str(tmp_path / "repo"),
        "FLEET_STOP_FILE": str(tmp_path / "STOP"),
        "FLEET_SLOTS_DIR": str(slots),
    }


def test_a_fresh_issue_starts_on_a_new_branch_from_main(env: dict[str, str]) -> None:
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == 0
    assert git.ran("fetch") == [["fetch", "origin"]]
    assert git.ran("switch") == [["switch", "-c", "issue/12", "origin/main"]]
    assert git.calls.index(["fetch", "origin"]) < git.calls.index(git.ran("switch")[0])
    assert launch.argv is not None
    assert launch.argv[:2] == ["claude", "-p"]
    assert "#12" in launch.argv[2]
    assert "--dangerously-skip-permissions" in launch.argv


def test_a_branch_already_in_the_clone_is_resumed(env: dict[str, str]) -> None:
    git, launch = FakeGit(refs=["refs/heads/issue/12"]), Launch()
    assert run(env, git, launch) == 0
    assert git.ran("switch") == [["switch", "issue/12"]]
    assert launch.argv is not None


def test_a_branch_another_slot_pushed_is_picked_up(env: dict[str, str]) -> None:
    git, launch = FakeGit(refs=["refs/remotes/origin/issue/12"]), Launch()
    assert run(env, git, launch) == 0
    assert git.ran("switch") == [["switch", "-c", "issue/12", "--track", "origin/issue/12"]]


def test_a_stopped_line_starts_nothing(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    Path(env["FLEET_STOP_FILE"]).write_text("main is red\n", encoding="utf-8")
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == STOPPED
    assert git.calls == []
    assert launch.argv is None
    assert "main is red" in capsys.readouterr().err


def test_a_slot_with_no_assignment_is_idle_not_failed(env: dict[str, str]) -> None:
    (Path(env["FLEET_SLOTS_DIR"]) / "agent01").unlink()
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == 0
    assert git.calls == []
    assert launch.argv is None


@pytest.mark.parametrize(
    "text", ["", "abc", "0", "-3", "12; id", "12 13", "1.5", "$(id)", "9" * 12]
)
def test_an_assignment_that_is_not_an_issue_number_is_refused(
    env: dict[str, str], text: str
) -> None:
    (Path(env["FLEET_SLOTS_DIR"]) / "agent01").write_text(text, encoding="utf-8")
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == BAD_ASSIGNMENT
    assert launch.argv is None


def test_a_dirty_clone_is_left_alone(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    git, launch = FakeGit(dirty=" M src/app.py\n"), Launch()
    assert run(env, git, launch) == NOT_READY
    assert git.ran("switch") == []
    assert git.ran("fetch") == []
    assert launch.argv is None
    assert "uncommitted" in capsys.readouterr().err


@pytest.mark.parametrize("step", ["status", "fetch", "switch"])
def test_a_git_failure_starts_nothing(env: dict[str, str], step: str) -> None:
    git, launch = FakeGit(fail=step), Launch()
    assert run(env, git, launch) == NOT_READY
    assert launch.argv is None


@pytest.mark.parametrize("missing", ["FLEET_SLOT", "FLEET_CLONE"])
def test_a_unit_that_sets_no_slot_or_clone_is_refused(env: dict[str, str], missing: str) -> None:
    del env[missing]
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == BAD_CONFIG
    assert launch.argv is None


def test_a_slot_name_cannot_walk_out_of_the_slots_directory(env: dict[str, str]) -> None:
    env["FLEET_SLOT"] = "../agent01"
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == BAD_CONFIG
    assert launch.argv is None


def test_the_prompt_is_fixed_apart_from_the_issue_number() -> None:
    assert fleet_agent.prompt(7).replace("7", "N") == fleet_agent.prompt(12).replace("12", "N")
    assert "Done when" in fleet_agent.prompt(7)
