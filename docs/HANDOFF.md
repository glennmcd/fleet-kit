# Handoff: align or combine fleet-kit and cc-practices-plugin

Written 2026-11-11 at the end of a claude.ai session. Read this first, then delete it once its items are done.

## Where things stand

- `fleet-kit` (this repo) is round 1 of rebuilding Alexander Marquardt's agent-fleet building blocks as a
  Claude Code plugin. **Not yet a git repo**: run `git init` and make the first commit.
- Contents: his `context_ceiling.py` and `wait_cap.py` hooks and `session_tokens.py`, vendored unchanged
  apart from a PEP 723 header (see NOTICE.md); new `scripts/fleet_report.py` (tokens per week, per merged
  PR and per changed line, price-weighted, baseline index, cache-expiry share, context distribution,
  first-turn cost). 30 unittest tests pass on Python 3.10; its totals match `session_tokens.py`.
- Not yet run: ruff, mypy (no network where it was built), and the report on real transcripts. Take a
  baseline on commitment-tracker-ai before any fleet runs.
- Reference: the "Agent Fleet Field Guide" Claude Doc (claude.ai artifact 8wqP89FKXQt1uJNGrTCSm1) and
  Marquardt's two articles saved in `Documents/Work/AI/`.
- Target setup: a cloud VM running about a dozen agents. Paid model runs are started by Glenn, not by Claude.

## Conflicts found between the two plugins

Breaks a fleet:
1. `guard-commit` (cc-practices, default `allowAgentCommits: false`) blocks `git commit`; past 200k tokens
   `context_ceiling` allows only git/gh and tells the agent to commit and push. Together a sub-agent is
   stuck and its work is lost. Options: `allowAgentCommits: true` in agent clones, or have guard-commit skip
   calls whose hook input has `agent_id` (sub-agents).
2. Steps that wait for a person stall unattended agents: `tdd-change` step 0 (wait for approval),
   `common.md` "ask before adding a dependency", and `compactOutput`'s `ask` decision. Proposed: an issue
   with a *Done when* list is the approval; anything else needing Glenn becomes a handoff and the agent
   ends; `compactOutput` off on the VM.

Tensions:
3. SessionStart loads ~4.5 KB of practices into every session; Marquardt caps the always-loaded guide at
   ~2 KB with topic files read on demand. Unverified: whether sub-agents receive SessionStart output, and
   whether it lands after the cache breakpoint (watch fleet_report's 1st-turn write column).
4. Tests: cc-practices says fakes and HTTP mocks; Marquardt says recorded real responses as fixtures.
   Reconcile as fakes fed recorded responses.
5. `code-reviewer` is cooperative (author-invoked, "do not invent issues", no verdict); his reviewer is
   independent, adversarial, and must approve at the PR head commit. Extend it rather than add a second.
6. Most cc-practices rules are prose; his rule is that checkable rules become gates, each citing its incident.

fleet-kit breaking cc-practices rules (fix in fleet-kit):
- `requires-python >=3.10` / ruff `py310` (python.md says 3.12+); no `uv.lock`; CI uses setup-python +
  unittest instead of `uv sync --locked`; CI actions not SHA-pinned, no `permissions:` block.

Cost note: with both plugins, each Bash call starts four `uv run --script` processes.

## Suggested split (not yet decided)

cc-practices keeps the general practices (rules, skills, reviewer, commit guard); fleet-kit holds only
what a fleet needs (token hooks, measurement, CI gates, control plane). Alternative: merge into one plugin
with a `fleet` config switch in `.claude/cc-practices.json`.

## Next

1. Decide split vs merge.
2. Fix conflicts 1 and 2 and the fleet-kit rule breaks.
3. Then round 2 of the roadmap (rules layer: byte-capped CLAUDE.md, topic files with incident numbers,
   rule delivery classes, code maps, handoff template).
