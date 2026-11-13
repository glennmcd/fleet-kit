"""Evasion kit for the silent-skips gate: spellings an agent might try, each must be caught."""

from __future__ import annotations

from pathlib import Path

import pytest
from gates import silent_skips

PY_EVASIONS = {
    "plain decorator": "@pytest.mark.skip\ndef test_a():\n    pass\n",
    "skipif": "@pytest.mark.skipif(True, reason='later')\ndef test_a():\n    pass\n",
    "xfail": "@pytest.mark.xfail\ndef test_a():\n    pass\n",
    "call in body": "def test_a():\n    pytest.skip('no cluster')\n",
    "importorskip": "boto3 = pytest.importorskip('boto3')\n",
    "aliased marker": "from pytest import mark as m\n\n@m.skip\ndef test_a():\n    pass\n",
    "imported name": "from pytest import skip\n\ndef test_a():\n    skip()\n",
    "imported decorator": "from pytest import skip\n\n@skip\ndef test_a():\n    pass\n",
    "getattr": "mark = getattr(pytest.mark, 'skip')\n",
    "module mark": "pytestmark = pytest.mark.skip\n",
    "unittest": "class T(unittest.TestCase):\n    @unittest.skip('x')\n    def test_a(self):\n        pass\n",
    "skipTest": "class T(unittest.TestCase):\n    def test_a(self):\n        self.skipTest('x')\n",
    "raise SkipTest": "def test_a():\n    raise unittest.SkipTest('x')\n",
    "add_marker": "def pytest_collection_modifyitems(items):\n    items[0].add_marker(pytest.mark.skip)\n",
    "param marks": "P = pytest.param(1, marks=pytest.mark.skip)\n",
    "issue far away": "# see #12\n\n\n\n\n@pytest.mark.skip\ndef test_a():\n    pass\n",
    "crlf": "@pytest.mark.skip\r\ndef test_a():\r\n    pass\r\n",
}

PY_ALLOWED = {
    "issue in reason": "@pytest.mark.skip(reason='blocked by #42')\ndef test_a():\n    pass\n",
    "issue comment above": "# #42: waiting for the fix\n@pytest.mark.skip\ndef test_a():\n    pass\n",
    "issue url": "@pytest.mark.skip(reason='https://github.com/o/r/issues/7')\ndef test_a():\n    pass\n",
    "variable called skip": "skip = 3\nprint(skip)\n",
    "word in a string": "def test_a():\n    assert 'skip this' != 'x'\n",
}

TS_EVASIONS = {
    "it.skip": "it.skip('a', () => {});",
    "test.skip": "test.skip('a', async () => {});",
    "describe.skip": "describe.skip('a', () => {});",
    "xit": "xit('a', () => {});",
    "xdescribe": "xdescribe('a', () => {});",
    "spaces": "it .  skip ('a', () => {});",
    "chained": "it.concurrent.skip('a', () => {});",
    "bracket": "it['skip']('a', () => {});",
    "fixme": "test.fixme('a', async () => {});",
    "todo": "it.todo('a');",
    "inside test": "test('a', async () => {\n  test.skip(true, 'no browser');\n});",
    "skipIf": "it.skipIf(process.env.CI)('a', () => {});",
}

TS_ONLY_ALWAYS = {
    "it.only": "it.only('a', () => {});",
    "test.only with issue": "// #12\ntest.only('a', () => {});",
    "bracket only": "it['only']('a', () => {});",
}

TS_ALLOWED = {
    "issue on line": "it.skip('a', () => {}); // #99",
    "issue above": "// blocked by #99\nit.skip('a', () => {});",
    "commented out": "// it.skip('a', () => {});",
    "plain": "it('a', () => {});",
}


@pytest.mark.parametrize("name", PY_EVASIONS)
def test_python_evasions_are_caught(name: str) -> None:
    assert silent_skips.check_text("tests/test_x.py", PY_EVASIONS[name]), name


@pytest.mark.parametrize("name", PY_ALLOWED)
def test_python_skips_with_an_issue_pass(name: str) -> None:
    assert silent_skips.check_text("tests/test_x.py", PY_ALLOWED[name]) == [], name


@pytest.mark.parametrize("name", TS_EVASIONS)
def test_typescript_evasions_are_caught(name: str) -> None:
    assert silent_skips.check_text("a.test.ts", TS_EVASIONS[name]), name


@pytest.mark.parametrize("name", TS_ONLY_ALWAYS)
def test_only_is_never_allowed(name: str) -> None:
    assert silent_skips.check_text("a.test.ts", TS_ONLY_ALWAYS[name]), name


@pytest.mark.parametrize("name", TS_ALLOWED)
def test_typescript_skips_with_an_issue_pass(name: str) -> None:
    assert silent_skips.check_text("a.test.ts", TS_ALLOWED[name]) == [], name


def test_unparseable_python_fails_instead_of_passing() -> None:
    found = silent_skips.check_text("tests/test_x.py", "def (:\n")
    assert found and "cannot parse" in found[0].message


def test_check_walks_the_configured_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("@pytest.mark.skip\ndef test_a(): ...\n")
    (tmp_path / "src.py").write_text("@pytest.mark.skip\ndef f(): ...\n")
    found = silent_skips.check(tmp_path, {})
    assert [f.path for f in found] == ["tests/test_a.py"]


def test_main_exits_one_on_a_finding(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("pytest.skip('x')\n")
    assert silent_skips.main(["--root", str(tmp_path)]) == 1
