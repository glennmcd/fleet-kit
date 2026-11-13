# CI gates

Each gate prints `path:line: [rule] message` per finding and exits 1 if there are any. All are standard-library
Python 3.12; run one locally with `PYTHONPATH=<fleet-kit>/scripts python -m gates.<name> --root . [--base <sha>]`.

Every gate exists because of a failure that slipped past review (Marquardt, *Guardrails for coding agents*),
and the repository's own settings must cite theirs: an audit rule or obligation without an `incident` is
refused.

## Calling the workflow

```yaml
jobs:
  gates:
    permissions:
      contents: read
    uses: glennmcd/fleet-kit/.github/workflows/gates.yml@<commit sha>
    with:
      kit-ref: <the same commit sha>        # must be a full SHA; the workflow refuses anything else
    secrets:
      kit-token: ${{ secrets.FLEET_KIT_TOKEN }}   # only while fleet-kit is private
```

While fleet-kit is a private repository, two things are needed: allow access under fleet-kit's
Settings > Actions > General > Access, and give the caller a read-only token for fleet-kit as `FLEET_KIT_TOKEN`
(the default token only reaches the calling repository). Neither is needed once it is public.

On a pull request every gate runs; on a push only the whole-tree gates (`silent_skips`, `file_size`,
`duplication`) run, since the diff gates already judged the change. Mark the `gates` job as a required check.

## Settings: `.fleet-gates.json`

```json
{
  "silent_skips": {"include": ["**/test_*.py"], "exclude": []},
  "file_size": {
    "cap": 800,
    "include": ["**/*.py", "**/*.ts"],
    "exclude": ["**/*.d.ts"],
    "exceptions": {"backend/big.py": {"max_lines": 950, "reason": "why", "issue": "#12"}}
  },
  "audit": {
    "disable": [],
    "exclude": [],
    "rules": [{"id": "log-transcript", "pattern": "log\(.*transcript", "paths": ["**/*.py"],
               "incident": "#9: transcript text reached the logs", "message": "never log transcripts"}]
  },
  "obligations": [{"id": "src-needs-tests", "when": ["src/**/*.py"], "also": ["tests/**"],
                   "except": ["**/__init__.py"], "incident": "#4: a route shipped untested"}],
  "duplication": {"min_lines": 8, "min_chars": 160, "baseline": ".fleet-gates/duplication-baseline.json"}
}
```

An issue reference is `#123` or an `/issues/123` or `/pull/123` URL. Globs: `**` crosses directories, `*` and
`?` do not.

## How each one works, and what it cannot see

- **silent_skips.** Python is parsed (so aliases like `from pytest import mark as m`, `getattr(pytest.mark,
  "skip")` and `pytest.importorskip` are caught); TypeScript is matched by pattern. A skip needs an issue
  reference on its line, the line above or the three below. `.only` never passes.
- **audit_greps.** Pull-request mode checks only added lines. `--all` sweeps the whole tree, for the weekly
  look at suppressions already in the code. A match passes if its line cites an issue (or the rule's `allow_if`).
- **file_size.** Counts lines whatever the line endings. An exception is a ceiling that only goes down; one
  for a file that shrank under the cap, or no longer exists, fails as stale.
- **obligations.** A rename or deletion counts as touching the old path, so moving a file out of a rule's reach
  does not escape it.
- **duplication.** Hashes runs of significant lines, so indentation, blank lines, comments and line endings
  do not hide a copy. It does not see renamed variables, reordered lines or a code line inserted into the copy;
  review does. Record today's duplicates once with `--write-baseline`; only new copies fail after that.
- **ratchet.** Compares the settings (and the duplication baseline) with those on the base commit.
  Loosening needs a person to change the base branch.

## The evasion kit

`tests/test_evasion_<gate>.py` holds the spellings an agent might try against each gate: `./tests/` against
`tests/`, a CRLF file, a decorator split over lines, a rename out of a rule's reach. A new gate is not finished
until its kit is: `tests/test_evasion_kit.py` fails if a gate module has none. When a review finds a gate blind
to a new spelling, add the spelling to the kit first, then fix the gate.
