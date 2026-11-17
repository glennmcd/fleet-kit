"""The agent's events as journal lines: one per step, no tool output, no secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import fleetwright_stream
import pytest
from fleetwright_stream import mask, summarize_lines

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stream"


def journal(name: str, secrets: tuple[str, ...] = ()) -> list[str]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return summarize_lines(lines, secrets)


# --- recorded runs --------------------------------------------------------------------------------


def test_a_recorded_run_reads_as_steps_with_exit_codes() -> None:
    out = journal("ok-fail-denied.jsonl")
    assert out[0].startswith("start model=claude-opus-5-5 claude=2.1.311 session=")
    assert out[1:7] == [
        "tool Bash: git log --oneline -3",
        "  → ok",
        "tool Bash: git show no-such-ref",
        "  → exit 128",
        "tool Bash: git status",
        "  → denied: This command requires approval",
    ]
    assert out[7] == "say: Results of the three commands:"
    assert out[-1].startswith("done success turns=4 ")
    assert len(out) == 9


def test_a_recorded_denial_and_a_read_show_their_target_not_their_content() -> None:
    out = journal("denied-and-read.jsonl")
    assert "tool Read: /home/agent01/repo/README.md" in out
    assert out.count("  → ok") == 1  # the Read
    assert sum(line.startswith("  → denied: ") for line in out) == 3
    text = "\n".join(out)
    assert "Commitment Tracker AI reads" not in text  # the file's content
    assert "Shared practices" not in text  # the SessionStart hook's output


def test_the_done_line_has_turns_time_cost_and_tokens() -> None:
    done = journal("ok-fail-denied.jsonl")[-1]
    for field in ("turns=", "time=", "cost=$", "in=", "out=", "cache_read=", "cache_write="):
        assert field in done, field


# --- single events --------------------------------------------------------------------------------


def event(**kw: object) -> str:
    return json.dumps(kw)


def assistant(*content: dict[str, object]) -> str:
    return event(type="assistant", message={"content": list(content)})


def result_for(tool_id: str, *, error: bool, content: str) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": content}
    return event(type="user", message={"content": [block]})


@pytest.mark.parametrize(
    ("name", "args", "line"),
    [
        ("Bash", {"command": "pytest -q", "description": "x"}, "tool Bash: pytest -q"),
        ("Read", {"file_path": "/r/a.py"}, "tool Read: /r/a.py"),
        (
            "Edit",
            {"file_path": "/r/a.py", "old_string": "S1", "new_string": "S2"},
            "tool Edit: /r/a.py",
        ),
        ("Write", {"file_path": "/r/b.py", "content": "secret body"}, "tool Write: /r/b.py"),
        ("Grep", {"pattern": "def run", "path": "src"}, "tool Grep: def run in src"),
        ("Glob", {"pattern": "**/*.py"}, "tool Glob: **/*.py"),
        (
            "Task",
            {"description": "review the diff", "prompt": "long"},
            "tool Task: review the diff",
        ),
        ("Skill", {"skill": "cc-practices:review-pr"}, "tool Skill: cc-practices:review-pr"),
        ("TodoWrite", {"todos": [{"content": "a"}]}, "tool TodoWrite"),
    ],
)
def test_a_tool_call_shows_its_target_and_never_its_body(
    name: str, args: dict[str, object], line: str
) -> None:
    out = summarize_lines(
        [assistant({"type": "tool_use", "id": "t1", "name": name, "input": args})]
    )
    assert out == [line]


def test_a_long_or_multiline_command_is_cut_to_one_line() -> None:
    cmd = "python - <<'EOF'\n" + "x = 1\n" * 100 + "EOF"
    out = summarize_lines(
        [assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}})]
    )
    assert len(out) == 1 and "\n" not in out[0]
    assert len(out[0]) <= len("tool Bash: ") + fleetwright_stream.ARG_MAX + 1


def test_a_failed_tool_without_an_exit_code_says_error_and_nothing_more() -> None:
    lines = [
        assistant({"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "/x"}}),
        result_for("t", error=True, content="File does not exist: /x and its whole text"),
    ]
    assert summarize_lines(lines)[-1] == "  → error"


def test_tool_output_is_never_printed() -> None:
    lines = [
        assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "env"}}),
        result_for("t", error=False, content="GH_TOKEN=hunter2hunter2\nPATH=/bin"),
    ]
    assert "hunter2" not in "\n".join(summarize_lines(lines))


def test_what_the_agent_says_is_its_first_line_cut_short() -> None:
    long = "First line " + "x" * 500 + "\nsecond line"
    out = summarize_lines([assistant({"type": "text", "text": "\n\n" + long})])
    assert out == ["say: " + ("First line " + "x" * 500)[: fleetwright_stream.SAY_MAX] + "…"]


def test_thinking_and_empty_text_print_nothing() -> None:
    lines = [assistant({"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "  "})]
    assert summarize_lines(lines) == []


def test_a_failing_hook_is_reported_and_a_passing_one_is_not() -> None:
    ok = event(
        type="system", subtype="hook_response", hook_name="SessionStart:startup", exit_code=0
    )
    bad = event(type="system", subtype="hook_response", hook_name="PreToolUse:Bash", exit_code=2)
    assert summarize_lines([ok, bad]) == ["hook PreToolUse:Bash exit 2"]


def test_an_unfinished_run_says_so() -> None:
    done = event(
        type="result", subtype="error_max_turns", is_error=True, num_turns=200, duration_ms=1
    )
    assert summarize_lines([done])[0].startswith("done error_max_turns turns=200 ")


def test_a_line_that_is_not_json_is_shown_cut_short_and_unknown_events_are_skipped() -> None:
    out = summarize_lines(
        ["Error: something broke " + "y" * 500, event(type="stream_event"), "[1, 2]"]
    )
    assert out[0].startswith("unparsed: Error: something broke ")
    assert len(out[0]) <= len("unparsed: ") + fleetwright_stream.SAY_MAX + 1
    assert out[1:] == ["unparsed: [1, 2]"]


def test_odd_shapes_do_not_crash_the_runner() -> None:
    odd = [
        event(type="assistant"),
        event(type="assistant", message={"content": "text"}),
        event(type="assistant", message={"content": [None, 3, {"type": "tool_use"}]}),
        event(type="user", message={"content": [{"type": "tool_result"}]}),
        event(type="result"),
        event(type="system", subtype="init"),
        event(type="system", subtype="permission_denied"),
    ]
    summarize_lines(odd)


# --- secrets --------------------------------------------------------------------------------------


def test_a_secret_in_a_command_is_masked() -> None:
    cmd = "curl -H 'Authorization: token ghp_abcdefghijklmnop' https://api.github.com"
    line = assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}})
    out = summarize_lines([line], ("ghp_abcdefghijklmnop",))
    assert out == ["tool Bash: curl -H 'Authorization: token ***' https://api.github.com"]


def test_masking_takes_its_secrets_from_the_environment_and_skips_short_values() -> None:
    env = {"GH_TOKEN": "ghp_longenoughvalue", "ANTHROPIC_API_KEY": "ab", "PATH": "/usr/bin"}
    assert fleetwright_stream.secrets_from(env) == ("ghp_longenoughvalue",)
    assert mask("x ghp_longenoughvalue y", ("ghp_longenoughvalue",)) == "x *** y"


@pytest.mark.parametrize(
    "token",
    [
        "ghs_" + "A1b2" * 9,  # an App installation token
        "ghs_123456_eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjIzIn0.c2ln-_x",  # the newer stateless form
        "ghp_" + "Z9" * 18,
        "gho_" + "q" * 36,
        "ghu_" + "q" * 36,
        "ghr_" + "q" * 36,
        "github_pat_11ABCDEFG0_" + "x" * 59,
    ],
)
def test_a_github_token_is_masked_even_when_it_is_not_in_the_environment(token: str) -> None:
    # An author's App token lives in a file, not the environment, and is renewed mid-run.
    cmd = f"echo {token} > /tmp/t && cat $FLEETWRIGHT_TOKEN_FILE"
    line = assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}})
    (out,) = summarize_lines([line])
    assert token not in out and token[4:20] not in out
    assert out == "tool Bash: echo *** > /tmp/t && cat $FLEETWRIGHT_TOKEN_FILE"


def test_words_that_only_start_like_a_token_are_left_alone() -> None:
    assert mask("see ghs_ and ghp_x in the docs", ()) == "see ghs_ and ghp_x in the docs"


def test_a_secret_split_by_the_cut_is_still_masked() -> None:
    secret = "ghp_" + "s" * 40
    cmd = "x" * (fleetwright_stream.ARG_MAX - 10) + secret
    line = assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}})
    out = summarize_lines([line], (secret,))
    assert "ghp_" not in out[0] and "ssss" not in out[0]


# --- the running process --------------------------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import sys, time
    for line in open(sys.argv[1], encoding="utf-8"):
        sys.stdout.write(line)
        sys.stdout.flush()
    if sys.argv[2] == "hang":
        time.sleep(30)
    sys.exit(int(sys.argv[2]) if sys.argv[2] != "hang" else 0)
    """
)


def fake_claude(tmp_path: Path, code: str) -> list[str]:
    script = tmp_path / "fake_claude.py"
    script.write_text(CHILD, encoding="utf-8")
    return [sys.executable, str(script), str(FIXTURES / "ok-fail-denied.jsonl"), code]


@pytest.mark.parametrize("code", ["0", "3"])
def test_the_runner_prints_each_step_and_exits_with_the_agents_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    assert fleetwright_stream.run(fake_claude(tmp_path, code), {}) == int(code)
    out = capsys.readouterr().out.splitlines()
    assert out == journal("ok-fail-denied.jsonl")


def test_a_runner_given_no_program_reports_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert fleetwright_stream.run(["/nonexistent/claude-binary"], {}) == 127
    assert "could not start" in capsys.readouterr().err


@pytest.mark.skipif(
    os.name != "posix", reason="signals and process groups are POSIX; the fleet runs on Linux"
)
def test_a_stopped_unit_ends_the_agent_and_reports_the_signal(tmp_path: Path) -> None:
    # systemd sends SIGTERM to every process in the unit; the runner waits for the agent.
    runner = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(fleetwright_stream.__file__).parent)!r})
        import fleetwright_stream
        sys.exit(fleetwright_stream.run({fake_claude(tmp_path, "hang")!r}, {{}}))
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", runner], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert proc.stdout is not None
    first = proc.stdout.readline()
    assert first.startswith("start ")
    os.kill(-proc.pid, signal.SIGTERM)  # the whole process group, as systemd does
    rest = proc.stdout.read()
    assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    assert "done" in rest or "stopped" in rest
