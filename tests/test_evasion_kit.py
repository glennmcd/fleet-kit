"""Meta-test: every gate has an evasion kit, so a new gate cannot ship without one."""

from __future__ import annotations

from pathlib import Path

GATES = Path(__file__).parent.parent / "scripts" / "gates"


def test_every_gate_has_an_evasion_test_module() -> None:
    gates = {p.stem for p in GATES.glob("*.py") if p.stem not in {"__init__", "common"}}
    kits = {
        p.stem.removeprefix("test_evasion_")
        for p in Path(__file__).parent.glob("test_evasion_*.py")
    }
    assert gates - kits == set(), f"gates without an evasion kit: {sorted(gates - kits)}"
