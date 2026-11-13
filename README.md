# fleet-kit

Building blocks for running a fleet of Claude Code agents without losing control of the code or the
token budget. Rebuilt from Alexander Marquardt's two articles, round by round, as a Claude Code plugin
installed from a directory marketplace (same setup as `cc-practices-plugin`).

- [Guardrails for coding agents](https://alexmarquardt.com/ai-tools/coding-agents-codebase-under-control/):
  rules, tests, CI gates, review and the control plane.
- [The same work on a third of the tokens](https://alexmarquardt.com/ai-tools/agent-fleet-token-efficiency/):
  where tokens go, how to measure them, what each change saved.

## Round 1: measure, and stop paying for context

| File | What it does | Origin |
|---|---|---|
| `hooks/context_ceiling.py` | `PreToolUse`: a sub-agent past 150k tokens of context is told to hand off at its next commit; past 200k only `git`/`gh` run | agent-fleet-kit, vendored |
| `hooks/wait_cap.py` | `PreToolUse` on Bash: a sub-agent may not wait in the foreground past the 5-minute prompt cache (270 s cap) | agent-fleet-kit, vendored |
| `scripts/session_tokens.py` | Tokens per session, one count per request | agent-fleet-kit, vendored |
| `scripts/first_context.py` | What a session's first model call carried (system prompt, SessionStart output, CLAUDE.md, skill and tool listings), part by part, largest first | new |
| `scripts/fleet_report.py` | Tokens per ISO week, per merged PR and per changed line, price-weighted, as an index against a baseline; cache-expiry share, context distribution, first-turn cost, top branches | new |

Both hooks judge sub-agents only and fail open. The vendored files are unchanged apart from a
`uv run --script` header; see [NOTICE.md](NOTICE.md).

## Install

Requires `uv` on `PATH` (the hooks run with `uv run --script`, standard library only).

```bash
claude plugin marketplace add C:/Users/glenn/Documents/git/fleet-kit      # or the VM path
claude plugin install fleet-kit@fleet-kit --scope local
```

Thresholds are environment variables: `CONTEXT_ADVISORY_TOKENS` (150000), `CONTEXT_CEILING_TOKENS`
(200000), `WAIT_CAP_SECS` (270), `WAIT_CAP_MAIN=1` to cap the main session too.

### With cc-practices

On the VM, export `CC_PRACTICES_UNATTENDED=1` where the agents are launched, so the hook processes
inherit it. Without it, cc-practices blocks `git commit` while the context ceiling allows only
git/gh and asks for a commit, so a sub-agent past the ceiling can neither work nor hand off. The
variable also swaps cc-practices' "wait for approval" steps for issue-as-approval and handoffs.
Leave it unset on machines where a person is watching.

Also run `rtk init -g --auto-patch` on the VM. rtk's hook compacts command output on every call and
leaves the permission decision to Claude Code, so it never waits for a person.

## Measure first

Take a baseline before changing anything, then compare each change against it:

```bash
gh pr list --state merged --limit 2000 \
  --json number,headRefName,mergedAt,additions,deletions > prs.json
uv run --script scripts/fleet_report.py --prs prs.json --project commitment-tracker-ai --since 2026-10-01
uv run --script scripts/fleet_report.py --prs prs.json --baseline 2026-W45,2026-W46 --json
```

How to read it:

- **Per line idx** is the headline: splitting work into smaller PRs cannot fake it.
- **Cache expiry** = price-weighted cost of re-writing a cache that died while the agent idled. Marquardt
  found 31% here; the wait cap took it to under 1%.
- **Median ctx / Ctx >=150k** show whether agents carry too much. The second is the share of calls at
  or above `--ceiling`, which defaults to the hook's 150k advisory level, not its 200k hard ceiling. His
  median fell 218k to 101k.
- **1st-turn write** large and growing = something injected after the cache breakpoint (a long
  CLAUDE.md or memory file) is breaking reuse across agents.
- Divide by work, not by hour. Note which changes landed in the same week; you cannot separate them.

### See what a new session starts with

Start a session, send one prompt, then:

```bash
uv run --script scripts/first_context.py --project commitment-tracker-ai
uv run --script scripts/first_context.py --project commitment-tracker-ai --show SessionStart
uv run --script scripts/first_context.py --project commitment-tracker-ai --subagent --show SubagentStart
```

The first prints each part of the first call's context with its size; `--show` prints the text of the
parts whose name matches (here, cc-practices' core rules). `--subagent` reads the newest sub-agent's
transcript instead: sub-agents get no SessionStart output, so cc-practices gives them its rules through a
`SubagentStart` hook. Tool definitions are not recorded, so the total
is lower than the 1st-turn tokens in `fleet_report.py`. To preview cc-practices' part without a session,
run its hook directly from the project folder:

```bash
echo {} | uv run --script ../cc-practices-plugin/hooks/practices.py session-start
```

Transcripts live in `~/.claude/projects/` and hold every file and command output the agent saw:
treat them, and this report's branch names, as sensitive.

## Roadmap

1. Measure + token hooks (done)
2. Rules layer: byte-capped CLAUDE.md, topic files with incident numbers, rule delivery classes, code maps, handoff template (done, in cc-practices 0.6.0)
3. CI gates: file-size cap, copy-paste check, no silent skips, audit greps, change obligations, gate evasion kit
4. Review + issues: read-only adversarial reviewer, verdict-at-head merge check, agent-ready issue template
5. Control plane (VM): clone per agent, write-outside-clone hook, slot/lane allocator, watchdog, stop the line

## Develop

```bash
uv run pytest            # or: python3 -m unittest discover tests
uv run ruff check . && uv run mypy
claude plugin validate .
```
