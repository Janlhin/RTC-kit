#!/usr/bin/env python3
"""rtc_calindex.py — L2 P2 日历索引：SQLite 只读缓存，可删可从 .ics 全量重建。

分工（与 ``docs/architecture.md`` §5 同一口径）：

- **源真相**：``<workspace>/calendar/<YYYY>.ics``（RFC 5545 纯文本，跨代可读）。
- **本索引**：``<workspace>/state/calendar-index.db``，只为查询加速；**派生缓存，
  不是真相**。删掉整个 db，``rebuild()`` 能从全部 ``.ics`` 零成本重建。
- **冲突口径**：索引与 ``.ics`` 不一致时**以 ``.ics`` 为准**；查询路径发现索引
  里有、``.ics`` 里没有的陈旧记录，只剔除并往 stderr 留一行可读告警，不反向
  改写 ``.ics``（沿用「派生永不反向写入真相」硬规则）。

纯标准库；不在 ``plan`` / ``due`` / ``ack`` 热路径上。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import rtc_ics as ics

INDEX_NAME = "calendar-index.db"

__all__ = [
    "INDEX_NAME",
    "index_path",
    "rebuild",
    "all_events",
    "get_event",
    "events_for_year",
    "find_uid",
    "diff_with_source",
    "warn_stale",
]

# 索引与直扫逐条比对时逐字段对齐的键；raw_json 另作整包对账。
_COMPARE_KEYS = (
    "uid",
    "kind",
    "start",
    "end",
    "rrule",
    "owner",
    "deadline",
    "status",
    "summary",
    "categories",
    "aliases",
    "tzid",
)


def index_path(workspace: Path) -> Path:
    """索引数据库路径：``<workspace>/state/calendar-index.db``。"""
    return workspace / "state" / INDEX_NAME


def _connect(workspace: Path) -> sqlite3.Connection:
    path = index_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    _tables(con)
    return con


def _tables(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
             uid TEXT PRIMARY KEY,
             kind TEXT,
             start TEXT NOT NULL,
             end TEXT,
             rrule TEXT,
             owner TEXT,
             deadline TEXT,
             status TEXT,
             summary TEXT,
             categories TEXT,
             aliases TEXT,
             tzid TEXT,
             dtstamp TEXT,
             rev TEXT,
             cross_year TEXT,
             raw_json TEXT NOT NULL,
             source_year INTEGER NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS aliases (
             alias TEXT NOT NULL,
             uid TEXT NOT NULL,
             PRIMARY KEY (alias, uid))"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_year ON events(source_year)")


def _year_of(iso: str) -> int:
    return datetime.fromisoformat(iso).year


def rebuild(workspace: Path) -> int:
    """从零扫描全部 ``.ics`` 重建索引，返回灌入的事件条数。

    先在一个事务里清空旧表再灌入；旧索引损坏 / 删除都不影响 ``.ics`` 真相。
    """
    events = ics.load_all_events(workspace)
    con = _connect(workspace)
    try:
        con.execute("BEGIN")
        con.execute("DELETE FROM events")
        con.execute("DELETE FROM aliases")
        for ev in events:
            uid = str(ev["uid"])
            con.execute(
                """INSERT INTO events
                   (uid, kind, start, end, rrule, owner, deadline, status, summary,
                    categories, aliases, tzid, dtstamp, rev, cross_year, raw_json, source_year)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uid,
                    ev.get("kind"),
                    str(ev["start"]),
                    str(ev.get("end") or ""),
                    ev.get("rrule"),
                    ev.get("owner"),
                    ev.get("deadline"),
                    ev.get("status"),
                    ev.get("summary"),
                    json.dumps(ev.get("categories") or [], ensure_ascii=False),
                    json.dumps(ev.get("aliases") or [], ensure_ascii=False),
                    ev.get("tzid"),
                    ev.get("dtstamp"),
                    str(ev.get("rev")) if ev.get("rev") is not None else None,
                    str(ev.get("crossYear")) if ev.get("crossYear") else None,
                    json.dumps(ev, ensure_ascii=False),
                    _year_of(str(ev["start"])),
                ),
            )
            for alias in ev.get("aliases") or []:
                con.execute(
                    "INSERT OR IGNORE INTO aliases (alias, uid) VALUES (?,?)",
                    (str(alias), uid),
                )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return len(events)


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(row["raw_json"]))


def all_events(workspace: Path) -> list[dict[str, Any]]:
    """从索引读全部事件；索引缺失时返回空列表（调用方可再 ``rebuild``）。"""
    path = index_path(workspace)
    if not path.exists():
        return []
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        _tables(con)
        rows = con.execute("SELECT raw_json FROM events ORDER BY start, uid").fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        con.close()


def get_event(workspace: Path, uid: str) -> dict[str, Any] | None:
    con = _connect(workspace)
    try:
        row = con.execute("SELECT raw_json FROM events WHERE uid = ?", (uid,)).fetchone()
        return _row_to_event(row) if row else None
    finally:
        con.close()


def events_for_year(workspace: Path, year: int) -> list[dict[str, Any]]:
    con = _connect(workspace)
    try:
        rows = con.execute(
            "SELECT raw_json FROM events WHERE source_year = ? ORDER BY start, uid", (year,)
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        con.close()


def find_uid(workspace: Path, alias: str) -> str | None:
    con = _connect(workspace)
    try:
        row = con.execute("SELECT uid FROM aliases WHERE alias = ?", (alias,)).fetchone()
        return str(row[0]) if row else None
    finally:
        con.close()


def diff_with_source(workspace: Path) -> list[str]:
    """比对索引与直扫 ``.ics``，返回不一致描述列表；空列表 = 逐条一致。"""
    source = {str(ev["uid"]): ev for ev in ics.load_all_events(workspace)}
    indexed = {str(ev["uid"]): ev for ev in all_events(workspace)}
    problems: list[str] = []

    for uid in sorted(set(indexed) - set(source)):
        problems.append(f"索引存在但 .ics 缺失（陈旧记录，以 .ics 为准）：{uid}")
    for uid in sorted(set(source) - set(indexed)):
        problems.append(f".ics 存在但索引缺失（需 rebuild）：{uid}")
    for uid in sorted(set(source) & set(indexed)):
        s, i = source[uid], indexed[uid]
        for key in _COMPARE_KEYS:
            if s.get(key) != i.get(key):
                problems.append(f"{uid} 字段 {key} 不一致：索引={i.get(key)!r}，.ics={s.get(key)!r}")
    return problems


def warn_stale(workspace: Path) -> None:
    """发现索引与 ``.ics`` 不一致时往 stderr 留一行可读告警；不修复、不反写真相。"""
    problems = diff_with_source(workspace)
    for problem in problems:
        print(f"calindex: 索引与 .ics 不一致，以 .ics 为准 —— {problem}", file=sys.stderr)
