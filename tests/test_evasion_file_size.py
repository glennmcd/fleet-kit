"""Evasion kit for the file-size gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gates import file_size


def write(root: Path, name: str, lines: int, eol: str = "\n", final: bool = True) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    body = eol.join(f"x{i} = {i}" for i in range(lines))
    p.write_bytes((body + (eol if final else "")).encode())


CFG = {"file_size": {"cap": 10}}


def run(root: Path, cfg: dict[str, Any] | None = None) -> list[str]:
    return [str(f) for f in file_size.check(root, CFG if cfg is None else cfg)]


def test_a_file_at_the_cap_passes_and_one_over_fails(tmp_path: Path) -> None:
    write(tmp_path, "ok.py", 10)
    write(tmp_path, "big.py", 11)
    assert [f.split(":")[0] for f in run(tmp_path)] == ["big.py"]


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final", [True, False])
def test_line_endings_and_missing_final_newline_do_not_hide_length(
    tmp_path: Path, eol: str, final: bool
) -> None:
    write(tmp_path, "big.py", 11, eol, final)
    assert len(run(tmp_path)) == 1


def test_unusual_directories_are_still_scanned(tmp_path: Path) -> None:
    write(tmp_path, "a/b/c/deep.ts", 11)
    write(tmp_path, "tests/big.py", 11)
    assert len(run(tmp_path)) == 2


def test_excluded_paths_are_skipped_but_lookalikes_are_not(tmp_path: Path) -> None:
    write(tmp_path, "gen/schema.d.ts", 50)
    write(tmp_path, "gen/schema.d.ts.py", 50)
    cfg = {"file_size": {"cap": 10, "exclude": ["gen/schema.d.ts"]}}
    assert [f.split(":")[0] for f in run(tmp_path, cfg)] == ["gen/schema.d.ts.py"]


@pytest.mark.parametrize(
    "key", ["big.py", "./big.py", "sub/../big.py", "big.py".replace("/", "\\")]
)
def test_exception_paths_match_however_they_are_spelled(tmp_path: Path, key: str) -> None:
    write(tmp_path, "big.py", 12)
    cfg = {
        "file_size": {
            "cap": 10,
            "exceptions": {key: {"max_lines": 12, "reason": "r", "issue": "#1"}},
        }
    }
    assert run(tmp_path, cfg) == []


def test_a_file_cannot_grow_past_its_exception(tmp_path: Path) -> None:
    write(tmp_path, "big.py", 13)
    cfg = {
        "file_size": {
            "cap": 10,
            "exceptions": {"big.py": {"max_lines": 12, "reason": "r", "issue": "#1"}},
        }
    }
    assert "ceiling of 12" in run(tmp_path, cfg)[0]


def test_a_stale_exception_fails(tmp_path: Path) -> None:
    write(tmp_path, "small.py", 5)
    cfg = {
        "file_size": {
            "cap": 10,
            "exceptions": {"small.py": {"max_lines": 12, "reason": "r", "issue": "#1"}},
        }
    }
    assert "stale" in run(tmp_path, cfg)[0]


def test_an_exception_for_a_missing_file_fails(tmp_path: Path) -> None:
    cfg = {
        "file_size": {
            "cap": 10,
            "exceptions": {"gone.py": {"max_lines": 12, "reason": "r", "issue": "#1"}},
        }
    }
    assert any("does not exist" in f for f in run(tmp_path, cfg))


@pytest.mark.parametrize(
    "exc", [{"max_lines": 12}, {"max_lines": 12, "reason": "r"}, {"reason": "r", "issue": "#1"}]
)
def test_an_incomplete_exception_fails(tmp_path: Path, exc: dict[str, object]) -> None:
    write(tmp_path, "big.py", 12)
    cfg = {"file_size": {"cap": 10, "exceptions": {"big.py": exc}}}
    assert run(tmp_path, cfg)


def test_the_default_cap_is_800(tmp_path: Path) -> None:
    write(tmp_path, "ok.py", 800)
    write(tmp_path, "big.py", 801)
    assert [f.split(":")[0] for f in run(tmp_path, {})] == ["big.py"]
