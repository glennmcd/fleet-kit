"""The GitHub App token minter: JWT, mint, write, renew, revoke, and the journal lines it leaves."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import fleetwright_token
import pytest
from fleetwright_token import App, TokenError, fingerprint, jwt, read_app_env

TOKEN = "ghs_" + "t" * 36
NEW_TOKEN = "ghs_" + "n" * 36
NOW = 1_800_000_000
APP_ENV = """\
# the fleet's GitHub App
FLEETWRIGHT_APP_CLIENT_ID=Iv23liABCdef123
FLEETWRIGHT_APP_REPO=glennmcd/commitment-tracker-ai
FLEETWRIGHT_APP_INSTALLATION_ID=77
"""


class GitHub:
    """Answers the three endpoints the minter calls, and records each request."""

    def __init__(self, status: int = 201, token: str = TOKEN) -> None:
        self.status = status
        self.token = token
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(body) if body else None,
            }
        )
        if self.status >= 300:
            return self.status, b'{"message": "boom"}'
        if url.endswith("/installation") and method == "GET":
            return 200, b'{"id": 77}'
        if url.endswith("/access_tokens"):
            reply = {
                "token": self.token,
                "expires_at": "2027-01-15T09:00:00Z",
                "permissions": (json.loads(body) if body else {})["permissions"],
                "repositories": [{"full_name": "glennmcd/commitment-tracker-ai"}],
            }
            return 201, json.dumps(reply).encode()
        if url.endswith("/installation/token") and method == "DELETE":
            return 204, b""
        return 404, b"{}"


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        self.slots = tmp_path / "slots"
        self.slots.mkdir()
        (self.slots / "agent01").write_text("issue 12\n", encoding="utf-8")
        monkeypatch.setattr(fleetwright_token, "RUN_DIR", str(self.run_dir))
        monkeypatch.setattr(fleetwright_token, "SLOTS_DIR", str(self.slots))
        self.github = GitHub()
        self.logged: list[str] = []
        self.out: list[str] = []
        self.ran: list[list[str]] = []
        self.chmods: list[tuple[str, int]] = []
        self.chowns: list[tuple[str, int, int]] = []
        monkeypatch.setattr(os, "chmod", lambda p, m: self.chmods.append((str(p), m)))
        monkeypatch.setattr(
            os, "chown", lambda p, u, g: self.chowns.append((str(p), u, g)), raising=False
        )

    def run(self, command: Sequence[str]) -> tuple[int, str]:
        self.ran.append(list(command))
        if command[:3] == ["systemctl", "show", "-p"]:
            return 0, "6b1e0c2d\n"
        return 1, ""

    def deps(self) -> fleetwright_token.Deps:
        return fleetwright_token.Deps(
            http=self.github,
            sign=lambda data: b"signature",
            now=lambda: NOW,
            log=self.logged.append,
            out=self.out.append,
            run=self.run,
            user=lambda name: (
                (1001, 1001) if name in ("agent01", "fleetwright-allocator") else None
            ),
            app_env=lambda: APP_ENV,
        )

    def main(self, *argv: str) -> int:
        return fleetwright_token.main(list(argv), self.deps())

    def dir(self, name: str = "agent01") -> Path:
        path = self.run_dir / name
        path.mkdir(exist_ok=True)
        return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


# --- app.env ----------------------------------------------------------------------------------


def test_app_env_gives_the_client_id_repository_and_installation() -> None:
    assert read_app_env(APP_ENV) == App("Iv23liABCdef123", "glennmcd/commitment-tracker-ai", "77")


def test_the_installation_id_is_optional_until_it_is_looked_up() -> None:
    text = "FLEETWRIGHT_APP_CLIENT_ID=Iv23li1\nFLEETWRIGHT_APP_REPO=o/r\n"
    assert read_app_env(text).installation_id is None


@pytest.mark.parametrize(
    "text",
    [
        "FLEETWRIGHT_APP_REPO=o/r\n",
        "FLEETWRIGHT_APP_CLIENT_ID=Iv1\n",
        "FLEETWRIGHT_APP_CLIENT_ID=Iv1;id\nFLEETWRIGHT_APP_REPO=o/r\n",
        "FLEETWRIGHT_APP_CLIENT_ID=Iv1\nFLEETWRIGHT_APP_REPO=../etc\n",
        "FLEETWRIGHT_APP_CLIENT_ID=Iv1\nFLEETWRIGHT_APP_REPO=o/r\nFLEETWRIGHT_APP_INSTALLATION_ID=7a\n",
    ],
)
def test_a_missing_or_malformed_value_is_refused(text: str) -> None:
    with pytest.raises(TokenError):
        read_app_env(text)


# --- the JWT ----------------------------------------------------------------------------------


def b64decode(part: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_the_jwt_names_the_app_and_lives_under_ten_minutes() -> None:
    signed: list[bytes] = []

    def sign(data: bytes) -> bytes:
        signed.append(data)
        return b"sig"

    header, claims, signature = jwt("Iv23li1", NOW, sign).split(".")
    assert b64decode(header) == {"alg": "RS256", "typ": "JWT"}
    # iat a minute back for clock drift; exp under GitHub's 10-minute limit.
    assert b64decode(claims) == {"iat": NOW - 60, "exp": NOW + 540, "iss": "Iv23li1"}
    assert signed == [f"{header}.{claims}".encode()]
    assert base64.urlsafe_b64decode(signature + "==") == b"sig"
    assert "=" not in header + claims + signature


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl binary")
def test_the_openssl_signature_verifies_with_the_apps_public_key(tmp_path: Path) -> None:
    key, pub, sig = tmp_path / "key.pem", tmp_path / "pub.pem", tmp_path / "sig"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(
        ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    token = jwt("Iv23li1", NOW, fleetwright_token.openssl_signer(str(key)))
    header, claims, signature = token.split(".")
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    data = tmp_path / "data"
    data.write_bytes(f"{header}.{claims}".encode())
    done = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig), str(data)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


# --- minting ----------------------------------------------------------------------------------


WRITE = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "actions": "read",
    "checks": "read",
    "statuses": "read",
    "metadata": "read",
}


def test_an_author_token_is_limited_to_the_repository_and_the_authors_permissions(
    world: World,
) -> None:
    assert (
        world.main(
            "mint",
            "--mode",
            "write",
            "--dir",
            str(world.dir()),
            "--owner",
            "agent01",
            "--slot",
            "agent01",
        )
        == 0
    )
    (request,) = world.github.requests
    assert request["method"] == "POST"
    assert request["url"] == "https://api.github.com/app/installations/77/access_tokens"
    assert request["headers"]["Authorization"].startswith("Bearer ")
    assert request["body"] == {"repositories": ["commitment-tracker-ai"], "permissions": WRITE}
    for forbidden in ("administration", "workflows"):
        assert forbidden not in request["body"]["permissions"]


def test_a_read_token_can_write_nothing(world: World) -> None:
    assert (
        world.main(
            "mint",
            "--mode",
            "read",
            "--dir",
            str(world.dir("allocate")),
            "--owner",
            "fleetwright-allocator",
        )
        == 0
    )
    permissions = world.github.requests[0]["body"]["permissions"]
    assert set(permissions.values()) == {"read"}
    assert set(permissions) == set(WRITE)


def test_the_token_and_its_record_are_written_for_the_owner_alone(world: World) -> None:
    folder = world.dir()
    world.main(
        "mint", "--mode", "write", "--dir", str(folder), "--owner", "agent01", "--slot", "agent01"
    )
    assert (folder / "gh-token").read_text(encoding="utf-8") == TOKEN
    record = json.loads((folder / "gh-token.json").read_text(encoding="utf-8"))
    assert record == {
        "id": fingerprint(TOKEN),
        "expires_at": "2027-01-15T09:00:00Z",
        "permissions": WRITE,
        "repositories": ["glennmcd/commitment-tracker-ai"],
    }
    assert TOKEN not in json.dumps(record)
    # Set on the temporary file, before it is renamed into place: no moment where another user
    # can read it.
    assert {(Path(p).name, m) for p, m in world.chmods} == {
        (".gh-token.new", 0o400),
        (".gh-token.json.new", 0o400),
    }
    assert {(Path(p).name, u, g) for p, u, g in world.chowns} == {
        (".gh-token.new", 1001, 1001),
        (".gh-token.json.new", 1001, 1001),
    }
    assert sorted(p.name for p in folder.iterdir()) == ["gh-token", "gh-token.json"]


def test_the_fingerprint_is_a_short_sha256_and_not_the_token() -> None:
    fp = fingerprint(TOKEN)
    assert fp.startswith("sha256:") and len(fp) == len("sha256:") + 12
    assert TOKEN[4:16] not in fp


def test_a_minted_line_names_the_slot_assignment_and_permissions_and_never_the_token(
    world: World,
) -> None:
    world.main(
        "mint",
        "--mode",
        "write",
        "--dir",
        str(world.dir()),
        "--owner",
        "agent01",
        "--slot",
        "agent01",
    )
    (line,) = world.logged
    assert line.startswith('token minted slot=agent01 assignment="issue 12" mode=write ')
    assert f"id={fingerprint(TOKEN)}" in line
    assert "expires=2027-01-15T09:00:00Z" in line
    assert "permissions=actions:read,checks:read,contents:write,issues:write,metadata:read," in line
    assert TOKEN not in line


def test_a_slot_with_no_assignment_is_logged_as_none(world: World) -> None:
    (world.slots / "agent01").unlink()
    world.main(
        "mint",
        "--mode",
        "write",
        "--dir",
        str(world.dir()),
        "--owner",
        "agent01",
        "--slot",
        "agent01",
    )
    assert "assignment=none" in world.logged[0]


def test_a_unit_without_a_slot_is_named_instead(world: World) -> None:
    world.main(
        "mint",
        "--mode",
        "read",
        "--dir",
        str(world.dir("watchdog")),
        "--owner",
        "fleetwright-allocator",
        "--unit",
        "fleetwright-watchdog",
    )
    assert world.logged[0].startswith("token minted unit=fleetwright-watchdog mode=read ")


def test_a_failed_mint_logs_the_status_writes_nothing_and_fails(world: World) -> None:
    world.github.status = 502
    folder = world.dir()
    code = world.main(
        "mint", "--mode", "write", "--dir", str(folder), "--owner", "agent01", "--slot", "agent01"
    )
    assert code == 1
    assert list(folder.iterdir()) == []
    (line,) = world.logged
    assert line == (
        'token mint failed slot=agent01 assignment="issue 12": '
        "HTTP 502 from POST /app/installations/77/access_tokens"
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--dir", "/home/agent01", "--owner", "agent01"],  # not under the run directory
        ["--dir", "{run}/../escape", "--owner", "agent01"],
        ["--dir", "{run}/agent01", "--owner", "nobody-here"],  # no such user
        ["--dir", "{run}/agent01", "--owner", "agent01", "--slot", "agent01;id"],
        ["--dir", "{run}/agent01", "--owner", "agent01", "--unit", "x y"],
    ],
)
def test_a_bad_destination_owner_or_name_is_refused_before_github_is_called(
    world: World, args: list[str]
) -> None:
    world.dir()
    argv = [a.replace("{run}", str(world.run_dir)) for a in args]
    assert world.main("mint", "--mode", "write", *argv) != 0
    assert world.github.requests == []


def test_a_drill_token_is_printed_and_logged_as_a_drill(world: World) -> None:
    assert world.main("mint", "--stdout") == 0
    assert world.out == [TOKEN]
    assert world.github.requests[0]["body"]["permissions"] == WRITE
    assert world.logged[0].startswith("token minted slot=- mode=drill ")
    assert TOKEN not in world.logged[0]


def test_the_installation_is_looked_up_with_the_apps_jwt(world: World) -> None:
    assert world.main("installation") == 0
    (request,) = world.github.requests
    assert request["method"] == "GET"
    assert (
        request["url"] == "https://api.github.com/repos/glennmcd/commitment-tracker-ai/installation"
    )
    assert request["headers"]["Authorization"].startswith("Bearer ")
    assert world.out == ["77"]


# --- renewing and revoking ------------------------------------------------------------------------


def test_renewing_mints_again_for_each_running_author_slot_only(world: World) -> None:
    for name in ("agent01", "check-agent01", "allocate", "review01"):
        folder = world.dir(name)
        (folder / "gh-token").write_text(TOKEN, encoding="utf-8")
    world.dir("agent02")  # a slot whose first mint has not landed: nothing to renew
    world.github.token = NEW_TOKEN
    assert world.main("renew") == 0
    assert len(world.github.requests) == 1
    assert (world.run_dir / "agent01" / "gh-token").read_text(encoding="utf-8") == NEW_TOKEN
    assert (world.run_dir / "check-agent01" / "gh-token").read_text(encoding="utf-8") == TOKEN
    (line,) = world.logged
    assert line.startswith(
        'token renewed slot=agent01 assignment="issue 12" mode=write invocation=6b1e0c2d '
    )
    assert [
        "systemctl",
        "show",
        "-p",
        "InvocationID",
        "--value",
        "fleetwright-agent@agent01.service",
    ] in world.ran


def test_a_failed_renewal_keeps_the_old_token_and_fails(world: World) -> None:
    (world.dir() / "gh-token").write_text(TOKEN, encoding="utf-8")
    world.github.status = 500
    assert world.main("renew") == 1
    assert (world.run_dir / "agent01" / "gh-token").read_text(encoding="utf-8") == TOKEN
    assert world.logged[0].startswith("token renew failed slot=agent01")


def test_revoking_deletes_the_token_with_itself_and_logs_its_fingerprint(world: World) -> None:
    folder = world.dir()
    (folder / "gh-token").write_text(TOKEN, encoding="utf-8")
    assert world.main("revoke", "--dir", str(folder), "--slot", "agent01") == 0
    (request,) = world.github.requests
    assert request["method"] == "DELETE"
    assert request["url"] == "https://api.github.com/installation/token"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert world.logged == [
        f'token revoked slot=agent01 assignment="issue 12" id={fingerprint(TOKEN)}'
    ]


def test_revoking_with_no_token_does_nothing(world: World) -> None:
    assert world.main("revoke", "--dir", str(world.dir()), "--slot", "agent01") == 0
    assert world.github.requests == [] and world.logged == []


def test_a_failed_revoke_is_logged_and_does_not_fail_the_unit(world: World) -> None:
    # The token expires within the hour anyway; a stop must not fail on it.
    folder = world.dir()
    (folder / "gh-token").write_text(TOKEN, encoding="utf-8")
    world.github.status = 502
    assert world.main("revoke", "--dir", str(folder), "--slot", "agent01") == 0
    assert world.logged[0].startswith("token revoke failed slot=agent01")
    assert TOKEN not in world.logged[0]
