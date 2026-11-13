# Plan

What is left of the roadmap in the README, rebuilding Marquardt's agent-fleet building blocks round by
round. fleet-kit holds what only a fleet needs (token hooks, measurement, CI gates, control plane);
cc-practices holds the general practices (rules, skills, reviewer, templates). Its plan, including the
rules layer of round 2 and the reviewer of round 4, is `cc-practices-plugin/docs/plans/PLAN.md`.

Round 1 is done: `context_ceiling` and `wait_cap` hooks, `session_tokens.py`, `fleet_report.py`, and a
baseline on commitment-tracker-ai (branch `docs/token-baseline`). Compare every change below against it.

## Round 2: rules layer: done

Most of round 2 is in cc-practices 0.6.0: core rules capped at 2 KB, pointers to topic files on edit,
a handoff template, a Python code map, and a `CLAUDE.md` size test. The `CLAUDE.md` cap moved there
from this plan because it applies to any project, not only a fleet (commitment-tracker-ai's grew from
3,438 to 8,689 bytes on 2026-10-23 and fails it).

The fleet part was measurement. `first_context.py` shows the first call is about 56k tokens from the
CLI and 79-85k from the desktop app (its system prompt is about 25k tokens larger); fleet agents run
from the CLI. The skill listing is capped at about 30k characters: parking the 24 AWS skills
(2026-11-12) cut it only to 23k, because claude.ai-synced skills (`anthropic-skills`,
`product-management`) took the freed room. Listing the skills the VM installs, and keeping claude.ai
sync off it, moved to round 5, since there is no VM setup yet.

## Round 3: CI gates

Checks every agent PR must pass, each citing the incident that caused it:

- File-size cap.
- Copy-paste (duplication) check.
- No silent skips: skipped or disabled tests fail the build unless they name an issue.
- Audit greps for patterns that must not appear.
- Change obligations: a change to X must also change Y (for example, a migration with its model).
- Gate evasion kit: tests that try to slip past each gate, so a weakened gate is caught.

Built: six gates plus a ratchet that refuses to loosen them (the evasion kit is each gate's
`tests/test_evasion_*.py`), run by `.github/workflows/gates.yml`; see `docs/gates.md`. commitment-tracker-ai
calls it from `feat/ci-gates`.

*Open*: fleet-kit is private, so callers need a read-only token (`FLEET_KIT_TOKEN`) and fleet-kit needs
Actions access enabled for the caller; the gates cannot find a renamed-variable copy.

## Round 4: review and issues (fleet part)

- Verdict-at-head merge check: a required status check that passes only when the adversarial
  reviewer (cc-practices) approved the PR's current head commit. A push after approval resets it.

*Done when*: a PR cannot merge without a reviewer verdict on its head SHA.

## Round 5: control plane (VM)

- VM setup that lists the skills it installs and keeps claude.ai skill sync off (from round 2).
- One clone per agent.
- Write-outside-clone hook: block edits outside the agent's own clone.
- Slot and lane allocator: which agent works on what, so two agents never touch the same files.
- Watchdog: notice stalled or looping agents and stop them.
- Stop the line: one switch that halts every agent.

*Done when*: about a dozen agents run on the VM with `CC_PRACTICES_UNATTENDED=1` and rtk's hook, each in
its own clone, and the watchdog and stop switch have been exercised.

## Also open

- Hook cost: with cc-practices installed, each Bash call starts three `uv run --script` processes.
