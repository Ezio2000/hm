# 本机运行与命令

## 前提

hm 当前项目要求 macOS、Python 3.14+、uv，以及已安装的 Claude Code 或 Codex。宿主须能访问用户目标电脑的终端或本机 hm MCP。仅导入 Skill ZIP 不会安装 hm、接通 MCP，也不会自动获得电脑权限。

## 安装与初始化

仅在用户请求安装时，在其本机执行：

```sh
uv tool install 'git+https://github.com/Ezio2000/hm.git'
```

这是从仓库安装当前代码；组织环境可按其软件审核流程固定已审查的版本。失败时检查依赖和网络，不绕过系统安全校验。

仅初始化用户选择的客户端：

```sh
hm init --engine claude
hm init --engine codex
```

上面是两个独立选项，不默认都执行。初始化会创建或接管配置布局，可能触发 macOS 管理员授权；由用户处理系统授权。不要通过修改全局 HOME 或 CODEX_HOME 切换目标。

`hm` 启动交互式终端 UI，适合用户本机维护敏感配置。`hm mcp` 是长驻 stdio MCP 服务，应由宿主 MCP 客户端启动，不在普通命令工具中调用后等待其退出。宿主若支持本机 stdio，可将命令设为 hm、参数设为 mcp；具体接入入口以该宿主实际功能为准，不保证 WorkBuddy 已提供该能力。

## CLI 查询

先用 `hm instances --json` 获取实际实例名称。以下命令无需读取明文凭据：

```sh
hm usage --since 7d --by instance model --limit 10 --json
hm speed --since 7d --by instance model --limit 10 --json
```

限定客户端可添加 `--engine claude` 或 `--engine codex`；限定实例时添加 `--instance` 和已发现的实例名。参数作为独立 argv 传递，不直接拼接未经验证的用户文本到 shell。

额度命令为 `hm quota <实例名> --json`，会向对应提供商发送鉴权请求。不指定实例将查询全部实例；用户只问一个账号时不要扩展查询范围。

用量与速度支持 `--since 7d`、`12h`、`5m`、`20s` 或 `all`；`--by` 可含 instance、model、type、session 和一个时间粒度（second、hour、day、week、month）。默认避免 all 和 session。CLI 的 --since 表示滚动时长；MCP days 表示包含今天的日历天范围，二者不必逐项相同。

## MCP 路由

- `overview()`：实例、插件、技能、MCP 名称及共享层概要。
- `check_status(name)`：配置、链接、登录等状态；有名称时限制范围。
- `usage_report(by, days, engine, instance)`：Token、估算费用、速度、首字时间。
- `read_quota(name)`：额度窗口、重置时间或余额。结果可能为 unsupported 或 error，不能转换成额度为零。
- 更改实例、插件、技能、提示词前读取当前工具的 schema，核对目标和操作的作用范围。敏感读写按 SKILL.md 的限制走本机 UI。

## 统计口径

- input 为非缓存输入，cache_read 与 cache_write 单独列出；不能把累计输入当成单次上下文长度。
- cost 根据本机价格配置估算，缺少价格时为 null。
- speed 单位 tok/s，first_token 为首字时间中位数，单位 ms。只有至少 200 个输出 token 且时长至少 1 秒的请求参与速度统计。
- Claude 会话可能归为 unknown，表示归属证据不足；不能随意分配给某实例。
- 用量查询可能更新本机统计缓存，但不应据此改动代理配置或删除会话。

## 故障处理

缺少 hm：指导安装，不伪造结果。只有云端环境：交给用户在目标电脑运行。认证错误：说明该接口的当前失败，让用户用客户端检查登录；不要读取或要求发送 token。版本不匹配：检查 help/schema 后调整。遇到一次明确失败先查原因，不循环重复调用额度接口。
