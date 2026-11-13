# Control plane (round 5)

What runs a dozen agents on one Linux machine without them trampling each other, and lets one person stop
them. This page covers what is built: the stop switch and the write guard, which protect against the rest; the
slots (an OS user, its clone and its service unit), for authors and for reviewers; and the allocator that gives
them work. The watchdog and the VM setup follow.

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
- **The agents must not be able to clear it.** Make `/var/lib/fleet` owned by the operator and read-only to the
  agents' users. If an agent's user can delete the file, the stop is only a suggestion.
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
a slot up:

```bash
uv run scripts/fleet_slot.py add agent01 --repo https://github.com/owner/name.git           # prints what it would do
sudo uv run scripts/fleet_slot.py add agent01 --repo https://github.com/owner/name.git --apply
sudo uv run scripts/fleet_slot.py add review01 --repo https://github.com/owner/name.git --apply
```

Without `--apply` it only prints the commands and the units. With it, as root, it creates `/var/lib/fleet`
(agents read, root writes), the user and the clone, sets the clone to push with the token from the unit's
environment, and installs the four units in [`systemd/`](../systemd). It runs no shell, stops at the first failure, and keeps a user or clone
that already exists. `--kit` says where fleet-kit is installed on the machine (default `/opt/fleet-kit`).

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

Each slot's home is mode 700, so one slot cannot read another's clone or transcripts.

Not set up here yet (the VM setup will do it): `uv`, `claude`, `gh` and `rtk` on the machine, and this plugin
and cc-practices installed for each slot's user. Without the plugin installed for that user, the hooks do not run.

## The slots' service units

[`fleet-agent@.service`](../systemd/fleet-agent@.service) and [`fleet-review@.service`](../systemd/fleet-review@.service)
are templates: `fleet-agent@agent01` runs as the user `agent01` in `/home/agent01/repo`. Both come from the one
instance name, so a unit cannot run one slot's agent in another slot's clone. Each sets `FLEET_CLONE`,
`FLEET_SLOT`, `FLEET_ROLE` and `CC_PRACTICES_UNATTENDED=1`, reads its secrets, and lets the process write only
its home and a private `/tmp` (`ProtectSystem=strict`). Neither restarts: one run is one assignment.

```bash
echo "issue 12" | sudo tee /var/lib/fleet/slots/agent01      # by hand; the allocator normally does this
sudo systemctl start fleet-agent@agent01
journalctl -u fleet-agent@agent01 -f
sudo systemctl stop 'fleet-agent@*' 'fleet-review@*'         # with fleet_stop.py stop, ends every agent
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
is left for a person. A reviewer checks out the pull request's head commit, detached. Then the script becomes
`claude -p` with a fixed prompt for the kind of work (a new issue, a fix round, or cc-practices' `review-pr`
skill), so systemd watches the agent itself. The agent runs with `--dangerously-skip-permissions`: nobody is
there to answer a prompt, and the limits are the OS user, the unit's sandbox and the two hooks.

## The allocator

[`scripts/fleet_allocate.py`](../scripts/fleet_allocate.py) gives free slots their next work, one pass at a time.
[`fleet-allocate.timer`](../systemd/fleet-allocate.timer) runs a pass 2 minutes after the last one ended, as root,
since it writes the assignment files and starts the units. It only reads GitHub.

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
sudo FLEET_CONFIG=/etc/fleet/fleet.json uv run --script /opt/fleet-kit/scripts/fleet_allocate.py --dry-run
sudo systemctl enable --now fleet-allocate.timer
journalctl -u fleet-allocate -f
```

`--dry-run` prints what a pass would assign and changes nothing. A pass does every read before it writes
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
