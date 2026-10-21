import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from typing import Any

from hm import initialize, profiles, quota, usage


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [headers, *[["-" if v is None else str(v) for v in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip()
                     for row in cells)


UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _span(text: str) -> timedelta | None:
    """7d、5m、20s 这样的时间跨度；all 表示不限。"""
    if text == "all":
        return None
    if not (match := re.fullmatch(rf"(\d+)([{''.join(UNITS)}])", text)):
        raise argparse.ArgumentTypeError(f"应形如 7d、5m、20s（单位 s m h d w）或 all：{text}")
    return timedelta(**{UNITS[match[2]]: int(match[1])})


def _usage(args: argparse.Namespace) -> list[dict[str, Any]]:
    since = datetime.now() - args.since if args.since is not None else None
    return usage.report(profiles.Paths.default(), args.by, since, args.engine,
                        instance=args.instance)


def _tokens(count: int) -> str:
    for size, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if count >= size:
            return f"{count / size:.1f}{suffix}"
    return str(count)


def _usage_text(rows: list[dict[str, Any]], by: list[str]) -> str:
    dims = ["time" if d in usage.BUCKETS else d for d in by]
    headers = [*dims, "requests", "input", "output", "cache_read", "cache_write", "cost",
               "speed(tok/s)", "first_token(ms)"]
    return _table(headers, [
        [*(row[d] for d in dims), row["requests"], *(_tokens(row[k]) for k in (
            "input", "output", "cache_read", "cache_write")),
         None if row["cost"] is None else f"${row['cost']:.2f}", row["speed"], row["first_token"]]
        for row in rows
    ])


def _quota(args: argparse.Namespace) -> list[dict[str, Any]]:
    instances = profiles.list_all_instances(profiles.Paths.default())
    if args.name:
        instances = [i for i in instances if i.name == args.name]
        if not instances:
            raise SystemExit(f"实例不存在：{args.name}")
    return quota.read_all(instances)


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")


def _quota_text(results: list[dict[str, Any]]) -> str:
    rows = []
    for item in results:
        if not item["supported"]:
            rows.append([item["name"], None, None, None, item["reason"]])
        elif "error" in item:
            rows.append([item["name"], item["provider"], None, None, item["error"]])
        for window in item.get("windows", []):
            rows.append([item["name"], item["provider"], window["label"],
                         f"{window['used_percent']:g}%", _local(window["resets_at"])])
        for balance in item.get("balance", []):
            rows.append([item["name"], item["provider"], "balance",
                         f"{balance['amount']:g} {balance['currency']}", None])
    return _table(["name", "provider", "window", "used / balance", "resets"], rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="hm：Claude Code / Codex 实例管理")
    commands = parser.add_subparsers(dest="command", metavar="[command]")
    commands.add_parser("mcp", help="启动 stdio MCP 服务")
    init_parser = commands.add_parser("init", help="初始化或迁移已有配置；必要时请求管理员授权")
    init_parser.add_argument("--engine", required=True, choices=["claude", "codex"],
                             help="要初始化的客户端")
    usage_parser = commands.add_parser("usage", help="按会话日志统计用量和费用")
    speed_parser = commands.add_parser("speed", help="按会话日志统计生成速度和首 token 延迟")
    for sub in (usage_parser, speed_parser):
        sub.add_argument("--by", nargs="+", choices=usage.DIMENSIONS,
                         help=("分组维度，时间粒度最多一个；speed 默认 instance model，"
                               "usage 跨度小于 1 天默认 hour，否则 day"))
        sub.add_argument("--since", type=_span, default=_span("7d"), metavar="SPAN",
                         help="最近多久，如 7d、12h、5m、20s，all 不限（默认 7d）")
        sub.add_argument("--engine", choices=["claude", "codex"])
        sub.add_argument("--limit", type=int, default=10,
                         help="最多输出几行，按最近请求时间倒序取，0 不限（默认 10）")
        sub.add_argument("--instance", help="只看该实例，如 claude-2")
    instances_parser = commands.add_parser("instances", help="列出实例")
    quota_parser = commands.add_parser("quota", help="查询套餐额度窗口和账户余额")
    quota_parser.add_argument("name", nargs="?", help="实例名，缺省查询全部")
    for sub in (usage_parser, speed_parser, quota_parser, instances_parser):
        sub.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    if args.command == "init":
        try:
            initialize.run(profiles.Paths.default(args.engine))
            print(f"{args.engine} 初始化完成")
        except (OSError, ValueError) as error:
            parser.error(str(error))
    elif args.command == "mcp":
        from hm.mcp import server

        server.run(transport="stdio")
    elif args.command == "instances":
        rows = [{"name": i.name, "engine": i.engine, "provider": i.provider, "model": i.model,
                 "command": i.command}
                for i in profiles.list_all_instances(profiles.Paths.default())]
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json
              else _table(["name", "engine", "provider", "model", "command"],
                          [list(row.values()) for row in rows]))
    elif args.command in ("usage", "speed", "quota"):
        is_usage = args.command in ("usage", "speed")
        if args.command == "speed" and not args.by:
            args.by = ["instance", "model"]
        if args.command == "usage" and not args.by:
            args.by = (["hour"] if args.since is not None and args.since < timedelta(days=1)
                       else ["day"])
        try:
            data = _usage(args) if is_usage else _quota(args)
        except ValueError as error:
            parser.error(str(error))
        if is_usage and args.limit and len(data) > args.limit:
            print(f"仅显示前 {args.limit} 行，共 {len(data)} 行（--limit 0 显示全部）",
                  file=sys.stderr)
            data = data[:args.limit]
        if args.command == "speed":
            dims = ["time" if d in usage.BUCKETS else d for d in args.by]
            keys = [*dims, "requests", "speed", "first_token", "last"]
            data = [{key: row[key] for key in keys} for row in data]
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "speed":
            print(_table([*dims, "requests", "speed(tok/s)", "first_token(ms)", "last"],
                         [list(row.values()) for row in data]))
        else:
            print(_usage_text(data, args.by) if args.command == "usage" else _quota_text(data))
    else:
        from hm.app import main as run_app

        run_app()
