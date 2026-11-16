# Plan

What is left of the roadmap in the README, rebuilding Marquardt's agent-fleet building blocks round by
round. fleetwright holds what only a fleet needs (token hooks, measurement, CI gates, control plane);
cc-practices holds the general practices (rules, skills, reviewer, templates). Its plan, including the
rules layer of round 2 and the reviewer of round 4, is `cc-practices-plugin/docs/plans/PLAN.md`.

Round 1 is done: `context_ceiling` and `wait_cap` hooks, `session_tokens.py`, `fleetwright_report.py`, and a
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
reusable workflow, which is why fleetwright is public (main and tags are protected by rulesets).

## Round 4: review and issues (fleet part)

- Verdict-at-head merge check: a `reviewer-verdict` check run, published by `verdict.yml` from a marker comment
  the reviewer (cc-practices) posts, that passes only for an approval of the PR's current head commit. A push after
  approval resets it. Release-please PRs are exempt by branch prefix. Built in fleetwright v0.3.0; see `docs/verdict.md`.

*Done when*: a PR cannot merge without a reviewer verdict on its head SHA. `reviewer-verdict` is a required check
on commitment-tracker-ai's main. The reviewer has its own account, `g26work`, which is the only one in
`verdict.reviewers` there; on the fleet machine it posts from review slots (round 5).

## Round 5: control plane (VM): done

- VM setup that lists the skills it installs and keeps claude.ai skill sync off (from round 2), and installs
  `uv`, `claude`, `gh`, `rtk` and both plugins for each slot's user. Written: `docs/vm-setup.md`, a checklist
  of what was done by hand on the first machine.
- One clone per agent, with a service unit per slot. Built: `scripts/fleetwright_slot.py`, `scripts/fleetwright_agent.py`
  and `systemd/fleetwright-agent@.service`. Run on the first machine.
- Write-outside-clone hook: block edits outside the agent's own clone. Built in v0.4.0: `hooks/write_guard.py`.
- Slot and lane allocator: which agent works on what, so two agents never touch the same files. Lanes are
  weighted, and two thirds of the author slots are kept for fix rounds. Reviews are fleet work too, as in
  Marquardt's fleet: review slots (`reviewNN`) review green PRs and post verdicts as `g26work`. Built:
  `scripts/fleetwright_allocate.py`, `systemd/fleetwright-review@.service` and `systemd/fleetwright-allocate.timer`. Run on
  the first machine.
- No root to run the fleet: groups `fleetwright-admin` (operators, a `claude` login, the allocator's own user),
  `fleetwright-agent` and `fleetwright-review`; a polkit rule for the fleet's units; the allocator as `fleetwright-allocator`;
  check units and a dry-run unit so admins never read a token; slices per role, no limits yet. Built:
  `fleetwright_slot.py init`, `scripts/fleetwright_check.py`. Run on the first machine (Ubuntu 26.04).
- Each agent's steps in the journal, one line per tool call with its exit code, no tool output, tokens masked.
  Built: `scripts/fleetwright_stream.py`, tested against two recorded runs (`tests/fixtures/stream/`). The watchdog
  can read a stall as "no journal line for N minutes".
- Watchdog: a timer job with no AI that checks for stalled agents, a red main, stuck PRs and failed units, and
  logs each run as caught, clean or missed. A red main sets the stop flag. Built: `scripts/fleetwright_watchdog.py`,
  `systemd/fleetwright-watchdog.service` and `systemd/fleetwright-watchdog.timer`. Run on the first machine.
- Stop the line: one switch that halts every agent. Built in v0.4.0: `hooks/stop_line.py` and
  `scripts/fleetwright_stop.py`.
- Token report without root: `fleetwright-report.service` runs `fleetwright_report.py` over every slot's
  transcripts for fleetwright-admin. Built in v0.5.0; reading its output fixed in v0.6.0.
- Dashboard: one terminal with a panel per author, reviewer, the allocator and the watchdog, each showing the
  unit's state, whether it starts on its own, its assignment, the time of its latest line and its last 5 lines,
  and four keystroke commands: stop and restart the fleet, pause and resume new work.
  Built in v0.6.0: `scripts/fleetwright_dash.py` (issue #3).

Decided (2026-11-13): a Linux machine reached over SSH first (one already runs), then a cloud VM; an OS user per
agent as the boundary, with the hooks for clear refusals; headless `claude -p` under one systemd unit per slot.
The work queue is GitHub issues written from cc-practices' agent-ready template, with one label per lane. Order:
stop switch and write guard, then clones and the slot unit, then the allocator, then the watchdog, then the VM
setup. See `docs/control-plane.md`.

*Done when*: four author agents run on the VM with `CC_PRACTICES_UNATTENDED=1` and rtk's hook, each in
its own clone, the watchdog and stop switch have been exercised, and `fleetwright_report.py` has been compared with the
round 1 baseline.

Result (2026-11-15), on the first machine (Ubuntu 26.04, four author slots and one review slot):

- **Ran**: six issues became six merged pull requests (#39, #43, #44, #49, #50, #51 of commitment-tracker-ai),
  each reviewed by the review slot as `g26work`. On the fleetwright units (after the rename), four author
  agents ran at the same time on issues 53 to 56 (2026-11-15, from 15:36:53 PST).
- **Exercised**: the stop switch (the allocator assigns nothing, a slot's check fails, the hook denies a tool
  call, and clearing restores all three) and the watchdog (it caught a stopped allocator timer, and was clean
  through a full cycle). The stop flag against agents in mid-run (2026-11-15): set while four agents worked,
  three were refused on their next tool call and ended within a minute, each saying what it left uncommitted.
  The fourth had started a one-minute `gh pr checks --watch` three seconds before the flag; the hook checks a
  call when it starts, so that call finished and the agent ended with its PR opened. While the flag stood the
  allocator assigned nothing; after clearing it gave the unfinished issues out again. A red main (2026-11-15):
  commitment-tracker-ai's manual `fleet-drill` workflow failed a check on main's head, and the watchdog's next
  pass, three minutes later, set the stop flag; the allocator assigned nothing and a slot's check failed until
  the flag was cleared. A stalled agent (2026-11-15): agent01 was paused with `SIGSTOP` at 19:02:28 while it
  worked on issue 62; the watchdog's passes at 19:02, 19:08 and 19:13 were clean, and the first pass after 15
  quiet minutes, at 19:18:06, reported `caught stalled: fleetwright-agent@agent01 (issue 62) has logged nothing
  for 15 min`. Every item of *Done when* has now been exercised on the machine.
- **Compared**: about 255,000 weighted tokens per merged PR, reviews included, against the baseline's 925,000.
  Median context 26,500 against 92,500 to 171,000, and no call at 150k or more. Per changed line the fleet is
  about 4,350 against 870, because five of the six PRs changed 2 to 24 lines; the one of ordinary size cost
  1,083 per line. The record is `docs/metrics/` on commitment-tracker-ai's `docs/token-baseline` branch.

Found by running it, all fixed:

- The allocator gave out issue 46 again five seconds after its PR merged, because GitHub's issue listing
  still showed it open. Fixed in 034d6d5: a pass skips an issue whose `issue/<n>` PR merged in the last 30
  minutes.
- A slot has no git identity, so an agent's first commit fails and it invents one. Fixed: `fleetwright_slot.py add`
  sets `agentNN (fleet)` and the `--git-email` address in the clone, and the four slots on the machine have it.
  The address must be the pushing account's noreply form, `<id>+<login>@users.noreply.github.com`, for GitHub to
  link the commits; the PR for issue 62 was the first with it.
- Two author agents approved their own pull requests. Fixed in 7f2a6ff and cc-practices 0.8.1; held in the
  run of issues 53 to 56, whose PRs carry verdicts from `g26work` only.
- After the rename, the slots' cached cc-practices 0.8.1 still wrote the `fleet-verdict` marker, which
  fleetwright 0.5.0 does not read, so `reviewer-verdict` failed on four approved PRs. Fixed on the machine by
  updating cc-practices to 0.9.0 in every slot and clearing those heads from `state.json` for a new review.
  `docs/vm-setup.md`'s *Updating* now updates both plugins on every slot together.
- The watchdog called main red while any run of a check had failed, even after a newer run of the same check
  passed. Found in the red-main drill; fixed in bd9f80d, which judges each check by its newest run.
- An agent stopped by the flag leaves its work uncommitted in the clone, and the slot's next run refuses the
  clone and fails, holding its issue until a person stashes the work. Fixed: the next run keeps the work
  when it is on the same issue's branch and tells the agent so, and stashes it otherwise.
- A slot's transcripts are readable only by the slot, so `fleetwright_report.py` needed root. Fixed:
  `fleetwright-report.service` runs it for fleetwright-admin with `CAP_DAC_READ_SEARCH` and no network or secrets.
  Its documented read used the unit's InvocationID, which is empty once a oneshot unit ends; fixed in 1d18685 to
  read from a journal cursor.

Released as v0.6.0 (2026-11-15); commitment-tracker-ai's repin to it is its PR 64. The reusable workflows it calls
are unchanged from v0.5.0.

## Also open

- Hook cost: measured on the fleet machine (2026-11-14), and no dispatcher for four author agents. See
  *Hook cost* in `docs/control-plane.md`; measure again before going past about a dozen.
