import json

from hm import cli, profiles


def test_initialize_from_zero(tmp_path, monkeypatch):
    paths = profiles.Paths(tmp_path, tmp_path / "system/ClaudeCode", tmp_path / "system/codex")
    monkeypatch.setattr(profiles.shutil, "which", lambda engine: f"/clients/{engine}")
    monkeypatch.setattr(profiles.Paths, "default", classmethod(
        lambda cls, engine="claude": profiles.for_engine(paths, engine)))
    for engine in ("claude", "codex"):
        monkeypatch.setattr("sys.argv", ["hm", "init", "--engine", engine])
        cli.main()
        p = profiles.for_engine(paths, engine)
        [inst] = profiles.list_instances(p)
        assert inst.name == f"{engine}-1"
        assert profiles.find_problems(p) == []
        settings = inst.settings_path.read_bytes()
        cli.main()
        assert inst.settings_path.read_bytes() == settings
    assert not paths.hm_dir.exists()


def test_initialize_migration(tmp_path, monkeypatch):
    paths = profiles.Paths(tmp_path, tmp_path / "system/ClaudeCode", tmp_path / "system/codex")
    monkeypatch.setattr(profiles.shutil, "which", lambda engine: f"/clients/{engine}")
    monkeypatch.setattr(profiles.Paths, "default", classmethod(
        lambda cls, engine="claude": profiles.for_engine(paths, engine)))
    claude = tmp_path / ".claude"
    (claude / "skills/personal").mkdir(parents=True)
    (claude / "skills/personal/SKILL.md").write_text("personal skill")
    (claude / "projects").mkdir()
    (claude / "projects/session.jsonl").write_text("session")
    (claude / "settings.json").write_text('{"model": "opus"}')
    (claude / "CLAUDE.md").write_text("personal prompt")
    (claude / ".credentials.json").write_text("login-token")
    (tmp_path / ".claude.json").write_text('{"mcpServers": {}, "loggedIn": true}')
    paths.claude_system.mkdir(parents=True)
    (paths.claude_system / "CLAUDE.md").write_text("shared prompt")
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "model-x"\n')
    (codex / "auth.json").write_text('{"token": "sample"}')
    (codex / "AGENTS.md").write_text("codex prompt")
    before_codex = {p.name: p.read_bytes() for p in codex.iterdir()}
    for engine in ("claude", "codex"):
        monkeypatch.setattr("sys.argv", ["hm", "init", "--engine", engine])
        cli.main()
        assert profiles.find_problems(profiles.for_engine(paths, engine)) == []
    [inst] = profiles.list_instances(paths)
    result = profiles.resolve(paths, inst)
    assert claude.is_symlink() and inst.model == "opus"
    assert (claude / ".credentials.json").read_text() == "login-token"
    assert (claude / "projects/session.jsonl").read_text() == "session"
    assert result.skills == [("personal", "实例")]
    assert [text for _, text in result.prompt] == ["shared prompt", "personal prompt"]
    assert json.loads((tmp_path / ".claude.json").read_text())["loggedIn"]
    assert {p.name: p.read_bytes() for p in codex.iterdir()} == before_codex
