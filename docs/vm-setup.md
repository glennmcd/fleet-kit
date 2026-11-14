# Fleet machine setup

What root does once on the fleet machine, in order. After this, the `fleet-admin` group runs the fleet without
root ([control-plane.md](control-plane.md), *Who needs root*). The versions are what the first machine runs
(Ubuntu 26.04, checked 2026-11-14); they are a record, not a pin.

Every step is safe to run again. Commands that start with `sudo` are the operator's; the agent never runs them.

## 1. System tools

| Tool | Where | How | Why this way |
|---|---|---|---|
| `git`, `acl`, `polkitd` | apt | `sudo apt install git acl polkitd` | `setfacl` opens a slot's home to the admins; polkit holds the rule for the fleet's units |
| `gh` 2.48 or later (2.104.0) | `/usr/bin/gh` | GitHub's apt repository, `cli.github.com/packages` | Ubuntu's own package is older and has no `gh api --slurp`, which the allocator needs |
| `claude` (2.1.311) | `/usr/bin/claude` | Anthropic's apt repository, `downloads.claude.ai/claude-code/apt/stable` | one copy owned by root, so no slot can change the binary it runs |
| `uv` (0.12.21) | `/usr/local/bin/uv` | the release binary, owned by root | every hook and script starts with `uv run --script` |
| `rtk` (1.2.0) | `/usr/local/bin/rtk` | the release binary from `rtk-ai/rtk`, owned by root | compact command output for the agents |
| `node` (22) | `/usr/bin/node` | apt | the consumer project's formatter and tests |

Check the owners. A tool on every slot's `PATH` that a login user owns lets that user run code as each slot:

```bash
ls -l /usr/local/bin/uv /usr/local/bin/rtk /usr/bin/gh /usr/bin/claude    # each root:root
sudo chown root:root /usr/local/bin/rtk                                   # if it is not
```

Tools the consumer project needs beyond these (for example `bun`) go in `/usr/local/bin` too, owned by root.
Without one, an agent downloads its own copy into its home, which nobody reviewed.

## 2. The kit and the practices

Both are clones owned by root, so the code that runs as the slots is not theirs to change:

```bash
sudo git clone https://github.com/glennmcd/fleet-kit.git /opt/fleet-kit
sudo git clone <the cc-practices repository> /opt/cc-practices-plugin
```

## 3. The machine and the slots

```bash
sudo /opt/fleet-kit/scripts/fleet_slot.py init --admin <you> --apply
sudo /opt/fleet-kit/scripts/fleet_slot.py add agent01 --repo https://github.com/<owner>/<name>.git --apply
sudo /opt/fleet-kit/scripts/fleet_slot.py add review01 --repo https://github.com/<owner>/<name>.git --apply
```

`init` creates the groups, the allocator's user, the `claude` login, `/var/lib/fleet`, the units, the polkit
rule and the sshd file. `add` creates one slot's user and clone. Log in again to get the `fleet-admin` group.

## 4. Secrets and the config

Written by hand, owned by root, mode 600, in `/etc/fleet` (mode 700). No admin reads them afterwards.

| File | Holds | Read by |
|---|---|---|
| `agent.env` | the model key (`ANTHROPIC_API_KEY`) and the authors' `GH_TOKEN` | every unit |
| `reviewer.env` | the reviewer account's `GH_TOKEN` and `FLEET_REVIEWER_LOGIN` | review slots only |
| `allocator.env` | a read-only `GH_TOKEN` (optional) | the allocator and the watchdog |
| `fleet.json` | the repository, the slots and the lanes | the allocator and the watchdog, through `LoadCredential` |

`fleet.json` lists the slots the allocator may use, so a slot that is not listed gets no work:

```json
{"repo": "<owner>/<name>", "slots": ["agent01", "agent02"], "review_slots": ["review01"],
 "lanes": {"feature": 3, "fix": 2, "tooling": 1}}
```

## 5. Each slot's Claude Code

Run as the slot, once per slot. `claude` keeps its settings and plugins in the slot's home:

```bash
sudo -u agent01 -H claude plugin marketplace add /opt/fleet-kit
sudo -u agent01 -H claude plugin marketplace add /opt/cc-practices-plugin
sudo -u agent01 -H claude plugin install fleet-kit@fleet-kit
sudo -u agent01 -H claude plugin install cc-practices@cc-practices
sudo -u agent01 -H rtk init -g --auto-patch
```

`rtk init` writes rtk's hook into the slot's `~/.claude/settings.json` and `RTK.md` beside a `CLAUDE.md` that
includes it.

**Skills.** A slot has the skills of its two plugins and no others: there is no `~/.claude/skills`, and the
slots sign in with an API key, not a claude.ai account, so claude.ai's skill sync has nothing to sync. Keep it
that way. A slot that is signed in to claude.ai gets that account's skills in every session's skill listing; in
round 2 they filled the room that parking 24 other skills had freed. `scripts/first_context.py` shows what a
session starts with.

## 6. Check, then enable

```bash
systemctl start fleet-check-agent@agent01 && journalctl -u fleet-check-agent@agent01 -o cat -n 20
systemctl start fleet-check-review@review01 && journalctl -u fleet-check-review@review01 -o cat -n 20
systemctl start fleet-allocate-dry-run && journalctl -u fleet-allocate-dry-run -o cat -n 50
sudo systemctl enable --now fleet-allocate.timer fleet-watchdog.timer
```

The first three need only `fleet-admin`. Each check must end with `every check passed`.

## Updating

The scripts and units run from `/opt/fleet-kit`. The hooks do not: `claude plugin install` copied the plugin
into each slot's `~/.claude/plugins/cache`, and a slot keeps running that copy until it is updated.

```bash
sudo git -C /opt/fleet-kit pull --ff-only
sudo /opt/fleet-kit/scripts/fleet_slot.py init --admin <you> --apply       # when units or the polkit rule changed
sudo -u agent01 -H claude plugin update fleet-kit@fleet-kit                # per slot, when hooks or skills changed
```

`init` rewrites the unit files, the polkit rule and the sshd file, then reloads systemd; existing users and
directories are kept. Enable any new timer afterwards (step 6).

## Adding a slot

```bash
sudo /opt/fleet-kit/scripts/fleet_slot.py add agent03 --repo https://github.com/<owner>/<name>.git --apply
```

Then step 5 for the new slot, its check from step 6, and its name in `/etc/fleet/fleet.json`. The allocator
reads the config on every pass, so nothing needs a restart.
