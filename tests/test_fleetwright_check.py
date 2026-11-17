"""The slot check: what it reports, that it prints no secret, and what makes it fail."""

from __future__ import annotations

import json
from collections.abc import Sequence

import fleetwright_check
import pytest
from fleetwright_check import Machine, run_checks
from fleetwright_slot import managed_settings

MANAGED = "/etc/claude-code/managed-settings.json"
TOKEN = "ghp_secretsecretsecret"
APP_TOKEN = "/run/fleetwright/check-agent01/gh-token"
APP_RECORD = "/run/fleetwright/check-agent01/gh-token.json"
APP_RECORD_TEXT = {
    "id": "sha256:3f9a1c07be42",
    "expires_at": "2027-01-15T09:00:00Z",
    "permissions": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "actions": "read",
        "checks": "read",
        "statuses": "read",
        "metadata": "read",
    },
    "repositories": ["glennmcd/commitment-tracker-ai"],
}
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
            "ANTHROPIC_API_KEY": KEY,
        }
        self.files = {MANAGED: managed_settings()}
        if role == "review":
            self.env["FLEETWRIGHT_REVIEWER_LOGIN"] = "g26work"
            self.env["GH_TOKEN"] = TOKEN
        else:
            # An author's GitHub access is the App token its check unit minted, never GH_TOKEN.
            self.env["FLEETWRIGHT_TOKEN_FILE"] = APP_TOKEN
            self.files[APP_RECORD] = json.dumps(APP_RECORD_TEXT)
        self.app_repos: str | None = "glennmcd/commitment-tracker-ai\n"
        self.tools = {"uv", "claude", "gh", "git", "rtk", "bwrap", "socat"}
        self.owners: dict[str, int] = {}  # uid by path; root unless named
        self.modes: dict[str, int] = {}  # mode by path; the home's mode unless named
        self.bwrap_ok = True
        self.nested_ok = True  # a user namespace inside the sandbox keeps its capabilities
        self.login = "g26work" if role == "review" else "glennmcd"
        self.gh_version = "2.104.0"
        self.gh_slurp = True
        self.porcelain = ""
        self.head: str | None = "main"  # the clone's branch; None when detached
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
        if command[:3] == ["gh", "api", "installation/repositories"]:
            return (0, self.app_repos) if self.app_repos is not None else (1, "")
        if "remote" in command:
            return 0, "https://github.com/glennmcd/commitment-tracker-ai.git\n"
        if "status" in command:
            return 0, self.porcelain
        if "symbolic-ref" in command:
            return (0, self.head + "\n") if self.head else (1, "")
        if command[:1] == ["bwrap"]:
            ok = self.nested_ok if "unshare" in command else self.bwrap_ok
            return (0, "") if ok else (1, "")
        if command[:3] == ["claude", "plugin", "list"]:
            return (0, self.plugins) if self.plugins is not None else (1, "")
        return 1, ""

    def machine(self) -> Machine:
        return Machine(
            env=self.env,
            run=self.run,
            which=lambda name: f"/usr/bin/{name}" if name in self.tools else None,
            exists=lambda path: path in self.paths,
            mode=lambda path: self.modes.get(path, self.home_mode),
            writable=lambda path: path in self.writable,
            listable=lambda path: path in self.listable,
            read=lambda path: self.files.get(path),
            owner=lambda path: self.owners.get(path, 0),
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


# --- an author's GitHub App token -----------------------------------------------------------------


def test_an_author_slot_reports_its_app_token_and_never_asks_for_a_user() -> None:
    fake = Fake("author")
    ok, detail = results(fake)["github"]
    assert ok
    assert detail == (
        "App token for glennmcd/commitment-tracker-ai: contents write, no administration or "
        "workflows, expires 2027-01-15T09:00:00Z"
    )
    # An installation token cannot call GET /user; the check must not need it.
    assert not any(c[:3] == ["gh", "api", "user"] for c in fake.ran)
    assert [
        "gh",
        "api",
        "installation/repositories",
        "--jq",
        ".repositories[].full_name",
    ] in fake.ran


def test_an_author_unit_that_mints_no_token_fails() -> None:
    fake = Fake("author")
    del fake.env["FLEETWRIGHT_TOKEN_FILE"]
    ok, detail = results(fake)["github"]
    assert not ok and "FLEETWRIGHT_TOKEN_FILE" in detail


def test_a_mint_that_failed_points_at_the_token_log() -> None:
    fake = Fake("author")
    del fake.files[APP_RECORD]
    ok, detail = results(fake)["github"]
    assert not ok and "journalctl -t fleetwright-token" in detail


@pytest.mark.parametrize(
    "permissions",
    [
        {**APP_RECORD_TEXT["permissions"], "administration": "write"},  # type: ignore[dict-item]
        {**APP_RECORD_TEXT["permissions"], "workflows": "write"},  # type: ignore[dict-item]
        {**APP_RECORD_TEXT["permissions"], "contents": "read"},  # type: ignore[dict-item]
    ],
)
def test_an_app_token_that_can_change_the_rules_or_cannot_push_fails(
    permissions: dict[str, str],
) -> None:
    fake = Fake("author")
    fake.files[APP_RECORD] = json.dumps({**APP_RECORD_TEXT, "permissions": permissions})
    assert not results(fake)["github"][0]


def test_an_app_token_github_refuses_fails() -> None:
    fake = Fake("author")
    fake.app_repos = None
    ok, detail = results(fake)["github"]
    assert not ok and "refused" in detail


def test_an_app_not_installed_on_the_clones_repository_fails() -> None:
    fake = Fake("author")
    fake.app_repos = "glennmcd/other-repo\n"
    ok, detail = results(fake)["github"]
    assert not ok and "glennmcd/commitment-tracker-ai" in detail


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


@pytest.mark.parametrize("tool", ["bwrap", "socat"])
def test_the_sandbox_needs_bubblewrap_and_socat(tool: str) -> None:
    fake = Fake("author")
    fake.tools.discard(tool)
    ok, detail = results(fake)["tools"]
    assert not ok and tool in detail


PROBE = ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "--dev", "/dev", "true"]


def nested_probes(fake: Fake) -> list[list[str]]:
    return [c for c in fake.ran if c[:1] == ["bwrap"] and "unshare" in c]


def without_open_sockets(fake: Fake) -> Fake:
    settings = json.loads(managed_settings())
    del settings["sandbox"]["network"]
    fake.files[MANAGED] = json.dumps(settings)
    return fake


def test_bubblewrap_that_cannot_start_under_the_unit_fails_and_says_why() -> None:
    fake = Fake("author")
    fake.bwrap_ok = False
    ok, detail = results(fake)["bubblewrap"]
    assert not ok and "apparmor" in detail.lower()
    assert PROBE in fake.ran


def test_with_unix_sockets_open_one_level_is_enough() -> None:
    # No seccomp helper runs, so nothing needs a namespace inside the sandbox.
    fake = Fake("author")
    fake.nested_ok = False
    ok, detail = results(fake)["bubblewrap"]
    assert ok and "no seccomp helper" in detail
    assert nested_probes(fake) == []


def test_without_open_sockets_the_helpers_nested_namespace_is_probed_too() -> None:
    fake = without_open_sockets(Fake("author"))
    ok, detail = results(fake)["bubblewrap"]
    assert ok and "seccomp helper" in detail
    assert nested_probes(fake) == [
        [
            "bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "unshare",
            "--user",
            "--map-root-user",
            "true",
        ]
    ]


def test_a_nested_namespace_without_capabilities_fails_and_names_both_fixes() -> None:
    # Ubuntu's unpriv_bwrap profile: bwrap starts, but the seccomp helper fails every command.
    fake = without_open_sockets(Fake("author"))
    fake.nested_ok = False
    ok, detail = results(fake)["bubblewrap"]
    assert not ok
    assert "allowAllUnixSockets" in detail and "vm-setup.md" in detail


@pytest.mark.parametrize("text", [None, "{not json"])
def test_unreadable_managed_settings_get_the_stricter_probe(text: str | None) -> None:
    fake = Fake("author")
    if text is None:
        del fake.files[MANAGED]
    else:
        fake.files[MANAGED] = text
    results(fake)
    assert len(nested_probes(fake)) == 1


def test_ready_managed_settings_pass() -> None:
    assert results(Fake("author"))["managed settings"] == (
        True,
        f"{MANAGED}: sandbox required, no way out, stop switch and write guard from root's kit",
    )


def broken(change: str) -> Fake:
    fake = Fake("author")
    settings = json.loads(managed_settings())
    if change == "sandbox off":
        settings["sandbox"]["enabled"] = False
    elif change == "unsandboxed allowed":
        settings["sandbox"]["allowUnsandboxedCommands"] = True
    elif change == "no fail if unavailable":
        del settings["sandbox"]["failIfUnavailable"]
    elif change == "no write guard":
        settings["hooks"]["PreToolUse"] = settings["hooks"]["PreToolUse"][:1]
    elif change == "no stop line":
        settings["hooks"]["PreToolUse"] = settings["hooks"]["PreToolUse"][1:]
    fake.files[MANAGED] = json.dumps(settings)
    return fake


@pytest.mark.parametrize(
    "change",
    [
        "sandbox off",
        "unsandboxed allowed",
        "no fail if unavailable",
        "no write guard",
        "no stop line",
    ],
)
def test_managed_settings_that_let_an_agent_out_fail(change: str) -> None:
    assert not results(broken(change))["managed settings"][0]


def test_missing_or_unreadable_managed_settings_fail() -> None:
    fake = Fake("author")
    del fake.files[MANAGED]
    ok, detail = results(fake)["managed settings"]
    assert not ok and "init" in detail
    fake.files[MANAGED] = "{not json"
    assert not results(fake)["managed settings"][0]


def test_managed_settings_a_non_root_user_could_change_fail() -> None:
    fake = Fake("author")
    fake.owners[MANAGED] = 1001
    assert not results(fake)["managed settings"][0]
    fake = Fake("author")
    fake.modes[MANAGED] = 0o664
    assert not results(fake)["managed settings"][0]


def test_hooks_the_slot_could_edit_fail() -> None:
    fake = Fake("author")
    fake.owners["/opt/fleetwright/hooks/write_guard.py"] = 1001
    ok, detail = results(fake)["managed settings"]
    assert not ok and "write_guard.py" in detail
    fake = Fake("author")
    fake.modes["/opt/fleetwright/hooks/stop_line.py"] = 0o666
    assert not results(fake)["managed settings"][0]


def test_a_hook_directory_the_slot_could_write_fails() -> None:
    # Whoever can write the directory can replace the file in it.
    fake = Fake("author")
    fake.owners["/opt/fleetwright/hooks"] = 1001
    ok, detail = results(fake)["managed settings"]
    assert not ok and "/opt/fleetwright/hooks" in detail
    fake = Fake("author")
    fake.modes["/opt/fleetwright/hooks"] = 0o777
    assert not results(fake)["managed settings"][0]


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


def test_a_missing_clone_fails() -> None:
    fake = Fake()
    fake.paths = set()
    assert not results(fake)["clone"][0]


def test_work_a_stopped_run_left_on_a_branch_passes_and_says_the_next_run_keeps_it() -> None:
    # fleetwright_agent.settle_leftovers keeps it for a run on the same branch and stashes it
    # otherwise.
    fake = Fake("author")
    fake.porcelain, fake.head = " M README.md\n", "issue/12"
    ok, detail = results(fake)["clone"]
    assert ok
    assert "uncommitted work on issue/12" in detail
    assert "a run on issue/12 keeps it" in detail and "any other run stashes it" in detail
    assert "70" not in detail


def test_work_left_on_a_detached_head_passes_and_says_the_next_run_stashes_it() -> None:
    fake = Fake()
    fake.porcelain, fake.head = "?? notes.txt\n", None
    ok, detail = results(fake)["clone"]
    assert ok and "detached head" in detail and "the next run stashes it" in detail


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
