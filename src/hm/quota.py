import base64
import json
import subprocess
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse

from hm import claude_auth, profiles

Quota = dict[str, Any]  # {"windows": [...], "balance": [...]}


def _iso(value: str | float) -> str:
    """Normalize an ISO string or a seconds/milliseconds timestamp to UTC ISO."""
    if isinstance(value, str):
        moment = datetime.fromisoformat(value)
    else:
        moment = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _window(label: str, used_percent: float, resets_at: str | float | None) -> dict[str, Any]:
    return {"label": label, "used_percent": round(used_percent, 1),
            "resets_at": _iso(resets_at) if resets_at is not None else None}


def _get(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "hm", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _bearer(creds: dict[str, Any]) -> str:
    token = creds["oauth_token"] or creds["api_key"]
    if not token:
        raise ValueError("Credentials not found")
    return f"Bearer {token}"


def _anthropic(creds: dict[str, Any]) -> Quota:
    if not creds["oauth_token"]:
        raise ValueError("Not logged in to an official account")
    data = _get("https://api.anthropic.com/api/oauth/usage",
                {"Authorization": _bearer(creds), "anthropic-beta": "oauth-2025-04-20"})
    labels = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d-opus",
              "seven_day_sonnet": "7d-sonnet"}
    return {"windows": [_window(label, data[key]["utilization"], data[key]["resets_at"])
                        for key, label in labels.items() if data.get(key)]}


def _chatgpt(creds: dict[str, Any]) -> Quota:
    token = creds["oauth_token"]
    if not token:
        raise ValueError("Official API keys have no quota endpoint; log in with ChatGPT")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid ChatGPT OAuth token")
    payload = parts[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    data = _get("https://chatgpt.com/backend-api/wham/usage",
                {"Authorization": _bearer(creds), "ChatGPT-Account-Id": account})
    windows = []
    for window in (data["rate_limit"].get("primary_window"),
                   data["rate_limit"].get("secondary_window")):
        if window:
            hours = window["limit_window_seconds"] // 3600
            windows.append(_window(f"{hours // 24}d" if hours % 24 == 0 else f"{hours}h",
                                   window["used_percent"], window["reset_at"]))
    credits = data.get("credits") or {}
    balance = ([{"amount": float(credits["balance"]), "currency": "credits"}]
               if credits.get("balance") is not None else [])
    return {"windows": windows, "balance": balance}


def _deepseek(creds: dict[str, Any]) -> Quota:
    data = _get("https://api.deepseek.com/user/balance", {"Authorization": _bearer(creds)})
    return {"balance": [{"amount": float(info["total_balance"]), "currency": info["currency"]}
                        for info in data["balance_infos"]]}


def _kimi(creds: dict[str, Any]) -> Quota:
    data = _get("https://api.kimi.com/coding/v1/usages", {"Authorization": _bearer(creds)})
    return {"windows": [
        _window(label, data["usages"][key]["used_ratio"] * 100, data["usages"][key]["reset_time"])
        for key, label in (("limit_5h", "5h"), ("limit_7d", "7d"))
    ]}


def _zai(creds: dict[str, Any]) -> Quota:
    data = _get("https://api.z.ai/api/monitor/usage/quota/limit",
                {"Authorization": creds["api_key"] or ""})
    units = {3: "h", 5: "mo", 6: "w"}
    prefix = {"TOKENS_LIMIT": "", "TIME_LIMIT": "tools-"}
    return {"windows": [
        _window(f"{prefix[limit['type']]}{limit['number']}{units[limit['unit']]}",
                limit["percentage"], limit["nextResetTime"])
        for limit in data["data"]["limits"]
    ]}


def _minimax(creds: dict[str, Any]) -> Quota:
    data = _get("https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains",
                {"Authorization": _bearer(creds)})
    windows = []
    for item in data["model_remains"]:
        name = item["model_name"]
        windows.append(_window(f"{name}/interval",
                               100 - item["current_interval_remaining_percent"],
                               item["end_time"]))
        windows.append(_window(f"{name}/weekly",
                               100 - item["current_weekly_remaining_percent"],
                               item["weekly_end_time"]))
    return {"windows": windows}


def _host(inst: profiles.Instance, name: str) -> bool:
    host = (urlparse(inst.base_url or "").hostname or "").rstrip(".")
    return host == name or host.endswith(f".{name}")


# Matched in order; add a provider by appending one entry here.
ADAPTERS: list[tuple[str, Callable[[profiles.Instance], bool],
                     Callable[[dict[str, Any]], Quota]]] = [
    ("anthropic", lambda i: i.engine == "claude" and i.is_official, _anthropic),
    ("chatgpt", lambda i: i.engine == "codex" and i.is_official, _chatgpt),
    ("deepseek", lambda i: _host(i, "deepseek.com"), _deepseek),
    ("kimi", lambda i: _host(i, "kimi.com"), _kimi),
    ("zai", lambda i: _host(i, "api.z.ai"), _zai),
    ("minimax", lambda i: _host(i, "minimaxi.com"), _minimax),
]


def read_one(inst: profiles.Instance) -> dict[str, Any]:
    for provider, matches, fetch in ADAPTERS:
        if matches(inst):
            try:
                if provider == "anthropic":
                    claude_auth.refresh(inst)
                creds = profiles.read_credentials(inst)
                try:
                    data = fetch(creds)
                except HTTPError as error:
                    if (provider != "anthropic" or error.code != 401
                            or not claude_auth.refresh(inst, creds["oauth_token"])):
                        raise
                    data = fetch(profiles.read_credentials(inst))
                return {"name": inst.name, "provider": provider, "supported": True,
                        "windows": [], "balance": [], **data}
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                return {"name": inst.name, "provider": provider, "supported": True,
                        "error": f"{type(error).__name__}: {error}"}
    return {"name": inst.name, "supported": False,
            "reason": f"Unrecognized provider {inst.provider}"}


def read_all(instances: list[profiles.Instance]) -> list[dict[str, Any]]:
    with ThreadPoolExecutor() as pool:
        return list(pool.map(read_one, instances))
