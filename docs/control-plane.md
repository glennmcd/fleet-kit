# Control plane (round 5)

What runs a dozen agents on one Linux machine without them trampling each other, and lets one person stop
them. This page covers what is built: the stop switch and the write guard, which protect against the rest,
and the slot (an OS user, its clone and its service unit). The allocator, the watchdog and the VM setup follow.

The design is two layers. **Hooks** give an agent a clear refusal and keep it from making honest mistakes.
**The agent's OS user** is the boundary that holds: each agent runs as its own user, and that user can write
only its own clone. A hook that reads command lines cannot see a program that writes for itself, so a hook
alone is not a sandbox.

## Environment an agent runs with

The agent's service unit sets these. On a desktop where they are unset, the hooks do nothing.

| Variable | Meaning |
|---|---|
| `FLEET_CLONE` | the agent's own clone. Turns on the write guard and the stop switch |
| `FLEET_SLOT` | the slot's name, `agentNN`; the same as the OS user |
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

One user per slot, named `agentNN`, with the clone in its home (`/home/agent01/repo`) and nothing else it can
write. [`scripts/fleet_slot.py`](../scripts/fleet_slot.py) sets a slot up:

```bash
uv run scripts/fleet_slot.py add agent01 --repo https://github.com/owner/name.git           # prints what it would do
sudo uv run scripts/fleet_slot.py add agent01 --repo https://github.com/owner/name.git --apply
```

Without `--apply` it only prints the commands and the unit. With it, as root, it creates `/var/lib/fleet`
(agents read, root writes), the user and the clone, sets the clone to push with the token from the unit's
environment, and installs the unit. It runs no shell, stops at the first failure, and keeps a user or clone
that already exists. `--kit` says where fleet-kit is installed on the machine (default `/opt/fleet-kit`).

The operator writes the secrets once, by hand. They are never in the repository or the unit:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/fleet/agent.env
sudoedit /etc/fleet/agent.env       # ANTHROPIC_API_KEY=... and GH_TOKEN=...
```

Not set up here yet (the VM setup will do it): `uv`, `claude`, `gh` and `rtk` on the machine, and this plugin
and cc-practices installed for each slot's user. Without the plugin installed for that user, the hooks do not run.

## The slot's service unit

[`systemd/fleet-agent@.service`](../systemd/fleet-agent@.service) is a template: `fleet-agent@agent01` runs as
the user `agent01` in `/home/agent01/repo`. Both come from the one instance name, so a unit cannot run one
slot's agent in another slot's clone. It sets `FLEET_CLONE`, `FLEET_SLOT` and `CC_PRACTICES_UNATTENDED=1`,
reads the secrets from `/etc/fleet/agent.env`, and lets the process write only its home and a private `/tmp`
(`ProtectSystem=strict`). It does not restart: one run is one assignment.

```bash
echo 12 | sudo tee /var/lib/fleet/slots/agent01      # assign issue 12 (the allocator will do this)
sudo systemctl start fleet-agent@agent01
journalctl -u fleet-agent@agent01 -f
sudo systemctl stop 'fleet-agent@*'                  # with fleet_stop.py stop, ends every agent
```

The unit runs [`scripts/fleet_agent.py`](../scripts/fleet_agent.py), which checks before it starts an agent:

| Check | If it fails |
|---|---|
| the stop flag is absent | exit 75; nothing starts while the fleet is stopped |
| the assignment file holds one issue number | no file: idle, exit 0. Anything else: exit 65 |
| the clone has no uncommitted changes | exit 70. It is left as it is for a person to look at |
| `git fetch` and the switch to `issue/<n>` work | exit 70 |

The branch is the local `issue/<n>` if the clone has it, the pushed one if another slot started it, else a new
one from `origin/main`. Then the script becomes `claude -p` with a fixed prompt that names the issue, so systemd
watches the agent itself. The agent runs with `--dangerously-skip-permissions`: nobody is there to answer a
prompt, and the limits are the OS user, the unit's sandbox and the two hooks.
