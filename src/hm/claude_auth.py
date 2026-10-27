"""Claude Code's credential store and OAuth refresh protocol."""

from __future__ import annotations

import getpass
import hashlib
import json
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError

if TYPE_CHECKING:
    from hm.profiles import Instance

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def read(inst: Instance) -> tuple[dict[str, Any], str | None]:
    service = "Claude Code-credentials"
    if not inst.is_default:
        service += "-" + hashlib.sha256(str(inst.path).encode()).hexdigest()[:8]
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            data = json.loads(out.stdout)
            if data:
                return data, f"Keychain: {service}"
    except FileNotFoundError:
        pass
    file = inst.path / ".credentials.json"
    return (json.loads(file.read_text()), str(file)) if file.exists() else ({}, None)


def _save(inst: Instance, data: dict[str, Any], source: str) -> None:
    content = json.dumps(data)
    if source.startswith("Keychain: "):
        out = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", getpass.getuser(),
             "-s", source.removeprefix("Keychain: "), "-w", content],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode:
            raise OSError(f"Could not save refreshed Claude credentials to {source}")
    else:
        file = inst.path / ".credentials.json"
        temporary = file.with_suffix(".json.tmp")
        try:
            temporary.write_text(content)
            temporary.chmod(file.stat().st_mode & 0o777)
            temporary.replace(file)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _refresh_lock(inst: Instance):
    # Claude Code uses a mkdir lock at this exact path (60-second stale threshold).
    # All work under the lock is bounded: three store calls (10s each) and HTTP (15s).
    lock = inst.path / ".oauth_refresh.lock"
    deadline = time.monotonic() + 20
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Claude OAuth refresh is locked by another process; retry later"
                ) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        lock.rmdir()


def _exchange(oauth: dict[str, Any]) -> dict[str, Any]:
    body = {"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
            "client_id": oauth.get("clientId") or CLIENT_ID}
    if oauth.get("scopes"):
        body["scope"] = " ".join(oauth["scopes"])
    request = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "hm",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code in (400, 401):
            try:
                detail = json.load(error).get("error")
            except (ValueError, AttributeError):
                detail = None
            code = detail.get("type") if isinstance(detail, dict) else detail
            message = f"Claude OAuth refresh failed (HTTP {error.code}, {code or error.reason})"
            if code == "invalid_grant":
                message += "; sign in again with this instance's claude auth login"
            raise ValueError(message) from error
        raise


def refresh(inst: Instance, rejected_token: str | None = None) -> bool:
    """Refresh expired stored OAuth credentials, or a token rejected with HTTP 401.

    Re-read under Claude's lock so concurrent queries share the rotated refresh token.
    Explicit settings tokens are user-managed and are never replaced.
    """
    if any(inst.env.get(key) for key in (
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    )):
        return False

    def needed(data):
        oauth = data.get("claudeAiOauth", {})
        if rejected_token is not None:
            return oauth.get("accessToken") == rejected_token
        expiry = oauth.get("expiresAt")
        return expiry is not None and expiry <= time.time() * 1000

    data, _ = read(inst)
    if not needed(data):
        return rejected_token is not None
    with _refresh_lock(inst):
        data, source = read(inst)
        if not needed(data):
            return True
        oauth = data.get("claudeAiOauth", {})
        if not oauth.get("refreshToken"):
            raise ValueError("Claude OAuth token expired or was rejected and has no refresh token; "
                             "sign in again with this instance's claude auth login")
        response = _exchange(oauth)
        updated = {"accessToken": response["access_token"],
                   "refreshToken": response.get("refresh_token") or oauth["refreshToken"],
                   "expiresAt": int((time.time() + response["expires_in"]) * 1000)}
        if "scope" in response:
            updated["scopes"] = response["scope"].split()
        if response.get("refresh_token_expires_in") is not None:
            updated["refreshTokenExpiresAt"] = int(
                (time.time() + response["refresh_token_expires_in"]) * 1000)
        # Preserve unrelated store updates and never overwrite a concurrent new login.
        latest, latest_source = read(inst)
        current = latest.get("claudeAiOauth", {})
        if latest_source != source or any(
            current.get(key) != oauth.get(key) for key in ("accessToken", "refreshToken")
        ):
            return True
        latest["claudeAiOauth"] = {**current, **updated}
        _save(inst, latest, source)
        return True
