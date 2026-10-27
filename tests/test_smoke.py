import io
import json
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from textual.widgets import Select, TextArea

from hm import claude_auth, cli, mcp, profiles, quota, usage
from hm.app import EditScreen, HarnessManager, InstanceTable


@pytest.mark.parametrize("engine", ["claude", "codex", "agy"])
def test_instance_lifecycle(paths, engine):
    paths = profiles.for_engine(paths, engine)
    if engine == "codex":
        paths.link_source.mkdir()
        (paths.link_source / "config.toml").write_text('model = "test-model"\n')
    elif engine == "agy":
        paths.link_source.mkdir(parents=True)
        (paths.root / "agy-1").mkdir(parents=True)
        (paths.root / "agy-1/settings.json").write_text('{"model": "test-model"}')
    template = profiles.list_instances(paths)[0]
    inst = profiles.create_instance(
        paths, f"{engine}-smoke", template, "" if engine == "agy" else "https://example.test/v1",
        "test-key", "test-model",
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


def test_agy_configuration_and_usage(paths, monkeypatch, tmp_path):
    paths = profiles.for_engine(paths, "agy")
    paths.link_source.mkdir(parents=True)
    default = paths.root / "agy-1"
    default.mkdir()
    (default / "settings.json").write_text('{"model": "old", "modelProvider": "gemini"}')
    template = profiles.load_instance(default, "agy")
    with pytest.raises(ValueError, match="custom provider"):
        profiles.create_instance(paths, "agy-bad", template, "https://example.test", "key", "m")
    inst = profiles.create_instance(paths, "agy-test", template, "", "key", "model-id")
    assert inst.settings["modelProvider"] == "gemini"
    assert inst.launch_env({})["GEMINI_API_KEY"] == "key"
    profiles.save_prompt(inst, "instance instructions")
    assert inst.prompt_path.read_text().startswith("---\ntrigger: always_on\n---\n")
    assert profiles.read_prompt(inst) == "instance instructions\n"
    shared = profiles.load_shared(paths)
    shared.mcp_path.parent.mkdir(parents=True)
    shared.mcp_path.write_text("")
    profiles.save_mcp(paths, shared, {"server": {"command": "example"}})
    assert profiles.list_mcp(paths, shared)["server"]["command"] == "example"
    with pytest.raises(ValueError, match="shared/agy"):
        profiles.save_mcp(paths, inst, {})
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='[{"name":"p","enabled":true}]', stderr="")

    monkeypatch.setattr(profiles.subprocess, "run", run)
    assert profiles.list_plugins(paths, inst)[0].enabled
    assert calls[0][1].startswith("--app_data_dir=")
    log = tmp_path / "brain/session/.system_generated/logs/transcript.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE",
                               "created_at": "2026-10-27T12:00:00Z", "input_tokens": 3,
                               "output_tokens": 2}) + "\n")
    [record] = usage.parse_agy(log)
    assert record["model"] == "unknown" and record["latency_ms"] is None


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
    mcp.save_mcp("shared/agy", {"memory": {"command": "npx", "args": ["memory"]}})
    assert mcp.read_layer("shared/agy")["mcp"]["memory"]["command"] == "npx"


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
    agy = paths.home / ".agy-profiles/agy-1"
    (agy / "brain/smoke-1/.system_generated/logs").mkdir(parents=True)
    agy_rows = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-10-20T12:00:00Z",
         "content": "The user changed setting `Model Selection` from None to test-model."},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-10-20T12:00:02Z",
         "input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 0},
    ]
    (agy / "brain/smoke-1/.system_generated/logs/transcript.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in agy_rows)
    )
    usage.save_pricing(paths, json.dumps({"test-model": dict.fromkeys(usage.PRICE_KEYS, 1)}))
    report = usage.report(paths, ["type"])
    assert {row["type"] for row in report} == {"Claude", "Codex", "Agy"}
    assert all(row["requests"] == 1 and row["output"] == 20 for row in report)
    assert sum(row["cost"] for row in report) == pytest.approx(90 / 1e6)

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
    assert len(usage.report(paths, ["type"])) == 3
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


def test_anthropic_quota_without_reset(paths, monkeypatch):
    monkeypatch.setattr(claude_auth, "read", lambda inst: ({}, None))
    monkeypatch.setattr(profiles, "read_credentials", lambda inst: {
        "oauth_token": "test-token", "api_key": None})
    monkeypatch.setattr(quota, "_get", lambda url, headers: {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 11.0, "resets_at": "2026-10-28T18:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
    })
    inst = next(i for i in profiles.list_instances(paths) if i.name == "claude-1")
    result = quota.read_one(inst)
    assert "error" not in result
    assert result["windows"] == [
        {"label": "5h", "used_percent": 0.0, "resets_at": None},
        {"label": "7d", "used_percent": 11.0, "resets_at": "2026-10-28T18:00:00Z"},
    ]
    output = cli._quota_text([result])
    assert "0%" in output and "11%" in output
    assert cli._local(None) is None


@pytest.mark.parametrize("storage", ["keychain", "file"])
def test_claude_quota_refresh(paths, monkeypatch, storage):
    inst = next(i for i in profiles.list_instances(paths) if i.name == "claude-2")
    now = [time.time()]
    expiry = now[0] + 30
    monkeypatch.setattr(claude_auth.time, "time", lambda: now[0])
    record = {"otherCredentials": {"keep": True}, "claudeAiOauth": {
        "accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": expiry * 1000,
        "scopes": ["user:profile"], "subscriptionType": "pro", "clientId": "test-client",
    }}
    file = inst.path / ".credentials.json"
    if storage == "file":
        file.write_text(json.dumps(record))
        file.chmod(0o600)

    def security(args, **kwargs):
        nonlocal record
        assert args[0] == "security"
        assert args[args.index("-s") + 1].startswith("Claude Code-credentials-")
        if args[1] == "find-generic-password":
            return SimpleNamespace(returncode=0 if storage == "keychain" else 44,
                                   stdout=json.dumps(record))
        assert args[1] == "add-generic-password" and storage == "keychain"
        record = json.loads(args[args.index("-w") + 1])
        return SimpleNamespace(returncode=0)

    def saved():
        return record if storage == "keychain" else json.loads(file.read_text())

    refreshes, requests, reject = [], [], []

    def urlopen(request, **kwargs):
        if request.full_url == claude_auth.TOKEN_URL:
            body = json.loads(request.data)
            assert body["grant_type"] == "refresh_token"
            assert body["client_id"] == "test-client"
            assert body["scope"] == "user:profile"
            assert body["refresh_token"] == saved()["claudeAiOauth"]["refreshToken"]
            refreshes.append(body["refresh_token"])
            return io.StringIO(json.dumps({
                "access_token": f"access-{len(refreshes)}",
                "refresh_token": f"refresh-{len(refreshes)}", "expires_in": 28800,
                "scope": "user:profile", "refresh_token_expires_in": 86400,
            }))
        assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
        token = request.get_header("Authorization")
        requests.append(token)
        assert token == f"Bearer {saved()['claudeAiOauth']['accessToken']}"
        if reject:
            raise HTTPError(request.full_url, reject.pop(0), "test error", {}, io.BytesIO())
        return io.StringIO('{"five_hour": {"utilization": 0, "resets_at": null}}')

    monkeypatch.setattr(claude_auth.subprocess, "run", security)
    monkeypatch.setattr(claude_auth.urllib.request, "urlopen", urlopen)
    assert profiles.read_credentials(inst)["oauth_token"] == "old-access"
    assert not refreshes  # Reading credentials must remain read-only.

    # Repeated calls near expiry reuse the token; no early refresh or background timer.
    for remaining in (30, 1, 0.001):
        now[0] = expiry - remaining
        assert "error" not in quota.read_one(inst)
        assert not refreshes
    now[0] = expiry

    # Concurrent quota calls must share one refresh and persist the rotated token.
    assert all("error" not in row for row in quota.read_all([inst, inst]))
    assert refreshes == ["old-refresh"]
    assert saved()["claudeAiOauth"]["refreshToken"] == "refresh-1"
    assert saved()["claudeAiOauth"]["expiresAt"] > time.time() * 1000
    assert saved()["claudeAiOauth"]["refreshTokenExpiresAt"] > time.time() * 1000
    assert saved()["claudeAiOauth"]["subscriptionType"] == "pro"
    assert saved()["otherCredentials"] == {"keep": True}
    assert not (inst.path / ".oauth_refresh.lock").exists()
    if storage == "file":
        assert file.stat().st_mode & 0o777 == 0o600

    # A 401 before local expiry forces exactly one refresh/retry, using the new refresh token.
    reject[:] = [401]
    assert "error" not in quota.read_one(inst)
    assert refreshes == ["old-refresh", "refresh-1"]
    assert requests[-2:] == ["Bearer access-1", "Bearer access-2"]
    reject[:] = [401, 401]
    assert "401" in quota.read_one(inst)["error"]
    assert len(refreshes) == 3 and not reject
    reject[:] = [429]
    assert "429" in quota.read_one(inst)["error"]
    assert len(refreshes) == 3

    # Invalid refresh credentials must report the cause without overwriting the store.
    before = saved()

    def invalid_refresh(request, **kwargs):
        raise HTTPError(request.full_url, 400, "Bad Request", {},
                        io.BytesIO(b'{"error": "invalid_grant"}'))

    monkeypatch.setattr(claude_auth.urllib.request, "urlopen", invalid_refresh)
    with pytest.raises(ValueError, match=r"invalid_grant.*sign in again"):
        claude_auth.refresh(inst, before["claudeAiOauth"]["accessToken"])
    assert saved() == before
    assert not (inst.path / ".oauth_refresh.lock").exists()

    inst.settings["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": "explicit-token"}
    assert not claude_auth.refresh(inst, "explicit-token")
    assert profiles.read_credentials(inst)["oauth_token"] == "explicit-token"
