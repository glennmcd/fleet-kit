# Handoff: align fleet-kit and cc-practices-plugin

First written 2026-11-11; updated 2026-11-12. The remaining work now lives in `docs/PLAN.md` here and
in `cc-practices-plugin/docs/PLAN.md`. Delete this file once the housekeeping below is done.

## Decided

- **Split, not merge.** cc-practices keeps the general practices (rules, skills, reviewer, commit guard,
  templates); fleet-kit holds only what a fleet needs (token hooks, measurement, CI gates, control plane).
- **Interface: `CC_PRACTICES_UNATTENDED=1`**, exported where the VM launches agents. Rejected: skipping
  guard-commit on `agent_id` (main sessions on the VM are unattended too) and a committed config key
  (the same clone runs attended on the laptop).
- **Command compaction is rtk's own hook** (`rtk init -g --auto-patch`), not a cc-practices hook: a hook
  in settings runs every time. cc-practices only warns at session start when rtk is installed without it.

## Done

- Conflict 1 (guard-commit vs context_ceiling): cc-practices `47232f0` (0.2.0). Unattended sessions may
  commit, so a sub-agent past 200k can commit, push and hand off.
- Conflict 2 (steps that wait for a person): same commit. `practices/unattended.md`: an issue with a
  *Done when* list is the approval; anything else needing Glenn becomes a handoff comment of 30 lines or
  fewer and the agent ends.
- `compactOutput` removed: cc-practices `96bb750` (0.3.0). rtk's hook returns no permission decision, so
  it works unattended. It honours `deny` rules but not `ask` rules; never allow `Bash(rtk:*)`.
- fleet-kit rule breaks fixed in `e23b96c`; `git init` done; fleet_report fixes in `0d3c670`.
- Baseline on commitment-tracker-ai: branch `docs/token-baseline` (pushed, no PR). About 870 weighted
  tokens per changed line; one branch used about 60% of tokens; W43 had 60% of calls at 150k+ context;
  cache expiry low.

## Housekeeping

- Not pushed: cc-practices is 4 commits ahead of origin; fleet-kit has no remote.
- cc-practices is enabled at local scope in commitment-tracker-ai only; `claude plugin list` shows it
  disabled elsewhere. A directory marketplace reads the plugin folder directly, so commits apply from
  the next session without an update.
- Run `rtk init -g --auto-patch` on the VM, and keep the AWS skills off it.
- Temporary worktree `../cta-baseline-tmp` (commitment-tracker-ai) still registered; remove with
  `git worktree remove ../cta-baseline-tmp`.
- Reference: the "Agent Fleet Field Guide" Claude Doc (claude.ai artifact 8wqP89FKXQt1uJNGrTCSm1) and
  Marquardt's two articles in `Documents/Work/AI/`. Paid model runs are started by Glenn, not by Claude.
