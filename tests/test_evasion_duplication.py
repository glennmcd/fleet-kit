"""Evasion kit for the copy-paste gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from gates import duplication

CFG = {"duplication": {"min_lines": 6, "min_chars": 60}}
BLOCK = [
    "def total(items):",
    "    result = 0",
    "    for item in items:",
    "        if item.active:",
    "            result += item.price * item.count",
    "    return round(result, 2)",
    "",
    "def label(items):",
    "    names = [item.name for item in items if item.active]",
    "    return ', '.join(sorted(names))",
]


def put(root: Path, name: str, lines: list[str], eol: str = "\n") -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((eol.join(lines) + eol).encode())


def run(root: Path, cfg: dict[str, Any] | None = None) -> list[str]:
    ns = argparse.Namespace(write_baseline=False)
    return [str(f) for f in duplication.check(root, CFG if cfg is None else cfg, ns)]


def test_a_verbatim_copy_is_found_in_both_files(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(tmp_path, "b.py", BLOCK)
    found = run(tmp_path)
    assert {f.split(":")[0] for f in found} == {"a.py", "b.py"}


def test_unique_code_passes(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(
        tmp_path,
        "b.py",
        [line.replace("total", "sum_up").replace("item", "row") for line in BLOCK][:5] + ["x = 1"],
    )
    assert run(tmp_path) == []


def mutate(kind: str) -> tuple[list[str], str]:
    if kind == "reindented":
        return ["        " + line if line else line for line in BLOCK], "\n"
    if kind == "trailing spaces":
        return [line + "   " for line in BLOCK], "\n"
    if kind == "tabs":
        return [line.replace("    ", "\t") for line in BLOCK], "\n"
    if kind == "crlf":
        return BLOCK, "\r\n"
    if kind == "blank lines inserted":
        return [x for line in BLOCK for x in (line, "", "")], "\n"
    if kind == "comments inserted":
        return [x for line in BLOCK for x in (line, "    # added so it differs")], "\n"
    if kind == "collapsed spaces":
        return [line.replace(" = ", "  =  ") for line in BLOCK], "\n"
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "reindented",
        "trailing spaces",
        "tabs",
        "crlf",
        "blank lines inserted",
        "comments inserted",
        "collapsed spaces",
    ],
)
def test_cosmetic_changes_do_not_hide_a_copy(tmp_path: Path, kind: str) -> None:
    put(tmp_path, "a.py", BLOCK)
    lines, eol = mutate(kind)
    put(tmp_path, "pkg/deep/b.py", lines, eol)
    assert run(tmp_path), kind


def test_a_copy_inside_one_file_is_found(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK + ["x = 1"] + BLOCK)
    assert run(tmp_path)


def test_a_copy_in_a_test_file_is_ignored_by_default(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(tmp_path, "tests/test_a.py", BLOCK)
    assert run(tmp_path, {"duplication": {"min_lines": 6, "min_chars": 60}}) == []


def test_known_limit_renamed_variables_are_not_found(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(tmp_path, "b.py", [line.replace("result", "acc").replace("item", "row") for line in BLOCK])
    assert run(tmp_path) == []


def test_the_baseline_allows_old_copies_but_not_a_third(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(tmp_path, "b.py", BLOCK)
    duplication.check(tmp_path, CFG, argparse.Namespace(write_baseline=True))
    saved = json.loads((tmp_path / ".fleet-gates/duplication-baseline.json").read_text())
    assert saved and all(v == 2 for v in saved.values())
    assert run(tmp_path) == []
    put(tmp_path, "c.py", BLOCK)
    assert {f.split(":")[0] for f in run(tmp_path)} == {"a.py", "b.py", "c.py"}


def test_findings_merge_into_one_range_per_block(tmp_path: Path) -> None:
    put(tmp_path, "a.py", BLOCK)
    put(tmp_path, "b.py", BLOCK)
    assert len(run(tmp_path)) == 2


def test_short_lines_and_imports_are_not_a_block(tmp_path: Path) -> None:
    lines = ["import os", "from x import y", "}", "else:", "pass", "return", ")", "]", "break"] * 3
    put(tmp_path, "a.py", lines)
    put(tmp_path, "b.py", lines)
    assert run(tmp_path) == []
