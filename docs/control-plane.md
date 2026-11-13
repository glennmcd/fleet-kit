# Control plane (round 5)

What runs a dozen agents on one Linux machine without them trampling each other, and lets one person stop
them. This page covers the first two pieces, which protect against the rest: the stop switch and the
write guard. The clones, the allocator, the watchdog and the VM setup follow.

The design is two layers. **Hooks** give an agent a clear refusal and keep it from making honest mistakes.
**The agent's OS user** is the boundary that holds: each agent runs as its own user, and that user can write
only its own clone. A hook that reads command lines cannot see a program that writes for itself, so a hook
alone is not a sandbox.

## Environment an agent runs with

The agent's service unit sets these. On a desktop where they are unset, the hooks do nothing.

| Variable | Meaning |
|---|---|
| `FLEET_CLONE` | the agent's own clone. Turns on the write guard and the stop switch |
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

One user per slot, for example `agent01`, with the clone in its home and nothing else it can write.

```bash
sudo useradd --create-home agent01
sudo install -d -o root -g root -m 755 /var/lib/fleet      # the stop file's directory: agents read, root writes
```

The slot's service unit sets `User=agent01`, `Environment=FLEET_CLONE=/home/agent01/repo`, and runs the
headless agent. That unit, the clone setup and the allocator are the next pieces.
