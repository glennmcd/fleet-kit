# Fleet machine setup

What root does once on the fleet machine, in order. After this, the `fleet-admin` group runs the fleet without
root ([control-plane.md](control-plane.md), *Who needs root*). The versions are what the first machine runs
(Ubuntu 26.04, checked 2026-11-14); they are a record, not a pin.

Every step is safe to run again. Commands that start with `sudo` are the operator's; the agent never runs them.

## 1. System tools

Every tool a slot runs is installed once for the whole machine, in `/usr/bin` or `/usr/local/bin`, owned by
root. Both folders are on each unit's `PATH`, so no slot needs its own copy. A slot cannot write either one,
so it cannot change a tool that it or another slot runs.

| Tool | Where | From | Why |
|---|---|---|---|
| `git`, `acl`, `polkitd`, `curl`, `unzip` | `/usr/bin` | Ubuntu | `setfacl` opens a slot's home to the admins; polkit holds the rule for the fleet's units |
| `node` 22, `npm` | `/usr/bin` | Ubuntu | the consumer project's formatter and tests |
| `gh` 2.48 or later (2.104.0) | `/usr/bin` | GitHub's apt repository | Ubuntu's own package is older and has no `gh api --slurp`, which the allocator needs |
| `claude` (2.1.311) | `/usr/bin` | Anthropic's apt repository | one copy for every slot; an apt install does not update itself |
| `uv`, `uvx` (0.12.21) | `/usr/local/bin` | release binary | every hook and script starts with `uv run --script` |
| `bun`, `bunx` (1.4.4) | `/usr/local/bin` | release binary | the consumer project's package manager and test runner |
| `rtk` (1.2.0) | `/usr/local/bin` | release binary | compact command output for the agents |

The commands are for x86-64. Run them as a login user with sudo, never as a slot.

**Ubuntu's packages:**

```bash
sudo apt update
sudo apt install git acl polkitd curl unzip gnupg nodejs npm
```

**`gh`**, from GitHub's repository:

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install gh
```

**`claude`**, from Anthropic's repository. The key check must print one fingerprint,
`31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`:

```bash
sudo curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc
gpg --show-keys /etc/apt/keyrings/claude-code.asc
echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
  | sudo tee /etc/apt/sources.list.d/claude-code.list
sudo apt update && sudo apt install claude-code
```

**`uv`, `bun` and `rtk`** are release binaries. Each is downloaded to a scratch folder, checked against the
release's checksum file, and copied into place by `install`, which sets the owner and mode in the same step:

```bash
cd "$(mktemp -d)"

# uv and uvx
curl -fsSLO https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz
curl -fsSLO https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz.sha256
sha256sum -c uv-x86_64-unknown-linux-gnu.tar.gz.sha256
tar -xzf uv-x86_64-unknown-linux-gnu.tar.gz
sudo install -o root -g root -m 755 uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/

# bun; bunx is the same binary under a second name
curl -fsSLO https://github.com/oven-sh/bun/releases/latest/download/bun-linux-x64.zip
curl -fsSLO https://github.com/oven-sh/bun/releases/latest/download/SHASUMS256.txt
sha256sum -c --ignore-missing SHASUMS256.txt
unzip -q bun-linux-x64.zip
sudo install -o root -g root -m 755 bun-linux-x64/bun /usr/local/bin/bun
sudo ln -sf bun /usr/local/bin/bunx

# rtk
curl -fsSLO https://github.com/rtk-ai/rtk/releases/latest/download/rtk-x86_64-unknown-linux-musl.tar.gz
curl -fsSLO https://github.com/rtk-ai/rtk/releases/latest/download/checksums.txt
sha256sum -c --ignore-missing checksums.txt
mkdir rtk && tar -xzf rtk-x86_64-unknown-linux-musl.tar.gz -C rtk
sudo install -o root -g root -m 755 "$(find rtk -type f -name rtk)" /usr/local/bin/rtk
```

Each `sha256sum` line must end in `OK`. Stop if one does not.

**Check the result.** Every line must show `root root` and a mode with no write bit for group or others
(`-rwxr-xr-x`). A tool on the slots' `PATH` that a login user owns lets that user run code as each slot:

```bash
ls -lL /usr/bin/gh /usr/bin/claude /usr/bin/node /usr/local/bin/{uv,uvx,bun,bunx,rtk}
ls -ld /usr/local/bin                                  # drwxr-xr-x root root
sudo chown root:root /usr/local/bin/rtk                # the fix for a wrong owner
sudo chmod 755 /usr/local/bin/rtk                      # and for a wrong mode
```

**Updating a tool** is the same commands again: `install` replaces the file in one step, so a running agent
keeps the copy it started with. `sudo apt update && sudo apt upgrade gh claude-code` covers the two apt ones.

A slot never installs a tool for itself. When a project needs one that is missing, an agent may download its
own copy into its home, which nobody reviewed: the first machine had no `bun`, and `agent01` fetched one into
`~/.local`. Add the tool here instead, and look for strays with `sudo ls /home/agent*/.local /home/agent*/.bun`.

## 2. The kit and the practices

Both are clones owned by root, so the code that runs as the slots is not theirs to change:

```bash
sudo git clone https://github.com/glennmcd/fleet-kit.git /opt/fleet-kit
sudo git clone <the cc-practices repository> /opt/cc-practices-plugin
```

## 3. The machine and the slots

```bash
sudo python3 /opt/fleet-kit/scripts/fleet_slot.py init --admin <you> --apply
sudo python3 /opt/fleet-kit/scripts/fleet_slot.py add agent01 --repo https://github.com/<owner>/<name>.git --git-email <id>+<login>@users.noreply.github.com --apply
sudo python3 /opt/fleet-kit/scripts/fleet_slot.py add review01 --repo https://github.com/<owner>/<name>.git --apply
```

`init` creates the groups, the allocator's user, the `claude` login, `/var/lib/fleet`, the units, the polkit
rule and the sshd file. `add` creates one slot's user and clone. An author slot also needs `--git-email`, the
noreply address of the account whose token pushes, and commits as `agentNN (fleet)` at it. Print the address
with that account's token: `gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'`. Log in again to get the `fleet-admin` group.

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
sudo python3 /opt/fleet-kit/scripts/fleet_slot.py init --admin <you> --apply       # when units or the polkit rule changed
sudo -u agent01 -H claude plugin update fleet-kit@fleet-kit                # per slot, when hooks or skills changed
```

`init` rewrites the unit files, the polkit rule and the sshd file, then reloads systemd; existing users and
directories are kept. Enable any new timer afterwards (step 6).

## Adding a slot

```bash
sudo python3 /opt/fleet-kit/scripts/fleet_slot.py add agent03 --repo https://github.com/<owner>/<name>.git --git-email <id>+<login>@users.noreply.github.com --apply
```

Then step 5 for the new slot, its check from step 6, and its name in `/etc/fleet/fleet.json`. The allocator
reads the config on every pass, so nothing needs a restart.
