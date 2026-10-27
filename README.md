# hm · harness-manager

A single control plane for [Claude Code](https://claude.com/claude-code), Codex and Antigravity CLI (`agy`) on macOS. hm manages instances, providers, credentials, models, settings, plugins, skills, MCP servers and prompts, and reports usage, cost, generation speed and quota from local session logs.

## Features

- **Instances.** Run multiple isolated Claude Code, Codex and Antigravity CLI instances side by side, each with its own provider and credentials, and launch them from a terminal UI.
- **Layered configuration.** Settings, plugins, skills, MCP servers and prompts are defined once in a per-client shared layer. An instance's effective configuration is its own layer combined with the shared layer.
- **Usage analytics.** Token counts, cost, generation speed (tok/s) and time to first token, grouped by time, model, session or instance. Cost is computed at query time from a user-editable price table.
- **Quota.** Plan windows and account balances across Anthropic, ChatGPT, DeepSeek, Z.ai, Kimi and MiniMax.
- **MCP server.** Every capability above is exposed over stdio MCP, so agents can read and change configuration through the same interface.

## Requirements

- macOS
- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- Claude Code, Codex and/or Antigravity CLI installed

## Installation

```sh
uv tool install 'git+https://github.com/Ezio2000/hm.git'
```

## Getting started

Initialize each client you use, then open the UI:

```sh
hm init --engine claude
hm init --engine codex
hm init --engine agy
hm
```

`hm init` is idempotent. With no existing configuration it creates a default instance; otherwise it adopts the existing one and preserves logins, sessions, instance prompts and skills. It never overwrites existing configuration.

## Layout

| Client | Default instance | Other instances | Shared layer |
| --- | --- | --- | --- |
| Claude Code | `~/.claude-profiles/claude-1` (`~/.claude` and `~/.claude.json` are symlinked to it) | `~/.claude-profiles/<name>`, selected via `CLAUDE_CONFIG_DIR` | `/Library/Application Support/ClaudeCode` and `~/.claude-profiles/shared` |
| Codex | `~/.codex` | `~/.codex-profiles/<name>`, selected via `CODEX_HOME` | `/etc/codex/config.toml` |
| Antigravity CLI | `~/.agy-profiles/agy-1` (`~/.gemini/antigravity-cli` is symlinked to it) | `~/.agy-profiles/<name>`, selected via `--app_data_dir` | Global MCP and rules in `~/.gemini/config`; CLI resources in `~/.agy-profiles/shared` |

Initialization requests macOS administrator authorization when a system directory is not writable; if you cancel, no configuration is migrated. It does not modify `.zshrc`. Instances are launched from the hm UI.

Antigravity CLI settings, skills and plugins are per instance. Its MCP configuration is
global, and instance prompts are stored as always-on rules in the instance's `rules/hm.md`.
Custom provider URLs are not supported; a Gemini API key can be supplied when creating an
instance. The CLI's shared `settings.json` and shared skills directory are not used by hm.

## CLI

| Command | Description |
| --- | --- |
| `hm` | Open the terminal UI |
| `hm init --engine {claude,codex,agy}` | Initialize or adopt an existing configuration |
| `hm instances` | List instances |
| `hm usage` | Tokens and cost from session logs |
| `hm speed` | Generation speed and time to first token |
| `hm quota [name]` | Plan windows and account balances |
| `hm mcp` | Start the stdio MCP server |

`quota` automatically refreshes expired stored Claude OAuth credentials and retries once
after HTTP 401, saving rotated tokens back to the same instance's credential store.
`read_credentials` remains read-only and never refreshes tokens.

`usage`, `speed` and `quota` accept `--json`. `usage` and `speed` share these options:

| Option | Description |
| --- | --- |
| `--by` | Group by `second`, `hour`, `day`, `week`, `month`, `type`, `model`, `session` or `instance`; at most one time granularity. Defaults: `instance model` for `speed`; `hour` or `day` for `usage`, depending on the span. |
| `--since` | Time span such as `7d`, `12h`, `5m` or `20s`; `all` for no limit. Default `7d`. |
| `--engine` | Restrict to `claude`, `codex` or `agy`. |
| `--instance` | Restrict to one instance, for example `claude-2` or `agy-2`. |
| `--limit` | Maximum rows, most recent first; `0` for no limit. Default `10`. |

```sh
hm usage --since 7d
hm speed --instance claude-2 --since 1h
hm speed --by day model --since 7d --limit 0 --json
```

Speed is reported in tok/s and `first_token` is the median time to first token in milliseconds. Both are estimated from log timestamps and show `-` (`null` in JSON) when no valid timing data exists. Only requests with at least 200 output tokens and at least one second of duration contribute to speed.

### Attribution

Claude Code writes all sessions to a shared `projects` directory, so hm attributes each session to an instance by matching its session ID against the instance's `session-env`, `file-history` and `history.jsonl`. Sessions that match no instance are reported as `unknown`. Codex and Antigravity CLI sessions are attributed by the instance directory that holds the log.

Antigravity transcripts do not always identify the model; such records are grouped as
`unknown`. They do not provide request timing suitable for generation speed calculations.

### Pricing

Cost is computed from `~/.hm/pricing.json` (USD per million tokens for `input`, `output`, `cache_read` and `cache_write`). Model names match exactly first, then by progressively dropping trailing `-` segments, so `claude-opus-5-5` falls back to `claude-opus-5`. Changes take effect immediately.

## MCP server

`hm mcp` serves the following tools over stdio: `overview`, `read_layer`, `save_settings`, `save_mcp`, `save_prompt`, `set_plugin`, `delete_skill`, `promote_skill`, `read_credentials`, `read_result`, `check_status`, `create_instance`, `delete_instance`, `reorder_instances`, `launch_instance`, `read_pricing`, `save_pricing`, `usage_report` and `read_quota`.

## Development

```sh
uv tool install --editable .
uv run --frozen ruff check src tests
uv run --frozen pytest -q
uv build
```

## Agent skill

[`skills/hm-assistant/SKILL.md`](skills/hm-assistant/SKILL.md) provides a portable
workflow for local usage, speed, quota and configuration assistance, with WorkBuddy
marketplace metadata. It requires hm on the user's macOS machine or a verified local
hm MCP connection; importing the skill does not install hm or grant local access.
The public skill avoids plaintext credential/configuration readers and directs
sensitive configuration changes to the local terminal UI. It is guidance, not a
technical sandbox or a guarantee of marketplace approval.

Package only `skills/hm-assistant/`, including its `LICENSE` and `references/`;
do not bundle local profiles, logs, credentials or the development environment.

## License

MIT. See [LICENSE](LICENSE).
