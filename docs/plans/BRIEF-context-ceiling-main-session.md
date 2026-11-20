# Brief: context ceiling and wait cap for author sessions

Status: approved and implemented, 2026-11-19 (authors only; review slots unchanged; `WAIT_CAP_SECS` stays
270 s). The two switches are set in `fleetwright_slot.py`'s author unit and, since a check unit keeps its
slot's environment, in `fleetwright-check-agent@.service` too. The VM checks below are still to be run.

## Problem

`hooks/context_ceiling.py` judges only sub-agents. It finds the transcript to read from `agent_id` in the
hook input, and returns at once when there is none (`transcript_for`, lines 67-69; `decide`, line 136). The
docstring says so: "the main session is never judged", and `test_main_session_is_never_judged` pins it.

`scripts/fleetwright_agent.py` starts every author as `claude -p ...`, so the author is the main session.
The hook never fires for it. An author run can grow past 150k and 200k tokens with no advisory and no
ceiling. Only sub-agents that an author starts itself are covered.

`hooks/wait_cap.py` has the same gap: it judges a Bash call only when the input carries `agent_id` or
`WAIT_CAP_MAIN=1` is set (line 77), and no unit sets it. An author can block in the foreground past the
prompt cache's lifetime, and its next call re-writes the whole context.

Neither gap was decided. Upstream wrote both hooks for a fleet of sub-agents, where "the main session is left
alone" is right. fleetwright vendored them unchanged (`e23b96c`, 2026-11-11), then chose headless `claude -p`
per slot (`a7ec1aa`, 2026-11-12) without coming back to the hooks.

The rest of fleetwright already assumes the opposite:

- `docs/control-plane.md`, allocator step 4: "A `partly done` handoff, which an agent at its context
  ceiling leaves, goes out again as usual." No author is ever made to leave one.
- `docs/control-plane.md`, *Hook cost*: "On a slot it also reads the session's transcript". On a slot it
  exits before it opens any file.
- `README.md`, *With cc-practices*: `CC_PRACTICES_UNATTENDED=1` exists so an agent past the ceiling can
  still commit and hand off.

## Approach

Add a main-session switch to the hook, the same shape as `wait_cap.py`'s `WAIT_CAP_MAIN=1` (which is
upstream's own pattern), and turn it on in the author unit.

1. **`hooks/context_ceiling.py`**
   - `JUDGE_MAIN = os.environ.get("CONTEXT_CEILING_MAIN") == "1"`.
   - `transcript_for`: with no `agent_id`, return `Path(transcript_path)` when `JUDGE_MAIN` is set, else
     `None` as today. Sub-agents are unchanged.
   - `_say_once` key: `agent_id` for a sub-agent, `main-<session_id>` for the main session. Today a
     payload without `agent_id` would key the marker as `"None"`, shared by every session on the machine,
     so the advisory would show once per machine, not once per run.
   - Docstring: describe the switch next to the thresholds.
   - Header comment: "Changes: this header and the `CONTEXT_CEILING_MAIN` switch. See NOTICE.md."
2. **`systemd/fleetwright-agent@.service`**: `Environment=CONTEXT_CEILING_MAIN=1` and
   `Environment=WAIT_CAP_MAIN=1`. `wait_cap.py` needs no code change; the switch is upstream's.
3. **Tests, `tests/test_vendored_kit.py`** (test first):
   - wait cap, main session (no `agent_id`): a foreground `timeout` over the cap is allowed with
     `WAIT_CAP_MAIN` unset and refused with it set;
   - main session, switch off: still `None` (the existing test, kept);
   - main session, switch on: advisory at 160k, once only; deny at 210k; a `git`/`gh` command allowed at 210k;
   - two main sessions with different `session_id`s each get their own advisory;
   - the author unit file sets `CONTEXT_CEILING_MAIN=1` and `WAIT_CAP_MAIN=1` (in whichever test already
     reads the unit files, or a small new one).
4. **Docs**
   - `NOTICE.md`: name the change to `context_ceiling.py` and its test file.
   - `README.md`: the hook's table row ("a sub-agent or, with `CONTEXT_CEILING_MAIN=1`, the session") and
     the thresholds line.
   - `README.md`: the `wait_cap.py` table row, which says "a sub-agent", gets the same "or, with
     `WAIT_CAP_MAIN=1`, the session".
   - `hooks/hooks.json` description: "end sub-agents and author sessions at a context ceiling; never let
     an agent's prompt cache expire while it waits".
   - `docs/control-plane.md`: the *Hook cost* paragraph (on a slot it now reads the session's transcript,
     tail only, at most 512 KB, so the 60 ms is a real lower bound again) and the author section, which
     should say the author is held to the ceiling and the wait cap.

## Not touched

- `wait_cap.py`'s code, the thresholds (150k / 200k, 270 s), the allocator, run caps and the handoff template.
- Review slots (`fleetwright-review@.service`), for both hooks. The handoff text says "commit and push", which
  is wrong for a reviewer; see open question 1.
- Upstream agent-fleet-kit. The change stays local and is recorded in NOTICE.md; offering it upstream is a
  separate step.

## Risks

- **Writing the handoff past the ceiling.** Past 200k only `git`/`gh` run, and `is_handoff_command`
  refuses `<`, so `gh issue comment --body-file` after a `Write`, or a heredoc, is refused. The handoff must
  be posted as `gh issue comment <n> --body "..."`. The advisory at 150k tells the agent to stop at its next
  commit, which leaves room. A test should show a multi-line `gh ... --body "..."` command is allowed.
- **More runs per issue.** An author that used to run on to 300k now ends near 150-200k and the issue
  goes out again. That spends runs from the issue's run cap sooner. This is the intended trade (a fresh run
  is cheaper than carrying the context), but issues near the cap may reach a person earlier.
- **Main-session context is larger from the start.** The author's system prompt, plugin practices and
  CLAUDE.md are counted, which a sub-agent's count mostly is not. Same thresholds, slightly less work per run.
- **Long foreground commands are refused.** With the wait cap on, an author's test suite or build that needs
  a `timeout` over 290 s, or a `sleep` over 270 s, is refused and the author is told to run it with
  `run_in_background` and poll. That costs a few extra calls, and a project whose suite runs longer than the
  cap always goes through the background path. The refusal text says how, so the run does not stall.
- **Transcript path.** For the main session the hook trusts `transcript_path` from Claude Code. If it is
  missing or unreadable the hook allows the call, as it already does.

## Validation

- `uv run pytest tests/test_vendored_kit.py` and the project's full lint, typecheck and test run (`verify`).
- On the fleet VM, one author run with `CONTEXT_ADVISORY_TOKENS` / `CONTEXT_CEILING_TOKENS` set
  low in a drop-in (for example 40k / 60k): the journal shows the advisory, then denials, then a `git push`
  and a `partly done` handoff comment, and the next allocator pass gives the issue out again. In the same
  run, `WAIT_CAP_SECS=30` in the drop-in: a foreground `sleep 60` is refused and the agent moves it to the
  background. The owner runs this; the agent does not touch the VM.
- **Cache lifetime of a `claude -p` author** (before the wait cap goes on, since its 270 s default assumes a
  five-minute cache). Each assistant record's `usage.cache_creation` splits cache writes into
  `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`. On the VM, over a recent author transcript:

  ```bash
  grep -o '"cache_creation":{[^}]*}' <author-transcript>.jsonl | sort | uniq -c | sort -rn | head
  ```

  - Writes under `ephemeral_5m`: the author has the sub-agent lifetime that upstream's cap was built for.
    Keep `WAIT_CAP_MAIN=1` at 270 s.
  - Writes under `ephemeral_1h` (as an interactive desktop session on this machine shows, checked
    2026-11-19): the 270 s cap is stricter than the cache needs. Keep the switch, and decide whether to raise
    `WAIT_CAP_SECS` for author slots; see open question 3.

  `scripts/session_tokens.py` and `fleetwright_report.py` could report the split per run; that is a separate
  change and not in this brief.

## Open questions

1. Hold review slots to the ceiling too? It needs a reviewer-specific handoff text ("post your verdict or
   say what is left"), which is a second change to the vendored hook. Proposed: not now. The wait cap has no
   such problem and could go on reviewers in the same change (one more line in `fleetwright-review@.service`);
   proposed: yes, if you agree.
2. Variable name: `CONTEXT_CEILING_MAIN` (proposed, matches the hook) or `CONTEXT_MAIN`.
3. If authors turn out to get a one-hour cache, raise `WAIT_CAP_SECS` on author slots (for example to 3300)
   or keep 270? Upstream exempts the main session because the coordinator, an interactive session, gets a
   one-hour cache while its sub-agents get five minutes (token-efficiency article, 2026-11-08). A shorter
   cap still costs nothing but a few polling calls. Proposed: keep 270 s until the check above is done, then
   decide.
