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


def whoami(login: str | None) -> fleet_agent.WhoAmI:
    return lambda: login


@pytest.fixture
def slots(tmp_path: Path) -> Path:
    path = tmp_path / "slots"
    path.mkdir()
    return path


def make_env(
    tmp_path: Path, slots: Path, slot: str, role: str, assignment: str | None
) -> dict[str, str]:
    if assignment is not None:
        (slots / slot).write_text(assignment + "\n", encoding="utf-8")
    env = {
        "FLEET_SLOT": slot,
        "FLEET_ROLE": role,
        "FLEET_CLONE": str(tmp_path / "repo"),
        "FLEET_STOP_FILE": str(tmp_path / "STOP"),
        "FLEET_SLOTS_DIR": str(slots),
    }
    if role == "review":
        env["FLEET_REVIEWER_LOGIN"] = "g26work"
    return env


@pytest.fixture
def env(tmp_path: Path, slots: Path) -> dict[str, str]:
    return make_env(tmp_path, slots, "agent01", "author", "issue 12")


@pytest.fixture
def review_env(tmp_path: Path, slots: Path) -> dict[str, str]:
    return make_env(tmp_path, slots, "review01", "review", "review 40")


# --- author slots -------------------------------------------------------------------------------


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


def test_a_local_branch_is_resumed_and_brought_up_to_its_pushed_state(env: dict[str, str]) -> None:
    git = FakeGit(refs=["refs/heads/issue/12", "refs/remotes/origin/issue/12"])
    assert run(env, git, Launch()) == 0
    assert git.ran("switch") == [["switch", "issue/12"]]
    assert git.ran("merge") == [["merge", "--ff-only", "origin/issue/12"]]


def test_a_local_branch_that_was_never_pushed_is_resumed_as_it_is(env: dict[str, str]) -> None:
    git = FakeGit(refs=["refs/heads/issue/12"])
    assert run(env, git, Launch()) == 0
    assert git.ran("merge") == []


def test_a_local_branch_that_diverged_from_its_pushed_state_starts_nothing(
    env: dict[str, str],
) -> None:
    git, launch = (
        FakeGit(refs=["refs/heads/issue/12", "refs/remotes/origin/issue/12"], fail="merge"),
        Launch(),
    )
    assert run(env, git, launch) == NOT_READY
    assert launch.argv is None


def test_a_branch_another_slot_pushed_is_picked_up(env: dict[str, str]) -> None:
    git = FakeGit(refs=["refs/remotes/origin/issue/12"])
    assert run(env, git, Launch()) == 0
    assert git.ran("switch") == [["switch", "-c", "issue/12", "--track", "origin/issue/12"]]


def test_a_fix_round_works_on_the_issue_branch_with_its_own_prompt(
    tmp_path: Path, slots: Path
) -> None:
    env = make_env(tmp_path, slots, "agent02", "author", "fix 12")
    git, launch = FakeGit(refs=["refs/remotes/origin/issue/12"]), Launch()
    assert run(env, git, launch) == 0
    assert git.ran("switch") == [["switch", "-c", "issue/12", "--track", "origin/issue/12"]]
    assert launch.argv is not None
    assert launch.argv[2] == fleet_agent.fix_prompt(12)
    assert launch.argv[2] != fleet_agent.issue_prompt(12)


# --- review slots -------------------------------------------------------------------------------


def test_a_review_checks_out_the_prs_head_and_runs_the_review(review_env: dict[str, str]) -> None:
    git, launch = FakeGit(), Launch()
    assert run(review_env, git, launch, whoami("g26work")) == 0
    assert git.ran("fetch") == [["fetch", "origin", "+refs/pull/40/head:refs/remotes/origin/pr/40"]]
    assert git.ran("switch") == [["switch", "--detach", "origin/pr/40"]]
    assert launch.argv is not None
    assert launch.argv[2] == fleet_agent.review_prompt(40)
    assert "review-pr" in launch.argv[2]


@pytest.mark.parametrize("login", ["glennmcd", None, "G26work-bot"])
def test_a_review_refuses_to_run_as_any_other_account(
    review_env: dict[str, str], login: str | None
) -> None:
    git, launch = FakeGit(), Launch()
    assert run(review_env, git, launch, whoami(login)) == BAD_CONFIG
    assert git.calls == [] and launch.argv is None


def test_the_login_check_ignores_case(review_env: dict[str, str]) -> None:
    assert run(review_env, FakeGit(), Launch(), whoami("G26Work")) == 0


def test_a_review_slot_needs_the_expected_login(review_env: dict[str, str]) -> None:
    del review_env["FLEET_REVIEWER_LOGIN"]
    launch = Launch()
    assert run(review_env, FakeGit(), launch, whoami("g26work")) == BAD_CONFIG
    assert launch.argv is None


@pytest.mark.parametrize(
    ("slot", "role", "assignment"),
    [
        ("agent01", "author", "review 40"),
        ("review01", "review", "issue 12"),
        ("review01", "review", "fix 12"),
    ],
)
def test_a_slot_refuses_work_of_the_other_role(
    tmp_path: Path, slots: Path, slot: str, role: str, assignment: str
) -> None:
    env = make_env(tmp_path, slots, slot, role, assignment)
    launch = Launch()
    assert run(env, FakeGit(), launch, whoami("g26work")) == BAD_ASSIGNMENT
    assert launch.argv is None


@pytest.mark.parametrize(
    ("slot", "role"),
    [("agent01", "review"), ("review01", "author"), ("agent01", ""), ("agent01", "admin")],
)
def test_the_slot_name_and_role_must_agree(
    tmp_path: Path, slots: Path, slot: str, role: str
) -> None:
    env = make_env(tmp_path, slots, slot, role, "issue 12")
    env["FLEET_REVIEWER_LOGIN"] = "g26work"
    assert run(env, FakeGit(), Launch(), whoami("g26work")) == BAD_CONFIG


# --- checks every slot makes ----------------------------------------------------------------------


def test_a_stopped_line_starts_nothing(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    Path(env["FLEET_STOP_FILE"]).write_text("main is red\n", encoding="utf-8")
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == STOPPED
    assert git.calls == []
    assert launch.argv is None
    assert "main is red" in capsys.readouterr().err


def test_a_slot_with_no_assignment_is_idle_not_failed(tmp_path: Path, slots: Path) -> None:
    env = make_env(tmp_path, slots, "agent01", "author", None)
    git, launch = FakeGit(), Launch()
    assert run(env, git, launch) == 0
    assert git.calls == []
    assert launch.argv is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "12",
        "issue",
        "issue abc",
        "issue 0",
        "issue -3",
        "issue 12; id",
        "issue 12 13",
        "deploy 12",
        "issue $(id)",
        "issue " + "9" * 12,
        "ISSUE 12",
    ],
)
def test_an_assignment_that_is_not_kind_and_number_is_refused(
    tmp_path: Path, slots: Path, text: str
) -> None:
    env = make_env(tmp_path, slots, "agent01", "author", text)
    launch = Launch()
    assert run(env, FakeGit(), launch) == BAD_ASSIGNMENT
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


@pytest.mark.parametrize("missing", ["FLEET_SLOT", "FLEET_CLONE", "FLEET_ROLE"])
def test_a_unit_that_sets_no_slot_clone_or_role_is_refused(
    env: dict[str, str], missing: str
) -> None:
    del env[missing]
    launch = Launch()
    assert run(env, FakeGit(), launch) == BAD_CONFIG
    assert launch.argv is None


def test_a_slot_name_cannot_walk_out_of_the_slots_directory(env: dict[str, str]) -> None:
    env["FLEET_SLOT"] = "../agent01"
    launch = Launch()
    assert run(env, FakeGit(), launch) == BAD_CONFIG
    assert launch.argv is None


def test_each_prompt_is_fixed_apart_from_its_number() -> None:
    for make in (fleet_agent.issue_prompt, fleet_agent.fix_prompt, fleet_agent.review_prompt):
        assert make(7).replace("7", "N") == make(12).replace("12", "N")
    assert "Done when" in fleet_agent.issue_prompt(7)
    assert "do not change" in fleet_agent.review_prompt(7).lower()
