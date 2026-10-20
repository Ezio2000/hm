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
    raise ValueError(f"实例不存在：{name}")


def _layer(target: str) -> tuple[profiles.Paths, profiles.Layer]:
    """target 是实例名，或 shared/claude、shared/codex。"""
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
    """读取最新实例和共享层概况。每个实例和 shared/claude、shared/codex 都有
    Setting、Plugin、Skill、MCP、Prompt 五部分，实例生效结果 = 实例层 + 共享层。"""
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
    """读取一层的完整内容：配置（含凭据）、插件版本和启停、技能及目录、MCP、Prompt。

    target 为实例名，或 shared/claude、shared/codex。
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
    """读取实例叠加共享层后实际生效的配置、插件、技能、MCP 和 Prompt。"""
    inst = _instance(name)
    return asdict(profiles.resolve(profiles.Paths.default(inst.engine), inst))


@server.tool()
def read_credentials(name: str) -> dict[str, Any]:
    """快速读取指定实例的 URL、API Key 或 OAuth access token（完整明文）及来源。

    读取配置、Claude macOS 钥匙串或本地登录文件；不刷新 token。
    未找到的凭据字段返回 null；不代表凭据已验证或未过期。
    """
    return profiles.read_credentials(_instance(name))


@server.tool()
def save_settings(target: str, settings: dict[str, Any]) -> str:
    """完整替换一层的配置；传入对象，自动保存为 JSON 或 TOML。先读取并保留其他字段。

    target 为实例名，或 shared/claude（managed-settings.json）、
    shared/codex（/etc/codex/config.toml）。
    """
    _, layer = _layer(target)
    text = (tomlkit.dumps(settings) if layer.engine == "codex"
            else json.dumps(settings, ensure_ascii=False, indent=2))
    profiles.save_settings(layer, text)
    return f"已保存 {layer.label} 的配置"


@server.tool()
def save_mcp(target: str, servers: dict[str, Any]) -> str:
    """完整替换一层自己的用户级 MCP（名称 → 配置）。

    shared/claude 保存后同步进所有 Claude 实例；实例里不能与共享 MCP 重名。
    """
    paths, layer = _layer(target)
    profiles.save_mcp(paths, layer, servers)
    return f"已保存 {layer.label} 的 MCP"


@server.tool()
def save_prompt(target: str, text: str) -> str:
    """替换一层的 Prompt：实例为 CLAUDE.md / AGENTS.md，shared/claude 为共享 CLAUDE.md，
    shared/codex 为系统配置的 developer_instructions。"""
    _, layer = _layer(target)
    profiles.save_prompt(layer, text)
    return f"已保存 {layer.label} 的 Prompt"


@server.tool()
def set_plugin(target: str, plugin: str, enabled: bool) -> str:
    """启停一层的插件。实例层明确保存启停；共享层关闭即删除条目。"""
    paths, layer = _layer(target)
    if plugin not in {p.id for p in profiles.list_plugins(paths, layer)}:
        raise ValueError(f"插件不存在：{plugin}")
    profiles.set_plugin(layer, plugin, enabled)
    return f"已更新 {layer.label} 的 {plugin}"


@server.tool()
def delete_skill(target: str, skill: str) -> str:
    """删除实例或共享层的技能目录；软链技能只删除软链。

    target 为实例名，或 shared/claude、shared/codex。
    """
    _, layer = _layer(target)
    if skill not in profiles.list_skills(layer):
        raise ValueError(f"技能不存在：{skill}")
    profiles.delete_skill(layer, skill)
    return f"已删除 {layer.label} 的技能 {skill}"


@server.tool()
def promote_skill(name: str, skill: str) -> str:
    """把指定实例的技能移到同类客户端共享层；共享层已有同名技能时不覆盖。"""
    inst = _instance(name)
    if skill not in profiles.list_skills(inst):
        raise ValueError(f"技能不存在：{skill}")
    profiles.promote_skill(profiles.Paths.default(inst.engine), inst, skill)
    return f"{skill} 已移到 shared/{inst.engine}"


@server.tool()
def read_pricing() -> dict[str, dict[str, float]]:
    """读取 Usage 的模型单价，单位为每百万 token 美元。

    每个模型含 input、output、cache_read、cache_write；未配置价格时返回日志中模型的零价模板。
    """
    paths = profiles.Paths.default()
    if not (paths.hm_dir / "pricing.json").exists():
        usage.sync(paths)
    return json.loads(usage.pricing_text(paths))


@server.tool()
def save_pricing(pricing: dict[str, dict[str, float]]) -> str:
    """完整替换 Usage 的模型单价；先 read_pricing 并保留其他模型。

    每个模型必须含 input、output、cache_read、cache_write，单位为每百万 token 美元。
    保存后再次查询 Usage 即使用新价格。
    """
    usage.save_pricing(profiles.Paths.default(), json.dumps(pricing, ensure_ascii=False, indent=2))
    return "已保存 Usage 价格"


@server.tool()
def usage_report(
    by: list[Literal["second", "hour", "day", "week", "month", "type", "model", "session",
                     "instance"]],
    days: int | None = 7,
    engine: Literal["claude", "codex"] | None = None,
    session: str | None = None,
    instance: str | None = None,
) -> list[dict[str, Any]]:
    """按会话日志统计用量，by 为分组维度（时间粒度最多选一个，second 即逐条请求），
    days 为最近几天（含今天，None 不限）。每行含 requests、input（未命中缓存）、output、
    cache_read、cache_write、cost（美元，按 ~/.hm/pricing.json，未配价格为 null）、
    speed（tok/s）、first_token（毫秒中位数）。session 取结果里的 session 值，只看该会话；
    instance 只看该实例（Claude 会话按实例的 session-env 对应，对不上的为 unknown）。"""
    return usage.report(profiles.Paths.default(), list(by), usage.since_days(days), engine,
                        session, instance)


@server.tool()
def read_quota(name: str | None = None) -> list[dict[str, Any]]:
    """查询实例的套餐额度窗口或账户余额；不传名称查询全部实例，并发请求。

    每项含 windows（label、used_percent、resets_at，UTC）和 balance（amount、currency）；
    供应商未识别时 supported 为 false，请求失败时返回 error，不影响其他实例。
    """
    paths = profiles.Paths.default()
    instances = [_instance(name)] if name is not None else profiles.list_all_instances(paths)
    return quota.read_all(instances)


@server.tool()
def launch_instance(name: str, cwd: str) -> str:
    """在 macOS Terminal 新窗口启动实例，不替换 MCP 服务。cwd 为启动后的工作目录。"""
    terminal = profiles.launch_instance(_instance(name), Path(cwd))
    return f"已在 Terminal 启动 {name}（{terminal}）"


@server.tool()
def create_instance(
    name: str, template: str, model: str, base_url: str = "", token: str = "",
) -> str:
    """复用模板的客户端及通用设置创建实例。官方账号地址和 token 留空。

    Codex 第三方使用 Responses API 地址；Claude Code 使用 Anthropic 兼容地址。
    """
    source = _instance(template)
    inst = profiles.create_instance(
        profiles.Paths.default(source.engine), name, source, base_url, token, model,
    )
    return f"已创建 {name}，启动命令：{inst.command}"


@server.tool()
def delete_instance(name: str) -> str:
    """删除实例及本地数据；官方实例先登出。默认 claude-1、codex-1 不可删除。"""
    profiles.delete_instance(_instance(name))
    return f"已删除 {name}"


@server.tool()
def reorder_instances(names: list[str]) -> str:
    """设置实例顺序，必须包含全部现有实例且不能重复。"""
    paths = profiles.Paths.default()
    current = {i.name for i in profiles.list_all_instances(paths)}
    if len(names) != len(current) or set(names) != current:
        raise ValueError("必须包含全部现有实例且不能重复")
    profiles.save_order(paths, names)
    return "已更新实例顺序"


@server.tool()
def check_status(name: str | None = None) -> dict[str, Any]:
    """检查实例配置、软链和登录状态；不传名称检查全部。另返回全局问题（含共享层）。"""
    paths = profiles.Paths.default()
    instances = [_instance(name)] if name is not None else profiles.list_all_instances(paths)
    statuses = []
    for inst in instances:
        if inst.settings_error:
            auth = {"error": "配置读取失败，未检查登录"}
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
