#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
# Vendored from https://github.com/alexander-marquardt/agent-fleet-kit (MIT, (c) Alexander Marquardt).
# Changes: this header and the CONTEXT_CEILING_MAIN switch. See NOTICE.md.
"""PreToolUse hook: a sub-agent hands off before its context gets expensive.

Every model call re-sends the whole conversation, so a long-lived agent pays
for its context again on every call. Past a certain size it is cheaper to end
the agent with a short written handoff and start a fresh one than to keep
carrying everything it has read. A line in the prompt asking for this is a rule
the agent has to remember; this hook makes it deterministic.

For a sub-agent (the hook input carries ``agent_id``) it reads the agent's
current context size and:

* below ``ADVISORY_TOKENS``: says nothing;
* at or above ``ADVISORY_TOKENS``: allows the call and, once per agent, tells it
  to finish at its next commit: push, write the handoff, end;
* at or above ``CEILING_TOKENS``: refuses every tool call except a Bash command
  made only of ``git`` and ``gh`` statements, which is what a handoff needs.

Current context is the latest assistant record's ``usage`` in the sub-agent's
own transcript: ``input_tokens + cache_read_input_tokens +
cache_creation_input_tokens``, the size of the prompt that call sent.

The hook guards cost, not safety, so anything it cannot read allows the call.
That includes a new sub-agent's first tool call, which can run before Claude
Code has written the agent's first reply to its transcript; the ceiling only
matters many calls later, so nothing is lost.
Thresholds can be set with ``CONTEXT_ADVISORY_TOKENS`` and
``CONTEXT_CEILING_TOKENS``.

The main session is left alone unless ``CONTEXT_CEILING_MAIN=1``. Then it is
judged the same way, from the session's own transcript. Set it where each agent
is a main session of its own, as a headless ``claude -p`` run is.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

ADVISORY_TOKENS = int(os.environ.get("CONTEXT_ADVISORY_TOKENS", "150000"))
CEILING_TOKENS = int(os.environ.get("CONTEXT_CEILING_TOKENS", "200000"))
TAIL_BYTES = 512 * 1024
HANDOFF_COMMANDS = {"git", "gh"}
SEPARATORS = {"&&", "||", ";"}
MARKER_DIR = Path(os.environ.get("CONTEXT_MARKER_DIR", tempfile.gettempdir()))
JUDGE_MAIN = os.environ.get("CONTEXT_CEILING_MAIN") == "1"

HANDOFF_STEPS = (
    "Hand off now: commit and push your work, post a short handoff (what is done, "
    "what is left, the next command) where the next agent will read it, then end. "
    "A fresh agent that reads the handoff costs less than carrying this context."
)


def transcript_for(payload: dict) -> Path | None:
    """The sub-agent's own transcript; for the main session, its transcript
    when ``JUDGE_MAIN`` is set, else None.

    ``transcript_path`` in the hook input is the SESSION's transcript; a
    sub-agent's records are in ``<session>/subagents/agent-<agent_id>.jsonl``.
    """
    if payload.get("agent_transcript_path"):
        return Path(payload["agent_transcript_path"])
    agent_id, session = payload.get("agent_id"), payload.get("transcript_path")
    if not session:
        return None
    if not agent_id:
        return Path(session) if JUDGE_MAIN else None
    session = Path(session)
    return session.parent / session.stem / "subagents" / f"agent-{agent_id}.jsonl"


def current_context(path: Path) -> int | None:
    """Tokens in the prompt of the agent's latest call, read from the file's end."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        lines = f.read().decode("utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if record.get("type") == "assistant" and isinstance(usage, dict):
            return sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
    return None


def is_handoff_command(command: str) -> bool:
    """True when every statement of *command* starts with ``git`` or ``gh``.

    Pipes, redirections and command substitution are refused, so the command
    cannot read files through another program. It is a cost guard, not a
    sandbox: ``git show`` can still read.
    """
    if any(token in command for token in ("$(", "`", ">", "<", "|&")):
        return False
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return False
    at_start, statements = True, 0
    for word in words:
        if word in SEPARATORS:
            at_start = True
        elif set(word) <= set(";&|"):
            return False  # a pipe, a background '&', or ';;'
        elif at_start:
            if word not in HANDOFF_COMMANDS:
                return False
            at_start, statements = False, statements + 1
    return statements > 0


def _who(payload: dict) -> str:
    """The key the advisory is said once for: the sub-agent, or the main session."""
    if payload.get("agent_id"):
        return str(payload["agent_id"])
    return f"main-{payload.get('session_id') or Path(str(payload.get('transcript_path'))).stem}"


def _say_once(agent_id: str) -> bool:
    """True the first time it is called for *agent_id*."""
    marker = MARKER_DIR / f"context-advisory-{agent_id}"
    try:
        marker.touch(exist_ok=False)
        return True
    except OSError:
        return False


def decide(payload: dict) -> dict | None:
    """The hook's output for one tool call, or None to allow it silently."""
    path = transcript_for(payload)
    if path is None or not path.is_file():
        return None
    context = current_context(path)
    if context is None:
        return None
    if context >= CEILING_TOKENS:
        tool_input = payload.get("tool_input") or {}
        if payload.get("tool_name") == "Bash" and is_handoff_command(str(tool_input.get("command", ""))):
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Context is {context:,} tokens, past the {CEILING_TOKENS:,} ceiling. "
                    f"Only git and gh commands run now. {HANDOFF_STEPS}"
                ),
            }
        }
    if context >= ADVISORY_TOKENS and _say_once(_who(payload)):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"Context is {context:,} tokens. At your next commit, stop there. "
                    f"{HANDOFF_STEPS} At {CEILING_TOKENS:,} tokens only git and gh will run."
                ),
            }
        }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        out = decide(payload) if isinstance(payload, dict) else None
    except Exception as exc:  # fail open: a cost guard never blocks on its own bug
        print(f"context_ceiling: {exc}", file=sys.stderr)
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
