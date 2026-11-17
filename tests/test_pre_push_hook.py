"""git-hooks/pre-push: one journal line per ref an author slot pushes, and never a blocked push."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "git-hooks" / "pre-push"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

PUSHED = (
    "refs/heads/issue/12 " + "a" * 40 + " refs/heads/issue/12 " + "0" * 40 + "\n"
    "(delete) " + "0" * 40 + " refs/heads/old " + "b" * 40 + "\n"
)


def run_hook(tmp_path: Path, logger_exit: int = 0) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "logged"
    logger = bin_dir / "logger"
    logger.write_text(
        f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\nexit {logger_exit}\n', encoding="utf-8"
    )
    logger.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(
        ["sh", str(HOOK), "origin", "https://github.com/o/r.git"],
        input=PUSHED,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return done, log.read_text(encoding="utf-8") if log.exists() else ""


def test_each_pushed_ref_is_one_line_tagged_for_the_watchdog(tmp_path: Path) -> None:
    done, logged = run_hook(tmp_path)
    assert done.returncode == 0
    assert logged.splitlines() == [
        "-t fleetwright-push ref=refs/heads/issue/12 sha=" + "a" * 40,
        "-t fleetwright-push ref=refs/heads/old sha=" + "0" * 40,
    ]


def test_a_logger_that_fails_never_blocks_the_push(tmp_path: Path) -> None:
    done, _ = run_hook(tmp_path, logger_exit=1)
    assert done.returncode == 0


def test_the_hook_is_committed_executable() -> None:
    mode = subprocess.run(
        ["git", "ls-files", "-s", "git-hooks/pre-push"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    ).stdout.split()[0]
    assert mode == "100755"
