# Verdict at head

A pull request may merge only when an independent reviewer approved its **current head commit**. A push
after approval changes the head, so the approval no longer applies and the PR is reviewed again.

## The contract

The reviewer posts a PR comment with this marker on a line of its own, followed by a readable summary:

```
<!-- fleet-verdict v1 {"sha": "<40 lowercase hex>", "verdict": "approve"} -->
```

`verdict` is `approve` or `request_changes`. Extra fields (`reviewer`, `findings`) are allowed and ignored.
[`scripts/gates/verdict.py`](../scripts/gates/verdict.py) reads the comments and publishes a check run named
`reviewer-verdict` on the head SHA. A verdict counts only if:

- the author is in `verdict.reviewers` (an empty list lets nothing pass);
- the marker is a line of its own, outside code fences, quotes, lists and indented blocks, and is the only
  marker in the comment, in exactly the `v1` shape;
- `sha` is the full head SHA, lowercase: no short form, no branch name;
- the comment was never edited (a corrected verdict is a new comment);
- no newer verdict at the same head says otherwise: the newest wins.

A draft never passes. A head branch starting with one of `verdict.exempt_branches` (release-please's) passes
without a verdict; the match is a plain, case-sensitive prefix.

## Calling it

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, converted_to_draft]
  issue_comment:
    types: [created]

jobs:
  verdict:
    if: github.event_name == 'pull_request' || github.event.issue.pull_request
    permissions: {contents: read, checks: write, pull-requests: read, issues: read}
    uses: glennmcd/fleet-kit/.github/workflows/verdict.yml@<commit sha> # <tag>
    with:
      kit-ref: <the same commit sha>
```

Settings, in the repository's `.fleet-gates.json` on its default branch:

```json
"verdict": {"reviewers": ["glennmcd"], "exempt_branches": ["release-please--"]}
```

Then, in the branch ruleset, require the status check `reviewer-verdict` with its source set to **GitHub
Actions**, so a comment or a commit status from anyone else cannot satisfy it. Add it after the PR that
introduces the workflow merges: a check that has never run cannot be picked.

The ratchet gate refuses a PR that adds a reviewer or an exempt prefix, or removes the section.

## What it does and does not guarantee

It guarantees the verdict is about the exact commit that merges, was written by a listed account, and was not
quoted, edited or replayed from an older commit. Independence from the author comes from two things outside
this check. The reviewer has its own GitHub account, the only one in `verdict.reviewers`, whose token only the
fleet's review slots hold (`docs/control-plane.md`): an author's run cannot post an approval. And the reviewer
agent sees only the diff and the issue, never the author's reasoning. Anyone holding the reviewer's token can
still approve, so keep it on the fleet machine, readable by root only.

## Security notes

The workflow runs with a write token and, for comments, in the default branch's context. It never checks out
the pull request's code, takes the comment text only through the API (never into a shell), reads settings from
the default branch so a PR cannot change who may review it, and takes the kit at a pinned SHA. A fork's pull
request gets a read-only token, cannot publish the check, and so stays blocked until a listed reviewer
comments; that comment triggers the workflow in the base repository.

## The evasion kit

`tests/test_evasion_verdict.py` holds the fakes and replays: short and uppercase SHAs, a marker in a fence,
quote, list or inline, two markers, edited comments, lookalike logins, an old SHA after a push, and a later
`request_changes`. Add a case there before fixing any gap a review finds.
