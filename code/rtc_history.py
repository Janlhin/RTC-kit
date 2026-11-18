"""执行历史的唯一真相源：state/history.db。

现在的历史靠两层：`state/alarms.json` 里 14 天的摘要 + `state/schedule/*.json` 快照。
删了 schedule 目录，这些天就查不到了；快照还只是「文件在就一定在」的运气。
这个模块把四张表落进 SQLite：

    windows    窗口定义（跨天不变）
    daily      每天每个窗口的执行结果
    checklist  清单项的逐日打勾状态
    events     所有状态迁移的审计轨迹

写入方是 plan / due / ack / check / postpone / cancel / complete / disable /
enable —— 它们各自在自己成功之后调 `sync_state()` 与 `log_event()`。
迁移期快照仍然照旧生成（人读、可 diff），但它不再是唯一的历史来源。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import rtc_core as core

SCHEMA_VERSION = 1

WINDOW_COLUMNS = (
    "window_id",
    "routine_id",
    "label",
    "start",
    "end",
    "interval_min",
    "one_off",
    "valid_from",
    "valid_until",
    "first_seen",
    "last_seen",
)


def history_db() -> Path:
    return core.WORKSPACE / "state" / "history.db"


def _connect() -> sqlite3.Connection:
    path = history_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    _tables(con)
    return con


def _tables(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS windows (
             window_id TEXT PRIMARY KEY,
             routine_id TEXT NOT NULL,
             label TEXT NOT NULL,
             start TEXT NOT NULL,
             end TEXT NOT NULL,
             interval_min INTEGER NOT NULL,
             one_off INTEGER NOT NULL DEFAULT 0,
             valid_from TEXT,
             valid_until TEXT,
             first_seen TEXT NOT NULL,
             last_seen TEXT NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS daily (
             date TEXT NOT NULL,
             window_id TEXT NOT NULL,
             label TEXT NOT NULL,
             type TEXT,
             expect INTEGER NOT NULL,
             fired INTEGER NOT NULL DEFAULT 0,
             acked_at TEXT,
             missed TEXT,
             state TEXT NOT NULL,
             PRIMARY KEY (date, window_id))"""
    )
    # 老库没有 type 列：补上，历史照样读（issue #9 §10 让类型显式化）
    cols = {r[1] for r in con.execute("PRAGMA table_info(daily)").fetchall()}
    if "type" not in cols:
        con.execute("ALTER TABLE daily ADD COLUMN type TEXT")
    con.execute(
        """CREATE TABLE IF NOT EXISTS checklist (
             date TEXT NOT NULL,
             window_id TEXT NOT NULL,
             item_id TEXT NOT NULL,
             item_label TEXT NOT NULL,
             ordinal INTEGER NOT NULL,
             checked_at TEXT,
             PRIMARY KEY (date, window_id, item_id))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts TEXT NOT NULL,
             date TEXT,
             window_id TEXT NOT NULL,
             item_id TEXT,
             kind TEXT NOT NULL,
             from_date TEXT,
             to_date TEXT,
             note TEXT)"""
    )
    # 跨代归档：补 actor/source 两列（谁改的、从哪来）。老 db 没有这两列，
    # 用 PRAGMA 检查后 ALTER，幂等——重复建表不会报错。
    cols = {row[1] for row in con.execute("PRAGMA table_info(events)")}
    if "actor" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'")
    if "source" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'cli'")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_window ON events(window_id, date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_checklist_item ON checklist(item_id, date)")


def routine_id(routine: dict | None) -> str:
    """时刻表的稳定身份。老文件没有顶层 id 时给一个固定占位，不编随机值。"""
    value = str((routine or {}).get("id") or "").strip()
    return value or "default"


def _hhmm(iso: str | None) -> str:
    return (iso or "")[11:16] or (iso or "")


def sync_state(state: dict, routine: dict | None = None) -> None:
    """把当天状态灌进 DB。幂等：同一份状态重复同步结果相同。"""
    if not state:
        return
    day = str(state.get("planDate") or "")
    if not day:
        return
    rid = routine_id(routine)
    con = _connect()
    try:
        for w in state.get("windows") or []:
            con.execute(
                """INSERT INTO windows (window_id, routine_id, label, start, end,
                       interval_min, one_off, valid_from, valid_until, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(window_id) DO UPDATE SET
                     label=excluded.label, start=excluded.start, end=excluded.end,
                     interval_min=excluded.interval_min, one_off=excluded.one_off,
                     valid_from=excluded.valid_from, valid_until=excluded.valid_until,
                     last_seen=excluded.last_seen""",
                (
                    w["id"],
                    rid,
                    w.get("label") or w["id"],
                    _hhmm(w.get("start")),
                    _hhmm(w.get("end")),
                    int(w.get("intervalMin") or 1),
                    1 if w.get("oneOff") else 0,
                    w.get("validFrom"),
                    w.get("validUntil"),
                    day,
                    day,
                ),
            )
            con.execute(
                """INSERT INTO daily (date, window_id, label, type, expect, fired, acked_at, missed, state)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, window_id) DO UPDATE SET
                     label=excluded.label, type=excluded.type, expect=excluded.expect,
                     fired=excluded.fired, acked_at=excluded.acked_at, missed=excluded.missed,
                     state=excluded.state""",
                (
                    day,
                    w["id"],
                    w.get("label") or w["id"],
                    w.get("type"),
                    int(w.get("expectFires") or 0),
                    int(w.get("fireCount") or 0),
                    w.get("ackedAt"),
                    w.get("missedReason"),
                    str(w.get("state") or "pending"),
                ),
            )
            items = w.get("checklist") or []
            done = {str(x) for x in (w.get("checked") or [])}
            for idx, item in enumerate(items, start=1):
                item_id = str(item.get("id") if isinstance(item, dict) else item)
                label = str(item.get("label") if isinstance(item, dict) else item)
                # Phase 2 之前勾选里存的是序号（"1"），之后是稳定 id；两者都认，
                # 免得换语义那天历史里出现「明明勾了却记成没勾」。
                hit = item_id in done or str(idx) in done or label in done
                con.execute(
                    """INSERT INTO checklist (date, window_id, item_id, item_label, ordinal, checked_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(date, window_id, item_id) DO UPDATE SET
                         item_label=excluded.item_label, ordinal=excluded.ordinal,
                         checked_at=COALESCE(excluded.checked_at, checklist.checked_at)""",
                    (day, w["id"], item_id, label, idx, day if hit else None),
                )
        con.commit()
    finally:
        con.close()


def log_event(
    kind: str,
    window_id: str,
    *,
    ts: datetime | None = None,
    day: str | None = None,
    item_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    note: str | None = None,
    actor: str = "system",
    source: str = "cli",
) -> None:
    """记一条事件。审计不参与判定，写失败也只是少一条轨迹，不该让命令失败。

    actor: 谁做的——user / model / external / system（默认 system，兼容老调用）
    source: 从哪来——cli / wechat / mcp / cron（默认 cli）
    """
    stamp = (ts or datetime.now(UTC)).isoformat()
    con = _connect()
    try:
        con.execute(
            """INSERT INTO events (ts, date, window_id, item_id, kind, from_date, to_date, note, actor, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (stamp, day, window_id, item_id, kind, from_date, to_date, note, actor, source),
        )
        con.commit()
    finally:
        con.close()


def rows_for_day(day: str) -> list[dict]:
    con = _connect()
    try:
        cur = con.execute(
            """SELECT date, window_id, label, expect, fired, acked_at, missed, state
               FROM daily WHERE date=? ORDER BY window_id""",
            (day,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    finally:
        con.close()


def rows_for_range(start: str, end: str) -> list[dict]:
    con = _connect()
    try:
        cur = con.execute(
            """SELECT date, window_id, label, expect, fired, acked_at, missed, state
               FROM daily WHERE date BETWEEN ? AND ? ORDER BY date, window_id""",
            (start, end),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    finally:
        con.close()


def events(window_id: str | None = None, since: str | None = None, until: str | None = None) -> list[dict]:
    sql = ("SELECT ts, date, window_id, item_id, kind, from_date, to_date, note, actor, source "
           "FROM events WHERE 1=1")
    params: list[str] = []
    if window_id:
        sql += " AND window_id=?"
        params.append(window_id)
    if since:
        sql += " AND ts>=?"
        params.append(since)
    if until:
        sql += " AND ts<=?"
        params.append(until)
    sql += " ORDER BY id"
    con = _connect()
    try:
        cur = con.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    finally:
        con.close()


def has_day(day: str) -> bool:
    return bool(rows_for_day(day))


def results_for_day(day: str) -> dict:
    """按快照 `results` 的形状返回某天的结果，供 history 命令直接复用渲染。

    只有「真的有结果」的窗口才算：计划刚生成、一条都没响的 pending 行不算——
    否则 history 会把「今天还没发生」说成一堆「没响成」。
    """
    out = {}
    for row in rows_for_day(day):
        if (
            int(row["fired"] or 0) == 0
            and row["acked_at"] is None
            and row["missed"] is None
            and row["state"] == "pending"
        ):
            continue
        out[row["window_id"]] = {
            "state": row["state"],
            "fires": row["fired"],
            "ackedAt": row["acked_at"],
            "missedReason": row["missed"],
            "label": row["label"],
        }
    return out


def days_in_month(month: str) -> list[str]:
    con = _connect()
    try:
        cur = con.execute(
            "SELECT DISTINCT date FROM daily WHERE date LIKE ? ORDER BY date",
            (f"{month}-%",),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


def stats(group_by: str = "window", missed_only: bool = False) -> list[dict]:
    """按窗口 / 日期 / 月份聚合：响了多少次、确认多少、漏多少。"""
    key = {"window": "window_id", "date": "date", "month": "substr(date, 1, 7)"}.get(group_by)
    if key is None:
        raise ValueError(f"不认识的分组 {group_by}")
    where = (
        " WHERE (missed IS NOT NULL OR (fired < expect AND state NOT IN ('skipped','cancelled','postponed')))"
        if missed_only
        else ""
    )
    sql = f"""SELECT {key} AS grp,
                     COUNT(*) AS days,
                     SUM(expect) AS expect,
                     SUM(fired) AS fired,
                     SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) AS acked,
                     SUM(CASE WHEN missed IS NOT NULL THEN 1 ELSE 0 END) AS missed
              FROM daily{where}
              GROUP BY grp ORDER BY grp"""
    con = _connect()
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
    finally:
        con.close()


def backfill_from_snapshots() -> int:
    """把已有的 schedule/*.json 灌进 DB——老工作区第一次跑 plan 时用，历史不丢。"""
    if not core.SCHEDULE_DIR.exists():
        return 0
    filled = 0
    con = _connect()
    try:
        for path in sorted(core.SCHEDULE_DIR.glob("*.json")):
            try:
                snap = core.load_json(path)
            except (ValueError, TypeError, OSError):
                continue
            if not isinstance(snap, dict):
                continue
            day = str(snap.get("date") or path.stem)
            for w in snap.get("windows") or []:
                con.execute(
                    """INSERT INTO windows (window_id, routine_id, label, start, end,
                           interval_min, one_off, valid_from, valid_until, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(window_id) DO NOTHING""",
                    (
                        w["id"],
                        "default",
                        w.get("label") or w["id"],
                        _hhmm(w.get("start")),
                        _hhmm(w.get("end")),
                        int(w.get("intervalMin") or 1),
                        1 if w.get("oneOff") else 0,
                        w.get("validFrom"),
                        w.get("validUntil"),
                        day,
                        day,
                    ),
                )
                result = (snap.get("results") or {}).get(w["id"]) or {}
                con.execute(
                    """INSERT INTO daily (date, window_id, label, expect, fired, acked_at, missed, state)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(date, window_id) DO NOTHING""",
                    (
                        day,
                        w["id"],
                        w.get("label") or w["id"],
                        int(w.get("expectFires") or 0),
                        int(result.get("fires") or 0),
                        result.get("ackedAt"),
                        result.get("missedReason"),
                        str(result.get("state") or "pending"),
                    ),
                )
            filled += 1
        con.commit()
    finally:
        con.close()
    return filled


def import_state_history(state: dict, routine: dict | None = None) -> None:
    """`state.alarms.json` 里那 14 天的摘要也进 DB——它是老工作区唯一的历史。"""
    if not state:
        return
    rid = routine_id(routine)
    con = _connect()
    try:
        for h in state.get("history") or []:
            day = str(h.get("date") or "")
            if not day:
                continue
            wid = str(h.get("id") or "")
            if not wid:
                continue
            con.execute(
                """INSERT INTO windows (window_id, routine_id, label, start, end,
                       interval_min, one_off, valid_from, valid_until, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,0,NULL,NULL,?,?)
                   ON CONFLICT(window_id) DO NOTHING""",
                (wid, rid, h.get("label") or wid, "", "", 1, day, day),
            )
            con.execute(
                """INSERT INTO daily (date, window_id, label, expect, fired, acked_at, missed, state)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(date, window_id) DO NOTHING""",
                (
                    day,
                    wid,
                    h.get("label") or wid,
                    int(h.get("expect") or 0),
                    int(h.get("fires") or 0),
                    h.get("ackedAt"),
                    h.get("missedReason"),
                    str(h.get("state") or "missed"),
                ),
            )
        con.commit()
    finally:
        con.close()
