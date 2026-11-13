"""Evasion kit for hooks/write_guard.py: each case tries to write outside the agent's clone.

The cases marked KNOWN LIMIT document what the hook cannot see. They assert that the call is allowed, so
the limit stays written down and a change that closes one shows up here. The real boundary for those is the
agent's OS user (docs/control-plane.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import write_guard

ROOT = Path(__file__).resolve().parent.parent


class World:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.clone = base / "clone"
        self.clone.mkdir()
        (self.clone / "sub").mkdir()
        self.tmp = base / "tmp"
        self.tmp.mkdir()
        self.shared = base / "shared"
        self.shared.mkdir()
        self.other = base / "other-clone"
        self.other.mkdir()
        # forward slashes: shell words treat a backslash as an escape
        self.out = self.other.as_posix()
        self.temp_env = {"TMPDIR": str(self.tmp), "TEMP": str(self.tmp), "TMP": str(self.tmp)}

    def bash(self, command: str, cwd: Path | None = None) -> dict[str, Any] | None:
        return write_guard.decide(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or self.clone)}
        )

    def tool(self, name: str, path: str, cwd: Path | None = None) -> dict[str, Any] | None:
        key = "notebook_path" if name == "NotebookEdit" else "file_path"
        return write_guard.decide(
            {"tool_name": name, "tool_input": {key: path}, "cwd": str(cwd or self.clone)}
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    w = World(tmp_path.resolve())
    monkeypatch.setenv("FLEET_CLONE", str(w.clone))
    monkeypatch.delenv("FLEET_ALLOW_WRITE", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(w.tmp))
    yield w


def denied(result: dict[str, Any] | None) -> bool:
    return bool(result) and result["hookSpecificOutput"]["permissionDecision"] == "deny"  # type: ignore[index]


# --- the file tools -------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_file_tools_may_write_inside_the_clone(world: World, tool: str) -> None:
    assert world.tool(tool, str(world.clone / "sub" / "a.py")) is None
    assert world.tool(tool, "sub/a.py") is None  # relative to the cwd


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_file_tools_may_not_write_outside_the_clone(world: World, tool: str) -> None:
    assert denied(world.tool(tool, str(world.other / "a.py")))
    assert denied(world.tool(tool, "../other-clone/a.py"))


def test_dot_dot_that_climbs_out_is_refused_and_one_that_stays_in_is_not(world: World) -> None:
    assert denied(world.tool("Write", str(world.clone / "sub" / ".." / ".." / "x")))
    assert world.tool("Write", str(world.clone / "sub" / ".." / "x")) is None


def test_a_sibling_directory_with_the_same_prefix_is_outside(world: World) -> None:
    sibling = world.base / "clone-evil"
    sibling.mkdir()
    assert denied(world.tool("Write", str(sibling / "x")))


def test_a_symlink_inside_the_clone_that_points_out_is_followed(world: World) -> None:
    link = world.clone / "escape"
    try:
        link.symlink_to(world.other, target_is_directory=True)
    except OSError:
        pytest.skip("this machine cannot create symlinks")
    assert denied(world.tool("Write", str(link / "x")))
    assert denied(world.bash("echo hi > escape/x"))


def test_a_file_tool_without_a_path_is_refused(world: World) -> None:
    assert denied(
        write_guard.decide({"tool_name": "Write", "tool_input": {}, "cwd": str(world.clone)})
    )


def test_the_agents_own_safety_settings_are_protected_even_inside_the_clone(world: World) -> None:
    for rel in (
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/x.sh",
        ".git/hooks/pre-commit",
    ):
        assert denied(world.tool("Write", str(world.clone / rel))), rel
        assert denied(world.bash(f"touch {rel}")), rel
    assert world.tool("Write", str(world.clone / ".claude" / "skills" / "a.md")) is None


def test_other_tools_are_not_judged(world: World) -> None:
    assert (
        write_guard.decide({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}})
        is None
    )


def test_the_hook_does_nothing_on_a_machine_that_is_not_a_fleet_agent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLEET_CLONE")
    assert world.tool("Write", str(world.other / "x")) is None
    assert world.bash(f"rm -rf {world.out}") is None


def test_extra_write_directories_and_the_temp_directory_are_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert world.tool("Write", str(world.tmp / "x")) is None
    assert denied(world.tool("Write", str(world.shared / "x")))
    monkeypatch.setenv("FLEET_ALLOW_WRITE", str(world.shared))
    assert world.tool("Write", str(world.shared / "x")) is None


# --- Bash: ways to write outside, all refused -----------------------------------------------------

ESCAPES = [
    "echo hi > {out}/x",
    "echo hi >> {out}/x",
    "echo hi 2> {out}/x",
    "echo hi &> {out}/x",
    "echo hi >| {out}/x",
    "echo hi | tee {out}/x",
    "echo hi | tee -a {out}/x",
    "cp a {out}/b",
    "cp -t {out} a",
    "cp --target-directory={out} a",
    "mv a {out}/b",
    "mv {out}/a b",
    "install -m 644 a {out}/b",
    "ln -s a {out}/b",
    "rsync -a ./ {out}/",
    "rm {out}/x",
    "rm -rf {out}",
    "rm -rf /",
    "rmdir {out}",
    "touch {out}/x",
    "mkdir -p {out}/d",
    "truncate -s 0 {out}/x",
    "chmod 777 {out}/x",
    "sed -i s/a/b/ {out}/f",
    "sed -i -e s/a/b/ {out}/f",
    "sed --in-place=.bak s/a/b/ {out}/f",
    "dd if=a of={out}/b",
    "tar -xf a.tar -C {out}",
    "tar xf a.tar -C {out}",
    "unzip a.zip -d {out}",
    "curl -o {out}/x http://example.test",
    "curl --output {out}/x http://example.test",
    "wget -O {out}/x http://example.test",
    "git -C {out} commit -m x",
    "git clone http://example.test/r.git {out}/r",
    "git worktree add {out}/wt",
    "find {out} -delete",
    "find . -fprint {out}/list",
]
EVASIONS = [  # the same writes, dressed up
    "cd {out} && touch x",
    "cd {out}; touch x",
    "cd .. && touch x",
    "cd {out}\ntouch x",
    "(cd {out} && touch x)",
    "true || touch {out}/x",
    "true & touch {out}/x",
    "sudo rm {out}/x",
    "sudo -u nobody rm {out}/x",
    "env FOO=1 rm {out}/x",
    "FOO=1 rm {out}/x",
    "nohup rm {out}/x",
    "timeout 5 rm {out}/x",
    "time rm {out}/x",
    "command rm {out}/x",
    "/bin/rm {out}/x",
    "bash -c 'touch {out}/x'",
    "bash -lc 'touch {out}/x'",
    'sh -c "touch {out}/x"',
    "bash -c 'bash -c \"touch {out}/x\"'",
    "eval 'touch {out}/x'",
    "eval touch {out}/x",
    "echo $(touch {out}/x)",
    "echo `touch {out}/x`",
    "echo ok; echo $(touch {out}/x)",
    "find . -exec cp {{}} {out}/ ';'",
    "find . -name x -exec rm {out}/x ';'",
    "xargs rm",
    "ls | xargs -I{{}} mv {{}} {out}/",
    "cat <<EOF > {out}/x\nbody\nEOF",
    "cat <<'EOF' | tee {out}/x\nbody\nEOF",
    "touch ok {out}/x",
    "touch {out}/x ok",
    "touch -- {out}/x",
    "touch $HOME/x",
    "touch ${{HOME}}/x",
    "touch ~/x",
    "touch {{a,b}}",
    "touch {out}/{{a,b}}",
    "echo hi > $UNSET/x",
    "echo hi > >(cat)",
    "cd $UNSET && touch x",
    "echo 'unterminated > {out}/x",
    "touch ../other-clone/x",
    "touch ./sub/../../other-clone/x",
    "rm -rf sub/*/../../../other-clone",
    "echo $(echo $(touch {out}/x))",
]


@pytest.mark.parametrize("command", ESCAPES + EVASIONS)
def test_a_write_outside_the_clone_is_refused(world: World, command: str) -> None:
    assert denied(world.bash(command.format(out=world.out))), command


# --- Bash: ordinary agent work is not refused -----------------------------------------------------

FINE = [
    "echo hi > out.txt",
    "echo hi >> sub/out.txt",
    "ls 2>&1 | head",
    "ls > /dev/null 2>&1",
    "cat f > /dev/null",
    "git status && git diff",
    "git commit -m 'fix: a > b'",
    "git -C . log",
    "git clone http://example.test/r.git here",
    "cp a b",
    "cp -r sub other",
    "mv a sub/b",
    "rm -rf build dist",
    "rm -rf ./build/*",
    "touch a b c",
    "mkdir -p sub/deep/er",
    "chmod +x run.sh",
    "sed -n 1,5p f",
    "sed -i s/a/b/ f",
    "grep -r 'a > b' .",
    "uv run pytest -q",
    "bun test",
    "echo $(git rev-parse HEAD)",
    "x=$(git rev-parse HEAD); echo $x",
    "cd sub && touch x",
    "cd sub; cd ..; touch x",
    "bash -c 'touch x'",
    "find . -name '*.py'",
    "find . -name '*.pyc' -delete",
    "tar -tf a.tar",
    "tar -czf out.tgz sub",
    "tar -xf a.tar -C sub",
    "curl -s http://example.test",
    "curl -o x.json http://example.test",
    "wget -O - http://example.test",
    "cat <<EOF > f\nrm -rf {out}\n> {out}/x\nEOF",
    "echo 'a > b'",
    "sort < in > out.txt",
    "echo {tmp}/x > {tmp}/y",
    "FOO=1 uv run pytest",
]


@pytest.mark.parametrize("command", FINE)
def test_ordinary_work_inside_the_clone_is_not_refused(world: World, command: str) -> None:
    assert world.bash(command.format(out=world.out, tmp=world.tmp.as_posix())) is None, command


def test_cd_back_inside_makes_relative_writes_fine_again(world: World) -> None:
    assert world.bash(f"cd {world.out} && cd {world.clone.as_posix()} && touch x") is None


def test_the_refusal_says_where_to_work(world: World) -> None:
    reason = world.bash(f"touch {world.out}/x")["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
    assert "outside this agent's clone" in reason and str(world.clone) in reason


# --- KNOWN LIMITS: what a hook that reads command lines cannot see --------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"open('{out}/x','w').write('')\"",
        "node -e \"require('fs').writeFileSync('{out}/x','')\"",
        "perl -i -pe 's/a/b/' {out}/f",
        "awk -i inplace '{{print}}' {out}/f",
        "make clean",
    ],
)
def test_known_limit_programs_that_write_for_themselves_are_not_seen(
    world: World, command: str
) -> None:
    assert world.bash(command.format(out=world.out)) is None


# --- the hook as the plugin runs it ---------------------------------------------------------------


def run_hook(payload: object, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "write_guard.py")],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=30,
    )


def test_run_as_a_process_it_prints_a_deny_decision(world: World) -> None:
    done = run_hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(world.other / "x")},
            "cwd": str(world.clone),
        },
        {"FLEET_CLONE": str(world.clone), **world.temp_env},
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_as_a_process_it_fails_closed_on_garbage_input(world: World) -> None:
    done = run_hook("{not json", {"FLEET_CLONE": str(world.clone), **world.temp_env})
    assert done.returncode == 0
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_as_a_process_it_says_nothing_for_an_allowed_call(world: World) -> None:
    done = run_hook(
        {"tool_name": "Write", "tool_input": {"file_path": "a.py"}, "cwd": str(world.clone)},
        {"FLEET_CLONE": str(world.clone), **world.temp_env},
    )
    assert done.returncode == 0 and done.stdout == ""
