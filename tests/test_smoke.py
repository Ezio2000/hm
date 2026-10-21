import json

import pytest
from textual.widgets import Select, TextArea

from hm import cli, mcp, profiles, quota, usage
from hm.app import EditScreen, HarnessManager, InstanceTable


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_instance_lifecycle(paths, engine):
    paths = profiles.for_engine(paths, engine)
    if engine == "codex":
        paths.link_source.mkdir()
        (paths.link_source / "config.toml").write_text('model = "test-model"\n')
    template = profiles.list_instances(paths)[0]
    inst = profiles.create_instance(
        paths, f"{engine}-smoke", template, "https://example.test/v1", "test-key", "test-model",
    )
    assert inst.model == "test-model"
    assert inst.broken_links(paths) == []
    profiles.save_prompt(profiles.load_shared(paths), "shared smoke rules")
    assert any("shared smoke rules" in text for _, text in profiles.resolve(paths, inst).prompt)
    profiles.delete_instance(inst)
    assert not inst.path.exists()
    assert paths.link_source.exists()


def test_cli_instances(paths, monkeypatch, capsys):
    monkeypatch.setattr(profiles.Paths, "default", classmethod(lambda cls: paths))
    monkeypatch.setattr("sys.argv", ["hm", "instances", "--json"])
    cli.main()
    assert [row["name"] for row in json.loads(capsys.readouterr().out)] == [
        "claude-1", "claude-2", "claude-kimi"]


async def test_mcp_read_write(paths, monkeypatch):
    monkeypatch.setattr(
        profiles.Paths, "default",
        classmethod(lambda cls, engine="claude": profiles.for_engine(paths, engine)),
    )
    names = {tool.name for tool in await mcp.server.list_tools()}
    assert {"overview", "read_layer", "read_result", "save_mcp"} <= names
    assert len(mcp.overview()["instances"]) == 3
    mcp.save_mcp("shared/claude", {"web": {"url": "https://example.test/mcp"}})
    assert mcp.read_layer("shared/claude")["mcp"]["web"]["url"] == "https://example.test/mcp"
    assert mcp.read_result("claude-2")["mcp"][0]["name"] == "web"


async def test_tui_edit_and_launch(paths, mock_auth_status):
    app = HarnessManager(paths)
    async with app.run_test() as pilot:
        assert app.query_one(InstanceTable).row_count == 3
        app.query_one("#actions", Select).value = "Edit"
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)
        app.screen.query_one(TextArea).text = '{"model": "haiku"}'
        await pilot.press("ctrl+s")
        assert not isinstance(app.screen, EditScreen)
        await pilot.click("#--content-tab-shared")
        assert app.scope == "shared"
        await pilot.click("#--content-tab-usage-tab")
        await app.workers.wait_for_complete()
        assert app.scope == "usage-tab"
        await pilot.click("#--content-tab-instance")
        app.query_one("#actions", Select).value = "Launch instance"
        await pilot.pause()
    assert app.return_value.name == "claude-1"
    assert app.return_value.model == "haiku"


def test_usage_report(paths, monkeypatch):
    claude_log = paths.link_source / "projects/smoke.jsonl"
    claude_log.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-10-20T12:00:00Z",
        "sessionId": "claude-smoke", "cwd": "/work/demo",
        "message": {"id": "reply-1", "model": "test-model",
                    "usage": {"input_tokens": 10, "output_tokens": 20}},
    }) + "\n")
    codex = paths.home / ".codex"
    (codex / "sessions").mkdir(parents=True)
    (codex / "config.toml").write_text('model = "test-model"\n')
    rows = [
        {"type": "session_meta", "payload": {"id": "codex-smoke", "cwd": "/work/demo"}},
        {"type": "turn_context", "payload": {"model": "test-model"}},
        {"type": "event_msg", "timestamp": "2026-10-20T12:00:00Z", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 30},
                "last_token_usage": {"input_tokens": 10, "output_tokens": 20}}}},
    ]
    (codex / "sessions/smoke.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    usage.save_pricing(paths, json.dumps({"test-model": dict.fromkeys(usage.PRICE_KEYS, 1)}))
    report = usage.report(paths, ["type"])
    assert {row["type"] for row in report} == {"Claude", "Codex"}
    assert all(row["requests"] == 1 and row["output"] == 20 for row in report)
    assert sum(row["cost"] for row in report) == pytest.approx(60 / 1e6)

    # The instance filter must apply before parsing; the other, unscanned client's cache
    # must be kept.
    session_env = paths.root / "claude-2/session-env/claude-smoke"
    session_env.mkdir(parents=True)
    codex_log = codex / "sessions/smoke.jsonl"
    codex_log.write_text(codex_log.read_text() + "\n")
    original = usage.parse_codex

    def unexpected_parse(path):
        pytest.fail(f"A Claude query must not parse Codex logs: {path}")

    monkeypatch.setattr(usage, "parse_codex", unexpected_parse)
    filtered = usage.report(paths, ["instance"], instance="claude-2")
    assert len(filtered) == 1
    assert filtered[0]["instance"] == "claude-2"
    assert filtered[0]["requests"] == 1
    assert usage.report(paths, ["type"], engine="claude")[0]["requests"] == 1
    with usage._connect(paths) as db:
        assert db.execute("select count(*) from records where engine = 'codex'").fetchone()[0] == 1
    monkeypatch.setattr(usage, "parse_codex", original)
    assert len(usage.report(paths, ["type"])) == 2
    assert usage.report(paths, ["type"], engine="codex", instance="claude-2") == []
    with pytest.raises(ValueError, match="Instance not found"):
        usage.report(paths, ["type"], instance="missing")


def test_quota_query(paths, monkeypatch):
    monkeypatch.setattr(profiles, "read_credentials", lambda inst: {
        "oauth_token": None, "api_key": "test-key"})

    def get(url, headers):
        assert url == "https://api.kimi.com/coding/v1/usages"
        assert headers["Authorization"] == "Bearer test-key"
        return {"usages": {
            "limit_5h": {"used_ratio": 0.25, "reset_time": "2026-10-20T12:00:00Z"},
            "limit_7d": {"used_ratio": 0.5, "reset_time": "2026-10-27T12:00:00Z"},
        }}

    monkeypatch.setattr(quota, "_get", get)
    inst = next(i for i in profiles.list_instances(paths) if i.name == "claude-kimi")
    [result] = quota.read_all([inst])
    assert result["supported"] and "error" not in result
    assert [window["used_percent"] for window in result["windows"]] == [25, 50]
