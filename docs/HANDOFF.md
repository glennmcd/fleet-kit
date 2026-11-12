# Handoff: align fleet-kit and cc-practices-plugin

First written 2026-11-11; updated 2026-11-12. Delete it once the open items below are done or moved to issues.

## Decided

- **Split, not merge.** cc-practices keeps the general practices (rules, skills, reviewer, commit guard);
  fleet-kit holds only what a fleet needs (token hooks, measurement, CI gates, control plane).
- **Interface: `CC_PRACTICES_UNATTENDED=1`**, exported where the VM launches agents. Rejected: skipping
  guard-commit on `agent_id` (main sessions on the VM are unattended too) and a committed config key
  (the same clone runs attended on the laptop).

## Done

- Conflict 1 (guard-commit vs context_ceiling): fixed in cc-practices `47232f0` (v0.2.0). Unattended
  sessions may commit, so a sub-agent past 200k can commit, push and hand off.
- Conflict 2 (steps that wait for a person): same commit. New `practices/unattended.md`: an issue with a
  *Done when* list is the approval; anything else needing Glenn becomes a handoff comment of 30 lines or
  fewer and the agent ends. tdd-change steps 0 and 5 and the dependency and commit rules defer to it;
  `compactOutput` is skipped (its `ask` would wait for a person).
- fleet-kit rule breaks: fixed in `e23b96c` (Python 3.12, `uv.lock`, CI on `uv sync --locked`, SHA-pinned
  actions, `permissions: contents: read`). `git init` done.
- fleet_report: `0d3c670` labels the share column `Ctx >=150k` (it counts calls at or over `--ceiling`,
  default the 150k advisory level) and skips `<synthetic>` records.
- Baseline on commitment-tracker-ai: branch `docs/token-baseline` (pushed, no PR), `docs/metrics/`.
  About 870 weighted tokens per changed line; one branch (part3plan) used about 60% of tokens; W43 had
  60% of calls at 150k+ context; cache expiry low. The whole first prompt is 80-84k tokens.

## Open

1. **Round 2 of the roadmap**: byte-capped CLAUDE.md, topic files with incident numbers, rule delivery
   classes, code maps, handoff template.
2. **Always-loaded size (tension 3).** Practices are ~4.5 KB per session (+~700 B unattended); Marquardt
   caps the guide at ~2 KB. Found: SessionStart output lands as a message block after the system prompt.
   Bigger: the skill listing is ~30k chars per session, ~19k of it the 25 AWS skills in
   `~/.agents/skills`; keep them off the VM. Still unchecked: whether sub-agents receive SessionStart.
   commitment-tracker-ai's CLAUDE.md grew 3,438 to 8,689 chars on 2026-10-23.
3. **Tests (tension 4)**: reconcile as fakes fed recorded real responses.
4. **Reviewer (tension 5)**: extend `code-reviewer` with an adversarial mode that gives a verdict at the
   PR head commit, rather than adding a second reviewer.
5. **Rules to gates (tension 6)**: candidates ruff `S` and `T20`, a duplicate-sentence check.
6. **Bash hook cost**: with both plugins each Bash call starts four `uv run --script` processes.
7. **compactOutput unattended**: on the VM use rtk's own hook (`rtk hook claude`) instead, or have
   compact-output mirror rtk's exit code (0 allow, 3 ask) and run the command unchanged on 3.

## Housekeeping

- Not pushed: cc-practices is 3 commits ahead of origin; fleet-kit has no remote.
- The installed cc-practices plugin is still 0.1.1 (local scope, commitment-tracker-ai); update it to
  get 0.2.0.
- Temporary worktree `../cta-baseline-tmp` (commitment-tracker-ai) still registered; remove with
  `git worktree remove ../cta-baseline-tmp`.
- Reference: the "Agent Fleet Field Guide" Claude Doc (claude.ai artifact 8wqP89FKXQt1uJNGrTCSm1) and
  Marquardt's two articles in `Documents/Work/AI/`. Paid model runs are started by Glenn, not by Claude.
