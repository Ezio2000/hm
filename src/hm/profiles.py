"""Instances, shared layers, ordering, configuration and status of Claude Code and Codex.

Every instance and every client's shared layer has five parts: Setting, Plugin, Skill, MCP
and Prompt. An instance's effective result = its own layer + the shared layer. The shared
layer sits where the client natively layers configuration:

  Part            Claude shared layer                      Codex shared layer
  Setting/Plugin  managed-settings.json (above instance)    whole file (below instance)
  Skill           shared/skills ← managed/.claude/skills   ~/.agents/skills
  MCP             shared/mcp.json -> instance .claude.json  mcp_servers
  Prompt          shared/CLAUDE.md ← managed/CLAUDE.md     developer_instructions

managed means /Library/Application Support/ClaudeCode. Claude has no native entry for shared
MCP (managed only accepts remote https), so hm syncs it into each instance.

claude-1 is the default instance: ~/.claude and ~/.claude.json are symlinked to it and it
starts as bare claude without CLAUDE_CONFIG_DIR. Other instances start with
CLAUDE_CONFIG_DIR=<instance dir>. The default Codex instance uses ~/.codex directly; the
others use ~/.codex-profiles through CODEX_HOME. Unlayered resources such as the plugin
install cache and session records are symlinked into each instance from link_source.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomlkit

from hm import claude_auth

LINKED_ITEMS = ("plugins", "projects", "statusline.sh")
CODEX_LINKED_ITEMS = ("plugins",)
AGY_LINKED_ITEMS = ("bin", "builtin")
DEFAULT_INSTANCE = "claude-1"
SHARED_DIR = "shared"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CLAUDE_SYSTEM = Path("/Library/Application Support/ClaudeCode")
CODEX_SYSTEM = Path("/etc/codex")


@dataclass(frozen=True)
class Paths:
    home: Path
    claude_system: Path = CLAUDE_SYSTEM
    codex_system: Path = CODEX_SYSTEM
    agy_system: Path | None = None
    engine: str = "claude"

    @classmethod
    def default(cls, engine: str = "claude") -> Paths:
        if engine not in ("claude", "codex", "agy"):
            raise ValueError("engine must be claude, codex or agy")
        return cls(Path.home(), engine=engine)

    @property
    def effective_agy_system(self) -> Path:
        return self.agy_system or (self.home / ".gemini/config")

    @property
    def root(self) -> Path:
        return self.home / f".{self.engine}-profiles"

    @property
    def link_source(self) -> Path:
        if self.engine == "codex":
            return self.home / ".codex"
        return self.root / SHARED_DIR

    @property
    def order(self) -> Path:
        return self.root / "order.json"

    @property
    def hm_dir(self) -> Path:
        return self.home / ".hm"


@dataclass
class Layer:
    """An instance or shared layer: where each of the five parts is stored and the loaded settings.

    When prompt_path is None the Prompt lives in the settings' developer_instructions;
    when mcp_path is None the MCP servers live in the settings' mcp_servers.
    """

    name: str
    engine: str
    settings_path: Path
    skills_dir: Path
    prompt_path: Path | None
    mcp_path: Path | None
    settings: dict[str, Any] = field(default_factory=dict)
    settings_error: str | None = None

    @property
    def shared(self) -> bool:
        return self.name == SHARED_DIR

    @property
    def format(self) -> str:
        return "toml" if self.engine == "codex" else "json"

    @property
    def label(self) -> str:
        return f"Shared · {self.engine.capitalize()}" if self.shared else self.name


class Instance(Layer):
    @property
    def path(self) -> Path:
        return self.settings_path.parent

    @property
    def is_default(self) -> bool:
        return self.name == f"{self.engine}-1"

    @property
    def env(self) -> dict[str, str]:
        return self.settings.get("env", {})

    @property
    def base_url(self) -> str | None:
        if self.engine == "codex":
            return self.provider_config.get("base_url")
        if self.engine == "agy":
            return None
        return self.env.get("ANTHROPIC_BASE_URL")

    @property
    def provider_config(self) -> dict[str, Any]:
        return self.settings.get("model_providers", {}).get(
            self.settings.get("model_provider", "openai"), {}
        )

    @property
    def is_official(self) -> bool:
        if self.engine == "codex":
            return self.settings.get("model_provider", "openai") == "openai"
        if self.engine == "agy":
            return self.settings.get("modelProvider") != "gemini"
        return self.base_url is None

    @property
    def provider(self) -> str:
        if self.base_url:
            return urlparse(self.base_url).netloc
        if self.engine == "codex":
            return self.settings.get("model_provider", "OpenAI")
        if self.engine == "agy":
            return "Google"
        return "Anthropic"

    @property
    def model(self) -> str:
        if self.engine == "agy":
            return self.settings.get("model") or "default"
        return self.env.get("ANTHROPIC_MODEL") or self.settings.get("model") or "default"

    @property
    def command(self) -> str:
        if self.engine == "agy":
            args = [self.engine]
            if not self.is_default:
                rel = os.path.relpath(self.path, self.path.parent.parent / ".gemini")
                args.append(f"--app_data_dir={rel}")
            if self.env:
                args = ["env", *(f"{k}={v}" for k, v in self.env.items()), *args]
            return shlex.join(args)
        if self.is_default:
            return self.engine
        variable = "CODEX_HOME" if self.engine == "codex" else "CLAUDE_CONFIG_DIR"
        return shlex.join(["env", f"{variable}={self.path}", self.engine])

    def launch_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CODEX_HOME", None)
        if self.engine == "codex":
            env["CODEX_HOME"] = str(self.path)
            return env
        if self.engine == "agy":
            if self.env.get("GEMINI_API_KEY"):
                env.pop("GOOGLE_API_KEY", None)
            env.update(self.env)
            return env
        if self.is_default:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(self.path)
        return env

    def broken_links(self, paths: Paths) -> list[str]:
        if self.engine == "codex" and self.is_default:
            return []
        items = (
            CODEX_LINKED_ITEMS if self.engine == "codex" else
            AGY_LINKED_ITEMS if self.engine == "agy" else
            LINKED_ITEMS
        )
        return [
            item
            for item in items
            if (paths.link_source / item).exists()
            if not _points_to(self.path / item, paths.link_source / item)
        ]


def _load[L: Layer](layer: L, missing_ok: bool = False) -> L:
    try:
        text = layer.settings_path.read_text()
        data = tomlkit.parse(text) if layer.engine == "codex" else json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Top level of the configuration must be an object")
        layer.settings = data
    except FileNotFoundError as e:
        if not missing_ok:
            layer.settings_error = str(e)
    except (OSError, ValueError) as e:
        layer.settings_error = str(e)
    return layer


def load_instance(path: Path, engine: str = "claude", name: str | None = None) -> Instance:
    codex = engine == "codex"
    agy = engine == "agy"
    return _load(Instance(
        name or path.name, engine,
        path / ("config.toml" if codex else "settings.json"),
        path / "skills",
        path / ("rules/hm.md" if agy else "AGENTS.md" if codex else "CLAUDE.md"),
        None if codex else path / ("mcp_config.json" if agy else ".claude.json"),
    ))


def load_shared(paths: Paths) -> Layer:
    if paths.engine == "codex":
        layer = Layer(SHARED_DIR, "codex", paths.codex_system / "config.toml",
                      paths.home / ".agents/skills", None, None)
    elif paths.engine == "agy":
        shared_dir = paths.effective_agy_system
        layer = Layer(SHARED_DIR, "agy", shared_dir / "settings.json",
                      shared_dir / "skills", shared_dir / "AGENTS.md",
                      shared_dir / "mcp_config.json")
    else:
        layer = Layer(SHARED_DIR, "claude", paths.claude_system / "managed-settings.json",
                      paths.link_source / "skills", paths.link_source / "CLAUDE.md",
                      paths.link_source / "mcp.json")
    return _load(layer, missing_ok=True)


def _read_order(paths: Paths) -> list[str]:
    try:
        order = json.loads(paths.order.read_text())
    except (OSError, ValueError):
        return [f"{paths.engine}-1"]
    if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
        return [f"{paths.engine}-1"]
    return order


def list_instances(paths: Paths) -> list[Instance]:
    """Sort by order.json; instances not in it come last, by name."""
    order = _read_order(paths)
    rank = {name: i for i, name in enumerate(order)}
    dirs = [p for p in paths.root.iterdir() if p.is_dir() and p.name != SHARED_DIR] \
        if paths.root.is_dir() else []
    instances = [load_instance(p, paths.engine) for p in dirs]
    if paths.engine == "codex" and (paths.link_source / "config.toml").exists():
        instances.append(load_instance(paths.link_source, "codex", "codex-1"))
    instances.sort(key=lambda i: (rank.get(i.name, len(rank)), i.name))
    return instances


def for_engine(paths: Paths, engine: str) -> Paths:
    return Paths(paths.home, paths.claude_system, paths.codex_system, paths.agy_system, engine)


def list_all_instances(paths: Paths) -> list[Instance]:
    instances = []
    for engine in ("claude", "codex", "agy"):
        instances += list_instances(for_engine(paths, engine))
    order = _read_order(for_engine(paths, "claude"))
    rank = {name: i for i, name in enumerate(order)}
    return sorted(instances, key=lambda i: rank.get(i.name, len(rank)))


def save_order(paths: Paths, names: list[str]) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.order.write_text(json.dumps(names, ensure_ascii=False, indent=2) + "\n")


# ---------- Setting ----------

def save_settings(layer: Layer, text: str) -> None:
    """Validate JSON/TOML, then write it verbatim, keeping the user's formatting."""
    if layer.engine == "agy" and layer.shared:
        raise ValueError("Antigravity CLI settings are per instance; edit an instance instead")
    data = tomlkit.parse(text) if layer.engine == "codex" else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Top level of the configuration must be an object")
    layer.settings_path.write_text(text if text.endswith("\n") else text + "\n")
    layer.settings, layer.settings_error = data, None


def _require_settings(layer: Layer) -> None:
    if layer.settings_error:
        raise ValueError(f"Failed to read settings of {layer.label}: {layer.settings_error}")


def _write_settings(layer: Layer, data: dict[str, Any]) -> None:
    save_settings(layer, tomlkit.dumps(data) if layer.engine == "codex"
                  else json.dumps(data, ensure_ascii=False, indent=2))


def plain(value: Any) -> Any:
    """Convert tomlkit objects to plain Python values; values read from JSON are returned as is."""
    if hasattr(value, "unwrap"):
        return value.unwrap()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def merge(base: dict[str, Any], top: dict[str, Any], union_lists: bool = False) -> dict[str, Any]:
    """top overrides base; objects merge key by key and, with union_lists, lists are unioned."""
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value, union_lists)
        elif union_lists and isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = out[key] + [v for v in value if v not in out[key]]
        else:
            out[key] = value
    return out


# ---------- Plugin ----------

@dataclass(frozen=True)
class Plugin:
    id: str
    version: str
    enabled: bool | None  # None means this layer does not set it


def cached_plugin_versions(paths: Paths) -> dict[str, str]:
    manifest = (".codex-plugin/plugin.json" if paths.engine == "codex"
                else ".claude-plugin/plugin.json")
    versions = {}
    cache = paths.link_source / "plugins/cache"
    for plugin in cache.glob("*/*"):
        files = sorted(plugin.glob(f"*/{manifest}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for file in files:
            try:
                version = json.loads(file.read_text()).get("version") or file.parent.parent.name
            except (OSError, ValueError):
                continue
            if version:
                versions[f"{plugin.name}@{plugin.parent.name}"] = str(version)
                break
    return versions


def plugin_versions(paths: Paths) -> dict[str, str]:
    cached = cached_plugin_versions(paths)
    if paths.engine == "codex":
        return cached
    try:
        installed = json.loads((paths.link_source / "plugins/installed_plugins.json").read_text())
    except (OSError, ValueError):
        installed = {}
    return {**{k: f"{v} (cached)" for k, v in cached.items()},
            **{k: v[0].get("version", "") for k, v in installed.get("plugins", {}).items() if v}}


def plugin_states(layer: Layer) -> dict[str, bool]:
    if layer.engine == "codex":
        return {k: bool(v["enabled"]) for k, v in layer.settings.get("plugins", {}).items()
                if isinstance(v, dict) and "enabled" in v}
    if layer.engine == "agy":
        return {}
    return {k: bool(v) for k, v in layer.settings.get("enabledPlugins", {}).items()}


def list_plugins(paths: Paths, layer: Layer) -> list[Plugin]:
    if layer.engine == "agy":
        if layer.shared:
            return []
        command = ["agy"]
        if not layer.is_default:
            rel = os.path.relpath(layer.path, paths.home / ".gemini")
            command.append(f"--app_data_dir={rel}")
        try:
            out = subprocess.run([*command, "plugin", "list", "--json"], capture_output=True,
                                 text=True, timeout=15, check=True, env=layer.launch_env())
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"Cannot list Antigravity plugins: {error}") from error
        return [Plugin(p["name"], p.get("version", ""), p.get("enabled"))
                for p in json.loads(out.stdout)]
    versions, states = plugin_versions(paths), plugin_states(layer)
    return [Plugin(i, versions.get(i, "not installed"), states.get(i))
            for i in sorted(versions.keys() | states.keys())]


def set_plugin(layer: Layer, plugin: str, enabled: bool) -> None:
    """An instance layer stores the enabled state explicitly; a shared layer only provides,
    and disabling removes the entry without suppressing the instance."""
    if layer.engine == "agy":
        if layer.shared:
            raise ValueError("Antigravity CLI plugins are managed per instance")
        command = ["agy"]
        if not layer.is_default:
            rel = os.path.relpath(layer.path, layer.path.parent.parent / ".gemini")
            command.append(f"--app_data_dir={rel}")
        try:
            subprocess.run([*command, "plugin", "enable" if enabled else "disable", plugin],
                           capture_output=True, text=True, timeout=15, check=True,
                           env=layer.launch_env())
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"Cannot update Antigravity plugin: {error}") from error
        return
    _require_settings(layer)
    data = layer.settings
    if layer.engine == "codex":
        table = data.setdefault("plugins", {})
        if layer.shared and not enabled:
            table.pop(plugin, None)
        else:
            table.setdefault(plugin, {})["enabled"] = enabled
    else:
        table = data.setdefault("enabledPlugins", {})
        if layer.shared and not enabled:
            table.pop(plugin, None)
        else:
            table[plugin] = enabled
    _write_settings(layer, data)


def plugin_mcp(paths: Paths, plugin: str) -> list[dict[str, str]]:
    """Read the MCP servers declared by the newest version in the plugin cache."""
    name, _, marketplace = plugin.partition("@")
    manifest = (".codex-plugin/plugin.json" if paths.engine == "codex"
                else ".claude-plugin/plugin.json")
    files = sorted((paths.link_source / "plugins/cache" / marketplace / name).glob(f"*/{manifest}"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []
    root = files[0].parent.parent
    declared = json.loads(files[0].read_text()).get("mcpServers")
    servers = dict(declared) if isinstance(declared, dict) else {}
    configs = [root / declared] if isinstance(declared, str) else [
        root / ".mcp.json", root / "mcp.json",
    ]
    for config in configs:
        if config.is_file():
            data = json.loads(config.read_text())
            servers.update(data.get("mcpServers", data))
    return [mcp_entry(k, v, f"Plugin · {plugin}", root) for k, v in servers.items()]


# ---------- Skill ----------

def list_skills(layer: Layer) -> list[str]:
    if layer.engine == "agy" and layer.shared:
        return []
    if not layer.skills_dir.is_dir():
        return []
    return sorted(p.name for p in layer.skills_dir.iterdir()
                  if not p.name.startswith(".") and p.is_dir())


def delete_skill(layer: Layer, name: str) -> None:
    path = layer.skills_dir / name
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def promote_skill(paths: Paths, inst: Instance, name: str) -> None:
    """Move an instance's own skill to the shared layer for all instances of the client."""
    if inst.engine == "agy":
        raise ValueError("Antigravity CLI skills are managed per instance")
    target = load_shared(paths).skills_dir / name
    if target.exists():
        raise ValueError(f"The shared layer already has {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(inst.skills_dir / name, target)


# ---------- MCP ----------

def mcp_entry(name: str, config: dict[str, Any], source: str = "",
              root: Path | None = None) -> dict[str, str]:
    transport = config.get("type") or ("http" if config.get("url") else "stdio")
    target = config.get("url") or " ".join([config.get("command", ""), *config.get("args", [])])
    if root:
        for variable in ("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}"):
            target = target.replace(variable, str(root))
    return {"name": name, "transport": transport, "target": target, "source": source}


def _read_json(path: Path) -> dict[str, Any]:
    text = path.read_text() if path.exists() else ""
    return json.loads(text) if text.strip() else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def list_mcp(paths: Paths, layer: Layer) -> dict[str, Any]:
    """A layer's own user-level MCP servers; entries in a Claude instance's .claude.json that
    share a name with the shared layer belong to the shared layer."""
    if layer.engine == "agy" and not layer.shared:
        return {}
    if layer.mcp_path is None:
        _require_settings(layer)
        return dict(layer.settings.get("mcp_servers", {}))
    servers = _read_json(layer.mcp_path).get("mcpServers", {})
    if layer.shared:
        return servers
    shared = list_mcp(paths, load_shared(paths))
    return {k: v for k, v in servers.items() if k not in shared}


def save_mcp(paths: Paths, layer: Layer, servers: dict[str, Any]) -> None:
    if not all(isinstance(v, dict) for v in servers.values()):
        raise ValueError("Every MCP config must be an object")
    if layer.engine == "agy":
        if not layer.shared:
            raise ValueError("Antigravity CLI MCP servers are configured in shared/agy")
        state = _read_json(layer.mcp_path)
        state["mcpServers"] = servers
        _write_json(layer.mcp_path, state)
        return
    if layer.mcp_path is None:
        _require_settings(layer)
        data = layer.settings
        if servers:
            data["mcp_servers"] = servers
        else:
            data.pop("mcp_servers", None)
        _write_settings(layer, data)
        return
    if layer.shared:
        # sync instances before writing the shared file, so a retry after a mid-way failure
        # can still compute the entries to remove
        removed = list_mcp(paths, layer).keys() - servers.keys()
        for inst in list_instances(paths):
            if inst.mcp_path is None:
                continue
            state = _read_json(inst.mcp_path)
            current = {k: v for k, v in state.get("mcpServers", {}).items() if k not in removed}
            state["mcpServers"] = {**current, **servers}
            _write_json(inst.mcp_path, state)
        _write_json(layer.mcp_path, {"mcpServers": servers})
        return
    shared = list_mcp(paths, load_shared(paths))
    if clash := sorted(servers.keys() & shared.keys()):
        raise ValueError(
            f"{', '.join(clash)} already in shared MCP; an instance cannot reuse the name")
    state = _read_json(layer.mcp_path)
    state["mcpServers"] = {**servers, **shared}
    _write_json(layer.mcp_path, state)


# ---------- Prompt ----------

def read_prompt(layer: Layer) -> str:
    if layer.prompt_path is None:
        return str(layer.settings.get("developer_instructions", ""))
    try:
        text = layer.prompt_path.read_text()
        if (layer.engine == "agy" and not layer.shared
                and text.startswith("---\ntrigger: always_on\n---\n")):
            return text.removeprefix("---\ntrigger: always_on\n---\n")
        return text
    except FileNotFoundError:
        return ""


def save_prompt(layer: Layer, text: str) -> None:
    if layer.prompt_path is not None:
        layer.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        if layer.engine == "agy" and not layer.shared and text.strip():
            text = "---\ntrigger: always_on\n---\n" + text
        layer.prompt_path.write_text(text if not text or text.endswith("\n") else text + "\n")
        return
    _require_settings(layer)
    data = layer.settings
    if text.strip():
        data["developer_instructions"] = tomlkit.string(text, multiline=True)
    else:
        data.pop("developer_instructions", None)
    _write_settings(layer, data)


# ---------- Result ----------

@dataclass
class Result:
    settings: dict[str, Any]
    plugins: list[str]
    skills: list[tuple[str, str]]
    mcp: list[dict[str, str]]
    prompt: list[tuple[str, str]]


def resolve(paths: Paths, inst: Instance) -> Result:
    """Layer the instance and shared layers by each client's own rules: shared wins for
    Claude, instance wins for Codex and Antigravity."""
    shared = load_shared(paths)
    for layer in (inst, shared):
        _require_settings(layer)
    own, common = plain(inst.settings), plain(shared.settings)
    codex = inst.engine == "codex"
    agy = inst.engine == "agy"
    if codex:
        settings = merge(common, own)
        plugins = sorted(k for k, v in settings.get("plugins", {}).items()
                         if isinstance(v, dict) and v.get("enabled"))
    elif agy:
        settings = own
        plugins = sorted(p.id for p in list_plugins(paths, inst) if p.enabled)
    else:
        settings = merge(own, common, union_lists=True)
        plugins = sorted(k for k, v in settings.get("enabledPlugins", {}).items() if v)

    own_skills, shared_skills = set(list_skills(inst)), set(list_skills(shared))
    if agy:
        shared_skills = set()
    both = ("Shared (overrides same-name instance skill)" if inst.engine == "claude"
            else "Instance + Shared (same name)")
    skills = [(n, both if n in own_skills and n in shared_skills
               else "Shared" if n in shared_skills else "Instance")
              for n in sorted(own_skills | shared_skills)]

    own_mcp, shared_mcp = list_mcp(paths, inst), list_mcp(paths, shared)
    servers = settings.get("mcp_servers", {}) if codex else {**own_mcp, **shared_mcp}
    mcp = [mcp_entry(k, v, "Instance + Shared (merged)" if k in own_mcp and k in shared_mcp
                     else "Shared" if k in shared_mcp else "Instance")
           for k, v in sorted(servers.items())]
    if not agy:
        for plugin in plugins:
            mcp += plugin_mcp(paths, plugin)

    if codex:
        prompt = [("developer_instructions", str(settings.get("developer_instructions", ""))),
                  (f"Instance {inst.prompt_path.name}", read_prompt(inst))]
    elif agy:
        prompt = [("Shared AGENTS.md", read_prompt(shared)),
                  ("Instance rules/hm.md", read_prompt(inst))]
    else:
        prompt = [("Shared CLAUDE.md", read_prompt(shared)),
                  ("Instance CLAUDE.md", read_prompt(inst))]
    return Result(settings, plugins, skills, mcp, [(t, s) for t, s in prompt if s.strip()])


# ---------- Instance lifecycle and status ----------

def auth_status(inst: Instance) -> dict[str, Any]:
    if inst.engine == "agy":
        command = [inst.engine]
        if not inst.is_default:
            rel = os.path.relpath(inst.path, inst.path.parent.parent / ".gemini")
            command.append(f"--app_data_dir={rel}")
        out = subprocess.run(
            [*command, "models"],
            env=inst.launch_env(),
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        msg = "Logged in" if out.returncode == 0 else (out.stderr or out.stdout).strip()
        return {"loggedIn": out.returncode == 0, "message": msg}
    out = subprocess.run(
        ["codex", "login", "status"] if inst.engine == "codex" else ["claude", "auth", "status"],
        env=inst.launch_env(),
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    if inst.engine == "codex":
        status = {"loggedIn": out.returncode == 0,
                  "message": (out.stdout or out.stderr).strip()}
        if out.returncode == 0:
            try:
                auth = json.loads((inst.path / "auth.json").read_text())
                payload = auth["tokens"]["id_token"].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                status["email"] = claims.get("email")
            except (OSError, ValueError, KeyError, IndexError, TypeError, binascii.Error):
                pass
        return status
    return json.loads(out.stdout)


def read_credentials(inst: Instance) -> dict[str, Any]:
    """Read an instance's endpoint and plaintext credentials without refreshing or changing
    login state."""
    if inst.settings_error:
        raise ValueError(inst.settings_error)
    # Claude credentials come only from the instance's own settings env; the process
    # environment may be inherited from another instance that called hm.
    env = inst.env if inst.engine in ("claude", "agy") else {**inst.launch_env(), **inst.env}
    result = {"name": inst.name, "engine": inst.engine, "url": inst.base_url,
              "api_key": None, "oauth_token": None, "source": None}
    if inst.engine == "claude":
        result["url"] = inst.base_url or "https://api.anthropic.com"
        for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if env.get(key):
                field = "oauth_token" if key == "CLAUDE_CODE_OAUTH_TOKEN" else "api_key"
                result[field] = env[key]
                result["source"] = key
                return result
        credentials, result["source"] = claude_auth.read(inst)
        result["oauth_token"] = credentials.get("claudeAiOauth", {}).get("accessToken")
    elif inst.engine == "agy":
        result["url"] = inst.base_url or "https://generativelanguage.googleapis.com"
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if env.get(key):
                result["api_key"] = env[key]
                result["source"] = key
                return result
        if inst.is_official:
            result["source"] = "Google Account"
    else:
        provider = inst.provider_config
        if provider.get("experimental_bearer_token"):
            result["api_key"] = provider["experimental_bearer_token"]
            result["source"] = "model_providers.experimental_bearer_token"
        elif provider.get("env_key"):
            key = provider["env_key"]
            result["api_key"] = env.get(key)
            result["source"] = key
        else:
            file = inst.path / "auth.json"
            if file.exists():
                credentials = json.loads(file.read_text())
                if credentials.get("auth_mode") == "apikey" or credentials.get("OPENAI_API_KEY"):
                    result["api_key"] = credentials.get("OPENAI_API_KEY")
                else:
                    result["oauth_token"] = (credentials.get("tokens") or {}).get("access_token")
                result["source"] = str(file)
        if not result["url"] and inst.is_official:
            result["url"] = ("https://chatgpt.com/backend-api/codex" if result["oauth_token"]
                             else "https://api.openai.com/v1")
    return result


def validate_new_name(paths: Paths, name: str) -> str | None:
    if not NAME_RE.match(name):
        return "Use lowercase letters, digits and hyphens only, and do not start with a hyphen"
    if name in (SHARED_DIR, "claude", "codex", "agy", "hm", "claude-1", "codex-1", "agy-1") or any(
        (for_engine(paths, engine).root / name).exists() for engine in ("claude", "codex", "agy")
    ):
        return f"{name} already exists"
    return None


def create_instance(
    paths: Paths, name: str, template: Instance, base_url: str, token: str, model: str,
) -> Instance:
    """Reuse the common settings with a new provider and model, keep trusted directories, and
    wire in shared MCP and unlayered resources."""
    if error := validate_new_name(paths, name):
        raise ValueError(error)
    if not model or (base_url and not token):
        raise ValueError("Enter a model; third-party endpoints also need a token")
    if paths.engine != template.engine:
        raise ValueError("The template and the new instance must use the same client")
    if not paths.link_source.is_dir():
        raise ValueError(f"Run hm init --engine {paths.engine} first")
    if paths.engine == "agy":
        if base_url:
            raise ValueError("Antigravity CLI does not support custom provider URLs")
        settings = json.loads(template.settings_path.read_text())
        settings["model"] = model
        settings.pop("env", None)
        if token:
            settings["modelProvider"] = "gemini"
            settings["env"] = {"GEMINI_API_KEY": token}
        else:
            settings.pop("modelProvider", None)
        path = paths.root / name
        path.mkdir(parents=True)
        _write_json(path / "settings.json", settings)
        for item in AGY_LINKED_ITEMS:
            if (paths.link_source / item).exists():
                (path / item).symlink_to(paths.link_source / item)
        return load_instance(path, "agy")
    if paths.engine == "codex":
        settings = tomlkit.parse(template.settings_path.read_text())
        for key in ("model_provider", "model_providers", "model_context_window",
                    "model_auto_compact_token_limit", "model_catalog_json", "service_tier",
                    "forced_login_method", "forced_chatgpt_workspace_id"):
            settings.pop(key, None)
        settings.get("agents", {}).pop("default_subagent_model", None)
        settings["model"] = model
        settings["cli_auth_credentials_store"] = "file"
        if base_url:
            settings["model_provider"] = name
            settings["model_providers"] = {name: {
                "name": name, "base_url": base_url, "wire_api": "responses",
                "experimental_bearer_token": token,
            }}
        path = paths.root / name
        path.mkdir(parents=True)
        (path / "config.toml").write_text(tomlkit.dumps(settings))
        for item in CODEX_LINKED_ITEMS:
            if (paths.link_source / item).exists():
                (path / item).symlink_to(paths.link_source / item)
        return load_instance(path, "codex")
    settings = json.loads(template.settings_path.read_text())
    for key in ("model", "modelSettings", "modelOverrides", "modelPicker", "availableModels",
                "apiKeyHelper"):
        settings.pop(key, None)
    env = settings.setdefault("env", {})
    for key in list(env):
        if key.startswith("ANTHROPIC_") or key in (
            "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        ):
            del env[key]
    settings["model"] = model
    if base_url:
        env.update(ANTHROPIC_BASE_URL=base_url, ANTHROPIC_AUTH_TOKEN=token)
        for family in ("FABLE", "OPUS", "SONNET", "HAIKU"):
            env[f"ANTHROPIC_DEFAULT_{family}_MODEL"] = model
    state: dict[str, Any] = {"hasCompletedOnboarding": True}
    try:
        source = json.loads((paths.root / DEFAULT_INSTANCE / ".claude.json").read_text())
        state["projects"] = {
            p: {"hasTrustDialogAccepted": True}
            for p, v in source.get("projects", {}).items()
            if v.get("hasTrustDialogAccepted")
        }
    except (OSError, ValueError):
        pass
    state["mcpServers"] = list_mcp(paths, load_shared(paths))
    path = paths.root / name
    path.mkdir(parents=True)
    _write_json(path / "settings.json", settings)
    _write_json(path / ".claude.json", state)
    for item in LINKED_ITEMS:
        if (paths.link_source / item).exists():
            (path / item).symlink_to(paths.link_source / item)
    return load_instance(path)


def launch_instance(inst: Instance, cwd: Path) -> str:
    """Start the interactive client in a separate terminal, using the UI's instance environment
    rules."""
    _require_settings(inst)
    directory = cwd.expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"Working directory is not a directory: {directory}")
    executable = shutil.which(inst.engine)
    if executable is None:
        raise ValueError(f"Client not found: {inst.engine}")
    if inst.engine == "agy":
        rel = os.path.relpath(inst.path, inst.path.parent.parent / ".gemini")
        args = ["env", *(["-u", "GOOGLE_API_KEY"] if inst.env.get("GEMINI_API_KEY") else []),
                *(f"{k}={v}" for k, v in inst.launch_env({}).items()), executable]
        if not inst.is_default:
            args.append(f"--app_data_dir={rel}")
    else:
        args = ["env", "-u", "CLAUDE_CONFIG_DIR", "-u", "CODEX_HOME",
                *(f"{k}={v}" for k, v in inst.launch_env({}).items()), executable]
    command = f"cd {shlex.quote(str(directory))} && exec {shlex.join(args)}"
    result = subprocess.run(
        ["osascript", "-e", 'on run argv\n'
         'tell application "Terminal" to do script (item 1 of argv)\nend run', command],
        capture_output=True, text=True, check=True, timeout=15,
    )
    return result.stdout.strip()


def delete_instance(inst: Instance) -> None:
    """Log an official-account instance out first to clear its keychain entry; rmtree removes
    only the symlinks themselves and never enters the shared directory."""
    if inst.is_default:
        raise ValueError(f"{inst.name} is the default instance and cannot be deleted")
    if inst.is_official and inst.engine != "agy":
        subprocess.run(
            ["codex", "logout"] if inst.engine == "codex" else ["claude", "auth", "logout"],
            env=inst.launch_env(),
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    shutil.rmtree(inst.path)


def _points_to(link: Path, target: Path) -> bool:
    return link.is_symlink() and link.resolve() == target.resolve()


def _writable(path: Path) -> bool:
    return os.access(path if path.exists() else path.parent, os.W_OK)


def find_problems(paths: Paths) -> list[str]:
    """Report only what is wrong; each entry states the impact and what to do."""
    shared = load_shared(paths)
    problems = []
    if not list_instances(paths):
        return []
    if shared.settings_error:
        problems.append(
            f"Failed to read shared settings {shared.settings_path}: {shared.settings_error}")
    if paths.engine == "agy":
        default = paths.root / "agy-1"
        native = paths.home / ".gemini/antigravity-cli"
        if not _points_to(native, default):
            problems.append("~/.gemini/antigravity-cli does not point to agy-1: "
                            "running agy directly does not use its settings and login")
        for inst in list_instances(paths):
            if inst.settings_error:
                problems.append(
                    f"The settings of {inst.name} are not valid JSON: this instance "
                    "will fail to start; select it and choose Edit from the Actions menu")
            problems += [f"{inst.name} is not linked to {item} in {paths.link_source}"
                         for item in inst.broken_links(paths)]
        return problems
    if paths.engine == "codex":
        if not (paths.codex_system.is_dir() and os.access(paths.codex_system, os.W_OK)):
            problems.append(f"{paths.codex_system} is missing or not writable: the Codex shared "
                            "layer cannot be changed; run hm init --engine codex")
        if shared.settings_path.exists() and not _writable(shared.settings_path):
            problems.append(
                "The Codex shared settings are not writable; run hm init --engine codex")
        for inst in list_instances(paths):
            if inst.settings_error:
                problems.append(f"Failed to read config.toml of {inst.name}: {inst.settings_error}")
            problems += [f"{inst.name} is not linked to {item} in {paths.link_source}"
                         for item in inst.broken_links(paths)]
        return problems
    default = paths.root / DEFAULT_INSTANCE
    if not _points_to(paths.home / ".claude", default):
        problems.append(f"~/.claude does not point to {DEFAULT_INSTANCE}: "
                        "running claude directly does not use its settings and login")
    if not _points_to(paths.home / ".claude.json", default / ".claude.json"):
        problems.append(f"~/.claude.json does not point to {DEFAULT_INSTANCE}/.claude.json: "
                        "its state is written elsewhere")
    problems += [f"The shared directory is missing {i}: no instance can use it"
                 for i in ("plugins", "projects") if not (paths.link_source / i).exists()]
    if not _writable(shared.settings_path):
        problems.append(f"Shared settings {shared.settings_path} are not writable: "
                        "the shared Setting and Plugin cannot be changed")
    native = [(paths.claude_system / "CLAUDE.md", shared.prompt_path, "Prompt"),
              (paths.claude_system / ".claude/skills", shared.skills_dir, "Skill")]
    problems += [f"Shared {part} is not wired in: {link} should be a symlink to {target}"
                 for link, target, part in native if not _points_to(link, target)]
    try:
        shared_mcp = list_mcp(paths, shared)
    except (OSError, ValueError) as e:
        problems.append(f"Failed to read shared MCP {shared.mcp_path}: {e}")
        shared_mcp = {}
    for inst in list_instances(paths):
        if inst.settings_error:
            problems.append(f"The settings of {inst.name} are not valid JSON: this instance "
                            "will fail to start; select it and choose Edit from the Actions menu")
        problems += [f"{inst.name} is not linked to the shared {item}: "
                     f"it cannot see the shared {item}"
                     for item in inst.broken_links(paths)]
        problems += [f"{own.name} of {inst.name} is still symlinked to the shared layer: "
                     "shared content loads twice; delete this symlink"
                     for own, target in ((inst.prompt_path, shared.prompt_path),
                                         (inst.skills_dir, shared.skills_dir))
                     if _points_to(own, target)]
        try:
            servers = _read_json(inst.mcp_path).get("mcpServers", {})
        except (OSError, ValueError):
            continue
        if stale := [n for n, c in shared_mcp.items() if servers.get(n) != c]:
            problems.append(f"{inst.name} has not synced the shared MCP {', '.join(stale)}: on the "
                            "Shared page, choose Edit for MCP from the Actions menu and save "
                            "once to sync")
    return problems
