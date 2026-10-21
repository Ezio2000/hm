"""会话日志用量统计：Claude Code 读共享 projects，Codex 读各实例 sessions。

解析结果按文件缓存在 ~/.hm/usage.db，文件大小或修改时间变了才重读；费用在查询时
按 ~/.hm/pricing.json（每百万 token 美元）计算，改价立即生效。input 统一指未命中缓存
的输入。耗时和首字从日志时间戳估算：耗时 = 请求起点到回复结束，
首字 = 起点到开始输出；速度只统计输出不少于 200 token、耗时不少于 1 秒的请求。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable, Iterator
from compression import zstd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hm.profiles import Paths, for_engine, list_instances

FIELDS = ("rid", "path", "time", "engine", "model", "session", "input", "output",
          "cache_read", "cache_write", "latency_ms", "first_token_ms")
BUCKETS = {"second": "%Y-%m-%d %H:%M:%S", "hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d",
           "week": "%G-W%V", "month": "%Y-%m"}
DIMENSIONS = (*BUCKETS, "type", "model", "session", "instance")
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
CACHE_VERSION = 1


def _ms(timestamp: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return None


def _timing(start: int | None, first: int | None, end: int | None) -> tuple[int | None, ...]:
    """耗时不在 0.1 秒到 1 小时之间、首字不在 (0, 耗时] 之间时视为取错了起点，丢弃。"""
    if start is None or end is None or not 100 <= end - start <= 3_600_000:
        return None, None
    latency = end - start
    return latency, first - start if first is not None and 0 < first - start <= latency else None


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读 JSONL（Codex 会把旧会话压成 .jsonl.zst），跳过坏行。"""
    opener = zstd.open if path.suffix == ".zst" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _session(cwd: str | None, session: str | None) -> str:
    return f"{Path(cwd or '-').name} {session or '-'}"


def parse_claude(path: Path) -> list[dict[str, Any]]:
    """同一条回复的每个内容块各写一行；起点沿第一块的父链往上找，跳过 attachment。"""
    chain: dict[str, tuple[str | None, int | None, bool]] = {}
    replies: dict[str, dict[str, Any]] = {}
    for row in _rows(path):
        ms = _ms(row.get("timestamp"))
        if row.get("uuid"):
            chain[row["uuid"]] = (row.get("parentUuid"), ms, row.get("type") == "attachment")
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict) \
                or not message.get("id") or message.get("model") == "<synthetic>":
            continue
        reply = replies.get(message["id"])
        if reply is None:
            content = message.get("content") or [{}]
            whole = row.get("apiBlockIndex", 0) == 0
            thinking = row.get("thinkingDurationMs") or 0
            reply = replies[message["id"]] = {
                "parent": row.get("parentUuid") if whole else None, "end": ms, "output": 0,
                "first": ms - thinking if whole and ms and thinking > 0
                and content[0].get("type") == "thinking" else None,
            }
        usage = message.get("usage") or {}
        reply.update(model=message.get("model") or "unknown", usage=usage,
                     session=_session(row.get("cwd"), row.get("sessionId")),
                     end=max(filter(None, (reply["end"], ms)), default=None),
                     output=max(reply["output"], usage.get("output_tokens") or 0))
    records = []
    for rid, reply in replies.items():
        usage = reply["usage"]
        cursor, start = reply["parent"], None
        for _ in range(32):
            if cursor not in chain:
                break
            parent, ms, attachment = chain[cursor]
            if not attachment:
                start = ms
                break
            cursor = parent
        latency, first = _timing(start, reply["first"], reply["end"])
        record = {"rid": rid, "time": reply["end"], "engine": "claude", "model": reply["model"],
                  "session": reply["session"], "input": usage.get("input_tokens") or 0,
                  "output": reply["output"],
                  "cache_read": usage.get("cache_read_input_tokens") or 0,
                  "cache_write": usage.get("cache_creation_input_tokens") or 0,
                  "latency_ms": latency, "first_token_ms": first}
        if record["time"] and any(record[k] for k in ("input", "output", "cache_read",
                                                      "cache_write")):
            records.append(record)
    return records


def parse_codex(path: Path) -> list[dict[str, Any]]:
    """每个有新增用量的 token_count 是一次请求；起点是第一个模型输出前最近的
    用户消息、工具结果或上一次请求结束，首字取 item_completed 的 started_at_ms。"""
    records: list[dict[str, Any]] = []
    session = session_id = model = None
    total_seen = -1
    boundary = start = first = end = None
    for row in _rows(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        kind, sub, ms = row.get("type"), payload.get("type") or "", _ms(row.get("timestamp"))
        if kind == "session_meta":
            session_id = payload.get("id")
            session = _session(payload.get("cwd"), session_id)
        elif kind == "turn_context":
            model, boundary = payload.get("model") or model, ms
        elif kind == "token_usage_record":
            end = ms
        elif kind == "response_item":
            if sub in ("reasoning", "agent_message") or sub.endswith("_call") \
                    or (sub == "message" and payload.get("role") == "assistant"):
                start = boundary if start is None else start
            elif sub == "message" or sub.endswith("_output"):
                boundary = ms
        elif sub in ("task_started", "turn_aborted"):
            boundary, start, first, end = ms, None, None, None
        elif sub == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") in ("Reasoning", "AgentMessage") and payload.get("started_at_ms"):
                first = min(first or payload["started_at_ms"], payload["started_at_ms"])
        elif sub == "token_count" and isinstance(info := payload.get("info"), dict):
            total = (info.get("total_token_usage") or {}).get("total_tokens") or 0
            usage = info.get("last_token_usage") or {}
            if total <= total_seen or not usage:
                continue  # 刷新限额时重发的快照不是新请求
            total_seen = total
            latency, first_token = _timing(start if start is not None else boundary,
                                           first, end or ms)
            cached = usage.get("cached_input_tokens") or 0
            records.append({
                "rid": f"{session_id or path}:{total}", "time": ms, "engine": "codex",
                "model": model or "unknown", "session": session or _session(None, None),
                "input": max((usage.get("input_tokens") or 0) - cached, 0),
                "output": usage.get("output_tokens") or 0, "cache_read": cached,
                "cache_write": usage.get("cache_write_input_tokens") or 0,
                "latency_ms": latency, "first_token_ms": first_token,
            })
            boundary, start, first, end = ms, None, None, None
    return records


def _sources(paths: Paths) -> list[tuple[str, Path]]:
    files = [("claude", f) for f in
             (for_engine(paths, "claude").link_source / "projects").rglob("*.jsonl")]
    for inst in list_instances(for_engine(paths, "codex")):
        for folder in ("sessions", "archived_sessions"):
            files += [("codex", f) for pattern in ("*.jsonl", "*.jsonl.zst")
                      for f in (inst.path / folder).rglob(pattern)]
    return files


def _connect(paths: Paths) -> sqlite3.Connection:
    paths.hm_dir.mkdir(exist_ok=True)
    db = sqlite3.connect(paths.hm_dir / "usage.db")
    db.execute("create table if not exists files (path text primary key, size int, mtime real)")
    db.execute(f"create table if not exists records ({', '.join(FIELDS)})")
    db.execute("create index if not exists records_path on records (path)")
    db.execute("create index if not exists records_time on records (time)")
    return db


def sync(paths: Paths, engine: str | None = None) -> None:
    """重读指定客户端新增或变化的日志文件，清掉已删除文件的记录。"""
    with _connect(paths) as db:
        if db.execute("pragma user_version").fetchone()[0] != CACHE_VERSION:
            db.execute("delete from records")
            db.execute("delete from files")
            db.execute(f"pragma user_version = {CACHE_VERSION}")
        known = {p: (s, m) for p, s, m in db.execute("select path, size, mtime from files")}
        seen = set()
        for source_engine, file in _sources(paths):
            key = str(file)
            seen.add(key)
            if engine and source_engine != engine:
                continue
            stat = file.stat()
            if known.get(key) == (stat.st_size, stat.st_mtime):
                continue
            records = parse_codex(file) if source_engine == "codex" else parse_claude(file)
            db.execute("delete from records where path = ?", (key,))
            db.executemany(f"insert into records values ({', '.join('?' * len(FIELDS))})",
                           [tuple({**r, "path": key}[f] for f in FIELDS) for r in records])
            db.execute("insert or replace into files values (?, ?, ?)",
                       (key, stat.st_size, stat.st_mtime))
        for key in known.keys() - seen:
            db.execute("delete from records where path = ?", (key,))
            db.execute("delete from files where path = ?", (key,))


# ---------- 价格 ----------

def load_pricing(paths: Paths) -> dict[str, dict[str, float]]:
    try:
        data = json.loads((paths.hm_dir / "pricing.json").read_text())
    except FileNotFoundError:
        return {}
    return {k.lower(): v for k, v in data.items()}


def pricing_text(paths: Paths) -> str:
    """价格文件内容；还没有时列出日志里出现过的模型，价格先填 0。"""
    file = paths.hm_dir / "pricing.json"
    if file.exists():
        return file.read_text()
    with _connect(paths) as db:
        models = sorted({m.lower().split("/")[-1].split("[")[0]
                         for (m,) in db.execute("select distinct model from records")})
    return json.dumps({m: dict.fromkeys(PRICE_KEYS, 0) for m in models}, indent=2) + "\n"


def save_pricing(paths: Paths, text: str) -> None:
    data = json.loads(text)
    if not isinstance(data, dict) or not all(
        isinstance(v, dict) and set(v) == set(PRICE_KEYS)
        and all(isinstance(n, int | float) for n in v.values()) for v in data.values()
    ):
        raise ValueError(f"格式为 {{模型: {{{', '.join(PRICE_KEYS)}}}}}，单位是每百万 token 美元")
    paths.hm_dir.mkdir(exist_ok=True)
    (paths.hm_dir / "pricing.json").write_text(text if text.endswith("\n") else text + "\n")


def price_of(pricing: dict[str, dict[str, float]], model: str) -> dict[str, float] | None:
    """先精确匹配，再逐段去掉末尾的 -xxx：claude-opus-5-5 可匹配 claude-opus-5。"""
    name = model.lower().split("/")[-1].split("[")[0]
    while name not in pricing:
        if "-" not in name:
            return None
        name = name.rsplit("-", 1)[0]
    return pricing[name]


# ---------- 聚合 ----------

def _instance_resolver(paths: Paths) -> Callable[[dict[str, Any]], str]:
    """记录所属实例：Codex 日志在实例自己的目录；Claude 日志共享，按各实例
    session-env、file-history 里的会话 ID 对应，对不上的记为 unknown。"""
    claude = {}
    for inst in list_instances(for_engine(paths, "claude")):
        for folder in ("session-env", "file-history"):
            for entry in (inst.path / folder).glob("*"):
                claude.setdefault(entry.name, inst.name)
    codex = [(f"{inst.path}/", inst.name) for inst in list_instances(for_engine(paths, "codex"))]

    def resolve(record: dict[str, Any]) -> str:
        if record["engine"] == "claude":
            return claude.get(record["session"].rsplit(" ", 1)[-1], "unknown")
        return next((name for prefix, name in codex if record["path"].startswith(prefix)),
                    "unknown")

    return resolve


def report(paths: Paths, by: list[str], since: datetime | None = None,
           engine: str | None = None, session: str | None = None,
           instance: str | None = None) -> list[dict[str, Any]]:
    """按 by 里的维度分组汇总，时间维度最多一个；engine、session、instance 用来过滤。"""
    if unknown := [d for d in by if d not in DIMENSIONS]:
        raise ValueError(f"不支持的维度：{unknown}，可选 {DIMENSIONS}")
    if sum(d in BUCKETS for d in by) > 1:
        raise ValueError("时间粒度只能选一个")
    if instance and instance != "unknown":
        owner = next((inst for kind in ("claude", "codex")
                      for inst in list_instances(for_engine(paths, kind))
                      if inst.name == instance), None)
        if owner is None:
            raise ValueError(f"实例不存在：{instance}")
        if engine and engine != owner.engine:
            return []
        engine = owner.engine
    sync(paths, engine)
    pricing = load_pricing(paths)
    sql = f"select {', '.join(FIELDS)} from records where time >= ?"
    args: list[Any] = [int(since.timestamp() * 1000) if since else 0]
    for column, value in (("engine", engine), ("session", session)):
        if value:
            sql += f" and {column} = ?"
            args.append(value)
    resolve = _instance_resolver(paths) if instance or "instance" in by else None
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    seen = set()
    with _connect(paths) as db:
        rows = db.execute(sql + " order by time", args).fetchall()
    for values in rows:
        r = dict(zip(FIELDS, values, strict=True))
        owner = resolve(r) if resolve else None
        if instance and owner != instance:
            continue
        if (r["engine"], r["rid"]) in seen:  # resume 等会把同一条回复写进多个文件
            continue
        seen.add((r["engine"], r["rid"]))
        labels = {"type": r["engine"].capitalize(), "model": r["model"], "session": r["session"],
                  "instance": owner,
                  **{b: datetime.fromtimestamp(r["time"] / 1000).strftime(f)
                     for b, f in BUCKETS.items()}}
        key = tuple(labels[d] for d in by)
        g = groups.setdefault(key, {"requests": 0, **dict.fromkeys(PRICE_KEYS, 0), "cost": 0,
                                    "speed_output": 0, "speed_ms": 0, "first_tokens": []})
        g["requests"] += 1
        g["last"] = r["time"]  # 记录按时间升序读出，最后一条就是最近一次
        for k in PRICE_KEYS:
            g[k] += r[k]
        price = price_of(pricing, r["model"])
        if price is None:
            g["cost"] = None
        elif g["cost"] is not None:
            g["cost"] += sum(r[k] * price[k] for k in PRICE_KEYS) / 1e6
        if r["latency_ms"] and r["output"] >= 200 and r["latency_ms"] >= 1000:
            g["speed_output"] += r["output"]
            g["speed_ms"] += r["latency_ms"]
        if r["first_token_ms"]:
            g["first_tokens"].append(r["first_token_ms"])
    result = []
    for key, g in groups.items():
        result.append({
            **{("time" if d in BUCKETS else d): v for d, v in zip(by, key, strict=True)},
            "requests": g["requests"], **{k: g[k] for k in PRICE_KEYS},
            "cost": g["cost"],
            "speed": round(g["speed_output"] * 1000 / g["speed_ms"], 1) if g["speed_ms"] else None,
            "first_token": int(statistics.median(g["first_tokens"])) if g["first_tokens"] else None,
            "last": datetime.fromtimestamp(g["last"] / 1000).strftime(BUCKETS["second"]),
        })
    result.sort(key=lambda row: (row.get("time", row["last"]), row["cost"] or 0), reverse=True)
    return result


def since_days(days: int | None) -> datetime | None:
    """最近 days 天（含今天）的起点；None 表示不限。"""
    if days is None:
        return None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)
