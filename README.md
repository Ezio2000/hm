# hm · harness-manager

在终端统一管理 Claude Code 和 Codex 的实例、共享配置、Plugin、Skill、MCP、Prompt，以及用量和额度。支持 macOS，要求 Python 3.14+、uv，以及已安装的对应客户端。

从 GitHub 安装：

```sh
uv tool install 'git+https://github.com/Ezio2000/hm.git'
```

安装后，按需初始化对应客户端，再打开界面：

```sh
hm init --engine claude
hm init --engine codex
hm
```

每次初始化一个客户端。没有配置时创建默认实例；已有配置时直接接入，保留登录、会话、实例 Prompt 和 Skill。重复执行不会覆盖已有配置。

Claude 的 `~/.claude` 和 `~/.claude.json` 迁入 `~/.claude-profiles/claude-1`，原位置建立软链；插件和会话资源接入 `~/.claude-profiles/shared`。Codex 默认实例保留在 `~/.codex`。

系统共享配置分别使用 `/Library/Application Support/ClaudeCode` 和 `/etc/codex`。目录不可写时弹出 macOS 管理员授权，取消后不迁移配置。初始化不修改 `.zshrc`；实例可从 hm 界面启动。

本地开发：

```sh
uv tool install --editable .
uv run --frozen ruff check src tests
uv run --frozen pytest -q
uv build
```

MIT 许可证，见 [LICENSE](LICENSE)。
