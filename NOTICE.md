# Notice

`hooks/context_ceiling.py`, `hooks/wait_cap.py`, `scripts/session_tokens.py` and
`tests/test_vendored_kit.py` are copied from
[agent-fleet-kit](https://github.com/alexander-marquardt/agent-fleet-kit) by Alexander Marquardt,
under the MIT License (full text in `NOTICE-agent-fleet-kit-LICENSE.txt`). Changes:

- a PEP 723 header on the two hooks so they run with `uv run --script`;
- `context_ceiling.py`: a `CONTEXT_CEILING_MAIN=1` switch that judges the main session too, from the
  session's own transcript, with the advisory said once per session. fleetwright starts each author as
  `claude -p`, which makes it the main session (upstream's fleet runs its workers as sub-agents);
- `tests/test_vendored_kit.py`: tests for that switch and for `WAIT_CAP_MAIN=1`.

Everything else is new, written from the descriptions in his articles.
