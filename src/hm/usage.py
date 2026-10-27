"""Usage statistics from session logs: Claude Code reads the shared projects directory,
Codex reads each instance's sessions.

Parsed results are cached per file in ~/.hm/usage.db and re-read only when the file size or
modification time changes; cost is computed at query time from ~/.hm/pricing.json (USD per
million tokens), so price changes apply immediately. input always means uncached input.
Latency and first token are estimated from log timestamps: latency = request start to end
of reply, first token = start to first output; speed counts only requests with at least
200 output tokens and at least 1 second of latency.
"""

from __future__ import annotations

import json
import re
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
CACHE_VERSION = 3


def _ms(timestamp: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return None


def _timing(start: int | None, first: int | None, end: int | None) -> tuple[int | None, ...]:
    """Drop timings whose latency is outside 0.1 s to 1 h or whose first token is outside
    (0, latency]; the start was misidentified."""
    if start is None or end is None or not 100 <= end - start <= 3_600_000:
        return None, None
    latency = end - start
    return latency, first - start if first is not None and 0 < first - start <= latency else None


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    """Read JSONL line by line (Codex compresses old sessions to .jsonl.zst), skipping bad lines."""
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
    """Each content block of one reply is its own line; the start is found by walking up the
    first block's parent chain, skipping attachments."""
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
    """Each token_count with new usage is one request; the start is the nearest user message,
    tool result or previous request end before the first model output, and the first token
    is the started_at_ms of item_completed."""
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
                continue  # snapshots resent when limits refresh are not new requests
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


def parse_agy(path: Path) -> list[dict[str, Any]]:
    """Parse transcript.jsonl from an Antigravity CLI session."""
    records: list[dict[str, Any]] = []
    conversation_id = path.parent.parent.parent.name
    model = "unknown"
    for row in _rows(path):
        ms = _ms(row.get("created_at") or row.get("timestamp"))
        source = row.get("source")
        kind = row.get("type")
        if source == "USER_EXPLICIT" or kind == "USER_INPUT":
            content = str(row.get("content") or "")
            pattern = r"`Model Selection` from .*? to ([^\n]+?)\.(?:\s[A-Z]|\s*$)"
            if m := re.search(pattern, content):
                model = re.sub(r"[^a-z0-9.]+", "-", m.group(1).lower()).strip("-")
        elif source == "MODEL" and kind == "PLANNER_RESPONSE":
            inp = row.get("input_tokens") or 0
            out = row.get("output_tokens") or 0
            cached = row.get("cache_read_tokens") or 0
            step_idx = row.get("step_index", len(records))
            if not ms or not any((inp, out, cached)):
                continue
            records.append({
                "rid": f"{conversation_id}:{step_idx}",
                "time": ms,
                "engine": "agy",
                "model": model,
                "session": f"- {conversation_id}",
                "input": inp,
                "output": out,
                "cache_read": cached,
                "cache_write": 0,
                "latency_ms": None,
                "first_token_ms": None,
            })
    return records


def _sources(paths: Paths) -> list[tuple[str, Path]]:
    files = [("claude", f) for f in
             (for_engine(paths, "claude").link_source / "projects").rglob("*.jsonl")]
    for inst in list_instances(for_engine(paths, "codex")):
        for folder in ("sessions", "archived_sessions"):
            files += [("codex", f) for pattern in ("*.jsonl", "*.jsonl.zst")
                      for f in (inst.path / folder).rglob(pattern)]
    for inst in list_instances(for_engine(paths, "agy")):
        files += [("agy", f) for f in (inst.path / "brain").rglob("transcript.jsonl")]
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
    """Re-read new or changed log files of the given client and drop records of deleted files."""
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
            records = (
                parse_codex(file) if source_engine == "codex" else
                parse_agy(file) if source_engine == "agy" else
                parse_claude(file)
            )
            db.execute("delete from records where path = ?", (key,))
            db.executemany(f"insert into records values ({', '.join('?' * len(FIELDS))})",
                           [tuple({**r, "path": key}[f] for f in FIELDS) for r in records])
            db.execute("insert or replace into files values (?, ?, ?)",
                       (key, stat.st_size, stat.st_mtime))
        for key in known.keys() - seen:
            db.execute("delete from records where path = ?", (key,))
            db.execute("delete from files where path = ?", (key,))


# ---------- Pricing ----------

def load_pricing(paths: Paths) -> dict[str, dict[str, float]]:
    try:
        data = json.loads((paths.hm_dir / "pricing.json").read_text())
    except FileNotFoundError:
        return {}
    return {k.lower(): v for k, v in data.items()}


def pricing_text(paths: Paths) -> str:
    """Pricing file content; if there is none, list the models seen in the logs at price 0."""
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
        raise ValueError(
            f"Expected {{model: {{{', '.join(PRICE_KEYS)}}}}} in USD per million tokens")
    paths.hm_dir.mkdir(exist_ok=True)
    (paths.hm_dir / "pricing.json").write_text(text if text.endswith("\n") else text + "\n")


def price_of(pricing: dict[str, dict[str, float]], model: str) -> dict[str, float] | None:
    """Match exactly first, then drop trailing -xxx segments: claude-opus-5-5 matches
    claude-opus-5."""
    name = model.lower().split("/")[-1].split("[")[0]
    while name not in pricing:
        if "-" not in name:
            return None
        name = name.rsplit("-", 1)[0]
    return pricing[name]


# ---------- Aggregation ----------

def _instance_resolver(paths: Paths) -> Callable[[dict[str, Any]], str]:
    """Instance owning a record: Codex logs live in the instance's own directory; Claude logs are
    shared and matched by session ID against each instance's session-env, file-history and
    history.jsonl, with unmatched ones recorded as unknown."""
    claude = {}
    for inst in list_instances(for_engine(paths, "claude")):
        for folder in ("session-env", "file-history"):
            for entry in (inst.path / folder).glob("*"):
                claude.setdefault(entry.name, inst.name)
        history = inst.path / "history.jsonl"
        if history.exists():
            for line in history.read_text(errors="ignore").splitlines():
                if match := re.search(r'"sessionId":"([^"]+)"', line):
                    claude.setdefault(match[1], inst.name)
    codex = [(f"{inst.path}/", inst.name) for inst in list_instances(for_engine(paths, "codex"))]
    agy = [(f"{inst.path}/", inst.name) for inst in list_instances(for_engine(paths, "agy"))]

    def resolve(record: dict[str, Any]) -> str:
        if record["engine"] == "claude":
            return claude.get(record["session"].rsplit(" ", 1)[-1], "unknown")
        if record["engine"] == "agy":
            return next((name for prefix, name in agy if record["path"].startswith(prefix)),
                        "unknown")
        return next((name for prefix, name in codex if record["path"].startswith(prefix)),
                    "unknown")

    return resolve


def report(paths: Paths, by: list[str], since: datetime | None = None,
           engine: str | None = None, session: str | None = None,
           instance: str | None = None) -> list[dict[str, Any]]:
    """Aggregate by the dimensions in by, at most one time dimension; engine, session and
    instance filter."""
    if unknown := [d for d in by if d not in DIMENSIONS]:
        raise ValueError(f"Unsupported dimensions: {unknown}; choose from {DIMENSIONS}")
    if sum(d in BUCKETS for d in by) > 1:
        raise ValueError("Choose at most one time granularity")
    if instance and instance != "unknown":
        owner = next((inst for kind in ("claude", "codex", "agy")
                      for inst in list_instances(for_engine(paths, kind))
                      if inst.name == instance), None)
        if owner is None:
            raise ValueError(f"Instance not found: {instance}")
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
        if (r["engine"], r["rid"]) in seen:  # resume etc. write one reply into several files
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
        g["last"] = r["time"]  # records come in ascending time order, so the last is the latest
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
    """Start of the last days days including today; None means no limit."""
    if days is None:
        return None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1)
