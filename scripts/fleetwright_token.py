#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Mint, renew and revoke the fleet's GitHub App tokens. Run by systemd as root, never by a slot.

    fleetwright_token.py installation                                print the App's installation id
    fleetwright_token.py mint --mode write --dir /run/fleetwright/agent01 --owner agent01 --slot agent01
    fleetwright_token.py mint --mode read --dir /run/fleetwright/watchdog --owner fleetwright-allocator \\
        --unit fleetwright-watchdog
    fleetwright_token.py mint --stdout                               a write token on stdout, for drills
    fleetwright_token.py renew                                       mint again for every running author slot
    fleetwright_token.py revoke --dir /run/fleetwright/agent01 --slot agent01

The App's private key (``/etc/fleetwright/app.pem``) and ``/etc/fleetwright/app.env`` (``FLEETWRIGHT_APP_CLIENT_ID``,
``FLEETWRIGHT_APP_REPO``, ``FLEETWRIGHT_APP_INSTALLATION_ID``) are root's. A unit mints from ``ExecStartPre=+``,
which runs as root before the unit's own user starts: the key never reaches a slot, only a token does.

A token is limited to the one repository and to the permissions of its mode: ``write`` for authors (contents,
pull requests and issues; actions, checks and statuses read), ``read`` for the allocator and the watchdog.
Neither has Administration or Workflows, so no token can change the rules, merge past them, or push a change to
``.github/workflows``. It lives one hour; ``renew`` (``fleetwright-token-refresh.timer``) replaces each running
author slot's token before then, and ``revoke`` (``ExecStopPost=+``) ends it with the run.

Each token goes to ``<dir>/gh-token``, mode 400, owned by the unit's user, with ``<dir>/gh-token.json`` beside it:
its fingerprint, expiry, permissions and repositories, never the token. Every event is one journal line through
``logger -t fleetwright-token``. Because this runs as root inside the unit, journald stamps the line with
``_UID=0`` and the unit's ``_SYSTEMD_UNIT`` and ``_SYSTEMD_INVOCATION_ID``, which no slot can forge::

    token minted slot=agent01 assignment="issue 12" mode=write id=sha256:3f9a1c07be42 expires=... permissions=...

``id`` is the first 12 hex characters of the token's SHA-256: hash a token found somewhere and grep the journal
for it. Standard library and the ``openssl`` binary only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_ENV = "/etc/fleetwright/app.env"
APP_KEY = "/etc/fleetwright/app.pem"
SLOTS_DIR = "/var/lib/fleetwright/slots"
RUN_DIR = "/run/fleetwright"
API = "https://api.github.com"
TOKEN_FILE = "gh-token"
RECORD_FILE = "gh-token.json"
# GitHub refuses a JWT that expires more than 10 minutes ahead; iat sits a minute back for clock drift.
JWT_BEHIND, JWT_AHEAD = 60, 540

PERMISSIONS: dict[str, dict[str, str]] = {
    "write": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "actions": "read",
        "checks": "read",
        "statuses": "read",
        "metadata": "read",
    },
    "read": {
        "contents": "read",
        "pull_requests": "read",
        "issues": "read",
        "actions": "read",
        "checks": "read",
        "statuses": "read",
        "metadata": "read",
    },
}

CLIENT_ID_RE = re.compile(r"[A-Za-z0-9.]{1,64}")
REPO_RE = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
INSTALLATION_RE = re.compile(r"[1-9][0-9]{0,19}")
SLOT_RE = re.compile(r"(agent|review)[0-9]{2}")
AUTHOR_RE = re.compile(r"agent[0-9]{2}")
UNIT_RE = re.compile(r"fleetwright-[a-z0-9-]{1,60}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DIR_NAME_RE = re.compile(r"[a-z0-9-]{1,64}")
ASSIGNMENT_RE = re.compile(r"(issue|fix|review) [1-9][0-9]{0,8}")

Http = Callable[[str, str, dict[str, str], "bytes | None"], tuple[int, bytes]]
Run = Callable[[Sequence[str]], tuple[int, str]]


class TokenError(Exception):
    """A reason, safe to log: it never holds a token or a key."""


@dataclass(frozen=True)
class App:
    client_id: str
    repo: str
    installation_id: str | None


@dataclass
class Deps:
    http: Http
    sign: Callable[[bytes], bytes]
    now: Callable[[], int]
    log: Callable[[str], None]
    out: Callable[[str], None]
    run: Run
    user: Callable[[str], tuple[int, int] | None]
    app_env: Callable[[], str]


# --- the App --------------------------------------------------------------------------------------


def read_app_env(text: str) -> App:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    client_id = values.get("FLEETWRIGHT_APP_CLIENT_ID", "")
    repo = values.get("FLEETWRIGHT_APP_REPO", "")
    installation = values.get("FLEETWRIGHT_APP_INSTALLATION_ID") or None
    if not CLIENT_ID_RE.fullmatch(client_id):
        raise TokenError(f"{APP_ENV}: FLEETWRIGHT_APP_CLIENT_ID is missing or not a client id")
    if not REPO_RE.fullmatch(repo) or ".." in repo:
        raise TokenError(f"{APP_ENV}: FLEETWRIGHT_APP_REPO is missing or not owner/name")
    if installation is not None and not INSTALLATION_RE.fullmatch(installation):
        raise TokenError(f"{APP_ENV}: FLEETWRIGHT_APP_INSTALLATION_ID is not a number")
    return App(client_id, repo, installation)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(client_id: str, now: int, sign: Callable[[bytes], bytes]) -> str:
    """The App's own short-lived proof, signed RS256 with its private key."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"iat": now - JWT_BEHIND, "exp": now + JWT_AHEAD, "iss": client_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    return f"{signing_input}.{_b64url(sign(signing_input.encode()))}"


def openssl_signer(key_path: str) -> Callable[[bytes], bytes]:
    def sign(data: bytes) -> bytes:
        try:
            done = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", key_path],
                input=data,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TokenError(f"openssl could not run: {exc.__class__.__name__}") from None
        if done.returncode != 0 or not done.stdout:
            raise TokenError(f"openssl could not sign with {key_path}")
        return done.stdout

    return sign


def fingerprint(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:12]


# --- GitHub ---------------------------------------------------------------------------------------


def _github(http: Http, method: str, path: str, bearer: str, body: Any = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fleetwright",
        "Authorization": f"Bearer {bearer}",
    }
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    status, reply = http(method, API + path, headers, data)
    if status >= 300:
        raise TokenError(f"HTTP {status} from {method} {path}")
    if not reply:
        return None
    try:
        return json.loads(reply)
    except ValueError:
        raise TokenError(f"{method} {path} gave a reply that is not JSON") from None


def _installation(app: App, deps: Deps, app_jwt: str) -> str:
    if app.installation_id:
        return app.installation_id
    reply = _github(deps.http, "GET", f"/repos/{app.repo}/installation", app_jwt)
    installation = str((reply or {}).get("id", ""))
    if not INSTALLATION_RE.fullmatch(installation):
        raise TokenError(f"GET /repos/{app.repo}/installation gave no installation id")
    return installation


def _mint(app: App, mode: str, deps: Deps) -> dict[str, Any]:
    app_jwt = jwt(app.client_id, deps.now(), deps.sign)
    installation = _installation(app, deps, app_jwt)
    reply = _github(
        deps.http,
        "POST",
        f"/app/installations/{installation}/access_tokens",
        app_jwt,
        {"repositories": [app.repo.split("/", 1)[1]], "permissions": PERMISSIONS[mode]},
    )
    if not isinstance(reply, dict) or not isinstance(reply.get("token"), str) or not reply["token"]:
        raise TokenError(f"POST /app/installations/{installation}/access_tokens gave no token")
    return reply


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TokenError(f"{method} {url} failed: {exc.__class__.__name__}") from None


# --- files and lines ------------------------------------------------------------------------------


def _record(reply: Mapping[str, Any]) -> dict[str, Any]:
    repositories = [
        str(r.get("full_name", "")) for r in reply.get("repositories") or [] if isinstance(r, dict)
    ]
    return {
        "id": fingerprint(str(reply["token"])),
        "expires_at": str(reply.get("expires_at", "")),
        "permissions": dict(reply.get("permissions") or {}),
        "repositories": repositories,
    }


def _place(folder: Path, name: str, text: str, uid: int, gid: int) -> None:
    """Write, close to all but the owner, and rename into place, so a reader never sees half a file."""
    tmp = folder / f".{name}.new"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o400)
    os.chown(tmp, uid, gid)  # type: ignore[attr-defined,unused-ignore]  # absent on Windows
    os.replace(tmp, folder / name)


def write_token(folder: Path, reply: Mapping[str, Any], uid: int, gid: int) -> None:
    _place(folder, RECORD_FILE, json.dumps(_record(reply), indent=2) + "\n", uid, gid)
    _place(folder, TOKEN_FILE, str(reply["token"]), uid, gid)


def _assignment(slot: str) -> str:
    try:
        text = (Path(SLOTS_DIR) / slot).read_text(encoding="utf-8").strip()
    except OSError:
        return "none"
    return f'"{text}"' if ASSIGNMENT_RE.fullmatch(text) else "none"


def _who(slot: str | None, unit: str | None) -> str:
    if slot == "-":
        return "slot=-"
    if slot:
        return f"slot={slot} assignment={_assignment(slot)}"
    return f"unit={unit}" if unit else "unit=-"


def _details(record: Mapping[str, Any]) -> str:
    permissions = ",".join(f"{k}:{v}" for k, v in sorted(record["permissions"].items()))
    return f"id={record['id']} expires={record['expires_at']} permissions={permissions}"


# --- commands -------------------------------------------------------------------------------------


def _folder(raw: str) -> Path:
    """A directory directly under RUN_DIR, as systemd's RuntimeDirectory= makes it."""
    folder = Path(raw)
    if (
        not folder.is_absolute()
        or ".." in folder.parts
        or folder.parent.resolve() != Path(RUN_DIR).resolve()
        or not DIR_NAME_RE.fullmatch(folder.name)
    ):
        raise ValueError(f"--dir {raw!r}: give a directory directly under {RUN_DIR}")
    if not folder.is_dir():
        raise ValueError(f"--dir {raw!r} does not exist (the unit's RuntimeDirectory= makes it)")
    return folder


def _check_names(slot: str | None, unit: str | None) -> None:
    if slot is not None and not SLOT_RE.fullmatch(slot):
        raise ValueError(f"--slot {slot!r}: give agentNN or reviewNN")
    if unit is not None and not UNIT_RE.fullmatch(unit):
        raise ValueError(f"--unit {unit!r}: give a fleetwright-... unit name")


def mint(args: argparse.Namespace, deps: Deps) -> int:
    app = read_app_env(deps.app_env())
    if args.stdout:
        reply = _mint(app, "write", deps)
        deps.log(f"token minted slot=- mode=drill {_details(_record(reply))}")
        deps.out(str(reply["token"]))
        return 0
    _check_names(args.slot, args.unit)
    folder = _folder(args.dir)
    if not USER_RE.fullmatch(args.owner) or (ids := deps.user(args.owner)) is None:
        raise ValueError(f"--owner {args.owner!r}: no such user")
    who = _who(args.slot, args.unit)
    try:
        reply = _mint(app, args.mode, deps)
    except TokenError as exc:
        deps.log(f"token mint failed {who}: {exc}")
        return 1
    write_token(folder, reply, *ids)
    deps.log(f"token minted {who} mode={args.mode} {_details(_record(reply))}")
    return 0


def renew(deps: Deps) -> int:
    """Mint again for every running author slot: its runtime directory exists only while its unit runs."""
    app = read_app_env(deps.app_env())
    failed = 0
    run_dir = Path(RUN_DIR)
    folders = (
        sorted(p for p in run_dir.iterdir() if AUTHOR_RE.fullmatch(p.name))
        if run_dir.is_dir()
        else []
    )
    for folder in folders:
        if not (folder / TOKEN_FILE).exists():
            continue  # its unit's own mint has not landed yet
        slot = folder.name
        who = _who(slot, None)
        ids = deps.user(slot)
        if ids is None:
            deps.log(f"token renew failed {who}: no such user")
            failed += 1
            continue
        unit = f"fleetwright-agent@{slot}.service"
        code, out = deps.run(["systemctl", "show", "-p", "InvocationID", "--value", unit])
        invocation = out.strip() if code == 0 and out.strip() else "-"
        try:
            reply = _mint(app, "write", deps)
        except TokenError as exc:
            deps.log(f"token renew failed {who}: {exc}")
            failed += 1
            continue
        write_token(folder, reply, *ids)
        deps.log(
            f"token renewed {who} mode=write invocation={invocation} {_details(_record(reply))}"
        )
    return 1 if failed else 0


def revoke(args: argparse.Namespace, deps: Deps) -> int:
    """End the run's token. A failure is logged, not fatal: the token expires within the hour."""
    _check_names(args.slot, args.unit)
    folder = _folder(args.dir)
    try:
        token = (folder / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not token:
        return 0
    who = _who(args.slot, args.unit)
    try:
        _github(deps.http, "DELETE", "/installation/token", token)
    except TokenError as exc:
        deps.log(f"token revoke failed {who} id={fingerprint(token)}: {exc}")
        return 0
    deps.log(f"token revoked {who} id={fingerprint(token)}")
    return 0


def installation(deps: Deps) -> int:
    app = read_app_env(deps.app_env())
    deps.out(
        _installation(
            App(app.client_id, app.repo, None), deps, jwt(app.client_id, deps.now(), deps.sign)
        )
    )
    return 0


# --- the real machine -----------------------------------------------------------------------------


def _log(line: str) -> None:
    try:
        done = subprocess.run(["logger", "-t", "fleetwright-token", line], check=False, timeout=10)
        if done.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(line, file=sys.stderr)


def _run(command: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(command), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout


def _user(name: str) -> tuple[int, int] | None:
    if sys.platform == "win32":  # pwd is Unix only; the minter runs on the fleet machine
        return None
    import pwd

    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def _app_env() -> str:
    try:
        return Path(APP_ENV).read_text(encoding="utf-8")
    except OSError:
        raise TokenError(f"cannot read {APP_ENV} (root's, written by the operator)") from None


def real_deps() -> Deps:
    import time

    return Deps(
        http=_http,
        sign=openssl_signer(APP_KEY),
        now=lambda: int(time.time()),
        log=_log,
        out=print,
        run=_run,
        user=_user,
        app_env=_app_env,
    )


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleetwright_token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("installation", help="print the App's installation id for the repository")
    m = sub.add_parser("mint", help="mint a token into a unit's runtime directory")
    m.add_argument("--mode", choices=sorted(PERMISSIONS), default="write")
    m.add_argument("--dir", help="the unit's runtime directory, under /run/fleetwright")
    m.add_argument("--owner", help="the unit's user, who alone may read the token")
    m.add_argument("--slot", help="the slot whose run this is (agentNN or reviewNN)")
    m.add_argument("--unit", help="the unit, when it is not a slot's")
    m.add_argument(
        "--stdout", action="store_true", help="print a write token instead (drills only)"
    )
    sub.add_parser("renew", help="mint again for every running author slot")
    r = sub.add_parser("revoke", help="revoke the token in a unit's runtime directory")
    r.add_argument("--dir", required=True)
    r.add_argument("--slot")
    r.add_argument("--unit")
    args = parser.parse_args(argv)
    deps = deps or real_deps()
    try:
        if args.command == "installation":
            return installation(deps)
        if args.command == "renew":
            return renew(deps)
        if args.command == "revoke":
            return revoke(args, deps)
        if not args.stdout and (args.dir is None or args.owner is None):
            raise ValueError("mint needs --dir and --owner, or --stdout")
        return mint(args, deps)
    except ValueError as exc:
        print(f"fleetwright_token: {exc}", file=sys.stderr)
        return 2
    except TokenError as exc:
        deps.log(f"token {args.command} failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
