# Vendored from https://github.com/alexander-marquardt/agent-fleet-kit (MIT). Unchanged.
"""Tests on fixed transcripts and hook inputs. Run: python3 -m unittest discover tests"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "scripts"))

import context_ceiling  # noqa: E402
import session_tokens  # noqa: E402
import wait_cap  # noqa: E402


def assistant(request_id: str, context: int, output: int = 100) -> str:
    """One assistant record whose prompt was *context* tokens."""
    usage = {
        "input_tokens": 2,
        "cache_read_input_tokens": context - 1002,
        "cache_creation_input_tokens": 1000,
        "output_tokens": output,
    }
    return json.dumps({"type": "assistant", "requestId": request_id, "message": {"role": "assistant", "usage": usage}})


class ContextCeiling(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        context_ceiling.MARKER_DIR = self.tmp
        self.session = self.tmp / "abc.jsonl"
        self.session.write_text(assistant("main", 500_000) + "\n")
        self.agent = self.tmp / "abc" / "subagents" / "agent-a1.jsonl"
        self.agent.parent.mkdir(parents=True)

    def payload(self, context: int | None, command: str = "ls", tool: str = "Bash", agent: str | None = "a1") -> dict:
        if context is not None:
            self.agent.write_text(assistant("r1", 50_000) + "\nnot json\n" + assistant("r2", context) + "\n")
        p = {"transcript_path": str(self.session), "tool_name": tool, "tool_input": {"command": command}}
        if agent:
            p["agent_id"] = agent
        return p

    def test_main_session_is_never_judged(self) -> None:
        self.assertIsNone(context_ceiling.decide(self.payload(None, agent=None)))

    def test_below_advisory_is_silent(self) -> None:
        self.assertIsNone(context_ceiling.decide(self.payload(120_000)))

    def test_advisory_is_said_once(self) -> None:
        first = context_ceiling.decide(self.payload(160_000))
        self.assertIn("additionalContext", first["hookSpecificOutput"])
        self.assertIsNone(context_ceiling.decide(self.payload(170_000)))

    def test_ceiling_refuses_other_tools(self) -> None:
        for command, tool in (("ls", "Bash"), ("cat notes.md", "Bash"), ("", "Read")):
            out = context_ceiling.decide(self.payload(210_000, command, tool))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", command)

    def test_ceiling_allows_a_handoff(self) -> None:
        command = 'git add -A && git commit -m "wip: parser" && git push origin HEAD\ngh pr comment 12 --body "done: a; left: b"'
        self.assertIsNone(context_ceiling.decide(self.payload(210_000, command)))

    def test_ceiling_refuses_disguised_reads(self) -> None:
        for command in ("git status | cat big.log", "git log > x; cat x", "git show $(cat f)", "gh pr view 1 & cat f"):
            self.assertFalse(context_ceiling.is_handoff_command(command), command)

    def test_missing_transcript_allows(self) -> None:
        self.assertIsNone(context_ceiling.decide(self.payload(None)))

    def test_hook_runs_as_a_program(self) -> None:
        payload = json.dumps(self.payload(210_000))
        env = {**os.environ, "CONTEXT_MARKER_DIR": str(self.tmp)}
        run = subprocess.run([sys.executable, str(ROOT / "hooks/context_ceiling.py")], input=payload,
                             capture_output=True, text=True, env=env, check=True)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class WaitCap(unittest.TestCase):
    def call(self, **tool_input) -> dict | None:
        return wait_cap.decide({"agent_id": "a1", "tool_name": "Bash", "tool_input": tool_input})

    def test_long_timeout_is_refused(self) -> None:
        self.assertEqual(self.call(command="pytest", timeout=600_000)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_long_sleep_is_refused(self) -> None:
        for command in ("sleep 600", "make && sleep 5m", "(sleep 300; echo done)"):
            self.assertIsNotNone(self.call(command=command), command)

    def test_short_waits_and_background_pass(self) -> None:
        self.assertIsNone(self.call(command="sleep 60", timeout=120_000))
        self.assertIsNone(self.call(command="sleep 900", timeout=1_000_000, run_in_background=True))
        self.assertIsNone(self.call(command="echo sleep 900 is a phrase"))

    def test_main_session_passes_by_default(self) -> None:
        self.assertIsNone(wait_cap.decide({"tool_name": "Bash", "tool_input": {"command": "sleep 900"}}))


class SessionTokens(unittest.TestCase):
    def test_each_request_counts_once(self) -> None:
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        same = assistant("r1", 10_000, output=50)
        path.write_text("\n".join([same, same, assistant("r2", 20_000, output=70), "{broken"]) + "\n")
        requests, totals = session_tokens.session_totals(str(path))
        self.assertEqual(requests, 2)
        self.assertEqual(totals["output_tokens"], 120)
        self.assertEqual(totals["cache_creation_input_tokens"], 2_000)


if __name__ == "__main__":
    unittest.main()
