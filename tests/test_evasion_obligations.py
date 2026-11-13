"""Evasion kit for the change-obligations gate."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
from gates import obligations

RULES = [
    {
        "id": "model-needs-migration",
        "when": ["app/models/**/*.py"],
        "also": ["app/migrations/**"],
        "except": ["**/__init__.py"],
        "incident": "#7: a column shipped without its migration",
    }
]


def ev(changes: list[tuple[str, str, str | None]]) -> list[str]:
    return [str(f) for f in obligations.evaluate(RULES, obligations.touched(changes))]


def test_touching_x_without_y_fails() -> None:
    found = ev([("M", "app/models/user.py", None)])
    assert len(found) == 1 and "#7" in found[0]


def test_touching_x_and_y_passes() -> None:
    assert ev([("M", "app/models/user.py", None), ("A", "app/migrations/0002.py", None)]) == []


def test_untouched_x_owes_nothing() -> None:
    assert ev([("M", "README.md", None)]) == []


def test_except_paths_are_free() -> None:
    assert ev([("M", "app/models/__init__.py", None)]) == []


def test_a_rename_out_of_reach_still_counts() -> None:
    assert len(ev([("R", "app/other/user.py", "app/models/user.py")])) == 1


def test_a_deletion_still_counts() -> None:
    assert len(ev([("D", "app/models/user.py", None)])) == 1


@pytest.mark.parametrize(
    "spelling", ["./app/models/user.py", r"app\models\user.py", "app/x/../models/user.py"]
)
def test_path_spellings_are_normalized(spelling: str) -> None:
    assert len(ev([("M", spelling, None)])) == 1


def test_a_look_alike_directory_is_not_the_owed_path() -> None:
    assert len(ev([("M", "app/models/user.py", None), ("A", "app/migrations_old/x.py", None)])) == 1


def test_a_rule_without_an_incident_is_refused() -> None:
    found = obligations.evaluate([{"id": "x", "when": ["a"], "also": ["b"]}], {"a"})
    assert "cites no incident" in found[0].message


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_check_reads_the_real_diff(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "app/models").mkdir(parents=True)
    (tmp_path / "app/models/user.py").write_text("a = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "switch", "-qc", "feat")
    (tmp_path / "app/models/user.py").write_text("a = 2\n")
    git(tmp_path, "commit", "-qam", "change")
    args = argparse.Namespace(base="main")
    assert len(obligations.check(tmp_path, {"obligations": RULES}, args)) == 1


def test_missing_base_is_an_error_not_a_pass(tmp_path: Path) -> None:
    found = obligations.check(tmp_path, {"obligations": RULES}, argparse.Namespace(base=None))
    assert found and "--base" in found[0].message
