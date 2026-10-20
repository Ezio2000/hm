"""初始化一个客户端：检查冲突、准备权限、接入默认实例和共享目录。"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import tomlkit

from hm import profiles
from hm.profiles import Paths


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _linked(path: Path, target: Path) -> bool:
    return path.is_symlink() and path.resolve() == target.resolve()


def _check(source: Path, target: Path) -> None:
    if _linked(source, target):
        return
    if source.is_symlink():
        raise ValueError(f"软链指向其他位置，请先整理：{source}")
    if _exists(source) and _exists(target):
        raise ValueError(f"两处都有内容，请先整理冲突：{source} 和 {target}")


def _file(path: Path, text: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _share(source: Path, target: Path, text: str | None = None) -> None:
    if _linked(source, target):
        return
    _check(source, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        source.rename(target)
    elif text is None:
        target.mkdir(exist_ok=True)
    else:
        _file(target, text)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.symlink_to(target)


def _read(path: Path, *, toml: bool = False) -> dict:
    if not path.exists():
        return {}
    data = tomlkit.parse(path.read_text()) if toml else json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"配置顶层必须为对象：{path}")
    return data


def _permissions(directories: list[Path], files: list[Path]) -> None:
    required = [p for p in directories if not p.is_dir() or not os.access(p, os.W_OK)]
    required += [p for p in files if p.exists() and not os.access(p, os.W_OK)]
    privileged = False
    for path in required:
        parent = path
        while not parent.exists():
            parent = parent.parent
        privileged |= not os.access(parent, os.W_OK)
    if not privileged:
        return
    if sys.platform != "darwin":
        raise OSError("系统共享目录不可写；管理员授权仅支持 macOS")
    print("准备目录权限，请在 macOS 对话框中授权：" + "、".join(map(str, required)))
    commands = []
    for path in dict.fromkeys(required):
        quoted = shlex.quote(str(path))
        if path in directories:
            commands.append(f"/bin/mkdir -p {quoted}")
        commands += [f"/usr/sbin/chown {os.getuid()}:{os.getgid()} {quoted}",
                     f"/bin/chmod u+rwX {quoted}"]
    result = subprocess.run(
        ["/usr/bin/osascript", "-e",
         'on run argv\ndo shell script (item 1 of argv) with administrator privileges\nend run',
         "set -e\n" + "\n".join(commands)], capture_output=True, text=True,
    )
    if result.returncode or any(not os.access(p, os.W_OK) for p in required):
        raise OSError(f"管理员授权取消或失败，尚未迁移配置：{result.stderr.strip()}")


def run(paths: Paths) -> None:
    if paths.engine not in ("claude", "codex"):
        raise ValueError("engine 必须为 claude 或 codex")
    if shutil.which(paths.engine) is None:
        raise ValueError(f"找不到客户端 {paths.engine}，请先安装并确保它在 PATH 中")
    shared = profiles.load_shared(paths)
    _read(shared.settings_path, toml=paths.engine == "codex")
    if paths.engine == "codex":
        _read(paths.link_source / "config.toml", toml=True)
        _permissions([paths.codex_system], [shared.settings_path])
        _file(paths.link_source / "config.toml", "")
        _file(shared.settings_path, "")
        shared.skills_dir.mkdir(parents=True, exist_ok=True)
        return

    native = paths.home / ".claude"
    default = paths.root / profiles.DEFAULT_INSTANCE
    original = default if _linked(native, default) or not native.exists() else native
    state = paths.home / ".claude.json"
    state_source = state if state.exists() else original / ".claude.json"
    _check(native, default)
    _check(state, original / ".claude.json")
    _read(original / "settings.json")
    _read(state_source)
    items = [i for i in profiles.LINKED_ITEMS
             if i != "statusline.sh" or (original / i).exists() or (paths.link_source / i).exists()]
    for item in items:
        _check(original / item, paths.link_source / item)
    system_prompt = paths.claude_system / "CLAUDE.md"
    system_skills = paths.claude_system / ".claude/skills"
    _check(system_prompt, shared.prompt_path)
    _check(system_skills, shared.skills_dir)
    common = _read(shared.mcp_path).get("mcpServers", {})
    if not isinstance(common, dict):
        raise ValueError(f"共享 MCP 必须为对象：{shared.mcp_path}")
    states = {i.mcp_path: _read(i.mcp_path) for i in profiles.list_instances(paths)}
    states[default / ".claude.json"] = _read(state_source)
    for path, data in states.items():
        own = data.get("mcpServers", {})
        if not isinstance(own, dict):
            raise ValueError(f"MCP 必须为对象：{path}")
        if conflict := [k for k in own.keys() & common.keys() if own[k] != common[k]]:
            raise ValueError(f"{path} 与共享 MCP 冲突：{', '.join(conflict)}")
    _permissions(
        [paths.claude_system, paths.claude_system / ".claude",
         *[p for p in (system_skills, shared.skills_dir) if p.is_dir()]],
        [shared.settings_path, system_prompt, shared.prompt_path, shared.mcp_path],
    )
    _share(native, default)
    _file(default / "settings.json", "{}\n")
    _share(state, default / ".claude.json", "{}\n")
    for item in items:
        _share(default / item, paths.link_source / item, "" if item == "statusline.sh" else None)
    _file(shared.settings_path, "{}\n")
    _share(system_prompt, shared.prompt_path, "")
    _share(system_skills, shared.skills_dir)
    _file(shared.mcp_path, '{"mcpServers": {}}\n')
    for path, data in states.items():
        if common.keys() - data.get("mcpServers", {}).keys():
            data["mcpServers"] = {**data.get("mcpServers", {}), **common}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
