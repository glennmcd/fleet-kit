"""Evasion kit for the audit-greps gate."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
from gates import audit_greps
from gates.common import Finding

RULES = audit_greps.rules_from({})


def scan(path: str, text: str) -> list[Finding]:
    return audit_greps.scan(path, 1, text, RULES, [])


EVASIONS = {
    "type ignore": ("a.py", "x = f()  # type: ignore"),
    "no space": ("a.py", "x = f()  #type:ignore"),
    "extra spaces": ("a.py", "x = f()  #   type :   ignore[arg-type]"),
    "uppercase": ("a.py", "x = f()  # TYPE: IGNORE"),
    "noqa": ("a.py", "import os  # noqa"),
    "noqa code": ("a.py", "import os  # noqa: F401"),
    "noqa spaced": ("a.py", "import os  #noqa:F401"),
    "bare except": ("a.py", "    except:"),
    "bare except spaced": ("a.py", "    except :"),
    "no cover": ("a.py", "def f():  # pragma: no cover"),
    "ts-ignore": ("a.ts", "// @ts-ignore"),
    "ts-nocheck": ("a.ts", "// @ts-nocheck"),
    "expect-error": ("a.tsx", "// @ts-expect-error"),
    "block comment": ("a.ts", "/* @ts-ignore */"),
    "eslint line": ("a.ts", "// eslint-disable-next-line no-console"),
    "eslint block": ("a.js", "/* eslint-disable */"),
    "biome": ("a.ts", "// biome-ignore lint/style/noNonNullAssertion: because"),
    "windows path": ("src\\a.py", "x = f()  # type: ignore"),
}


@pytest.mark.parametrize("name", EVASIONS)
def test_evasions_are_caught(name: str) -> None:
    path, text = EVASIONS[name]
    assert scan(path.replace("\\", "/"), text), name


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("a.py", "x = f()  # type: ignore  # #12"),
        ("a.ts", "// @ts-ignore https://github.com/o/r/issues/12"),
        ("a.py", "    except ValueError:"),
        ("a.md", "write `# type: ignore` here"),
        ("a.py", "x = 1"),
    ],
)
def test_clean_lines_and_cited_suppressions_pass(path: str, text: str) -> None:
    assert scan(path, text) == []


def test_a_rule_without_an_incident_is_refused() -> None:
    rules = [{"id": "x", "pattern": "foo"}]
    assert any("cites no incident" in f.message for f in audit_greps.validate(rules))


def test_config_adds_and_disables_rules() -> None:
    cfg = {
        "audit": {
            "disable": ["noqa"],
            "rules": [
                {"id": "print", "pattern": r"\bprint\(", "paths": ["**/*.py"], "incident": "#9"}
            ],
        }
    }
    rules = audit_greps.rules_from(cfg)
    assert not audit_greps.scan("a.py", 1, "import os  # noqa", rules, [])
    assert audit_greps.scan("a.py", 1, "print(1)", rules, [])


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def init_repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")


def test_diff_mode_only_checks_added_lines(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "a.py").write_text("old = f()  # type: ignore\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "switch", "-qc", "feat")
    (tmp_path / "a.py").write_text("old = f()  # type: ignore\nnew = g()  # noqa\n")
    git(tmp_path, "commit", "-qam", "change")
    found = audit_greps.check(tmp_path, {}, _args(base="main"))
    assert [(f.path, f.line, f.rule) for f in found] == [("a.py", 2, "noqa")]


def test_all_mode_finds_the_old_line_too(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old = f()  # type: ignore\n")
    found = audit_greps.check(tmp_path, {}, _args(all=True))
    assert [f.rule for f in found] == ["type-ignore"]


def _args(base: str | None = None, all: bool = False) -> argparse.Namespace:

    return argparse.Namespace(base=base, all=all)
