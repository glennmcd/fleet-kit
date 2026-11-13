"""Evasion kit for the ratchet: every way to loosen a gate must be caught."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from gates import ratchet

BASE = {
    "file_size": {
        "cap": 800,
        "exclude": ["**/schema.d.ts"],
        "exceptions": {"a.py": {"max_lines": 900, "reason": "r", "issue": "#1"}},
    },
    "audit": {"rules": [{"id": "print", "pattern": "print\\(", "incident": "#2"}]},
    "obligations": [
        {"id": "o", "when": ["src/**"], "also": ["tests/**", "docs/**"], "incident": "#3"}
    ],
    "duplication": {"min_lines": 8},
}


def edit(fn: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    cfg = copy.deepcopy(BASE)
    fn(cfg)
    return cfg


LOOSENINGS = {
    "cap raised": lambda c: c["file_size"].__setitem__("cap", 1000),
    "cap removed": lambda c: c["file_size"].pop("cap"),
    "exception raised": lambda c: c["file_size"]["exceptions"]["a.py"].__setitem__(
        "max_lines", 950
    ),
    "exception spelled differently": lambda c: c["file_size"]["exceptions"].__setitem__(
        "./a.py", c["file_size"]["exceptions"].pop("a.py") | {"max_lines": 999}
    ),
    "new exception": lambda c: c["file_size"]["exceptions"].__setitem__("b.py", {"max_lines": 850}),
    "new exclude": lambda c: c["file_size"]["exclude"].append("backend/**"),
    "default rule disabled": lambda c: c["audit"].__setitem__("disable", ["noqa"]),
    "rule removed": lambda c: c["audit"].__setitem__("rules", []),
    "rule pattern changed": lambda c: c["audit"]["rules"][0].__setitem__("pattern", "nothing"),
    "rule allow_if widened": lambda c: c["audit"]["rules"][0].__setitem__("allow_if", ".*"),
    "audit exclude": lambda c: c["audit"].__setitem__("exclude", ["src/**"]),
    "obligation removed": lambda c: c.__setitem__("obligations", []),
    "obligation narrowed": lambda c: c["obligations"][0].__setitem__("also", ["docs/**"]),
    "obligation excepted": lambda c: c["obligations"][0].__setitem__("except", ["src/**"]),
    "obligation when narrowed": lambda c: c["obligations"][0].__setitem__("when", []),
    "min_lines raised": lambda c: c["duplication"].__setitem__("min_lines", 20),
    "duplication exclude": lambda c: c["duplication"].__setitem__("exclude", ["src/**"]),
    "skips exclude": lambda c: c.__setitem__("silent_skips", {"exclude": ["tests/**"]}),
}


@pytest.mark.parametrize("name", LOOSENINGS)
def test_loosening_is_caught(name: str) -> None:
    assert ratchet.compare(BASE, edit(LOOSENINGS[name])), name


TIGHTENINGS = {
    "cap lowered": lambda c: c["file_size"].__setitem__("cap", 700),
    "exception lowered": lambda c: c["file_size"]["exceptions"]["a.py"].__setitem__(
        "max_lines", 850
    ),
    "exception removed": lambda c: c["file_size"]["exceptions"].clear(),
    "rule added": lambda c: c["audit"]["rules"].append(
        {"id": "x", "pattern": "x", "incident": "#4"}
    ),
    "obligation added": lambda c: c["obligations"].append(
        {"id": "p", "when": ["a"], "also": ["b"], "incident": "#5"}
    ),
    "obligation widened": lambda c: c["obligations"][0]["also"].append("more/**"),
    "min_lines lowered": lambda c: c["duplication"].__setitem__("min_lines", 6),
}


@pytest.mark.parametrize("name", TIGHTENINGS)
def test_tightening_passes(name: str) -> None:
    assert ratchet.compare(BASE, edit(TIGHTENINGS[name])) == [], name


def test_the_duplication_baseline_may_shrink_but_not_grow() -> None:
    assert ratchet.compare_baselines({"a": 2}, {"a": 2}) == []
    assert ratchet.compare_baselines({"a": 2}, {}) == []
    assert ratchet.compare_baselines({"a": 2}, {"a": 3})
    assert ratchet.compare_baselines({"a": 2}, {"a": 2, "b": 2})
