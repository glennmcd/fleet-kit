"""Tests for scripts/first_context.py, on transcripts written to a temp folder."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import first_context as fc  # noqa: E402


def att(**fields: Any) -> dict[str, Any]:
    return {"type": "attachment", "attachment": fields}


def user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


RECORDS: list[dict[str, Any]] = [
    {"type": "permission-mode"},
    att(type="hook_success", hookName="SessionStart:startup", content="# Shared practices\n- x"),
    user("<command-name>/model</command-name>"),
    user("<local-command-stdout>Set model</local-command-stdout>"),
    user("implement the issue"),
    att(type="skill_listing", content="- a: " + "s" * 200),
    att(
        type="instructions",
        files=[{"path": "C:/p/CLAUDE.md", "type": "Project", "content": "rule"}],
    ),
    att(type="deferred_tools_delta", addedLines=["Cron", "Monitor"]),
    att(type="date", date="2026-11-12"),
    att(type="prompt_snapshot", systemPrompt=["You are", " an agent"]),
    {"type": "assistant", "message": {"content": "after the first reply"}},
    att(type="hook_success", hookName="PostToolUse", content="LATER"),
]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def transcript(self, rel: str, records: list[dict[str, Any]] = RECORDS) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in records]
        lines.insert(2, "not json")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class FirstContext(Fixture):
    def test_parts_before_the_first_reply(self) -> None:
        parts = {p.name: p.text for p in fc.first_context(self.transcript("p/s.jsonl"))}
        self.assertEqual(parts["hook SessionStart:startup"], "# Shared practices\n- x")
        self.assertEqual(parts["instructions Project C:/p/CLAUDE.md"], "rule")
        self.assertEqual(parts["deferred_tools_delta"], "Cron\nMonitor")
        self.assertEqual(parts["system prompt"], "You are\n an agent")
        self.assertEqual(parts["user message"], "implement the issue")
        self.assertIn("2026-11-12", parts["date"])
        self.assertTrue(parts["skill listing"].startswith("- a: "))
        self.assertNotIn("hook PostToolUse", parts)

    def test_slash_command_echoes_are_not_user_messages(self) -> None:
        parts = fc.first_context(self.transcript("p/s.jsonl"))
        self.assertEqual(
            [p.text for p in parts if p.name == "user message"], ["implement the issue"]
        )


class NewestTranscript(Fixture):
    def test_newest_main_transcript_of_the_project(self) -> None:
        old = self.transcript("C--git-my-app/old.jsonl")
        new = self.transcript("C--git-my-app/new.jsonl")
        self.transcript("C--git-my-app/new/subagents/agent-x.jsonl")
        self.transcript("C--git-other/newer.jsonl")
        os.utime(old, (1, 1))
        self.assertEqual(fc.newest_transcript(self.root, "my-app"), new)
        self.assertIsNone(fc.newest_transcript(self.root, "missing"))


class Cli(Fixture):
    def run_main(self, args: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fc.main(args)
        return code, out.getvalue() + err.getvalue()

    def test_table_is_largest_first_and_show_prints_text(self) -> None:
        self.transcript("C--git-my-app/s.jsonl")
        code, out = self.run_main(
            ["--root", str(self.root), "--project", "my-app", "--show", "start"]
        )
        self.assertEqual(code, 0)
        rows = [line for line in out.splitlines() if line.startswith("| ") and "Part" not in line]
        self.assertTrue(rows[0].startswith("| skill listing |"))
        self.assertIn("=== hook SessionStart:startup ===\n# Shared practices", out)
        self.assertIn("tool definitions", out)

    def test_json_output(self) -> None:
        path = self.transcript("p/s.jsonl")
        code, out = self.run_main([str(path), "--json"])
        self.assertEqual(code, 0)
        self.assertIn(
            {"name": "date", "text": '{"date": "2026-11-12"}', "chars": 22}, json.loads(out)
        )

    def test_missing_transcript_fails_cleanly(self) -> None:
        code, out = self.run_main(["--root", str(self.root), "--project", "nothing"])
        self.assertEqual(code, 1)
        self.assertIn("no transcript found", out)

    def test_transcript_without_context_fails_cleanly(self) -> None:
        path = self.transcript("p/empty.jsonl", [{"type": "assistant"}])
        self.assertEqual(self.run_main([str(path)])[0], 1)


if __name__ == "__main__":
    unittest.main()
