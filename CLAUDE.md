# fleetwright

## Invariants

The fleet relies on these. A change that breaks one is wrong even if every other test passes. Each names the
test that enforces it; if you change what an invariant covers, change its test in the same commit.

- **Every author agent is held to the context ceiling and the wait cap.** An author runs as `claude -p`, so it
  is the main session, and both hooks skip the main session unless `CONTEXT_CEILING_MAIN=1` and
  `WAIT_CAP_MAIN=1` are set. If you change how agents are started (another launcher, the Agent SDK, sub-agents
  of a coordinator), check that the hooks still judge them. Review slots are not held to them yet: the
  handoff text does not fit a reviewer. Tests: `test_author_unit_holds_the_session_to_the_hooks`
  (`tests/test_plugin.py`), `ContextCeilingMain` and `WaitCap` (`tests/test_vendored_kit.py`).
- **An agent never writes outside its own clone**, and cannot edit its own safety settings inside it. Tests:
  `tests/test_evasion_write_guard.py`.
- **Nothing starts, and every tool call is refused, while the line is stopped.** Tests:
  `tests/test_fleetwright_stop.py`, `test_a_stopped_line_starts_nothing` (`tests/test_fleetwright_agent.py`).
- **Safety hooks fail closed; cost hooks fail open.** `stop_line.py` and `write_guard.py` refuse on their own
  error; `context_ceiling.py` and `wait_cap.py` allow. Never swap these.
- **Secrets stay out of units, logs and the author's environment.** `agent.env` holds the model key and
  nothing else; an author's GitHub token is minted per run and revoked after; the journal masks secrets.
  Tests: `test_an_author_slot_holds_no_github_token_in_its_environment`
  (`tests/test_fleetwright_slot.py`), `test_a_secret_in_a_command_is_masked` and the masking tests beside it
  (`tests/test_fleetwright_stream.py`).
- **The committed units are the generated ones.** Change a unit in `scripts/fleetwright_slot.py`, then
  regenerate `systemd/`; never edit `systemd/*.service` by hand. Test:
  `test_the_committed_units_are_the_generated_ones` (`tests/test_fleetwright_slot.py`).
- **Every script runs with `uv run --script`**, so each carries a PEP 723 header. Test:
  `test_every_script_runs_without_the_projects_environment` (`tests/test_plugin.py`).

## Vendored code

`hooks/context_ceiling.py`, `hooks/wait_cap.py`, `scripts/session_tokens.py` and `tests/test_vendored_kit.py`
come from agent-fleet-kit under the MIT License. They may be changed to meet the invariants above. Each change
is listed in `NOTICE.md` and in the file's header comment, and the copyright notice and
`NOTICE-agent-fleet-kit-LICENSE.txt` stay. Upstream was written for workers that are sub-agents of one
coordinator session; check that assumption whenever fleetwright's setup differs.
