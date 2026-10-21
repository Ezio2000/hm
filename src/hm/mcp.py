import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import tomlkit
from mcp.server.fastmcp import FastMCP

from hm import profiles, quota, usage

server = FastMCP("hm")


def _instance(name: str) -> profiles.Instance:
    for inst in profiles.list_all_instances(profiles.Paths.default()):
        if inst.name == name:
            return inst
    raise ValueError(f"Instance not found: {name}")


def _layer(target: str) -> tuple[profiles.Paths, profiles.Layer]:
    """target is an instance name, or shared/claude or shared/codex."""
    if target.startswith("shared/"):
        paths = profiles.Paths.default(target.removeprefix("shared/"))
        return paths, profiles.load_shared(paths)
    inst = _instance(target)
    return profiles.Paths.default(inst.engine), inst


def _summary(paths: profiles.Paths, layer: profiles.Layer) -> dict[str, Any]:
    try:
        mcp = sorted(profiles.list_mcp(paths, layer))
    except (OSError, ValueError):
        mcp = None
    return {"settings_error": layer.settings_error,
            "plugins": profiles.plugin_states(layer),
            "skills": profiles.list_skills(layer),
            "mcp": mcp}


@server.tool()
def overview() -> dict[str, Any]:
    """Read the current instances and shared layers. Every instance, shared/claude and
    shared/codex has five parts: Setting, Plugin, Skill, MCP and Prompt. An instance's
    effective result = its own layer + the shared layer."""
    paths = profiles.Paths.default()
    return {
        "instances": [
            {"name": i.name, "engine": i.engine, "provider": i.provider, "model": i.model,
             **_summary(profiles.for_engine(paths, i.engine), i)}
            for i in profiles.list_all_instances(paths)
        ],
        "shared": {
            f"shared/{engine}": _summary(p, profiles.load_shared(p))
            for engine in ("claude", "codex")
            for p in [profiles.for_engine(paths, engine)]
        },
    }


@server.tool()
def read_layer(target: str) -> dict[str, Any]:
    """Read the full content of one layer: settings (including credentials), plugin versions
    and enabled state, skills and their directory, MCP servers, and Prompt.

    target is an instance name, or shared/claude or shared/codex.
    """
    paths, layer = _layer(target)
    if layer.settings_error:
        raise ValueError(layer.settings_error)
    return {"settings_path": str(layer.settings_path),
            "settings": profiles.plain(layer.settings),
            "plugins": [asdict(p) for p in profiles.list_plugins(paths, layer)],
            "skills_dir": str(layer.skills_dir),
            "skills": profiles.list_skills(layer),
            "mcp": profiles.plain(profiles.list_mcp(paths, layer)),
            "prompt": profiles.read_prompt(layer)}


@server.tool()
def read_result(name: str) -> dict[str, Any]:
    """Read the effective settings, plugins, skills, MCP servers and Prompt of an instance
    after its own layer is combined with the shared layer."""
    inst = _instance(name)
    return asdict(profiles.resolve(profiles.Paths.default(inst.engine), inst))


@server.tool()
def read_credentials(name: str) -> dict[str, Any]:
    """Quickly read an instance's URL, API key or OAuth access token (full plaintext) and
    where each came from.

    Reads the settings, the Claude macOS keychain or local login files; never refreshes a
    token. Credential fields that are not found are null; a value is not verified and may
    have expired.
    """
    return profiles.read_credentials(_instance(name))


@server.tool()
def save_settings(target: str, settings: dict[str, Any]) -> str:
    """Replace a layer's settings in full; pass an object and it is saved as JSON or TOML.
    Read first and keep the other fields.

    target is an instance name, or shared/claude (managed-settings.json) or
    shared/codex (/etc/codex/config.toml).
    """
    _, layer = _layer(target)
    text = (tomlkit.dumps(settings) if layer.engine == "codex"
            else json.dumps(settings, ensure_ascii=False, indent=2))
    profiles.save_settings(layer, text)
    return f"Saved settings of {layer.label}"


@server.tool()
def save_mcp(target: str, servers: dict[str, Any]) -> str:
    """Replace a layer's own user-level MCP servers in full (name -> config).

    shared/claude is synced into every Claude instance after saving; an instance cannot
    reuse the name of a shared MCP server.
    """
    paths, layer = _layer(target)
    profiles.save_mcp(paths, layer, servers)
    return f"Saved MCP of {layer.label}"


@server.tool()
def save_prompt(target: str, text: str) -> str:
    """Replace a layer's Prompt: CLAUDE.md / AGENTS.md for an instance, the shared CLAUDE.md
    for shared/claude, and developer_instructions in the system config for shared/codex."""
    _, layer = _layer(target)
    profiles.save_prompt(layer, text)
    return f"Saved Prompt of {layer.label}"


@server.tool()
def set_plugin(target: str, plugin: str, enabled: bool) -> str:
    """Enable or disable a plugin in one layer. An instance layer stores the state
    explicitly; in a shared layer, disabling removes the entry."""
    paths, layer = _layer(target)
    if plugin not in {p.id for p in profiles.list_plugins(paths, layer)}:
        raise ValueError(f"Plugin not found: {plugin}")
    profiles.set_plugin(layer, plugin, enabled)
    return f"Updated {plugin} of {layer.label}"


@server.tool()
def delete_skill(target: str, skill: str) -> str:
    """Delete a skill directory from an instance or a shared layer; for a symlinked skill
    only the symlink is removed.

    target is an instance name, or shared/claude or shared/codex.
    """
    _, layer = _layer(target)
    if skill not in profiles.list_skills(layer):
        raise ValueError(f"Skill not found: {skill}")
    profiles.delete_skill(layer, skill)
    return f"Deleted skill {skill} of {layer.label}"


@server.tool()
def promote_skill(name: str, skill: str) -> str:
    """Move a skill from an instance to the shared layer of the same client; an existing
    shared skill with the same name is not overwritten."""
    inst = _instance(name)
    if skill not in profiles.list_skills(inst):
        raise ValueError(f"Skill not found: {skill}")
    profiles.promote_skill(profiles.Paths.default(inst.engine), inst, skill)
    return f"Moved {skill} to shared/{inst.engine}"


@server.tool()
def read_pricing() -> dict[str, dict[str, float]]:
    """Read the per-model prices used by Usage, in USD per million tokens.

    Each model has input, output, cache_read and cache_write; when no prices are configured,
    a zero-price template of the models found in the logs is returned.
    """
    paths = profiles.Paths.default()
    if not (paths.hm_dir / "pricing.json").exists():
        usage.sync(paths)
    return json.loads(usage.pricing_text(paths))


@server.tool()
def save_pricing(pricing: dict[str, dict[str, float]]) -> str:
    """Replace the per-model prices used by Usage in full; call read_pricing first and keep
    the other models.

    Every model must have input, output, cache_read and cache_write, in USD per million
    tokens. The next Usage query uses the new prices.
    """
    usage.save_pricing(profiles.Paths.default(), json.dumps(pricing, ensure_ascii=False, indent=2))
    return "Saved Usage pricing"


@server.tool()
def usage_report(
    by: list[Literal["second", "hour", "day", "week", "month", "type", "model", "session",
                     "instance"]],
    days: int | None = 7,
    engine: Literal["claude", "codex"] | None = None,
    session: str | None = None,
    instance: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate usage from session logs. by lists the grouping dimensions (at most one time
    granularity; second means per request); days is the last N days including today (None
    for no limit). Each row has requests, input (cache misses), output, cache_read,
    cache_write, cost (USD from ~/.hm/pricing.json, null when no price is configured),
    speed (tok/s) and first_token (median, milliseconds). session takes a session value from
    the results and restricts to that session; instance restricts to that instance (Claude
    sessions are matched to instances by session-env, file-history and history.jsonl;
    unmatched ones are unknown)."""
    return usage.report(profiles.Paths.default(), list(by), usage.since_days(days), engine,
                        session, instance)


@server.tool()
def read_quota(name: str | None = None) -> list[dict[str, Any]]:
    """Query an instance's plan quota windows or account balance; without a name, query all
    instances concurrently.

    Each item has windows (label, used_percent, resets_at in UTC) and balance (amount,
    currency); supported is false when the provider is not recognized, and error is returned
    when a request fails, without affecting other instances.
    """
    paths = profiles.Paths.default()
    instances = [_instance(name)] if name is not None else profiles.list_all_instances(paths)
    return quota.read_all(instances)


@server.tool()
def launch_instance(name: str, cwd: str) -> str:
    """Launch an instance in a new macOS Terminal window without replacing the MCP server.
    cwd is the working directory after launch."""
    terminal = profiles.launch_instance(_instance(name), Path(cwd))
    return f"Launched {name} in Terminal ({terminal})"


@server.tool()
def create_instance(
    name: str, template: str, model: str, base_url: str = "", token: str = "",
) -> str:
    """Create an instance that reuses the template's client and common settings. Leave the
    URL and token empty for an official account.

    Third-party Codex providers use a Responses API URL; Claude Code uses an
    Anthropic-compatible URL.
    """
    source = _instance(template)
    inst = profiles.create_instance(
        profiles.Paths.default(source.engine), name, source, base_url, token, model,
    )
    return f"Created {name}; launch command: {inst.command}"


@server.tool()
def delete_instance(name: str) -> str:
    """Delete an instance and its local data; an official instance is logged out first.
    The defaults claude-1 and codex-1 cannot be deleted."""
    profiles.delete_instance(_instance(name))
    return f"Deleted {name}"


@server.tool()
def reorder_instances(names: list[str]) -> str:
    """Set the instance order; it must contain every existing instance exactly once."""
    paths = profiles.Paths.default()
    current = {i.name for i in profiles.list_all_instances(paths)}
    if len(names) != len(current) or set(names) != current:
        raise ValueError("The order must contain every existing instance exactly once")
    profiles.save_order(paths, names)
    return "Updated instance order"


@server.tool()
def check_status(name: str | None = None) -> dict[str, Any]:
    """Check an instance's settings, symlinks and login state; without a name, check all.
    Also returns global problems (including the shared layers)."""
    paths = profiles.Paths.default()
    instances = [_instance(name)] if name is not None else profiles.list_all_instances(paths)
    statuses = []
    for inst in instances:
        if inst.settings_error:
            auth = {"error": "Failed to read settings; login not checked"}
        elif inst.is_official:
            try:
                auth = profiles.auth_status(inst)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                auth = {"error": str(error)}
        else:
            auth = {"type": "token", "configured": bool(
                (inst.provider_config.get("experimental_bearer_token")
                 or os.environ.get(inst.provider_config.get("env_key", "")))
                if inst.engine == "codex" else
                inst.env.get("ANTHROPIC_AUTH_TOKEN") or inst.env.get("ANTHROPIC_API_KEY")
            ), "verified": False}
        statuses.append({"name": inst.name, "engine": inst.engine,
                         "settings_error": inst.settings_error,
                         "broken_links": inst.broken_links(
                             profiles.for_engine(paths, inst.engine)), "auth": auth})
    return {"instances": statuses, "global_problems": [
        problem for engine in ("claude", "codex")
        for problem in profiles.find_problems(profiles.for_engine(paths, engine))
    ]}
