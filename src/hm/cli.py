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
    """A time span such as 7d, 5m or 20s; all means no limit."""
    if text == "all":
        return None
    if not (match := re.fullmatch(rf"(\d+)([{''.join(UNITS)}])", text)):
        raise argparse.ArgumentTypeError(
            f"Expected a span like 7d, 5m or 20s (units s m h d w) or all: {text}")
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
            raise SystemExit(f"Instance not found: {args.name}")
    return quota.read_all(instances)


def _local(iso: str | None) -> str | None:
    return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M") if iso else None


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
    parser = argparse.ArgumentParser(description="hm: manage Claude Code and Codex instances")
    commands = parser.add_subparsers(dest="command", metavar="[command]")
    commands.add_parser("mcp", help="start the stdio MCP server")
    init_parser = commands.add_parser(
        "init", help="initialize or adopt an existing configuration; may ask for admin rights")
    init_parser.add_argument("--engine", required=True, choices=["claude", "codex", "agy"],
                             help="client to initialize")
    usage_parser = commands.add_parser("usage", help="token usage and cost from session logs")
    speed_parser = commands.add_parser(
        "speed", help="generation speed and time to first token from session logs")
    for sub in (usage_parser, speed_parser):
        sub.add_argument("--by", nargs="+", choices=usage.DIMENSIONS,
                         help=("grouping dimensions, at most one time granularity; defaults: "
                               "speed uses instance model, usage uses hour for spans under "
                               "1 day, otherwise day"))
        sub.add_argument("--since", type=_span, default=_span("7d"), metavar="SPAN",
                         help="time span such as 7d, 12h, 5m or 20s; all for no limit (default 7d)")
        sub.add_argument("--engine", choices=["claude", "codex", "agy"])
        sub.add_argument("--limit", type=int, default=10,
                         help="maximum rows, most recent first; 0 for no limit (default 10)")
        sub.add_argument("--instance", help="only this instance, e.g. claude-2")
    instances_parser = commands.add_parser("instances", help="list instances")
    quota_parser = commands.add_parser("quota", help="plan quota windows and account balances")
    quota_parser.add_argument("name", nargs="?", help="instance name; all instances if omitted")
    for sub in (usage_parser, speed_parser, quota_parser, instances_parser):
        sub.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args()
    if args.command == "init":
        try:
            initialize.run(profiles.Paths.default(args.engine))
            print(f"{args.engine} initialized")
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
            print(f"Showing the first {args.limit} of {len(data)} rows (--limit 0 shows all)",
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
