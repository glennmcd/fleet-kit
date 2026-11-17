"""bin/gh: the real gh, with the GitHub App token read from its file at each call."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "bin" / "gh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


def fake_gh(tmp_path: Path) -> Path:
    """Prints the token it was given and its arguments."""
    gh = tmp_path / "real-gh"
    gh.write_text('#!/bin/sh\necho "token=${GH_TOKEN:-unset} args=$*"\n', encoding="utf-8")
    gh.chmod(0o755)
    return gh


def run(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "FLEETWRIGHT_TOKEN_FILE")}
    return subprocess.run(
        ["sh", str(SHIM), *args],
        capture_output=True,
        text=True,
        env={**base, "FLEETWRIGHT_REAL_GH": str(fake_gh(tmp_path)).replace("\\", "/"), **env},
        check=False,
    )


def test_the_token_file_is_read_at_each_call(tmp_path: Path) -> None:
    token = tmp_path / "gh-token"
    token.write_text("ghs_first", encoding="utf-8")
    env = {"FLEETWRIGHT_TOKEN_FILE": str(token).replace("\\", "/")}
    assert run(tmp_path, env, "pr", "list").stdout == "token=ghs_first args=pr list\n"
    token.write_text("ghs_renewed", encoding="utf-8")
    assert run(tmp_path, env, "api", "user").stdout == "token=ghs_renewed args=api user\n"


def test_without_a_token_file_gh_runs_as_it_would_alone(tmp_path: Path) -> None:
    done = run(tmp_path, {"GH_TOKEN": "ghp_reviewer"}, "pr", "view", "7")
    assert done.stdout == "token=ghp_reviewer args=pr view 7\n"


def test_an_unreadable_token_file_stops_before_gh_runs(tmp_path: Path) -> None:
    done = run(tmp_path, {"FLEETWRIGHT_TOKEN_FILE": str(tmp_path / "missing")}, "pr", "list")
    assert done.returncode != 0
    assert "token=" not in done.stdout


def test_the_shim_is_committed_executable() -> None:
    mode = subprocess.run(
        ["git", "ls-files", "-s", "bin/gh"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()[0]
    assert mode == "100755"
