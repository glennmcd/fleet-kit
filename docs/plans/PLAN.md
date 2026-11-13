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

## Round 3: CI gates: done

Checks every agent PR must pass, each citing the incident that caused it:

- File-size cap.
- Copy-paste (duplication) check.
- No silent skips: skipped or disabled tests fail the build unless they name an issue.
- Audit greps for patterns that must not appear.
- Change obligations: a change to X must also change Y (for example, a migration with its model).
- Gate evasion kit: tests that try to slip past each gate, so a weakened gate is caught.

Built: six gates plus a ratchet that refuses to loosen them (the evasion kit is each gate's
`tests/test_evasion_*.py`), run by `.github/workflows/gates.yml`; see `docs/gates.md`. commitment-tracker-ai
calls it (merged in PR 30; `gates / gates` is a required check on main).

*Open*: the gates cannot find a renamed-variable copy. A public repository cannot call a private repo's
reusable workflow, which is why fleet-kit is public (main and tags are protected by rulesets).

## Round 4: review and issues (fleet part)

- Verdict-at-head merge check: a `reviewer-verdict` check run, published by `verdict.yml` from a marker comment
  the reviewer (cc-practices) posts, that passes only for an approval of the PR's current head commit. A push after
  approval resets it. Release-please PRs are exempt by branch prefix. Built in fleet-kit v0.3.0; see `docs/verdict.md`.

*Done when*: a PR cannot merge without a reviewer verdict on its head SHA. `reviewer-verdict` is a required check
on commitment-tracker-ai's main. The reviewer has its own account, `g26work`, which is the only one in
`verdict.reviewers` there; on the fleet machine it posts from review slots (round 5).

## Round 5: control plane (VM)

- VM setup that lists the skills it installs and keeps claude.ai skill sync off (from round 2), and installs
  `uv`, `claude`, `gh`, `rtk` and both plugins for each slot's user.
- One clone per agent, with a service unit per slot. Built: `scripts/fleet_slot.py`, `scripts/fleet_agent.py`
  and `systemd/fleet-agent@.service`. Not yet run on a real machine: `systemd-analyze verify` and one slot's
  first run are the operator's.
- Write-outside-clone hook: block edits outside the agent's own clone. Built in v0.4.0: `hooks/write_guard.py`.
- Slot and lane allocator: which agent works on what, so two agents never touch the same files. Lanes are
  weighted, and two thirds of the author slots are kept for fix rounds. Reviews are fleet work too, as in
  Marquardt's fleet: review slots (`reviewNN`) review green PRs and post verdicts as `g26work`. Built:
  `scripts/fleet_allocate.py`, `systemd/fleet-review@.service` and `systemd/fleet-allocate.timer`. Not yet run
  on a real machine.
- Watchdog: a timer job with no AI that checks for stalled agents, a red main, stuck PRs and failed units, and
  logs each run as caught, clean or missed. A red main sets the stop flag.
- Stop the line: one switch that halts every agent. Built in v0.4.0: `hooks/stop_line.py` and
  `scripts/fleet_stop.py`.

Decided (2026-11-13): a Linux machine reached over SSH first (one already runs), then a cloud VM; an OS user per
agent as the boundary, with the hooks for clear refusals; headless `claude -p` under one systemd unit per slot.
The work queue is GitHub issues written from cc-practices' agent-ready template, with one label per lane. Order:
stop switch and write guard, then clones and the slot unit, then the allocator, then the watchdog, then the VM
setup. See `docs/control-plane.md`.

*Done when*: about a dozen agents run on the VM with `CC_PRACTICES_UNATTENDED=1` and rtk's hook, each in
its own clone, the watchdog and stop switch have been exercised, and `fleet_report.py` has been compared with the
round 1 baseline.

## Also open

- Hook cost: fleet-kit alone now starts four `uv run --script` processes on each Bash call (stop line, write
  guard, context ceiling, wait cap), before cc-practices' own. Measure it and consider one dispatcher before a
  dozen agents run.
