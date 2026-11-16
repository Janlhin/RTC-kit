import pathlib

p = pathlib.Path(r"C:\Users\Janlhin\AppData\Local\Temp\rtckit-work\code\rtc.py")
t = p.read_text(encoding="utf-8")

helpers = '''def _metrics_tables(con) -> None:
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


def metrics_sync(state: dict) -> int:
    """把当天状态 + 历史 + 每日快照灌进 metrics.db，返回快照索引行数。

    这是**派生索引**，不是真相；真丢了下次 sync 能重建。
    """
    con = sqlite3.connect(DB)
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
        if SCHEDULE_DIR.exists():
            for f in sorted(SCHEDULE_DIR.glob("*.json")):
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
                        (s.get("date"), w["id"], w.get("label"), w.get("start"), w.get("end"),
                         int(w.get("expectFires", 0)), 1 if w.get("oneOff") else 0,
                         w.get("validFrom"), w.get("validUntil")),
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
                           ON CONFLICT(date, window) DO UPDATE SET label=excluded.label""",
                        (s.get("date"), wid, r.get("label") or wid, None, None,
                         int(r.get("expect") or 0), 0, None, None),
                    )
                    snap_count += 1
        con.commit()
        return snap_count
    finally:
        con.close()


def metrics_dump() -> Path | None:
    """把库导成纯文本 SQL：git 里能看出「今天多了哪几行」，二进制 .db 看不出。"""
    if not DB.exists():
        return None
    out = WORKSPACE / "state" / "metrics.sql"
    con = sqlite3.connect(DB)
    try:
        with out.open("w", encoding="utf-8", newline="\\n") as fh:
            for line in con.iterdump():
                fh.write(line + "\\n")
    finally:
        con.close()
    return out


'''

start = "def cmd_metrics(args) -> int:"
end = '    if args.action == "schedule":'

new_preamble = '''def cmd_metrics(args) -> int:
    state = load_state()
    if not state:
        print("metrics: 没有状态文件", file=sys.stderr)
        return 3

    if args.action == "sync":
        snap_count = metrics_sync(state)
        print(f"metrics: 已同步（schedule 索引 {snap_count} 行）", file=sys.stderr)
        return 0

    con = sqlite3.connect(DB)
    _metrics_tables(con)
'''

i = t.index(start)
j = t.index(end, i)
t = t[:i] + helpers + new_preamble + t[j:]
p.write_text(t, encoding="utf-8")
print("cmd_metrics 已拆分；helpers 已插入")
