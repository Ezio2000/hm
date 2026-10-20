"""Claude Code / Codex 的实例、共享层、排序、配置和状态。

每个实例和每个客户端的共享层都有 Setting、Plugin、Skill、MCP、Prompt 五部分，
实例实际生效的结果 = 实例层 + 共享层。共享层尽量放在客户端原生叠加读取的位置：

  部分            Claude 共享层                            Codex 共享层（/etc/codex/config.toml）
  Setting/Plugin  managed-settings.json（高于实例）        整个文件（低于实例）
  Skill           shared/skills ← managed/.claude/skills   ~/.agents/skills
  MCP             shared/mcp.json → 各实例 .claude.json    mcp_servers
  Prompt          shared/CLAUDE.md ← managed/CLAUDE.md     developer_instructions

managed 指 /Library/Application Support/ClaudeCode；Claude 共享 MCP 没有原生入口
（managed 只接受远程 https），由 hm 同步写进各实例。

claude-1 是默认实例：~/.claude 和 ~/.claude.json 软链到它，用裸 claude 启动，
不设 CLAUDE_CONFIG_DIR。其余实例用 CLAUDE_CONFIG_DIR=<实例目录> 启动。
Codex 默认实例直接使用 ~/.codex，其余实例通过 CODEX_HOME 使用 ~/.codex-profiles。
插件安装缓存、会话记录等不分层的资源从 link_source 软链进各实例。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
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

LINKED_ITEMS = ("plugins", "projects", "statusline.sh")
CODEX_LINKED_ITEMS = ("plugins",)
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
    engine: str = "claude"

    @classmethod
    def default(cls, engine: str = "claude") -> Paths:
        if engine not in ("claude", "codex"):
            raise ValueError("engine 必须为 claude 或 codex")
        return cls(Path.home(), engine=engine)

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
    """实例或共享层：五部分各自的存放位置和已读入的配置。

    prompt_path 为 None 时 Prompt 存在配置的 developer_instructions 里；
    mcp_path 为 None 时 MCP 存在配置的 mcp_servers 里。
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
        return self.base_url is None

    @property
    def provider(self) -> str:
        if self.base_url:
            return urlparse(self.base_url).netloc
        return (self.settings.get("model_provider", "OpenAI")
                if self.engine == "codex" else "Anthropic")

    @property
    def model(self) -> str:
        return self.env.get("ANTHROPIC_MODEL") or self.settings.get("model") or "默认"

    @property
    def command(self) -> str:
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
        if self.is_default:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(self.path)
        return env

    def broken_links(self, paths: Paths) -> list[str]:
        if self.engine == "codex" and self.is_default:
            return []
        items = CODEX_LINKED_ITEMS if self.engine == "codex" else LINKED_ITEMS
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
            raise ValueError("配置顶层必须是对象")
        layer.settings = data
    except FileNotFoundError as e:
        if not missing_ok:
            layer.settings_error = str(e)
    except (OSError, ValueError) as e:
        layer.settings_error = str(e)
    return layer


def load_instance(path: Path, engine: str = "claude", name: str | None = None) -> Instance:
    codex = engine == "codex"
    return _load(Instance(
        name or path.name, engine,
        path / ("config.toml" if codex else "settings.json"),
        path / "skills",
        path / ("AGENTS.md" if codex else "CLAUDE.md"),
        None if codex else path / ".claude.json",
    ))


def load_shared(paths: Paths) -> Layer:
    if paths.engine == "codex":
        layer = Layer(SHARED_DIR, "codex", paths.codex_system / "config.toml",
                      paths.home / ".agents/skills", None, None)
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
    """按 order.json 排序；不在其中的实例排在后面，按名称排。"""
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
    return Paths(paths.home, paths.claude_system, paths.codex_system, engine)


def list_all_instances(paths: Paths) -> list[Instance]:
    instances = list_instances(for_engine(paths, "claude"))
    instances += list_instances(for_engine(paths, "codex"))
    order = _read_order(for_engine(paths, "claude"))
    rank = {name: i for i, name in enumerate(order)}
    return sorted(instances, key=lambda i: rank.get(i.name, len(rank)))


def save_order(paths: Paths, names: list[str]) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.order.write_text(json.dumps(names, ensure_ascii=False, indent=2) + "\n")


# ---------- Setting ----------

def save_settings(layer: Layer, text: str) -> None:
    """校验 JSON/TOML 后原样写入，保留用户的排版。"""
    data = tomlkit.parse(text) if layer.engine == "codex" else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("配置顶层必须是对象")
    layer.settings_path.write_text(text if text.endswith("\n") else text + "\n")
    layer.settings, layer.settings_error = data, None


def _require_settings(layer: Layer) -> None:
    if layer.settings_error:
        raise ValueError(f"{layer.label} 的配置读取失败：{layer.settings_error}")


def _write_settings(layer: Layer, data: dict[str, Any]) -> None:
    save_settings(layer, tomlkit.dumps(data) if layer.engine == "codex"
                  else json.dumps(data, ensure_ascii=False, indent=2))


def plain(value: Any) -> Any:
    """把 tomlkit 对象转成普通 Python 值；JSON 读入的值原样返回。"""
    if hasattr(value, "unwrap"):
        return value.unwrap()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def merge(base: dict[str, Any], top: dict[str, Any], union_lists: bool = False) -> dict[str, Any]:
    """top 覆盖 base；对象逐键合并，union_lists 时列表取并集。"""
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
    enabled: bool | None  # None 表示这一层没有设置


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
    return {**{k: f"{v}（缓存）" for k, v in cached.items()},
            **{k: v[0].get("version", "") for k, v in installed.get("plugins", {}).items() if v}}


def plugin_states(layer: Layer) -> dict[str, bool]:
    if layer.engine == "codex":
        return {k: bool(v["enabled"]) for k, v in layer.settings.get("plugins", {}).items()
                if isinstance(v, dict) and "enabled" in v}
    return {k: bool(v) for k, v in layer.settings.get("enabledPlugins", {}).items()}


def list_plugins(paths: Paths, layer: Layer) -> list[Plugin]:
    versions, states = plugin_versions(paths), plugin_states(layer)
    return [Plugin(i, versions.get(i, "未安装"), states.get(i))
            for i in sorted(versions.keys() | states.keys())]


def set_plugin(layer: Layer, plugin: str, enabled: bool) -> None:
    """实例层明确保存启停；共享层只负责提供，关闭即删除条目，不去压住实例。"""
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
    """读取插件缓存里最新版本声明的 MCP。"""
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
    return [mcp_entry(k, v, f"插件 · {plugin}", root) for k, v in servers.items()]


# ---------- Skill ----------

def list_skills(layer: Layer) -> list[str]:
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
    """把实例自己的技能移到共享层，同类客户端的所有实例都能用。"""
    target = load_shared(paths).skills_dir / name
    if target.exists():
        raise ValueError(f"共享层已有 {name}")
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
    return json.loads(path.read_text()) if path.exists() else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def list_mcp(paths: Paths, layer: Layer) -> dict[str, Any]:
    """这一层自己的用户级 MCP；Claude 实例的 .claude.json 里同名于共享层的条目属于共享层。"""
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
        raise ValueError("每个 MCP 的配置都必须是对象")
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
        # 先同步实例再写共享文件，中途失败时重试仍能算出要移除的条目
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
        raise ValueError(f"{'、'.join(clash)} 已在共享 MCP 中，实例里不能重名")
    state = _read_json(layer.mcp_path)
    state["mcpServers"] = {**servers, **shared}
    _write_json(layer.mcp_path, state)


# ---------- Prompt ----------

def read_prompt(layer: Layer) -> str:
    if layer.prompt_path is None:
        return str(layer.settings.get("developer_instructions", ""))
    try:
        return layer.prompt_path.read_text()
    except FileNotFoundError:
        return ""


def save_prompt(layer: Layer, text: str) -> None:
    if layer.prompt_path is not None:
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
    """按客户端自己的规则叠加实例层和共享层：Claude 共享层优先，Codex 实例层优先。"""
    shared = load_shared(paths)
    for layer in (inst, shared):
        _require_settings(layer)
    own, common = plain(inst.settings), plain(shared.settings)
    codex = inst.engine == "codex"
    settings = merge(common, own) if codex else merge(own, common, union_lists=True)
    if codex:
        plugins = sorted(k for k, v in settings.get("plugins", {}).items()
                         if isinstance(v, dict) and v.get("enabled"))
    else:
        plugins = sorted(k for k, v in settings.get("enabledPlugins", {}).items() if v)

    own_skills, shared_skills = set(list_skills(inst)), set(list_skills(shared))
    both = "共享（覆盖同名实例技能）" if not codex else "实例 + 共享（同名）"
    skills = [(n, both if n in own_skills and n in shared_skills
               else "共享" if n in shared_skills else "实例")
              for n in sorted(own_skills | shared_skills)]

    own_mcp, shared_mcp = list_mcp(paths, inst), list_mcp(paths, shared)
    servers = settings.get("mcp_servers", {}) if codex else {**own_mcp, **shared_mcp}
    mcp = [mcp_entry(k, v, "实例 + 共享（合并）" if k in own_mcp and k in shared_mcp
                     else "共享" if k in shared_mcp else "实例")
           for k, v in sorted(servers.items())]
    for plugin in plugins:
        mcp += plugin_mcp(paths, plugin)

    if codex:
        prompt = [("developer_instructions", str(settings.get("developer_instructions", ""))),
                  (f"实例 {inst.prompt_path.name}", read_prompt(inst))]
    else:
        prompt = [("共享 CLAUDE.md", read_prompt(shared)), ("实例 CLAUDE.md", read_prompt(inst))]
    return Result(settings, plugins, skills, mcp, [(t, s) for t, s in prompt if s.strip()])


# ---------- 实例生命周期与状态 ----------

def auth_status(inst: Instance) -> dict[str, Any]:
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
    """读取实例连接地址和明文凭据，不刷新或修改登录状态。"""
    if inst.settings_error:
        raise ValueError(inst.settings_error)
    # Claude 凭据只认实例自己的 settings env；进程环境可能继承自调用 hm 的其他实例。
    env = inst.env if inst.engine == "claude" else {**inst.launch_env(), **inst.env}
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
        service = "Claude Code-credentials"
        if not inst.is_default:
            service += "-" + hashlib.sha256(str(inst.path).encode()).hexdigest()[:8]
        credentials = {}
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                credentials = json.loads(out.stdout)
                result["source"] = f"Keychain: {service}"
        except FileNotFoundError:
            pass
        file = inst.path / ".credentials.json"
        if not credentials and file.exists():
            credentials = json.loads(file.read_text())
            result["source"] = str(file)
        result["oauth_token"] = credentials.get("claudeAiOauth", {}).get("accessToken")
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
        return "名称只能用小写字母、数字和连字符，且不能以连字符开头"
    if name in (SHARED_DIR, "claude", "codex", "hm", "claude-1", "codex-1") or any(
        (for_engine(paths, engine).root / name).exists() for engine in ("claude", "codex")
    ):
        return f"{name} 已存在"
    return None


def create_instance(
    paths: Paths, name: str, template: Instance, base_url: str, token: str, model: str,
) -> Instance:
    """复用通用设置，使用新供应商和模型，沿用信任目录，接入共享 MCP 和不分层的资源。"""
    if error := validate_new_name(paths, name):
        raise ValueError(error)
    if not model or (base_url and not token):
        raise ValueError("请填写模型；第三方接口还需要填写 token")
    if paths.engine != template.engine:
        raise ValueError("模板和新实例必须使用相同客户端")
    if not paths.link_source.is_dir():
        raise ValueError(f"请先运行 hm init --engine {paths.engine}")
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
    """MCP 在独立终端启动交互客户端，沿用界面的实例环境规则。"""
    _require_settings(inst)
    directory = cwd.expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"工作目录不是目录：{directory}")
    executable = shutil.which(inst.engine)
    if executable is None:
        raise ValueError(f"找不到客户端：{inst.engine}")
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
    """官方账号实例先登出以清掉它的钥匙串条目；rmtree 只删软链本身，不会进入共享目录。"""
    if inst.is_default:
        raise ValueError(f"{inst.name} 是默认实例，不能删除")
    if inst.is_official:
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
    """只报告出了问题的地方，每条说明影响是什么、怎么处理。"""
    shared = load_shared(paths)
    problems = []
    if not list_instances(paths):
        return []
    if shared.settings_error:
        problems.append(f"共享配置 {shared.settings_path} 读取失败：{shared.settings_error}")
    if paths.engine == "codex":
        if not (paths.codex_system.is_dir() and os.access(paths.codex_system, os.W_OK)):
            problems.append(f"{paths.codex_system} 不存在或没有写权限：Codex 共享层改不了，"
                            "运行 hm init --engine codex")
        if shared.settings_path.exists() and not _writable(shared.settings_path):
            problems.append("Codex 共享配置不可写，运行 hm init --engine codex")
        for inst in list_instances(paths):
            if inst.settings_error:
                problems.append(f"{inst.name} 的 config.toml 读取失败：{inst.settings_error}")
            problems += [f"{inst.name} 没有连到 {paths.link_source} 的 {item}"
                         for item in inst.broken_links(paths)]
        return problems
    default = paths.root / DEFAULT_INSTANCE
    if not _points_to(paths.home / ".claude", default):
        problems.append(f"~/.claude 没有指向 {DEFAULT_INSTANCE}："
                        "直接运行 claude 时用的不是它的配置和登录")
    if not _points_to(paths.home / ".claude.json", default / ".claude.json"):
        problems.append(f"~/.claude.json 没有指向 {DEFAULT_INSTANCE}/.claude.json："
                        "它的状态会写到别处")
    problems += [f"共享目录里缺少 {i}：所有实例都用不到它" for i in ("plugins", "projects")
                 if not (paths.link_source / i).exists()]
    if not _writable(shared.settings_path):
        problems.append(f"共享设置 {shared.settings_path} 没有写权限："
                        "共享层的 Setting 和 Plugin 改不了")
    native = [(paths.claude_system / "CLAUDE.md", shared.prompt_path, "Prompt"),
              (paths.claude_system / ".claude/skills", shared.skills_dir, "Skill")]
    problems += [f"共享 {part} 没有接入：{link} 应软链到 {target}"
                 for link, target, part in native if not _points_to(link, target)]
    try:
        shared_mcp = list_mcp(paths, shared)
    except (OSError, ValueError) as e:
        problems.append(f"共享 MCP {shared.mcp_path} 读取失败：{e}")
        shared_mcp = {}
    for inst in list_instances(paths):
        if inst.settings_error:
            problems.append(f"{inst.name} 的配置不是合法 JSON：这个实例启动会出错，"
                            "选中后从“操作”菜单选择“编辑”修改")
        problems += [f"{inst.name} 没有连到共享的 {item}：这个实例看不到共享的 {item}"
                     for item in inst.broken_links(paths)]
        problems += [f"{inst.name} 的 {own.name} 仍软链到共享层：共享内容会加载两次，"
                     f"删掉这个软链即可"
                     for own, target in ((inst.prompt_path, shared.prompt_path),
                                         (inst.skills_dir, shared.skills_dir))
                     if _points_to(own, target)]
        try:
            servers = _read_json(inst.mcp_path).get("mcpServers", {})
        except (OSError, ValueError):
            continue
        if stale := [n for n, c in shared_mcp.items() if servers.get(n) != c]:
            problems.append(f"{inst.name} 没有同步共享 MCP 的 {'、'.join(stale)}："
                            "在 Shared 页的 MCP 从“操作”菜单选择“编辑”，保存一次即可同步")
    return problems
