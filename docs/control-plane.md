# Control plane (round 5)

What runs a few agents (four authors in round 5) on one Linux machine without them trampling each other, and lets one person stop
them. This page covers what is built: the stop switch and the write guard, which protect against the rest; the
slots (an OS user, its clone and its service unit), for authors and for reviewers; who needs root (only setup);
the allocator that gives them work; and the watchdog. Setting the machine up is in [vm-setup.md](vm-setup.md).

The design is two layers. **Hooks** give an agent a clear refusal and keep it from making honest mistakes.
**The agent's OS user** is the boundary that holds: each agent runs as its own user, and that user can write
only its own clone. A hook that reads command lines cannot see a program that writes for itself, so a hook
alone is not a sandbox.

## Environment an agent runs with

The agent's service unit sets these. On a desktop where they are unset, the hooks do nothing.

| Variable | Meaning |
|---|---|
| `FLEET_CLONE` | the agent's own clone. Turns on the write guard and the stop switch |
| `FLEET_SLOT` | the slot's name, `agentNN` or `reviewNN`; the same as the OS user |
| `FLEET_ROLE` | `author` or `review`; must agree with the slot's name |
| `FLEET_REVIEWER_LOGIN` | review slots only, from `reviewer.env`: the account verdicts must be posted as |
| `FLEET_SLOTS_DIR` | where assignments are read from; default `/var/lib/fleet/slots` |
| `FLEET_STOP_FILE` | the stop flag file; default `/var/lib/fleet/STOP` |
| `FLEET_ALLOW_WRITE` | extra writable directories, separated by `:` (for example a shared cache) |

The system temp directory is always writable.

## Stop the line

```bash
uv run scripts/fleet_stop.py stop "the deploy script is looping"
uv run scripts/fleet_stop.py status      # exit 1 while stopped
uv run scripts/fleet_stop.py clear
```

`stop` writes the flag file. [`hooks/stop_line.py`](../hooks/stop_line.py) refuses every tool call of every
agent while it exists, with the reason from the file. An agent can still answer in text, so it can say where it
stopped, but it cannot work around the stop.

- **It stops new tool calls, not running commands.** A test run that is already going finishes. Pair the flag
  file with `systemctl stop 'fleet-agent@*'` to end the processes.
- **The agents must not be able to clear it.** `fleet_slot.py init` gives `/var/lib/fleet` to the fleet-admin
  group, read-only to the agents' users, so any admin can stop and clear the line without root and no agent
  can. If an agent's user can delete the file, the stop is only a suggestion.
- **It fails closed.** If the hook cannot check the file, or crashes, the call is refused.

## Write guard

[`hooks/write_guard.py`](../hooks/write_guard.py) refuses a write whose target is outside `FLEET_CLONE`,
`FLEET_ALLOW_WRITE` or the temp directory.

It checks the `Edit`, `Write`, `MultiEdit` and `NotebookEdit` paths, with symlinks resolved, and the targets of
`Bash` commands: redirections, `tee`, `rm`, `mv`, `cp`, `touch`, `mkdir`, `sed -i`, `dd of=`, `tar -x`,
`curl -o`, `git clone`, `find -delete` and the rest of the list in the file. It follows `cd`, `sudo`/`env`
wrappers, `bash -c` and `eval` strings, `$(...)` and `find -exec`. A target it cannot work out (an unset
variable, a brace expansion, a process substitution) is refused rather than guessed.

It also protects the agent's own safety settings, even inside its clone: `.claude/settings*.json`,
`.claude/hooks/` and `.git/hooks/`. An agent that can edit them can switch the guards off.

### What it cannot see

These are allowed, on purpose, and written down as tests (`KNOWN LIMIT` in
`tests/test_evasion_write_guard.py`):

- code that writes from inside a program: `python -c`, `node -e`, `perl -i`, a build script, a test suite;
- reads. An agent can read another clone;
- anything the OS lets the agent's user do outside the checkout. This is why each agent gets its own user.

## OS user per agent (what holds)

One user per slot, with the clone in its home (`/home/agent01/repo`) and nothing else it can write. `agentNN`
slots are authors: they work on issues and fix rounds. `reviewNN` slots are reviewers: they review pull requests
and post verdicts as the reviewer's own GitHub account. [`scripts/fleet_slot.py`](../scripts/fleet_slot.py) sets
up the machine once, then each slot:

```bash
uv run scripts/fleet_slot.py init --admin "$USER"                    # prints what it would do
sudo uv run scripts/fleet_slot.py init --admin "$USER" --apply      # the machine, once
sudo uv run scripts/fleet_slot.py add agent01 --repo https://github.com/owner/name.git --apply
sudo uv run scripts/fleet_slot.py add review01 --repo https://github.com/owner/name.git --apply
```

Without `--apply` they only print. `init` creates the groups and users of [Who needs root](#who-needs-root),
gives `/var/lib/fleet` to fleet-admin, and installs the units in [`systemd/`](../systemd), the polkit rule and
the sshd file. `--kit` says where fleet-kit is installed (default `/opt/fleet-kit`). `add` creates the slot's
user in its role's group, its clone, and sets the clone to push with the token from the unit's environment.
Both run no shell, stop at the first failure, and keep a user or clone that already exists, so a slot made
before `init` existed is brought up to date by running `add` again.

The operator writes the secrets once, by hand. They are never in the repository or the unit:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/fleet/agent.env
sudoedit /etc/fleet/agent.env       # ANTHROPIC_API_KEY=... and GH_TOKEN=... (the authors' account)
sudo install -m 600 -o root -g root /dev/null /etc/fleet/reviewer.env
sudoedit /etc/fleet/reviewer.env    # GH_TOKEN=... (the reviewer's account) and FLEET_REVIEWER_LOGIN=g26work
```

Only the review unit reads `reviewer.env`, after `agent.env`, so its `GH_TOKEN` replaces the authors' one and an
author's run never has it. The variable must be `GH_TOKEN`: `gh` prefers `GH_TOKEN` to `GITHUB_TOKEN`, so a
reviewer token under `GITHUB_TOKEN` would lose to the authors' token. Both the runner and `post_verdict.py` check
that `gh` is signed in as `FLEET_REVIEWER_LOGIN` and refuse otherwise, so that mistake stops a review instead of
posting an approval as the author. The reviewer's account must be in `verdict.reviewers` in the repository's
`.fleet-gates.json`.

Give the reviewer's token as little as it needs: a fine-grained token that can read the repository and write
pull request comments, and cannot push or merge. A review slot runs a model over code an author wrote, with no
one watching, and its prompt is the only thing telling it not to push. The token's scope is what holds.

The review slot also loads `agent.env` for the model key, so the authors' `GH_TOKEN` is in its environment
until `reviewer.env` replaces it. The login checks catch a mix-up; keeping the model key in a file of its own
would remove the authors' token from review slots altogether, and is the next step if that matters to you.

Each slot's home is mode 700, so one slot cannot read another's clone or transcripts. An ACL lets fleet-admin
read it, to look at a clone left for a person.

Not set up here yet (the VM setup will do it): `uv`, `claude`, `gh` and `rtk` on the machine, and this plugin
and cc-practices installed for each slot's user. Without the plugin installed for that user, the hooks do not run.
Install `uv` where the allocator finds it too: `/usr/local/bin`, or `~/.local/bin` of `fleet-allocator`.

`gh` must be 2.48 or later: the allocator reads a pull request's comments with `gh api --paginate --slurp`.
Ubuntu's own package is older (26.04 ships 2.46), and with it every pass fails once a pull request is open
(`unknown flag: --slurp`). Install `gh` from GitHub's apt repository instead; the package keeps its name, so
apt replaces Ubuntu's:

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install gh
```

The slot check reports it (`gh version` below).

## Who needs root

Root sets the machine up (`init`, `add`, the secrets, `systemctl enable`), and nothing after that. Running the
fleet is the **fleet-admin** group's: the operators, a `claude` login for the operator's agent (not in sudo),
and the allocator's own user, `fleet-allocator`.

| Group | Members | What it gives |
|---|---|---|
| `fleet-admin` | operators, `claude`, `fleet-allocator` | start, stop, restart and reset the fleet's units (polkit); write `/var/lib/fleet` (assignments, state, the stop flag); read the slots' homes; read the journal (`systemd-journal`) |
| `fleet-agent` | `agentNN` | nothing on files; no SSH login |
| `fleet-review` | `reviewNN` | nothing on files; no SSH login |

The polkit rule, `/etc/polkit-1/rules.d/50-fleet.rules`, allows `start`, `stop`, `restart` and `reset-failed`
on `fleet-agent@agentNN`, `fleet-review@reviewNN`, their check units, `fleet-allocate` (service and timer),
`fleet-allocate-dry-run` and `fleet-watchdog` (service and timer), and on no other unit. Each template takes only its own kind of slot, so an author
unit cannot be started as a reviewer. Installing or enabling units and reloading systemd stay root's.

What fleet-admin does **not** get, because either would make it root by another name:

- **The secrets.** `/etc/fleet` stays root-only. systemd reads the env files as root before it drops to the
  unit's user, so no admin needs to. To check a slot or preview the allocator, start the unit that runs as
  that user with those secrets, and read its result in the journal:

  ```bash
  systemctl start fleet-check-review@review01       # or fleet-check-agent@agent01
  journalctl -u fleet-check-review@review01 -o cat -n 30
  ```

  [`fleet_check.py`](../scripts/fleet_check.py) reports, without printing a secret: the slot's role, the
  unattended switch, the tools on the PATH, that `gh` has `api --slurp` (2.48 or later), the model key's name, the `gh` login (and that it is
  `FLEET_REVIEWER_LOGIN` on a review slot), the clone, both plugins, the home's mode, the sandbox (no write to
  `/var/lib/fleet`, no read of `/etc/fleet`) and the stop flag.
- **The code.** `/opt/fleet-kit`, the units and the slots' plugins run as the slots or the allocator; they stay
  root's, so an admin cannot change what those users run.

Members of a new group must log in again before they have it (`id` shows the groups).

The `claude` login has no password; it signs in with a key of its own, made on the operator's machine for this
use only, so revoking it touches nothing else:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/fleet-claude -C claude@fleet          # on the operator's machine
sudo install -d -m 700 -o claude -g claude /home/claude/.ssh          # on the fleet machine
sudo install -m 600 -o claude -g claude /dev/stdin /home/claude/.ssh/authorized_keys < fleet-claude.pub
```

The slots' units run in `fleet-agent.slice` and `fleet-review.slice`, under `fleet.slice` with the allocator
and the checks; `systemctl status fleet.slice` shows the whole fleet. They have no limits yet. When the first
runs show what an agent needs, add `MemoryMax=` and `CPUQuota=` in a drop-in, so one runaway test suite
cannot starve the reviewer.

## The slots' service units

[`fleet-agent@.service`](../systemd/fleet-agent@.service) and [`fleet-review@.service`](../systemd/fleet-review@.service)
are templates: `fleet-agent@agent01` runs as the user `agent01` in `/home/agent01/repo`. Both come from the one
instance name, so a unit cannot run one slot's agent in another slot's clone. Each sets `FLEET_CLONE`,
`FLEET_SLOT`, `FLEET_ROLE` and `CC_PRACTICES_UNATTENDED=1`, reads its secrets, and lets the process write only
its home and a private `/tmp` (`ProtectSystem=strict`). Neither restarts: one run is one assignment.

As a member of fleet-admin, no `sudo`:

```bash
echo "issue 12" > /var/lib/fleet/slots/agent01      # by hand; the allocator normally does this
systemctl start fleet-agent@agent01
journalctl -u fleet-agent@agent01 -f
systemctl stop 'fleet-agent@*' 'fleet-review@*'     # with fleet_stop.py stop, ends every agent
systemctl reset-failed fleet-agent@agent01          # after looking at a failed run
```

The units run [`scripts/fleet_agent.py`](../scripts/fleet_agent.py), which checks before it starts an agent:

| Check | If it fails |
|---|---|
| the slot's name and `FLEET_ROLE` agree | exit 64 |
| the stop flag is absent | exit 75; nothing starts while the fleet is stopped |
| review slots: `gh` is signed in as `FLEET_REVIEWER_LOGIN` | exit 64 |
| the assignment is `issue <n>` or `fix <n>` (authors) or `review <pr>` (reviewers) | no file: idle, exit 0. Anything else: exit 65 |
| the clone has no uncommitted changes | exit 70. It is left as it is for a person to look at |
| `git fetch` and the checkout work | exit 70 |

An author works on the branch `issue/<n>`: the local one, fast-forwarded to the pushed one; the pushed one if
another slot started it; else a new one from `origin/main`. A local branch that has diverged from the pushed one
is left for a person. A reviewer checks out the pull request's head commit, detached. Then the script starts
`claude -p` with a fixed prompt for the kind of work (a new issue, a fix round, or cc-practices' `review-pr`
skill), and exits with the agent's exit code. The agent runs with `--dangerously-skip-permissions`: nobody is
there to answer a prompt, and the limits are the OS user, the unit's sandbox and the two hooks.

### Watching an agent

The agent's events are streamed ([`fleet_stream.py`](../scripts/fleet_stream.py)), one short line per step, so
the journal shows a run while it happens:

```bash
journalctl -u fleet-review@review01 -f -o cat
```

```
start model=claude-opus-5-5 claude=2.1.311 session=...
tool Bash: gh pr diff 43
  → ok
tool Bash: uv run pytest -q
  → exit 1
tool Bash: git push
  → denied: This command requires approval
say: The tests fail on the new parser; reviewing that first.
done success turns=23 time=6m12s cost=$0.84 in=12 out=9450 cache_read=812345 cache_write=40211
```

A line shows the tool and its target (a command, a path, a pattern) and how the call ended (`ok`, `exit <n>`,
`error`, or `denied:` with the reason, which is where a refusal from the write guard or the stop switch shows).
It never shows a tool's output, a file's body or an edit. Every line is cut short, and the values of the tokens
in the unit's environment are replaced with `***`. The `done` line carries the run's turns, time, cost and
tokens; a run that ends without one was stopped or crashed (`stopped by signal 15`). The full record is the
transcript in the slot's home, `~/.claude/projects/`, which only root can read.

## The allocator

[`scripts/fleet_allocate.py`](../scripts/fleet_allocate.py) gives free slots their next work, one pass at a time.
[`fleet-allocate.timer`](../systemd/fleet-allocate.timer) runs a pass 2 minutes after the last one ended, as the
user `fleet-allocator`, a member of fleet-admin: it writes the assignment files and starts the units through the
polkit rule, and can do nothing an admin cannot. It only reads GitHub.

It reads `/etc/fleet/fleet.json`:

```json
{
  "repo": "glennmcd/commitment-tracker-ai",
  "slots": ["agent01", "agent02"],
  "review_slots": ["review01"],
  "lanes": {"feature": 3, "fix": 2, "tooling": 1}
}
```

Optional: `in_flight_share` (0.67), `max_runs_per_issue` (4) and `ready_label` (`agent-ready`).

The file sits with the secrets, so it is root's to change (`sudoedit /etc/fleet/fleet.json`). The allocator does
not run as root: its units pass the file in with `LoadCredential=`, which systemd reads as root and hands to the
unit as a private copy for that run only.

Each pass:

1. Does nothing while the stop flag exists.
2. Reads each slot's unit. An inactive slot is free, and its finished assignment is cleared. A failed slot keeps
   its assignment and holds that work until a person looks (`systemctl reset-failed fleet-agent@agent01`).
3. Sorts the open pull requests from `issue/<n>` branches. Other branches, drafts and approved PRs are left alone.
   - **Review**: checks passed and no verdict at the head commit. Review slots take these, oldest first. A head
     is reviewed once: if the review posted nothing, the PR waits for a person.
   - **Fix round**: checks failed, a merge conflict, or `request_changes` at the head. A red PR is never sent to
     review, since that review would only repeat what CI says.
4. Picks new issues: open, labelled `agent-ready` and one `lane:<name>` from the config, with a *Done when* list
   that has a filled-in checkbox, no assignee and no open PR. The lane is a weighted draw among lanes that have
   work; within a lane, the oldest issue goes first.
5. Fills author slots. Two thirds of them (`in_flight_share`, counting slots already on a fix round) are kept
   for fix rounds and the rest for new issues; a share left unused goes to the other kind.
6. Writes `/var/lib/fleet/slots/<slot>` and starts the slot's unit.

An issue is given out at most `max_runs_per_issue` times, fix rounds included; after that its PR waits for a
person. Run counts and reviewed heads are kept in `/var/lib/fleet/state.json`. Which accounts' verdicts count is
read from the repository's `.fleet-gates.json`, the same list the merge check uses.

```bash
systemctl start fleet-allocate-dry-run                        # fleet-admin: one pass that changes nothing
journalctl -u fleet-allocate-dry-run -o cat -n 50             # what it would assign
sudo systemctl enable --now fleet-allocate.timer              # root, once
journalctl -u fleet-allocate -f
systemctl stop fleet-allocate.timer                           # fleet-admin: pause the passes
```

`fleet-allocate-dry-run` runs `fleet_allocate.py --dry-run` as the allocator, with its token, so whoever starts it
never holds the token. It prints what a pass would assign and changes nothing. A pass does every read before it writes
anything: if `systemctl` gives an answer it does not know, a GitHub call fails or times out, a listing reaches its
limit (200 issues, 100 pull requests), or `state.json` is unreadable, it ends with nothing changed. A unit that
fails to start gives its work back and is not counted as a run.

Only pull requests from branches of the repository itself are handed out. A fork can name its branch
`issue/<n>`, and its code must never reach a slot that holds tokens; fork PRs are left to people.

Without `/etc/fleet/allocator.env` the allocator uses the authors' token from `agent.env`, which can write. Put
a read-only `GH_TOKEN` in `allocator.env` so a bug in the allocator cannot change the repository. Merging an approved PR is not the allocator's
job: GitHub's required checks decide when it can merge, and a person (or auto-merge) merges it.

The labels it reads, created once per repository:

```bash
gh label create agent-ready --description "An agent can pick this up: it has a Done when list"
gh label create lane:feature
gh label create lane:fix
gh label create lane:tooling
```

## The watchdog

[`scripts/fleet_watchdog.py`](../scripts/fleet_watchdog.py) is a timer job with no AI in it. Every 5 minutes
it looks for four things the fleet cannot see about itself, as the allocator's user:

| Check | What it catches | What it does |
|---|---|---|
| `failed-unit` | a slot's unit or `fleet-allocate.service` has failed, or `fleet-allocate.timer` is not running | reports it, with the command to look and reset |
| `stalled` | a running slot that has written no journal line for `stall_minutes` (15) | reports it; `systemctl stop` on the unit frees the slot |
| `red-main` | a failed check on the default branch's head commit | sets the stop flag, since every agent would branch from a broken main |
| `stuck-pr` | an open `issue/<n>` pull request unchanged for `stuck_pr_minutes` (120) that no running slot holds | reports what it waits for: a fix round, a review, its checks, or a person to merge it |

Each finding is one journal line that starts with `caught` (a problem) or `missed` (the check could not be
made, so a problem may have gone unseen). The last line is the run's result:

```text
caught stalled: fleet-agent@agent01 (issue 12) has logged nothing for 23 min (limit 15); `systemctl stop fleet-agent@agent01` frees the slot
missed stuck-pr: gh pr list --repo failed: HTTP 502
watchdog: caught 1, missed 1
```

A run with nothing to report prints `watchdog: clean`. The unit exits 1 when a check was missed, so
`systemctl --failed` shows a watchdog that cannot see.

```bash
sudo systemctl enable --now fleet-watchdog.timer              # root, once
journalctl -u fleet-watchdog -o cat -n 30                     # the last runs
systemctl start fleet-watchdog                                # fleet-admin: one pass now
systemctl stop fleet-watchdog.timer                           # fleet-admin: pause it
```

The stop flag is the only thing it writes. It starts and stops no unit and writes nothing to GitHub. A red
main stops the line once: a line that is already stopped keeps its note, and a person clears the flag
(`fleet_stop.py clear`) after main is green. If the flag is cleared while main is still red, the next run
sets it again. A cancelled check is not red.

The stall check reads the journal, which the allocator's user cannot. The unit adds the `systemd-journal`
group for this run only (`SupplementaryGroups=`). The limits are optional keys in `/etc/fleet/fleet.json`:

```json
"watchdog": {"stall_minutes": 15, "stuck_pr_minutes": 120}
```

A long command is not a stall: the `wait_cap` hook holds an agent's waits to about 5 minutes, well under
`stall_minutes`. An approved pull request that nobody merges for two hours is reported as stuck on every
run until it is merged; raise `stuck_pr_minutes` if that is noise.

## Hook cost

fleet-kit starts four `uv run --script` processes before each Bash call (stop line, write guard, context
ceiling, wait cap), and cc-practices starts a fifth. Measured on the fleet machine (8 cores, Python 3.14,
uv 0.12, 2026-11-14), as the mean of 20 runs with a warm cache:

| Hook | `uv run --script` | `python3` alone |
|---|---|---|
| `stop_line.py` | 47 ms | 31 ms |
| `write_guard.py` | 63 ms | 44 ms |
| `context_ceiling.py` (a 620 KB transcript) | 60 ms | 40 ms |
| `wait_cap.py` | 45 ms | 27 ms |
| cc-practices `practices.py guard-commit` | 79 ms | 66 ms |

Run one after another, fleet-kit's four take about 215 ms of CPU. Started together, as Claude Code starts a
tool call's hooks, they take 70 ms of wall time. An empty `python3` takes 16 ms, and `uv` adds 15 to 20 ms
to each hook.

**Decision: no dispatcher for four author agents.** A Bash call that runs a test suite takes seconds and the
model call around it takes longer, so 70 ms is under 1% of a step. One dispatcher process would save about
150 ms of CPU per Bash call. Against that, the four hooks stay separate files with separate tests, and one
that crashes cannot take the stop switch down with it. Measure again before going past about a dozen agents,
or if hooks start to show in `systemd-cgtop fleet.slice`.
