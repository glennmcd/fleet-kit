"""The slot check: what it reports, that it prints no secret, and what makes it fail."""

from __future__ import annotations

from collections.abc import Sequence

import fleetwright_check
import pytest
from fleetwright_check import Machine, run_checks

TOKEN = "ghp_secretsecretsecret"
KEY = "sk-ant-secretsecret"
CLONE = "/home/review01/repo"


class Fake:
    """A slot as its unit sees it; each test breaks one thing."""

    def __init__(self, role: str = "review") -> None:
        slot = "review01" if role == "review" else "agent01"
        self.env = {
            "FLEETWRIGHT_SLOT": slot,
            "FLEETWRIGHT_ROLE": role,
            "FLEETWRIGHT_CLONE": f"/home/{slot}/repo",
            "CC_PRACTICES_UNATTENDED": "1",
            "GH_TOKEN": TOKEN,
            "ANTHROPIC_API_KEY": KEY,
        }
        if role == "review":
            self.env["FLEETWRIGHT_REVIEWER_LOGIN"] = "g26work"
        self.tools = {"uv", "claude", "gh", "git", "rtk"}
        self.login = "g26work" if role == "review" else "glennmcd"
        self.gh_version = "2.104.0"
        self.gh_slurp = True
        self.porcelain = ""
        self.plugins: str | None = "fleetwright@fleetwright\ncc-practices@cc-practices\n"
        self.paths = {f"/home/{slot}/repo/.git"}
        self.home_mode = 0o700
        self.writable: set[str] = set()
        self.listable: set[str] = set()
        self.ran: list[list[str]] = []

    def run(self, command: Sequence[str]) -> tuple[int, str]:
        self.ran.append(list(command))
        if list(command) == ["gh", "--version"]:
            return 0, f"gh version {self.gh_version} (2026-11-12)\nhttps://github.com/cli/cli\n"
        if list(command) == ["gh", "api", "--help"]:
            flags = "      --paginate   Make additional HTTP requests\n"
            if self.gh_slurp:
                flags += "      --slurp[=true|false]   Use with --paginate to return an array\n"
            return 0, flags
        if command[:3] == ["gh", "api", "user"]:
            return (0, self.login + "\n") if self.login else (1, "")
        if "remote" in command:
            return 0, "https://github.com/glennmcd/commitment-tracker-ai.git\n"
        if "status" in command:
            return 0, self.porcelain
        if command[:3] == ["claude", "plugin", "list"]:
            return (0, self.plugins) if self.plugins is not None else (1, "")
        return 1, ""

    def machine(self) -> Machine:
        return Machine(
            env=self.env,
            run=self.run,
            which=lambda name: f"/usr/bin/{name}" if name in self.tools else None,
            exists=lambda path: path in self.paths,
            mode=lambda path: self.home_mode,
            writable=lambda path: path in self.writable,
            listable=lambda path: path in self.listable,
        )


def results(fake: Fake) -> dict[str, tuple[bool, str]]:
    return {name: (ok, detail) for name, ok, detail in run_checks(fake.machine())}


def test_a_ready_review_slot_passes_every_check() -> None:
    got = results(Fake())
    assert all(ok for ok, _ in got.values()), got
    assert got["github"][1] == "signed in as g26work"


def test_a_ready_author_slot_passes_and_needs_no_reviewer_login() -> None:
    got = results(Fake("author"))
    assert all(ok for ok, _ in got.values()), got


def test_a_reviewer_signed_in_as_anyone_else_fails() -> None:
    fake = Fake()
    fake.login = "glennmcd"
    ok, detail = results(fake)["github"]
    assert not ok and "glennmcd" in detail and "g26work" in detail


def test_a_review_slot_without_its_expected_login_fails() -> None:
    fake = Fake()
    del fake.env["FLEETWRIGHT_REVIEWER_LOGIN"]
    assert not results(fake)["github"][0]


def test_no_token_fails_without_calling_github() -> None:
    fake = Fake()
    del fake.env["GH_TOKEN"]
    ok, detail = results(fake)["github"]
    assert not ok and "GH_TOKEN" in detail
    assert not any(c[:3] == ["gh", "api", "user"] for c in fake.ran)


def test_github_refusing_the_token_fails() -> None:
    fake = Fake()
    fake.login = ""
    assert not results(fake)["github"][0]


def test_no_model_key_fails() -> None:
    fake = Fake()
    del fake.env["ANTHROPIC_API_KEY"]
    assert not results(fake)["model key"][0]
    fake.env["CLAUDE_CODE_OAUTH_TOKEN"] = "x"
    assert results(fake)["model key"][0]


def test_a_missing_tool_is_named() -> None:
    fake = Fake()
    fake.tools.discard("rtk")
    ok, detail = results(fake)["tools"]
    assert not ok and "rtk" in detail


def test_a_gh_that_can_slurp_pages_passes_and_shows_its_version() -> None:
    assert results(Fake())["gh version"] == (True, "gh 2.104.0, has api --slurp")


def test_a_gh_too_old_for_the_allocator_fails_and_says_how_to_fix_it() -> None:
    # Ubuntu 26.04's own gh package is 2.46: every allocator pass fails once a PR is open.
    fake = Fake()
    fake.gh_version, fake.gh_slurp = "2.46.0", False
    ok, detail = results(fake)["gh version"]
    assert not ok
    assert "2.46.0" in detail and "--slurp" in detail and "cli.github.com" in detail


def test_a_gh_that_will_not_run_fails_the_version_check() -> None:
    fake = Fake()
    fake.tools.discard("gh")
    fake.gh_version = ""
    assert not results(fake)["gh version"][0]


def test_a_missing_plugin_is_named() -> None:
    fake = Fake()
    fake.plugins = "fleetwright@fleetwright\n"
    ok, detail = results(fake)["plugins"]
    assert not ok and "cc-practices" in detail


def test_an_unlistable_plugin_set_fails() -> None:
    fake = Fake()
    fake.plugins = None  # the command fails
    assert not results(fake)["plugins"][0]


def test_a_dirty_or_missing_clone_fails() -> None:
    fake = Fake()
    fake.porcelain = " M README.md\n"
    ok, detail = results(fake)["clone"]
    assert not ok and "70" in detail
    fake = Fake()
    fake.paths = set()
    assert not results(fake)["clone"][0]


def test_a_home_open_to_others_fails_and_an_admin_acl_does_not() -> None:
    fake = Fake()
    fake.home_mode = 0o750  # the fleetwright-admin ACL shows as group bits
    assert results(fake)["home"][0]
    fake.home_mode = 0o755
    assert not results(fake)["home"][0]


def test_a_sandbox_that_lets_the_slot_write_the_fleet_dir_or_read_secrets_fails() -> None:
    fake = Fake()
    fake.writable = {"/var/lib/fleetwright"}
    assert not results(fake)["sandbox"][0]
    fake = Fake()
    fake.listable = {"/etc/fleetwright"}
    assert not results(fake)["sandbox"][0]


def test_the_stop_flag_is_reported() -> None:
    fake = Fake()
    fake.paths.add("/var/lib/fleetwright/STOP")
    assert not results(fake)["stop flag"][0]


def test_a_slot_whose_role_does_not_match_its_name_fails() -> None:
    fake = Fake()
    fake.env["FLEETWRIGHT_ROLE"] = "author"
    assert not results(fake)["slot"][0]
    fake = Fake()
    fake.env["CC_PRACTICES_UNATTENDED"] = "0"
    assert not results(fake)["unattended"][0]


def test_no_secret_value_is_ever_printed(capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake()
    fake.login = "glennmcd"  # a failing run prints the most detail
    fake.tools.discard("uv")
    code = fleetwright_check.report(fake.machine())
    out = capsys.readouterr()
    assert code == 1
    for secret in (TOKEN, KEY):
        assert secret not in out.out and secret not in out.err


def test_the_report_marks_each_line_and_ends_with_a_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fleetwright_check.report(Fake().machine()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert all(line.startswith(("ok    ", "FAIL  ")) for line in lines[:-1])
    assert lines[-1] == "review01: every check passed"
