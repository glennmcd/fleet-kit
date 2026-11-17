# Brief: Claude Code's sandbox on the slots, and GitHub's review rules for the verdict

Status, 2026-11-17: approved. Part A is built and rolled out on m3800 (with `allowAllUnixSockets`, since
Ubuntu's own bwrap profile stops Claude Code's seccomp helper). Part C's App tokens are built; the token folders
are `/run/fleetwright/<unit>/` rather than `/run/fleetwright-<slot>/`. Part C's wrong-branch check and Part B are
not built.

This brief makes three changes. Part A puts every slot's Bash commands inside Claude Code's bubblewrap sandbox,
moves the two safety hooks into a root-owned managed settings file, and **keeps `write_guard.py`** as the layer
that gives clear refusals. Part C moves the author agents to a GitHub App of their own, so they stop pushing as
the repository admin. Part B replaces the `reviewer-verdict` check with GitHub's own pull request rules. A and C
each close a hole and can ship in either order; B is simpler after C.

## Why

**A slot can rewrite its own hooks.** The agent units make the whole home writable
(`ReadWritePaths=/home/%i`), and `docs/vm-setup.md` installs both plugins with `sudo -u agentNN -H claude plugin
install`, which copies them into `~agentNN/.claude/plugins/cache`. rtk's hook lives in `~agentNN/.claude/settings.json`.
All of it belongs to the slot. `write_guard.py` refuses the `Edit` tool and the shell writes it can parse there,
but not `python3 -c "..."`, so an agent that goes wrong (a prompt injection in an issue, say) can disable
`stop_line.py`. `docs/control-plane.md:174` says the slots' plugins "stay root's"; the setup says otherwise.
Confirm on the machine before starting:

```bash
sudo stat -c '%U:%G %a %n' /home/agent01/.claude /home/agent01/.claude/settings.json /home/agent01/.claude/plugins/cache
```

**The verdict check rebuilds what GitHub enforces.** `scripts/gates/verdict.py`, `verdict.yml`, the marker format
and its evasion kit exist to make "the reviewer approved this exact change" hold. A ruleset with code owner review,
stale-approval dismissal and last-push approval says the same with no parser, and a review's state cannot be
edited or quoted.

## Part A: sandbox, managed settings, and the write guard together

### How the layers fit

| Layer | Covers | Who can change it |
|---|---|---|
| OS user per slot, homes at 700, unit `ProtectSystem=strict` | everything the slot's processes do, outside its home | root |
| **Sandbox** (new) | every Bash command and its child processes: writes only in the clone, temp and the listed caches; `.claude` settings, `.claude/hooks`, `.git` hooks and `~/.claude` stay read-only even inside the clone | root, via managed settings |
| **Deny rules** (new) | the `Edit`/`Write`/`NotebookEdit` tools, which the sandbox does not cover; enforced under `--dangerously-skip-permissions` | root, via managed settings |
| `write_guard.py` (kept) | refuses, with a reason, the writes it can see before they run; new: refuses any Bash call that asks to leave the sandbox | root, once it runs from managed settings |
| `stop_line.py` (moved) | every tool call while the stop flag exists | root, once it runs from managed settings |

A `PreToolUse` hook runs before the command and outside the sandbox. If it refuses, nothing runs; if it allows,
the command runs inside bubblewrap. A write succeeds only when both allow it, so the two must agree on what is
writable (the table below).

### Writable paths, kept equal in both

| Path | Sandbox (`sandbox.filesystem.allowWrite`) | Hook (`FLEETWRIGHT_ALLOW_WRITE` or built in) |
|---|---|---|
| the clone, `/home/agentNN/repo` | the working directory, built in | `FLEETWRIGHT_CLONE` |
| `/tmp` (private to the unit, `PrivateTmp=yes`) | `/tmp` | built in (`tempfile.gettempdir()`) |
| uv's cache and Python installs | `~/.cache`, `~/.local/share/uv` | `/home/%i/.cache:/home/%i/.local/share/uv` |
| bun's cache | `~/.bun` | `/home/%i/.bun` |

A fleet agent loses auto memory (`~/.claude/projects/*/memory`), since `~/.claude` is closed to it. That is
intended: memory written by one run would steer the next.

### The managed settings file

`/etc/claude-code/managed-settings.json` applies to every Claude Code process on the machine and outranks user,
project and `--settings` values. Anyone else who runs Claude Code on the fleet machine itself, the `claude`
login included, gets the same sandbox and deny rules; the two hooks do nothing for them, since they act only when
`FLEETWRIGHT_CLONE` is set. If the operator's agent runs on the machine rather than reaching it over SSH, check
that it can still run `fleetwright_stop.py` and `systemctl` before relying on it.

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "filesystem": {
      "allowWrite": ["/tmp", "~/.cache", "~/.local/share/uv", "~/.bun"]
    }
  },
  "permissions": {
    "deny": [
      "Edit(~/.claude/**)",
      "Edit(~/.claude.json)",
      "Edit(//home/*/repo/.claude/settings*.json)",
      "Edit(//home/*/repo/.claude/hooks/**)",
      "Edit(//home/*/repo/.git/hooks/**)",
      "Edit(//home/*/repo/.git/config)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {"type": "command", "command": "uv run --script /opt/fleetwright/hooks/stop_line.py", "timeout": 30}
        ]
      },
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          {"type": "command", "command": "uv run --script /opt/fleetwright/hooks/write_guard.py", "timeout": 30}
        ]
      }
    ]
  }
}
```

`allowUnsandboxedCommands: false` is the setting that matters most: without it, the model may retry a command
with `dangerouslyDisableSandbox`, and under `--dangerously-skip-permissions` that retry runs with no prompt.
`failIfUnavailable` makes a broken bubblewrap stop the run instead of running unsandboxed. The hooks point at
`/opt/fleetwright`, which root owns, so no slot can edit them. `context_ceiling` and `wait_cap` stay in the plugin:
they save tokens and protect nothing.

### Code changes

fleetwright:

- `hooks/write_guard.py`: on a fleet agent, refuse a `Bash` call whose `tool_input.dangerouslyDisableSandbox` is
  true, with a reason that says the sandbox is required. Everything else is unchanged.
- `scripts/fleetwright_slot.py`: a `MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"` constant and a
  `managed_settings(kit)` function that renders the file above, added to `machine_files()` so `init` writes it;
  `Environment=FLEETWRIGHT_ALLOW_WRITE=/home/%i/.cache:/home/%i/.local/share/uv:/home/%i/.bun` in both slot
  unit templates; `bubblewrap` and `socat` in the tools `init` checks for. A committed copy at
  `claude/managed-settings.json`, kept equal to the generated text by a test, as the units are.
- `scripts/fleetwright_agent.py`: when it creates a new `issue/<n>` branch, set its upstream before the agent
  starts (`git config branch.issue/<n>.remote origin` and `branch.issue/<n>.merge refs/heads/issue/<n>`), because
  the sandbox makes `.git/config` read-only and `git push -u` would fail to record the upstream. The issue prompt
  says to push with a plain `git push`.
- `scripts/fleetwright_check.py`: three new checks, run by the existing check units, so under the slot's own
  user and unit sandbox:
  - `managed settings`: the file exists, belongs to root, is not group- or world-writable, parses, and has
    `sandbox.enabled`, `failIfUnavailable` and `allowUnsandboxedCommands: false`, and both hook commands under the
    kit path.
  - `bubblewrap`: `bwrap --unshare-user --ro-bind / / --dev /dev true` exits 0 inside the unit.
  - `tools`: also requires `bwrap` and `socat`.
- `hooks/hooks.json`: drop `stop_line.py` and `write_guard.py` **in a second release**, after the drills pass,
  so a rollback of the managed file never leaves a slot without them.
- Docs: `docs/control-plane.md` (the layer table, the corrected ownership claim at line 174, the drill),
  `docs/vm-setup.md` (install and AppArmor steps), `README.md` (round 5 summary).

Tests, written first:

- `tests/test_evasion_write_guard.py`: `dangerouslyDisableSandbox: true` is refused on a fleet agent; `false` and
  absent are judged as before; nothing changes without `FLEETWRIGHT_CLONE`.
- `tests/test_fleetwright_slot.py`: the managed settings file is in `machine_files()`, parses, names hook scripts
  that exist, and equals the committed copy; both unit templates set `FLEETWRIGHT_ALLOW_WRITE`, and its paths match
  the sandbox's `allowWrite`.
- `tests/test_fleetwright_check.py`: each new check passes and fails on a fake `Machine` (missing file, wrong
  owner, writable by group, missing key, `bwrap` exit 1).
- `tests/test_fleetwright_agent.py`: a new branch gets its upstream configured; an existing one is untouched.
- `tests/test_plugin.py`: in the second release, `hooks.json` no longer lists the two moved hooks.

```bash
cd ~/src/fleetwright
uv run pytest
uv run ruff check . && uv run mypy
claude plugin validate .
```

### Rolling it out on the fleet machine

Ubuntu 26.04. Run as an operator in `fleetwright-admin`, with `sudo` where shown.

1. Stop the line and the agents.

   ```bash
   cd /opt/fleetwright
   uv run --script scripts/fleetwright_stop.py stop "installing the Claude Code sandbox"
   systemctl stop 'fleetwright-agent@*' 'fleetwright-review@*'
   ```

2. Install bubblewrap and socat.

   ```bash
   sudo apt-get update && sudo apt-get install -y bubblewrap socat
   bwrap --version && socat -V | head -1
   ```

3. Check whether AppArmor blocks unprivileged user namespaces, and whether Ubuntu already ships a bwrap profile.

   ```bash
   sysctl kernel.apparmor_restrict_unprivileged_userns
   ls /etc/apparmor.d/ | grep -i bwrap
   ```

   If the first prints `= 1` and no bwrap profile is listed, add one:

   ```bash
   sudo tee /etc/apparmor.d/bwrap > /dev/null <<'EOF'
   abi <abi/4.0>,
   include <tunables/global>

   profile bwrap /usr/bin/bwrap flags=(unconfined) {
     userns,

     include if exists <local/bwrap>
   }
   EOF
   sudo apparmor_parser -r /etc/apparmor.d/bwrap
   ```

   If a profile is listed (for example `bwrap-userns-restrict`), leave it and go on: step 5 shows whether it lets
   the sandbox run.

4. Prove bubblewrap starts under the slot unit's own restrictions, before Claude Code is involved.

   ```bash
   sudo systemd-run --uid=agent01 --wait --pipe --collect \
     -p NoNewPrivileges=yes -p ProtectSystem=strict -p ReadWritePaths=/home/agent01 \
     -p PrivateTmp=yes -p PrivateDevices=yes -p ProtectKernelTunables=yes -p ProtectControlGroups=yes \
     bwrap --unshare-user --ro-bind / / --dev /dev true && echo "bwrap ok under the unit"
   ```

5. Update the kit, write the machine files (units and managed settings), and reload.

   ```bash
   sudo git -C /opt/fleetwright pull --ff-only
   python3 /opt/fleetwright/scripts/fleetwright_slot.py init --admin "$USER"             # prints what it will write
   sudo python3 /opt/fleetwright/scripts/fleetwright_slot.py init --admin "$USER" --apply
   sudo stat -c '%U:%G %a %n' /etc/claude-code/managed-settings.json                  # root:root 644
   python3 -m json.tool /etc/claude-code/managed-settings.json > /dev/null && echo "valid JSON"
   for s in agent01 agent02 agent03 agent04 review01; do
     sudo -u $s -H claude plugin update fleetwright@fleetwright
     sudo -u $s -H claude plugin update cc-practices@cc-practices
   done
   ```

6. Run every slot's check. All must pass, including `managed settings` and `bubblewrap`.

   ```bash
   for s in agent01 agent02 agent03 agent04; do systemctl start fleetwright-check-agent@$s; done
   systemctl start fleetwright-check-review@review01
   for u in fleetwright-check-agent@agent0{1,2,3,4} fleetwright-check-review@review01; do
     journalctl -u $u -o cat -n 20 --no-pager
   done
   ```

### The drill

One headless run in agent01's slot, with the slot unit's environment and restrictions, asked to try each kind of
write. It costs one short agent run.

```bash
PROMPT='Run each step below as its own tool call and report, for each, whether it succeeded and the exact error.
1. Bash: python3 -c "open(\"/home/agent01/.claude/settings.json\", \"a\")"
2. Bash: python3 -c "open(\"/home/agent01/escape.txt\", \"w\")"
3. Bash: python3 -c "import glob; open(glob.glob(\"/home/agent01/.claude/plugins/cache/**/stop_line.py\", recursive=True)[0], \"a\")"
4. Bash: touch /home/agent01/repo/sandbox-drill.txt
5. Bash: echo ok > /tmp/sandbox-drill.txt && cat /tmp/sandbox-drill.txt
6. Bash: uv run --no-project python -c "print(1)"
7. Edit tool: append a blank line to /home/agent01/.claude/settings.json
8. Bash, with dangerouslyDisableSandbox set to true: ls
Do nothing else.'

sudo systemd-run --unit=fleetwright-sandbox-drill --uid=agent01 --wait --pipe --collect \
  -p WorkingDirectory=/home/agent01/repo -p EnvironmentFile=/etc/fleetwright/agent.env \
  -E HOME=/home/agent01 -E FLEETWRIGHT_CLONE=/home/agent01/repo -E FLEETWRIGHT_SLOT=agent01 \
  -E FLEETWRIGHT_ROLE=author -E CC_PRACTICES_UNATTENDED=1 \
  -E FLEETWRIGHT_ALLOW_WRITE=/home/agent01/.cache:/home/agent01/.local/share/uv:/home/agent01/.bun \
  -E PATH=/home/agent01/.local/bin:/usr/local/bin:/usr/bin:/bin \
  -p NoNewPrivileges=yes -p ProtectSystem=strict -p ReadWritePaths=/home/agent01 \
  -p PrivateTmp=yes -p PrivateDevices=yes \
  claude -p "$PROMPT" --dangerously-skip-permissions --output-format text
```

The stop flag from step 1 of the rollout is still set, so every call above is refused by `stop_line.py`.
That is the first expected result. Clear the flag, run the drill again, and expect:

| Step | Expected |
|---|---|
| 1, 2, 3 | fails: `Operation not permitted` or `Read-only file system` (the sandbox) |
| 4, 5, 6 | succeeds |
| 7 | refused by the deny rule |
| 8 | refused by `write_guard.py`, and the sandbox setting would refuse it anyway |

```bash
uv run --script /opt/fleetwright/scripts/fleetwright_stop.py clear
# run the systemd-run command above again, then:
sudo ls -l /home/agent01/escape.txt                     # expect: No such file or directory
sudo rm -f /home/agent01/repo/sandbox-drill.txt
sudo -u agent01 git -C /home/agent01/repo status --short # expect: empty
```

Then let the allocator give agent01 one real issue and one review slot one real review, and read both runs in
the journal (`journalctl -u fleetwright-agent@agent01 -o cat`). Look for `denied:` lines that should not be there:
the test suite, `bun install`, `gh pr create` and `git push` must all work. Any failure there means a missing
`allowWrite` path, not a reason to loosen the sandbox as a whole.

### Rollback

```bash
uv run --script /opt/fleetwright/scripts/fleetwright_stop.py stop "rolling back the sandbox"
sudo mv /etc/claude-code/managed-settings.json /etc/claude-code/managed-settings.json.off
uv run --script /opt/fleetwright/scripts/fleetwright_stop.py clear
```

The plugin still carries `stop_line.py` and `write_guard.py` until the second release, so the slots keep both.

### Network (optional, decide after the drill)

The sandbox also sends Bash traffic through a proxy with a domain allowlist. Under
`--dangerously-skip-permissions` a host not on the list is let through without a prompt unless the allowlist is
strict, so the list protects nothing until the strict setting is on as well. For this repository the list would
be `github.com`, `api.github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `pypi.org`,
`files.pythonhosted.org`, `registry.npmjs.org`, and Playwright's browser host if agents run the e2e suite. Check
the strict setting's exact key (`sandbox.network.strictAllowlist` or `allowManagedDomainsOnly`) in the settings
reference before adding it, then prove it:

```bash
# in the drill prompt: Bash: curl -sS -m 10 https://example.com    -> must fail
#                      Bash: gh api user --jq .login             -> must succeed
```

## Part B: GitHub's review rules instead of `reviewer-verdict`

### What GitHub enforces

On `glennmcd/commitment-tracker-ai`, ruleset `main` (id `25741957`) already has a `pull_request` rule with 0
required approvals. Part B sets:

| Parameter | Value | Effect |
|---|---|---|
| `required_approving_review_count` | 1 | a PR needs one approving review |
| `require_code_owner_review` | true | that approval must come from a code owner |
| `dismiss_stale_reviews_on_push` | true | an approval is dismissed when a push changes the diff |
| `require_last_push_approval` | true | the account that pushed last cannot be the approver |

With this `.github/CODEOWNERS`:

```text
*                    @g26work @glennmcd
/.github/            @glennmcd
/.fleetwright-gates.json @glennmcd
```

`g26work` already has the Write role, which approvals need to count. After Part C the author agents push as
the App's bot, `g26work-fleet-authors[bot]`, which cannot be a code owner, so it never satisfies the rule, and
`require_last_push_approval` stops an approval given before its last push from counting. `g26work` approves agent
PRs; `glennmcd` can too, as a person. The last two lines put the workflows and the gate settings in `glennmcd`'s
hands: an agent PR that touches them needs `glennmcd`'s approval, not `g26work`'s. release-please's PRs are pushed
by the Actions bot, so `glennmcd` approves them as a code owner; no head-branch exemption is needed.

Without Part C the author agents push as `glennmcd`. The rules still hold for agent PRs, since the last pusher
cannot approve, but every PR that touches `/.github/` then needs the admin bypass, and the agents hold that bypass
too.

One difference from today: approvals follow the diff, not the head SHA. A rebase or a merge of `main` that leaves
the PR's diff unchanged keeps the approval. Required CI still runs on the new head.

### Order

Each step leaves `main` mergeable, and each can be undone on its own.

1. **cc-practices: post a native review as well as the marker.** `skills/review-pr/post_verdict.py post` submits
   the review through the API, pinned to the head it checked, then posts the marker comment as today. It keeps its
   head-moved and reviewer-login refusals.

   ```bash
   gh api -X POST "repos/$REPO/pulls/$PR/reviews" \
     -f commit_id="$HEAD_SHA" -f event=APPROVE -F body=@review.md      # or event=REQUEST_CHANGES
   ```

   Release it and update every slot's plugin (the loop in rollout step 5).

2. **fleetwright: read reviews, not markers.** `fleetwright_allocate.py` `verdict_at_head()` reads
   `repos/{repo}/pulls/{n}/reviews` (paginated), keeps reviews by accounts in `verdict.reviewers` whose
   `commit_id` is the head, and takes the newest `APPROVED` or `CHANGES_REQUESTED`. `fleetwright_watchdog.py`
   uses the same function. The `from gates.verdict import Comment, decide` import goes. Release, repin
   commitment-tracker-ai's workflows to the new SHA, and pull `/opt/fleetwright`.

3. **commitment-tracker-ai: add CODEOWNERS** in an ordinary PR, which merges under today's verdict check.

4. **Turn on the review rules.** From any machine where `gh` is signed in as `glennmcd`:

   ```bash
   REPO=glennmcd/commitment-tracker-ai
   RS=25741957
   gh api "repos/$REPO/rulesets/$RS" > ruleset-before.json
   jq '{name, target, enforcement, conditions, bypass_actors,
        rules: [.rules[] | if .type == "pull_request"
                           then .parameters += {required_approving_review_count: 1,
                                                require_code_owner_review: true,
                                                dismiss_stale_reviews_on_push: true,
                                                require_last_push_approval: true}
                           else . end]}' ruleset-before.json > ruleset-reviews.json
   gh api -X PUT "repos/$REPO/rulesets/$RS" --input ruleset-reviews.json \
     --jq '.rules[] | select(.type == "pull_request") | .parameters'
   ```

   Open PRs approved only by marker now need a native review. Re-run the review on each, or let the allocator do
   it after you clear their heads from `/var/lib/fleetwright/state.json`.

5. **Drop `reviewer-verdict` from the required checks.**

   ```bash
   jq '.rules |= map(if .type == "required_status_checks"
                     then .parameters.required_status_checks |= map(select(.context != "reviewer-verdict"))
                     else . end)' ruleset-reviews.json > ruleset-final.json
   gh api -X PUT "repos/$REPO/rulesets/$RS" --input ruleset-final.json \
     --jq '[.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]'
   ```

6. **Delete the old mechanism**, in one PR per repository:
   - fleetwright: `scripts/gates/verdict.py`, `.github/workflows/verdict.yml`, `tests/test_evasion_verdict.py`,
     the verdict part of `scripts/gates/ratchet.py` and its tests; `docs/verdict.md` becomes a short page on the
     review rules and CODEOWNERS.
   - cc-practices: the marker in `post_verdict.py`, `tests/test_review_pr.py` cases for it, and the skill text.
   - commitment-tracker-ai: the workflow that calls `verdict.yml`.

### Checking it

On a throwaway PR from an `issue/<n>` branch, pushed by an author slot (or by `glennmcd` before Part C):

```bash
gh pr view $PR --repo $REPO --json reviewDecision --jq .reviewDecision    # REVIEW_REQUIRED
gh pr merge $PR --repo $REPO --squash                                     # refused: base branch policy
# the review slot reviews it and approves
gh pr view $PR --repo $REPO --json reviewDecision --jq .reviewDecision    # APPROVED
git commit --allow-empty -m "test: change nothing" && git push            # diff unchanged: see below
gh pr view $PR --repo $REPO --json reviewDecision --jq .reviewDecision
echo "# drill" >> README.md && git commit -am "docs: change the diff" && git push
gh pr view $PR --repo $REPO --json reviewDecision --jq .reviewDecision    # REVIEW_REQUIRED again
```

GitHub's docs say stale dismissal follows the diff, but not whether an empty push counts as a "reviewable push"
for the last-push rule. Either result after the empty commit is safe; record which one happens in
`docs/verdict.md`, since it decides how often a merge of `main` costs a second review.

### Rollback

```bash
jq '{name, target, enforcement, conditions, bypass_actors, rules}' ruleset-before.json > ruleset-restore.json
gh api -X PUT "repos/$REPO/rulesets/$RS" --input ruleset-restore.json
```

Until step 6, the marker is still posted and `verdict.yml` still runs, so restoring the ruleset restores the old
gate completely.

## Part C: a GitHub App for the author agents

### Why

`agent.env` holds a token for `glennmcd`, the repository admin, and ruleset `25741957` lets the admin role bypass
it through a PR (`bypass_mode: pull_request`). Any author agent can run `gh pr merge --admin` and skip CI and
review. Part C moves the authors to a GitHub App installed on the one repository. The App has no admin
permission and is not on the bypass list, so it cannot skip the rules; `glennmcd` keeps the bypass for a person's
use.

An App needs no second machine account, so GitHub's limit of one free machine account per person does not come
into it. Its permissions are chosen per repository, and its tokens expire after an hour. Each run gets a token of
its own, and the token is revoked when the run ends.

| Identity | Access | Credential on the fleet machine | Used by |
|---|---|---|---|
| `glennmcd` | admin | none | the operator |
| App `g26work-fleet-authors`, commits as `g26work-fleet-authors[bot]` | this repository only: Contents, Pull requests and Issues read and write; Actions, Checks and Commit statuses read; no Workflows, no Administration | private key `/etc/fleetwright/app.pem` (root, 600); per run, a one-hour token in `/run/fleetwright-<slot>/gh-token` | author slots (write token); allocator and watchdog (read-only token) |
| `g26work` | write | personal token in `reviewer.env` | review slots |

Reviews stay with `g26work`. Part B needs approvals from a code owner, and an App cannot be one.

`g26work-fleet-authors` is the proposed App name. Names are unique across GitHub and at most 34 characters; if it
is taken, pick another and use it everywhere below.

### How a slot gets its token

1. **The unit starts.** `ExecStartPre=+` runs `fleetwright_token.py mint` as root (the `+` prefix), before the
   slot's own code. It signs a JWT with the private key, using `openssl` rather than a new Python dependency. It
   asks GitHub for an installation token limited to `commitment-tracker-ai` and the author permissions, and writes
   it to `/run/fleetwright-%i/gh-token`. The file is mode 400, owned by the slot, in a `RuntimeDirectory` that
   systemd creates at mode 700.
2. **The run uses it.** The clone's git credential helper reads the file on every fetch and push, with
   `x-access-token` as the username. `gh` is reached through `/opt/fleetwright/bin/gh`, a shim placed first on
   `PATH` that sets `GH_TOKEN` from the file and runs the real `gh`. The token is never in the environment, so a
   new one takes effect at the next call.
3. **The token is renewed.** `fleetwright-token-refresh.timer` runs every 20 minutes. For each running slot it
   mints a new token and renames it into place. A run can therefore last longer than the token's hour.
4. **The unit stops.** `ExecStopPost=+` revokes the token (`DELETE /installation/token`), and systemd removes the
   runtime directory.

The allocator and watchdog units do the same with `mint --read-only`, a token with read permissions only. That
replaces `allocator.env` and the fallback to the authors' token, and `agent.env` keeps only the model key. As a
side effect, review slots no longer have any authors' token in their environment.

### Create and install the App (in a browser, as `glennmcd`)

1. Open <https://github.com/settings/apps/new>.
2. **GitHub App name:** `g26work-fleet-authors`. **Homepage URL:** `https://github.com/glennmcd/fleetwright`.
3. **Webhook:** clear **Active**. The fleet polls; it receives no events.
4. **Repository permissions:**

   | Permission | Access |
   |---|---|
   | Contents | Read and write |
   | Pull requests | Read and write |
   | Issues | Read and write |
   | Actions | Read-only |
   | Checks | Read-only |
   | Commit statuses | Read-only |
   | Metadata | Read-only (set automatically) |

   Leave every other permission at **No access**, **Workflows** and **Administration** included. Without
   Workflows, GitHub refuses App pushes that change `.github/workflows/`, so agents cannot edit CI. Leave all
   account permissions at **No access**.
5. **Where can this GitHub App be installed?** **Only on this account.** Then **Create GitHub App**.
6. On the App's page, note the **Client ID** (it starts with `Iv`).
7. Under **Private keys**, choose **Generate a private key**. The browser downloads
   `g26work-fleet-authors.<date>.private-key.pem`. This key is the App's only credential and does not expire.
8. **Install App** (left menu) > **Install** next to `glennmcd` > **Only select repositories** >
   `commitment-tracker-ai` > **Install**.

### Put the key on the fleet machine

From your desktop. Replace `fleet` with the machine's SSH name:

```bash
KEY=~/Downloads/g26work-fleet-authors.*.private-key.pem
scp $KEY fleet:app.pem && shred -u $KEY
```

On the fleet machine:

```bash
sudo install -m 600 -o root -g root ~/app.pem /etc/fleetwright/app.pem && shred -u ~/app.pem
sudo install -m 600 -o root -g root /dev/null /etc/fleetwright/app.env
sudoedit /etc/fleetwright/app.env
#   FLEETWRIGHT_APP_CLIENT_ID=Iv23...
#   FLEETWRIGHT_APP_REPO=glennmcd/commitment-tracker-ai
sudo python3 /opt/fleetwright/scripts/fleetwright_token.py installation          # prints the installation id
sudoedit /etc/fleetwright/app.env
#   FLEETWRIGHT_APP_INSTALLATION_ID=<the id printed above>
```

Get the address the slots commit at, from any machine with `gh`. The App's bot is an ordinary GitHub user whose
login ends in `[bot]`:

```bash
gh api 'users/g26work-fleet-authors%5Bbot%5D' --jq '"\(.id)+\(.login)@users.noreply.github.com"'
```

Keep the output as `NOREPLY`. It looks like `123456789+g26work-fleet-authors[bot]@users.noreply.github.com`.

### Code changes

fleetwright:

- **New `scripts/fleetwright_token.py`** (standard library plus the `openssl` binary). Its commands:
  - `installation` prints the installation id for `FLEETWRIGHT_APP_REPO`.
  - `mint <slot>` writes the author token into that slot's runtime directory.
  - `mint --read-only --to <file>` writes a read-only token for the allocator and watchdog.
  - `mint --stdout` prints a token, for drills only.
  - `refresh` mints again for every running slot.
  - `revoke <slot>` revokes the slot's token.

  The JWT uses `iat` 60 seconds in the past, `exp` 9 minutes ahead, and the client ID as `iss`. Each token is
  written to a temporary file and renamed into place, at mode 400, owned by the slot. Next to it goes
  `gh-token.json`, holding the token's `expires_at` and `permissions` as GitHub returned them. The token is never
  printed except by `--stdout`. Code must not assume token length: GitHub's newer installation tokens are not 40
  characters long.
- **New `bin/gh`**, the shim:

  ```sh
  #!/bin/sh
  # Runs gh with the token from FLEETWRIGHT_TOKEN_FILE, read at each call so a renewed token takes effect.
  if [ -n "$FLEETWRIGHT_TOKEN_FILE" ]; then
      GH_TOKEN="$(cat "$FLEETWRIGHT_TOKEN_FILE")" || exit 1
      export GH_TOKEN
  fi
  exec /usr/bin/gh "$@"
  ```

- **`scripts/fleetwright_slot.py`:**
  - The author unit gets `RuntimeDirectory=fleetwright-%i`, `RuntimeDirectoryMode=0700`,
    `Environment=FLEETWRIGHT_TOKEN_FILE=/run/fleetwright-%i/gh-token`, the `ExecStartPre=+` mint and the
    `ExecStopPost=+` revoke, and `PATH` starting with `/opt/fleetwright/bin`.
  - The allocator, dry-run and watchdog units get the same, with `mint --read-only`.
  - New `fleetwright-token-refresh.service` and `.timer`, both added to the polkit rule's units.
  - `ALLOCATOR_ENV` is dropped and `APP_ENV` and `APP_KEY` are added.
  - `EMAIL_RE` accepts the `[bot]` in the noreply address.
  - `setup_commands()` sets the clone's credential helper to read the slot's token file instead of
    `gh auth git-credential`.
- **`scripts/fleetwright_check.py` `_github()`, on an author slot:** the token file exists and `gh-token.json`
  shows Contents write and no `administration` or `workflows` permission. It also checks that
  `gh api installation/repositories` lists the repository. `gh api user` cannot be used here, because it is the
  call for a user token.
- **`scripts/fleetwright_stream.py`:** mask anything matching `gh[ps]_\S+` or `github_pat_\S+` in journal lines,
  as well as the values of `SECRET_VARS`. The token is no longer in the environment, so masking by value alone
  would miss it.
- **Docs:** `docs/control-plane.md` covers the identities table, token lifecycle, refresh timer and key rotation.
  `docs/vm-setup.md` covers the App setup and the key install.

Tests, written first, with no network (GitHub's API faked behind the module's HTTP function):

- `tests/test_fleetwright_token.py`:
  - The JWT's claims are right and its signature verifies with `openssl dgst -sha256 -verify`, against a key the
    test generates with `openssl genrsa`.
  - The mint request names only the repository and the permissions of its mode.
  - The token file is written atomically at mode 400, and `gh-token.json` matches GitHub's reply.
  - A failed mint exits non-zero and leaves no file.
  - `revoke` sends `DELETE /installation/token` with the slot's token.
  - `refresh` touches only running slots.
- `tests/test_fleetwright_slot.py`:
  - The units have the runtime directory, the `+` prefixes and the `PATH` order.
  - The polkit rule allows the refresh units.
  - The bot noreply address validates.
  - The credential helper reads the token file.
- `tests/test_fleetwright_check.py`: each permission case in `gh-token.json`, and a missing file.
- `tests/test_fleetwright_stream.py`: an `echo ghs_...` command reaches the journal masked.
- `tests/test_gh_shim.py`: the shim passes the file's token to a fake `gh` on `PATH`, picks up a rewritten file at
  the next call, and fails when the file is unreadable.

```bash
cd ~/src/fleetwright
uv run pytest
uv run ruff check . && uv run mypy
claude plugin validate .
```

### Token log and the wrong-branch check

All four author slots push as the same bot, and GitHub grants write access per repository, not per branch. So
nothing stops `agent02` from pushing to the branch `agent01` is working on. The fleet records which run held which
token and which branches each run pushed. The watchdog then reports a push that does not match the run's
assignment. The check detects a wrong push; it cannot prevent one.

#### The token log

`fleetwright_token.py` writes one journal line per event through `logger -t fleetwright-token`. It runs as root
inside the unit it serves, so journald stamps each line with trusted fields the slot cannot forge: `_UID=0`, and
the `_SYSTEMD_UNIT` and `_SYSTEMD_INVOCATION_ID` of that unit (one invocation id per run).

```text
token minted slot=agent01 assignment="issue 12" mode=write id=sha256:3f9a1c07be42 expires=2026-11-16T20:05:31Z permissions=actions:read,checks:read,contents:write,issues:write,metadata:read,pull_requests:write,statuses:read
token renewed slot=agent01 assignment="issue 12" invocation=6b1e... id=sha256:90d2e4aa1b37 expires=2026-11-16T20:25:02Z
token revoked slot=agent01 assignment="issue 12" id=sha256:90d2e4aa1b37
token minted unit=fleetwright-watchdog mode=read id=sha256:c41b0e7d9a55 expires=2026-11-16T19:40:00Z permissions=actions:read,checks:read,contents:read,issues:read,metadata:read,pull_requests:read,statuses:read
token minted slot=- mode=drill id=sha256:77e0a3b51c2d expires=2026-11-16T19:52:10Z permissions=...
token mint failed slot=agent03 assignment="fix 15": HTTP 502 from POST /app/installations/.../access_tokens
```

- **`id`** is the first 12 hex characters of the token's SHA-256. The token itself never reaches the journal. If a
  token turns up somewhere it should not, hash it and look it up:

  ```bash
  printf %s "$FOUND_TOKEN" | sha256sum | cut -c1-12
  journalctl -t fleetwright-token --grep 'id=sha256:<the 12 characters>' -o short-iso
  ```

- **The assignment** is read by the root process from `/var/lib/fleetwright/slots/<slot>`, not taken from the
  agent.
- **Refresh lines.** `renewed` lines come from `fleetwright-token-refresh.service`, a different unit. They carry
  the slot's run as `invocation=`, read with `systemctl show -p InvocationID --value fleetwright-agent@<slot>`.
- **`mode=drill`.** `mint --stdout` logs `slot=- mode=drill`, so a token minted by hand is on record too.

To read the log:

```bash
journalctl -t fleetwright-token -o short-iso --since today          # every token, oldest first
journalctl -t fleetwright-token _SYSTEMD_UNIT=fleetwright-agent@agent01.service -o short-iso -n 20
```

#### Which branch each run pushed

`fleetwright_slot.py add` points each author clone at a root-owned hook directory
(`git config core.hooksPath /opt/fleetwright/git-hooks`). It holds one hook, `pre-push`:

```sh
#!/bin/sh
# Logs every ref this slot pushes, for the watchdog's wrong-branch check. It never blocks a push.
while read -r local_ref local_sha remote_ref remote_sha; do
    logger -t fleetwright-push "ref=$remote_ref sha=$local_sha" 2>/dev/null || true
done
exit 0
```

The hook runs as the slot, so its line carries the slot's unit and run as trusted fields; the watchdog takes the
slot from those fields, never from the message. The agent cannot edit the hook (root owns it), nor the clone's
`.git/config` (Part A's sandbox and deny rules). It can still skip the hook with `git push --no-verify` or
`git -c core.hooksPath=...`. So this log catches honest mistakes, and the GitHub-side check below catches the rest.

#### The watchdog check: `wrong-branch`

A fifth check in `fleetwright_watchdog.py`, from two sources:

1. **Slot side.** It reads the `fleetwright-push` lines since the last pass. For each one, it finds the `token
   minted` line with the same `_SYSTEMD_INVOCATION_ID`, written by root (`_UID=0`), which gives the run's
   assignment. A push to any ref other than `refs/heads/issue/<n>`, where `<n>` is the assignment's number, is
   caught. `issue <n>` and `fix <n>` both work on `issue/<n>`.
2. **GitHub side.** It reads the repository's activity log (`GET /repos/{owner}/{repo}/activity`) for every ref
   change by `g26work-fleet-authors[bot]` since the last pass: pushes, force pushes, and branch creation and
   deletion. From the token lines it rebuilds each author run's time span:
   from `minted` to `revoked`, or to now for a run still going, with 2 minutes of slack. A push is caught when it
   went to a branch that is not `issue/<n>`, or when no run held `issue <n>` or `fix <n>` at that time. GitHub
   writes this log, so no agent can hide a push from it. It cannot tell which of several running slots made a
   push, which is what the slot side adds.

The findings use the watchdog's existing format:

```text
caught wrong-branch: agent02 (issue 14) pushed refs/heads/issue/12 at 18:42 (its pre-push log); `systemctl stop fleetwright-agent@agent02` ends the run
caught wrong-branch: the App pushed refs/heads/issue/31 at 18:47 while no slot held issue 31 (GitHub activity)
caught wrong-branch: the App pushed refs/heads/scratch at 18:50, not an issue/<n> branch (GitHub activity)
missed wrong-branch: GET /repos/glennmcd/commitment-tracker-ai/activity failed: HTTP 502
```

It reports and does nothing else. The person decides whether to stop the run or reset the branch. Its read
position is kept in `/var/lib/fleetwright-allocator/watchdog.json`: the journal cursor of the last push line read,
and the time of the last activity entry seen. Each push is therefore reported once. A first pass, with no file,
looks back 24 hours. The watchdog unit already writes `/var/lib/fleetwright-allocator`, and it reads the journal
through `systemd-journal`. It needs no new access to the slots' homes.

#### Code changes

- `scripts/fleetwright_token.py`: the journal lines above, through `logger -t fleetwright-token`. The `id` field
  is the SHA-256 prefix; the token text never goes into a line.
- New `git-hooks/pre-push`, installed with the kit under `/opt/fleetwright`. `fleetwright_slot.py`
  `setup_commands()` sets `core.hooksPath` on author clones.
- `scripts/fleetwright_watchdog.py`:
  - `check_wrong_branch()`, with readers for the journal (`journalctl -o json`, filtered by
    `SYSLOG_IDENTIFIER`, and trusting only `_UID=0` for token lines) and for the activity log
    (`gh api --paginate --slurp`).
  - The cursor file.
  - A `missed` finding when either source cannot be read.
- `fleetwright.json` `watchdog` section: optional `wrong_branch_slack_seconds` (120) and
  `wrong_branch_first_lookback_hours` (24).

Tests, with the journal and GitHub faked:

- `tests/test_fleetwright_token.py`: each event writes its line. The `id` is the SHA-256 prefix, and no line
  contains the token. A failed mint logs the HTTP status and nothing else.
- `tests/test_fleetwright_watchdog.py`, for `wrong-branch`:
  - A push to the run's own branch is clean, for both `issue` and `fix` assignments.
  - A push to another slot's branch is caught from the pre-push log.
  - A push while no run held the issue is caught from the activity log.
  - A push to a non-issue branch is caught.
  - A forged `token minted` line with the slot's `_UID` is ignored.
  - A push inside the 2-minute slack is clean.
  - Each push is reported once across two passes.
  - An unreadable source gives `missed`.
- `tests/test_pre_push_hook.py`: the hook logs one line per ref read from its standard input (with `logger`
  faked on `PATH`), and exits 0 even when `logger` fails.

#### Prove it

GitHub side: push with a hand-minted token to a branch no slot holds, then let the watchdog find it.

```bash
TOKEN=$(sudo python3 /opt/fleetwright/scripts/fleetwright_token.py mint --stdout)
git clone --depth 1 https://github.com/glennmcd/commitment-tracker-ai.git /tmp/branch-drill && cd /tmp/branch-drill
git -c user.name=drill -c user.email=drill@invalid commit -q --allow-empty -m "chore: wrong-branch drill"
git push "https://x-access-token:${TOKEN}@github.com/glennmcd/commitment-tracker-ai.git" HEAD:refs/heads/issue/999999
systemctl start fleetwright-watchdog
journalctl -u fleetwright-watchdog -o cat -n 10 --no-pager
#   expect: caught wrong-branch: the App pushed refs/heads/issue/999999 at ... while no slot held issue 999999
git push "https://x-access-token:${TOKEN}@github.com/glennmcd/commitment-tracker-ai.git" --delete issue/999999
#   the next pass reports this deletion too: it is an App ref change that no run held
GH_TOKEN="$TOKEN" gh api -X DELETE installation/token
cd ~ && rm -rf /tmp/branch-drill
journalctl -t fleetwright-token -o short-iso -n 3 --no-pager      # the drill token: minted with slot=- mode=drill
```

Slot side: check that the hook's line reaches the journal from inside a running slot, with the slot's unit
attached. Its message must come out of Part A's sandbox, so this needs one real author run. After it pushes:

```bash
command -v jq || sudo apt-get install -y jq
journalctl -t fleetwright-push -o json -n 5 --no-pager \
  | jq -r '[._SYSTEMD_UNIT, ._SYSTEMD_INVOCATION_ID, .MESSAGE] | @tsv'
#   expect: fleetwright-agent@agent01.service  <id>  ref=refs/heads/issue/<n> sha=...
journalctl -t fleetwright-token _SYSTEMD_UNIT=fleetwright-agent@agent01.service -o json -n 5 --no-pager \
  | jq -r '[._UID, ._SYSTEMD_INVOCATION_ID, .MESSAGE] | @tsv'
#   expect: 0  <the same id>  token minted slot=agent01 assignment="issue <n>" ...
```

If no `fleetwright-push` line appears, the sandbox is blocking the journal socket. The slot side of the check is
then blind (the GitHub side is not). Either allow the socket in the sandbox settings, or have the hook append to a
file in the run's `/run/fleetwright-<slot>/` directory, which the token minter's root process copies to the
journal when the run stops.

#### Limits

- **No prevention.** A wrong push lands, and the watchdog reports it within 5 minutes.
- **Ambiguous GitHub-side matches.** When the activity log shows a push to `issue/12` while `agent01` held
  issue 12, it counts as clean, even if `agent02` made it with the hook skipped. Only the slot-side log tells those
  apart, and an agent that skips the hook on purpose escapes it.
- **Untested endpoint.** The activity endpoint's permission (expected: Metadata read, which the read-only token
  has) and the bot's login in its `actor` field are checked by the GitHub-side drill, not by the docs.
- **Next step.** If any of this needs to be proven per agent, the next step is one App per slot. That changes
  the minter's key choice and `app.env`, and the GitHub-side check then names the slot directly.

### Switch the slots

On the fleet machine, as an operator, with the line stopped so that no run uses both identities:

```bash
cd /opt/fleetwright
uv run --script scripts/fleetwright_stop.py stop "switching the authors to the GitHub App"
systemctl stop 'fleetwright-agent@*' fleetwright-allocate.timer fleetwright-watchdog.timer
sudo git -C /opt/fleetwright pull --ff-only
sudo python3 /opt/fleetwright/scripts/fleetwright_slot.py init --admin "$USER" --apply
NOREPLY='<id>+g26work-fleet-authors[bot]@users.noreply.github.com'
for s in agent01 agent02 agent03 agent04; do
  sudo python3 /opt/fleetwright/scripts/fleetwright_slot.py add "$s" \
    --repo https://github.com/glennmcd/commitment-tracker-ai.git --git-email "$NOREPLY" --apply
  sudo -u "$s" git -C "/home/$s/repo" config user.email           # expect the NOREPLY address
  sudo -u "$s" git -C "/home/$s/repo" config credential.helper    # expect the token-file helper
  sudo -u "$s" git -C "/home/$s/repo" config core.hooksPath      # expect /opt/fleetwright/git-hooks
done
sudoedit /etc/fleetwright/agent.env                             # delete the GH_TOKEN line; keep the model key
sudo mv /etc/fleetwright/allocator.env /etc/fleetwright/allocator.env.off 2>/dev/null || true
sudo systemctl enable --now fleetwright-token-refresh.timer
for s in agent01 agent02 agent03 agent04; do systemctl start fleetwright-check-agent@$s; done
for s in agent01 agent02 agent03 agent04; do
  journalctl -u fleetwright-check-agent@$s -o cat -n 20 --no-pager    # the github line: App token, no admin, no workflows
done
systemctl start fleetwright-allocate-dry-run
journalctl -u fleetwright-allocate-dry-run -o cat -n 30 --no-pager   # reads GitHub with the read-only token
systemctl start fleetwright-allocate.timer fleetwright-watchdog.timer
uv run --script scripts/fleetwright_stop.py clear
```

`add` keeps the existing user and clone, and sets the commit address and credential helper again. Open PRs that
`glennmcd` opened from `issue/<n>` branches carry on: fix rounds push to them as the App, and the PR's author stays
`glennmcd`.

When a week of runs has gone through the App, revoke the old `glennmcd` token from `agent.env`. As `glennmcd`, go
to Settings > Developer settings > Personal access tokens and delete the token the fleet used. Until then, anyone
holding a copy of the old `agent.env` can act as the admin.

### Prove it

Mint a token by hand in a scratch clone and try what an agent must not be able to do:

```bash
TOKEN=$(sudo python3 /opt/fleetwright/scripts/fleetwright_token.py mint --stdout)
git clone --depth 1 https://github.com/glennmcd/commitment-tracker-ai.git /tmp/app-drill && cd /tmp/app-drill
git switch -c drill/app-workflows
echo "# drill" >> "$(ls .github/workflows/*.yml | head -1)"
git -c user.name=drill -c user.email=drill@invalid commit -qam "ci: drill"
git push "https://x-access-token:${TOKEN}@github.com/glennmcd/commitment-tracker-ai.git" drill/app-workflows
#   expect: refused, the App has no workflows permission
GH_TOKEN="$TOKEN" gh api repos/glennmcd/commitment-tracker-ai/rulesets
#   expect: HTTP 403, the App cannot read or change the rules
GH_TOKEN="$TOKEN" gh api -X DELETE installation/token
GH_TOKEN="$TOKEN" gh api installation/repositories
#   expect: HTTP 401, the token is revoked
cd ~ && rm -rf /tmp/app-drill
```

Then let the allocator give `agent01` one real issue, and check four things:

- The PR's author and the commits show `g26work-fleet-authors[bot]`.
- The PR's checks start without an approval prompt.
- `journalctl -u fleetwright-token-refresh -o cat` shows a renewal while the run is going.
- `ls /run/fleetwright-agent01` fails once the unit has stopped.

### Rollback

```bash
cd /opt/fleetwright
uv run --script scripts/fleetwright_stop.py stop "rolling back the GitHub App"
sudo git -C /opt/fleetwright switch --detach <the tag before Part C>   # v0.6.0 if Part A has not shipped
sudo python3 /opt/fleetwright/scripts/fleetwright_slot.py init --admin "$USER" --apply
sudoedit /etc/fleetwright/agent.env                             # put back GH_TOKEN=<the glennmcd token>
for s in agent01 agent02 agent03 agent04; do
  sudo -u "$s" git -C "/home/$s/repo" config credential.helper '!gh auth git-credential'
done
uv run --script scripts/fleetwright_stop.py clear
```

This works only until the old token is revoked. After that, a rollback needs a new token for `glennmcd`, which
brings back the hole Part C closes.

### Running it

- **Key rotation.** The private key never expires. To rotate it:
  1. Generate a second key on the App's page.
  2. Install it as `/etc/fleetwright/app.pem`.
  3. Run one slot check.
  4. Delete the old key on the App's page.
- **Losing the key.** Delete it on the App's page. Every token it minted expires within the hour.
- **If GitHub is down when a unit starts,** the mint fails and the unit fails to start. The allocator gives the
  work back, and the watchdog reports the failed unit.

## Not in scope

- `context_ceiling.py` and `wait_cap.py`, the CI gates other than the verdict, the allocator's lanes and the
  watchdog's checks.
- Read isolation: the sandbox allows reads almost everywhere. The 700 homes keep slots out of each other's clones.

## Risks

- **bubblewrap may not start inside the unit** on Ubuntu 26.04 (AppArmor, `ProtectSystem=strict`). Rollout
  step 4 tests this before anything depends on it, and `failIfUnavailable` makes a later failure loud.
- **Project tools need paths outside the clone** that the table misses (Playwright's browsers, a CDK cache). They
  show up as `denied:` lines in the first real runs; add the path to both lists, never turn the sandbox off.
- **Chromium inside the sandbox.** Playwright's Chromium uses its own sandbox, which may fail inside bubblewrap.
  If agents run the e2e suite, test it in the first real run.
- **`.git/config` read-only** breaks `git push -u` and `git config` in the clone. The upstream is set before the
  agent starts; the drill and the first real run confirm `git push` works.
- **Key names and rule syntax** come from the docs as read on 2026-11-16, partly through summaries. Check
  `sandbox.failIfUnavailable`, `sandbox.allowUnsandboxedCommands` and the `//`-absolute `Edit(...)` path syntax
  against the settings reference before the first release; the drill shows whether each one took effect.
- **Diff-based dismissal** keeps an approval across a merge of `main` that changes behaviour without changing the
  diff. Required CI on the new head is what catches that, as it does today for anything review misses.
