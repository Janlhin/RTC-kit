"""执行情况的统计账本：建表、同步、文本导出、report。

只读旁路：输入只读 workspace 的 alarms.json 与快照目录，输出只写自己的 metrics.db。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import rtc_core as core


def _metrics_tables(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS daily (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             expect INTEGER NOT NULL, fired INTEGER NOT NULL,
             acked_at TEXT, missed TEXT,
             PRIMARY KEY (date, window))"""
    )
    # schedule 表：每日快照的 SQL 索引。快照文件是权威存档，这张表只为查询方便。
    con.execute(
        """CREATE TABLE IF NOT EXISTS schedule (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             start TEXT, end TEXT, expect INTEGER NOT NULL,
             one_off INTEGER NOT NULL DEFAULT 0,
             valid_from TEXT, valid_until TEXT,
             PRIMARY KEY (date, window))"""
    )


def metrics_sync(state: dict[str, Any]) -> int:
    """把当天状态 + 历史 + 每日快照灌进 metrics.db，返回快照索引行数。

    这是**派生索引**，不是真相；真丢了下次 sync 能重建。
    """
    con = sqlite3.connect(core.DB)
    try:
        _metrics_tables(con)
        for w in state["windows"]:
            con.execute(
                """INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(date, window) DO UPDATE SET
                     label=excluded.label, expect=excluded.expect, fired=excluded.fired,
                     acked_at=excluded.acked_at, missed=excluded.missed""",
                (
                    state["planDate"],
                    w["id"],
                    w["label"],
                    w["expectFires"],
                    w["fireCount"],
                    w.get("ackedAt"),
                    w.get("missedReason"),
                ),
            )
        for h in state.get("history", []):
            con.execute(
                """INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(date, window) DO UPDATE SET
                     label=excluded.label, expect=excluded.expect,
                     fired=excluded.fired, acked_at=excluded.acked_at, missed=excluded.missed""",
                (
                    h["date"],
                    h["id"],
                    h.get("label") or h["id"],
                    int(h.get("expect", 0)),
                    h.get("fires", 0),
                    h.get("ackedAt"),
                    h.get("missedReason"),
                ),
            )
        snap_count = 0
        if core.SCHEDULE_DIR.exists():
            for f in sorted(core.SCHEDULE_DIR.glob("*.json")):
                try:
                    s = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for w in s.get("windows", []):
                    con.execute(
                        """INSERT INTO schedule (date, window, label, start, end, expect,
                                                one_off, valid_from, valid_until)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(date, window) DO UPDATE SET
                             label=excluded.label, start=excluded.start, end=excluded.end,
                             expect=excluded.expect, one_off=excluded.one_off,
                             valid_from=excluded.valid_from, valid_until=excluded.valid_until""",
                        (
                            s.get("date"),
                            w["id"],
                            w.get("label"),
                            w.get("start"),
                            w.get("end"),
                            int(w.get("expectFires", 0)),
                            1 if w.get("oneOff") else 0,
                            w.get("validFrom"),
                            w.get("validUntil"),
                        ),
                    )
                    snap_count += 1
                # 「有结果但已不在计划里」的条目（如当天被推迟走的窗口）也要进索引，
                # 否则查历史时那一项凭空消失。
                planned = {w["id"] for w in s.get("windows", [])}
                for wid, r in (s.get("results") or {}).items():
                    if wid in planned:
                        continue
                    con.execute(
                        """INSERT INTO schedule (date, window, label, start, end, expect,
                                                one_off, valid_from, valid_until)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(date, window) DO UPDATE SET
                             label=excluded.label, start=NULL, end=NULL""",
                        (
                            s.get("date"),
                            wid,
                            r.get("label") or wid,
                            None,
                            None,
                            int(r.get("expect") or 0),
                            0,
                            None,
                            None,
                        ),
                    )
                    snap_count += 1
        con.commit()
        return snap_count
    finally:
        con.close()


def metrics_dump() -> Path | None:
    """把库导成纯文本 SQL：git 里能看出「今天多了哪几行」，二进制 .db 看不出。"""
    if not core.DB.exists():
        return None
    out = core.WORKSPACE / "state" / "metrics.sql"
    con = sqlite3.connect(core.DB)
    try:
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            for line in con.iterdump():
                fh.write(line + "\n")
    finally:
        con.close()
    return out


def _last_week(today: date) -> tuple[date, date]:
    """上周一 ~ 上周日。周一才算「上周」结束，所以以本周一往前推。"""
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def _week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_report(now: datetime) -> list[str] | None:
    """周一早上的一页小账：上周靠谱吗。

    只在周一输出；发过就记在 state/.weekly-last.json（ISO 周号），一天多条不重复。
    数据全来自 metrics.db；库不在、数据太少都只回一句解释，绝不编百分比。
    """
    if now.weekday() != 0:
        return None
    key = _week_key(now.date())
    mark = core.WORKSPACE / "state" / ".weekly-last.json"
    if mark.exists():
        try:
            if json.loads(mark.read_text(encoding="utf-8")).get("week") == key:
                return None
        except (OSError, ValueError):
            pass

    start, end = _last_week(now.date())
    title = f"📊 上周（{start:%m-%d} ~ {end:%m-%d}）"
    if not core.DB.exists():
        lines = [title, "还没有 statistics 数据（metrics.db 没生成），这周先照常跑"]
    else:
        con = sqlite3.connect(core.DB)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT date, window, label, expect, fired, acked_at, missed
                   FROM daily WHERE date BETWEEN ? AND ?""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            prev = con.execute(
                """SELECT COUNT(*) rows, SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) acked
                   FROM daily WHERE date BETWEEN ? AND ?""",
                ((start - timedelta(days=7)).isoformat(), (start - timedelta(days=1)).isoformat()),
            ).fetchone()
            span = con.execute(
                """SELECT MIN(start), MAX(start) FROM schedule WHERE date BETWEEN ? AND ?""",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        finally:
            con.close()
        if len(rows) < 3:
            lines = [title, f"上周记录太少（{len(rows)} 个窗口），先看这周"]
        else:
            # 「提醒 N 次」数的是提醒条目，不是每个窗口里 5 分钟一次的重复响铃——
            # 用户脑子里的一周是「该提醒我 42 次」，不是「喇叭响了 294 遍」。
            slots = len(rows)
            acked = sum(1 for r in rows if r["acked_at"])
            missed = sum(1 for r in rows if r["missed"])
            rate = f"{acked / slots * 100:.0f}%"
            lines = [title, f"提醒 {slots} 次 · 确认 {acked} 次（{rate}）· 漏响 {missed} 次"]
            worst: dict[str, int] = {}
            for r in rows:
                if r["missed"]:
                    worst[r["label"]] = worst.get(r["label"], 0) + 1
            if worst:
                top, n = max(worst.items(), key=lambda kv: kv[1])
                lines.append(f"最常漏：{top} {n} 次")
            if span and span["MIN(start)"]:
                lines.append(f"最早 {span['MIN(start)'][11:16]} · 最晚 {span['MAX(start)'][11:16]}")
            if prev and prev["rows"] and prev["acked"] is not None:
                old = prev["acked"] / prev["rows"] * 100
                lines.append(f"比上上周：确认率 {old:.0f}% → {rate}")
            paused = _paused_days(start, end)
            if paused:
                lines.append(f"上周暂停 {paused} 天")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"week": key, "at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")
    return lines


def _paused_days(start: date, end: date) -> int:
    """上周被 pause 盖住的天数。读不到就当没暂停——宁可少说一句，不编。"""
    path = core.WORKSPACE / "state" / "paused.json"
    try:
        p = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    try:
        f, t = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
    except (KeyError, ValueError, TypeError):
        return 0
    lo, hi = max(f, start), min(t, end)
    return (hi - lo).days + 1 if lo <= hi else 0


def cmd_metrics(args: argparse.Namespace) -> int:
    state = core.load_state()
    if not state:
        print("metrics: 没有状态文件", file=sys.stderr)
        return 3

    if args.action == "sync":
        snap_count = metrics_sync(state)
        print(f"metrics: 已同步（schedule 索引 {snap_count} 行）", file=sys.stderr)
        return 0

    con = sqlite3.connect(core.DB)
    _metrics_tables(con)
    if args.action == "schedule":
        if args.date:
            rows = con.execute(
                """SELECT date, window, label, start, end, expect, one_off,
                          valid_from, valid_until
                   FROM schedule WHERE date=?""",
                (args.date,),
            ).fetchall()
            if not rows:
                print(f"metrics: schedule 表里没有 {args.date}", file=sys.stderr)
                con.close()
                return 4
            for r in rows:
                # start/end 可能为 NULL：「有结果但已不在计划里」的条目（当天被推迟走的）
                span = f"{r[3][11:16]}–{r[4][11:16]}" if r[3] and r[4] else "（已移出计划）"
                print(
                    f"{r[0]}  {r[1]:<12} {r[2]:<12} {span}"
                    f"  ×{r[5]}"
                    + ("  [一次性]" if r[6] else "")
                    + (f"  {r[7]}~{r[8]}" if r[7] or r[8] else "")
                )
        else:
            rows = con.execute("SELECT date, COUNT(*) FROM schedule GROUP BY date ORDER BY date").fetchall()
            print(f"schedule 表覆盖 {len(rows)} 天" + (f"：{rows[0][0]} ~ {rows[-1][0]}" if rows else ""))
        con.close()
        return 0

    cur = con.execute(
        """SELECT COUNT(*) rows,
                  SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) acked,
                  SUM(CASE WHEN missed IS NOT NULL THEN 1 ELSE 0 END) missed,
                  SUM(fired) fires
             FROM daily"""
    )
    rows, acked, missed, fires = cur.fetchone()
    resolved = con.execute(
        "SELECT COUNT(*) FROM daily WHERE acked_at IS NOT NULL OR missed IS NOT NULL"
    ).fetchone()[0]
    print(f"总行数 {rows}  已结算 {resolved}")
    if resolved:
        print(f"确认率（按已结算口径）{acked}/{resolved} = {acked / resolved * 100:.1f}%")
    print(f"确认 {acked}  漏响 {missed}  响铃尝试合计 {fires}")
    span = con.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM schedule").fetchone()
    if span and span[0]:
        print(f"日程快照索引：{span[0]} ~ {span[1]}（{span[2]} 天）")
    con.close()
    return 0
