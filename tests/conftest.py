import json
from pathlib import Path

import pytest

from hm import profiles
from hm.profiles import LINKED_ITEMS, Paths


@pytest.fixture
def mock_auth_status(monkeypatch):
    monkeypatch.setattr(profiles, "auth_status", lambda inst: {"loggedIn": False})


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """仿照真实结构：共享层接入 managed 目录，claude-1（~/.claude 指向它）、
    一个官方实例和一个第三方实例；Codex 系统目录可写但还没有配置。"""
    p0 = Paths(tmp_path, tmp_path / "ClaudeCode", tmp_path / "etc-codex")
    shared = p0.link_source
    for item in LINKED_ITEMS:
        target = shared / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in item:
            target.write_text("")
        else:
            target.mkdir()
    (shared / "skills").mkdir()
    (shared / "CLAUDE.md").write_text("shared rules\n")
    hm_server = {"command": "hm", "args": ["mcp"]}
    (shared / "mcp.json").write_text(json.dumps({"mcpServers": {"hm": hm_server}}))
    p0.claude_system.mkdir()
    (p0.claude_system / "managed-settings.json").write_text("{}")
    (p0.claude_system / "CLAUDE.md").symlink_to(shared / "CLAUDE.md")
    (p0.claude_system / ".claude").mkdir()
    (p0.claude_system / ".claude/skills").symlink_to(shared / "skills")
    p0.codex_system.mkdir()

    def make(name: str, settings: dict) -> None:
        path = p0.root / name
        path.mkdir()
        (path / "settings.json").write_text(json.dumps(settings))
        (path / ".claude.json").write_text(json.dumps({"mcpServers": {"hm": hm_server}}))
        for item in LINKED_ITEMS:
            (path / item).symlink_to(shared / item)

    make("claude-1", {"model": "opus"})
    (tmp_path / ".claude").symlink_to(p0.root / "claude-1")
    (tmp_path / ".claude.json").symlink_to(p0.root / "claude-1/.claude.json")
    make("claude-2", {"model": "opus"})
    make("claude-kimi", {"env": {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
                                 "ANTHROPIC_MODEL": "k3[1M]"}})
    return p0
