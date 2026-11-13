#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""What a session's first model call carried, read from its transcript.

Claude Code records the context it sends before the first reply: the system prompt, SessionStart
hook output (cc-practices' core rules), CLAUDE.md and other instruction files, the skill, agent,
MCP and deferred-tool listings, and the first user message. This prints each part with its size,
largest first, so you can see what every agent pays for on every call. ``--show`` prints the
text of the parts whose name contains a word, for example ``--show SessionStart``.

    uv run --script scripts/first_context.py --project commitment-tracker-ai
    uv run --script scripts/first_context.py --project commitment-tracker-ai --show SessionStart
    uv run --script scripts/first_context.py --project commitment-tracker-ai --subagent
    uv run --script scripts/first_context.py path/to/session.jsonl --json

Transcripts hold everything the agent saw: treat the output as sensitive.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 4  # rough English average; good enough to rank the parts
# Records of slash commands typed before the first prompt (/model, /plugin); not sent as a prompt.
SLASH_COMMAND_ECHOES = ("<local-command", "<command-")
NOT_RECORDED = (
    "Not in the transcript: tool definitions. Compare the total with the first reply's input,\n"
    "cache-read and cache-write tokens (fleet_report.py's 1st-turn columns) to see their size."
)


@dataclass(frozen=True)
class Part:
    name: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return str(value["text"])
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _attachment_parts(att: dict[str, Any]) -> list[Part]:
    kind = str(att.get("type", "?"))
    if kind == "prompt_snapshot":
        return [Part("system prompt", _text(att.get("systemPrompt")))]
    # SessionStart output arrives as hook_success; SubagentStart context as hook_additional_context.
    if kind in {"hook_success", "hook_additional_context"}:
        return [Part(f"hook {att.get('hookName', '?')}", _text(att.get("content")))]
    if kind == "instructions":
        files = att.get("files") or []
        return [
            Part(f"instructions {f.get('type', '?')} {f.get('path', '?')}", _text(f.get("content")))
            for f in files
            if isinstance(f, dict)
        ]
    if kind == "skill_listing":
        return [Part("skill listing", _text(att.get("content")))]
    for key in ("addedLines", "addedBlocks", "text", "content"):
        if att.get(key):
            return [Part(kind, _text(att[key]))]
    rest = {k: v for k, v in att.items() if k != "type"}
    return [Part(kind, json.dumps(rest, ensure_ascii=False))]


def first_context(path: Path) -> list[Part]:
    """The parts recorded before the transcript's first assistant reply."""
    parts: list[Part] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "assistant":
                break
            att = record.get("attachment")
            if isinstance(att, dict):
                parts.extend(_attachment_parts(att))
            elif record.get("type") == "user" and not record.get("isMeta"):
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                text = _text(content)
                if text and not text.startswith(SLASH_COMMAND_ECHOES):
                    parts.append(Part("user message", text))
    return parts


def newest_transcript(root: Path, project: str, subagent: bool = False) -> Path | None:
    """The newest transcript in the project folders matching `project`: a main session's, or with
    `subagent` a sub-agent's (kept in `<session>/subagents/`)."""
    needle = project.replace("\\", "-").replace("/", "-").replace(":", "-").lower()
    folders = [p for p in root.iterdir() if p.is_dir() and p.name.lower().endswith(needle)]
    pattern = "*/subagents/*.jsonl" if subagent else "*.jsonl"
    files = [f for folder in folders for f in folder.glob(pattern)]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def render(path: Path, parts: Iterable[Part]) -> str:
    ordered = sorted(parts, key=lambda p: p.chars, reverse=True)
    total = sum(p.chars for p in ordered)
    lines = [
        f"First context of {path.name} ({total:,} chars, ~{total // CHARS_PER_TOKEN:,} tokens)",
        "",
        "| Part | Chars | ~Tokens | Share |",
        "|---|---:|---:|---:|",
    ]
    for part in ordered:
        share = part.chars / total if total else 0.0
        tokens = part.chars // CHARS_PER_TOKEN
        lines.append(f"| {part.name} | {part.chars:,} | {tokens:,} | {share:.0%} |")
    return "\n".join([*lines, "", NOT_RECORDED])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("transcript", nargs="?", help="a .jsonl transcript (default: newest)")
    parser.add_argument("--project", help="pick the newest transcript of this project folder")
    parser.add_argument(
        "--subagent", action="store_true", help="with --project, the newest sub-agent transcript"
    )
    parser.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    parser.add_argument("--show", action="append", default=[], help="print parts matching this")
    parser.add_argument("--json", action="store_true", help="print the parts as JSON")
    args = parser.parse_args(argv)

    if args.transcript:
        path: Path | None = Path(args.transcript)
    elif args.project:
        path = newest_transcript(Path(args.root), args.project, args.subagent)
    else:
        parser.error("give a transcript path or --project")
    if path is None or not path.is_file():
        print(f"no transcript found ({args.transcript or args.project})", file=sys.stderr)
        return 1

    parts = first_context(path)
    if not parts:
        print(f"{path}: nothing recorded before the first reply", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(p) | {"chars": p.chars} for p in parts], indent=2))
        return 0
    print(render(path, parts))
    for word in args.show:
        for part in parts:
            if word.lower() in part.name.lower():
                print(f"\n=== {part.name} ===\n{part.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
