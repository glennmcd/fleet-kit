"""Gate: a change may tighten the gates but never loosen them.

Incident (Marquardt): "An agent will find the gap in your gate. It is not malicious; it is optimising."
The shortest way past a failing gate is to edit the gate's settings, so the settings are compared with the
ones on the base branch and any loosening fails: a higher file-size cap or exception ceiling, a new
exception, a removed or disabled audit rule, a removed obligation or a narrower `also`, a smaller
duplication window, a larger duplication baseline. Loosening needs a person, who can change the base branch.

Run with `--base <commit>`: the base config is read with `git show <base>:.fleet-gates.json`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from gates.common import CONFIG_NAME, Finding, norm_path, run_gate

RULE = "gate-loosened"


def _f(message: str) -> Finding:
    return Finding(CONFIG_NAME, 1, RULE, message)


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = []
    o, n = old.get("file_size", {}), new.get("file_size", {})
    if "cap" in o and int(n.get("cap", 10**9)) > int(o["cap"]):
        found.append(_f(f"file_size.cap raised from {o['cap']} to {n.get('cap', 'unset')}"))
    old_exc = {norm_path(k): v for k, v in o.get("exceptions", {}).items()}
    for key, exc in n.get("exceptions", {}).items():
        before = old_exc.get(norm_path(key))
        if before is None:
            found.append(_f(f"new file-size exception for {key}; split the file instead"))
        elif int(exc.get("max_lines", 10**9)) > int(before.get("max_lines", 0)):
            found.append(_f(f"file-size exception for {key} raised to {exc.get('max_lines')}"))
    for key in n.get("exclude", []):
        if key not in o.get("exclude", []):
            found.append(_f(f"file_size.exclude gained `{key}`"))

    oa, na = old.get("audit", {}), new.get("audit", {})
    for rid in set(na.get("disable", [])) - set(oa.get("disable", [])):
        found.append(_f(f"audit rule `{rid}` disabled"))
    old_rules = {r.get("id"): r for r in oa.get("rules", [])}
    new_rules = {r.get("id"): r for r in na.get("rules", [])}
    for rid, rule in old_rules.items():
        if rid not in new_rules:
            found.append(_f(f"audit rule `{rid}` removed"))
        elif new_rules[rid].get("pattern") != rule.get("pattern") or new_rules[rid].get(
            "allow_if"
        ) != rule.get("allow_if"):
            found.append(_f(f"audit rule `{rid}` changed; replace it with a new rule instead"))
    for key in na.get("exclude", []):
        if key not in oa.get("exclude", []):
            found.append(_f(f"audit.exclude gained `{key}`"))

    old_ob = {r.get("id"): r for r in old.get("obligations", [])}
    new_ob = {r.get("id"): r for r in new.get("obligations", [])}
    for rid, rule in old_ob.items():
        if rid not in new_ob:
            found.append(_f(f"obligation `{rid}` removed"))
            continue
        if set(new_ob[rid].get("also", [])) < set(rule.get("also", [])) or set(
            new_ob[rid].get("except", [])
        ) > set(rule.get("except", [])):
            found.append(_f(f"obligation `{rid}` narrowed"))
        if set(new_ob[rid].get("when", [])) < set(rule.get("when", [])):
            found.append(_f(f"obligation `{rid}` now covers fewer paths"))

    od, nd = old.get("duplication", {}), new.get("duplication", {})
    if "min_lines" in od and int(nd.get("min_lines", 8)) > int(od["min_lines"]):
        found.append(_f("duplication.min_lines raised, so smaller copies pass"))
    for key in nd.get("exclude", []):
        if key not in od.get("exclude", []):
            found.append(_f(f"duplication.exclude gained `{key}`"))

    ov, nv = old.get("verdict"), new.get("verdict")
    if ov is not None and nv is None:
        found.append(_f("verdict section removed"))
    elif ov is not None and nv is not None:
        known = {r.lower() for r in ov.get("reviewers", [])}
        for login in nv.get("reviewers", []):
            if login.lower() not in known:
                found.append(_f(f"verdict.reviewers gained `{login}`"))
        for prefix in nv.get("exempt_branches", []):
            if prefix not in ov.get("exempt_branches", []):
                found.append(_f(f"verdict.exempt_branches gained `{prefix}`"))

    os_, ns = old.get("silent_skips", {}), new.get("silent_skips", {})
    for key in ns.get("exclude", []):
        if key not in os_.get("exclude", []):
            found.append(_f(f"silent_skips.exclude gained `{key}`"))
    return found


def compare_baselines(old: dict[str, int], new: dict[str, int]) -> list[Finding]:
    found = []
    for h, count in new.items():
        if count > old.get(h, 1):
            found.append(Finding("duplication-baseline", 1, RULE, f"baseline grew for block {h}"))
    return found


def _show(root: Path, base: str, path: str) -> str | None:
    p = subprocess.run(["git", "-C", str(root), "show", f"{base}:{path}"], capture_output=True)
    return p.stdout.decode() if p.returncode == 0 else None


def check(root: Path, config: dict[str, Any], args: argparse.Namespace) -> list[Finding]:
    if not args.base:
        return [_f("the ratchet needs --base <commit>")]
    old_text = _show(root, args.base, CONFIG_NAME)
    if old_text is None:
        return []  # the config is new on this branch: nothing to loosen yet
    found = compare(json.loads(old_text), config)
    path = config.get("duplication", {}).get("baseline", ".fleet-gates/duplication-baseline.json")
    old_base = _show(root, args.base, path)
    new_file = root / path
    if old_base is not None and new_file.exists():
        found += compare_baselines(json.loads(old_base), json.loads(new_file.read_text()))
    return found


def main(argv: list[str] | None = None) -> int:
    return run_gate("ratchet", check, argv)


if __name__ == "__main__":
    sys.exit(main())
