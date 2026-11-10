#!/usr/bin/env python3
"""rtc.py — 提醒主链路的确定性执行核心

设计要点：
  1. 不调用模型。判断"该不该响"是纯布尔逻辑。
  2. 状态只有一份：<workspace>/state/alarms.json，符合 contract/alarm-state.schema.json。
  3. 时刻表只有一份：<workspace>/rules/routines.json，符合 contract/routine.schema.json。
  4. 写入是原子的：临时文件 + os.replace，并在写入前比对 rev。
  5. plan 只增不改：已有 acked/missed/skipped 的窗口原样保留。

宿主契约（这是与运行时唯一的耦合面）：
  - stdout 只有两种形态：要投递的正文，或空（空 = 静默）
  - stderr 承载全部诊断信息
  - 退出码 0 正常 / 2 状态冲突 / 3 校验失败 / 4 无匹配窗口

用法：
  python rtc.py --workspace <dir> plan   [--now ISO] [--force]
  python rtc.py --workspace <dir> due    [--now ISO]
  python rtc.py --workspace <dir> ack    [--window ID] [--text TXT] [--now ISO]
  python rtc.py --workspace <dir> status [--json]
  python rtc.py --workspace <dir> metrics sync|report

环境变量 RTC_WORKSPACE 可代替 --workspace；都不给时用当前目录。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WORKSPACE = Path(os.environ.get("RTC_WORKSPACE") or Path.cwd())
STATE = WORKSPACE / "state" / "alarms.json"
ROUTINE = WORKSPACE / "rules" / "routines.json"
DB = WORKSPACE / "state" / "metrics.db"
GRACE_MIN = 15


class Conflict(Exception):
    """rev 不一致，放弃写入。"""


class Validation(Exception):
    """生成的计划不满足校验。"""


# ---------------------------------------------------------------- 基础设施


def now_tz(tz_offset: str) -> datetime:
    sign = 1 if tz_offset[0] == "+" else -1
    hh, mm = tz_offset[1:].split(":")
    return datetime.now(timezone(sign * timedelta(hours=int(hh), minutes=int(mm))))


def parse_iso(s: str, fallback_tz: timezone) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz)
    return dt


def tz_of(offset: str) -> timezone:
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, data: dict, expected_rev: int | None) -> None:
    """写入前比对 rev；不一致则抛 Conflict，绝不覆盖别人的写入。"""
    if expected_rev is not None and path.exists():
        current = load_json(path)
        if int(current.get("rev", 0)) != expected_rev:
            raise Conflict(f"rev 已变：期望 {expected_rev}，实际 {current.get('rev')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".alarms.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_routine() -> dict:
    if not ROUTINE.exists():
        raise Validation(f"找不到时刻表：{ROUTINE}")
    return load_json(ROUTINE)


def load_state() -> dict | None:
    return load_json(STATE) if STATE.exists() else None


# ---------------------------------------------------------------- 心跳与自检

HEARTBEAT_STALE_MIN = 60


def hb_path() -> Path:
    return WORKSPACE / "state" / "heartbeat.json"


def touch_heartbeat(now: datetime) -> None:
    """每次 due 都记一笔，无论响不响。这是「调度器还活着」的唯一证据。"""
    p = hb_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"heartbeat: 写入失败 {e}", file=sys.stderr)


def read_heartbeat() -> datetime | None:
    p = hb_path()
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(load_json(p)["at"])
    except (ValueError, KeyError, TypeError, OSError):
        return None


def self_check(state: dict, now: datetime, first_run: bool = False) -> list[str]:
    """只报告「任务在跑但可能没送达」的信号，正常时不输出任何东西。"""
    probs: list[str] = []
    hb = read_heartbeat()

    if hb is None:
        if not first_run:
            probs.append("⚠️ 没有调度器心跳：rtc-due 可能从未成功运行过")
    else:
        gap_min = (now - hb).total_seconds() / 60
        if gap_min > HEARTBEAT_STALE_MIN:
            probs.append(
                f"⚠️ 调度器最近一次心跳在 {gap_min / 60:.1f} 小时前，这段时间里的窗口可能没响"
            )

    yesterday = (now.date() - timedelta(days=1)).isoformat()
    for h in state.get("history", []):
        if h.get("date") != yesterday:
            continue
        if h.get("state") == "missed" and int(h.get("fires", 0)) == 0:
            probs.append(f"⚠️ 昨天「{h.get('id')}」整段没响过一次，当时调度器可能没在跑")
    return probs


# ---------------------------------------------------------------- 计划生成


def plan_kind(d: date, routine: dict) -> str:
    overrides = routine.get("dateTypes", {}).get("holidayOverrides", [])
    iso = d.isoformat()
    for o in overrides:
        if o.get("date") == iso:
            return o["type"]
    # 契约里 0=周日，Python 里 weekday() 0=周一，必须换算
    dow = (d.weekday() + 1) % 7
    if dow in routine["dateTypes"]["workday"]:
        return "workday"
    if dow in routine["dateTypes"]["weekend"]:
        return "weekend"
    return "holiday"


def expect_fires(start: time, end: time, interval: int) -> int:
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes < 0:
        raise Validation("窗口跨午夜，当前版本不支持")
    return minutes // interval + 1


def build_windows(routine: dict, d: date, kind: str, now: datetime) -> list[dict]:
    tz = tz_of(routine["tzOffset"])
    out = []
    for w in routine["alarmWindows"]:
        allowed = w.get("dateTypes") or []
        if allowed and kind not in allowed:
            continue
        sh, sm = (int(x) for x in w["start"].split(":"))
        eh, em = (int(x) for x in w["end"].split(":"))
        start = datetime.combine(d, time(sh, sm), tzinfo=tz)
        end = datetime.combine(d, time(eh, em), tzinfo=tz)
        exp = expect_fires(time(sh, sm), time(eh, em), int(w["intervalMin"]))
        if w.get("expectFires") and int(w["expectFires"]) != exp:
            raise Validation(
                f"窗口 {w['id']} 的 expectFires={w['expectFires']} 与算术值 {exp} 不符"
            )
        expired = now > end
        out.append(
            {
                "id": w["id"],
                "label": w.get("label", w["id"]),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "intervalMin": int(w["intervalMin"]),
                "expectFires": exp,
                "fireCount": 0,
                "state": "skipped" if expired else "pending",
                "lastFiredAt": None,
                "ackedAt": None,
                "ackText": None,
                "missedAt": None,
                "missedReason": "late_plan" if expired else None,
                "ackHint": (w.get("ackHints") or ["知道了"])[0],
            }
        )
    if not out:
        raise Validation("今天没有任何窗口：检查 routines.json 的 dateTypes")
    return out


TERMINAL = {"acked", "missed", "skipped"}


def carry_forward(old: dict | None, fresh: list[dict]) -> list[dict]:
    """只增不改：同日同 id 的终态窗口原样带回，避免重建冲掉确认。"""
    if not old:
        return fresh
    prev = {w["id"]: w for w in old.get("windows", [])}
    merged = []
    for w in fresh:
        p = prev.get(w["id"])
        if p and p.get("state") in TERMINAL:
            merged.append(p)
        else:
            merged.append(w)
    return merged


def rollover(old: dict | None, today_iso: str) -> list[dict]:
    """日切归档：每个窗口都进历史，不只是 pending/ringing。

    只压未终结的窗口会丢掉 acked 的记录，账本会永久缺一天（原设计的一处缺口）。
    """
    hist = list((old or {}).get("history", []))
    if not old:
        return hist
    plan_date = old.get("planDate", today_iso)
    for w in old.get("windows", []):
        state = w.get("state")
        reason = w.get("missedReason")
        if state in {"pending", "ringing"}:
            state = "missed"
            reason = reason or "day_rollover"
        hist.append(
            {
                "date": plan_date,
                "id": w["id"],
                "state": state,
                "fires": w.get("fireCount", 0),
                "ackedAt": w.get("ackedAt"),
                "missedReason": reason if state == "missed" else None,
            }
        )
    # 同 date+id 去重，后写覆盖先写
    dedup: dict[tuple, dict] = {}
    for h in hist:
        dedup[(h.get("date"), h.get("id"))] = h
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=14)).isoformat()
    return [h for k, h in sorted(dedup.items()) if h.get("date", "") >= cutoff]


def cmd_plan(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    today = now.date().isoformat()

    old = load_state()
    if old and old.get("planDate") == today and not args.force:
        print(f"plan: {today} 已存在，跳过（rev={old.get('rev')}）", file=sys.stderr)
        return 0

    kind = plan_kind(now.date(), routine)
    fresh = build_windows(routine, now.date(), kind, now)

    windows = carry_forward(old, fresh) if old and old.get("planDate") == today else fresh
    history = rollover(old, today) if not (old and old.get("planDate") == today) else list(old.get("history", []))
    total = sum(w["expectFires"] for w in windows)
    if len(windows) != len(fresh):
        raise Validation(f"窗口数 {len(windows)} 与时刻表 {len(fresh)} 不符")

    state = {
        "version": 1,
        "rev": int((old or {}).get("rev", 0)) + 1,
        "timezone": routine.get("timezone", "Asia/Shanghai"),
        "planDate": today,
        "planKind": kind,
        "generatedBy": "rtc-plan",
        "generatedAt": now.isoformat(),
        "windows": windows,
        "history": history,
    }
    atomic_write(STATE, state, expected_rev=int((old or {}).get("rev", 0)) if old else None)
    print(
        f"plan: {today} {kind} {len(windows)} 窗口 / {total} 次，rev={state['rev']}",
        file=sys.stderr,
    )

    # 自检结果走 stdout：有异常才输出，宿主照常投递；正常时一个字都不打印
    for line in self_check(state, now, first_run=(old is None)):
        print(line)
    return 0


# ---------------------------------------------------------------- 到点判断


def cmd_due(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    touch_heartbeat(now)

    state = load_state()
    if not state or state.get("planDate") != now.date().isoformat():
        rc = cmd_plan(argparse.Namespace(now=now.isoformat(), force=False))
        if rc != 0:
            return rc
        state = load_state()

    rev = int(state["rev"])
    changed = False

    # 1) 过期未确认 → missed
    for w in state["windows"]:
        end = datetime.fromisoformat(w["end"])
        if now > end and w["state"] in {"pending", "ringing"}:
            w["state"] = "missed"
            w["missedAt"] = now.isoformat()
            w["missedReason"] = "window_expired"
            changed = True

    # 2) 找命中窗口
    hit = None
    for w in state["windows"]:
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        if start <= now <= end and w["state"] in {"pending", "ringing"}:
            hit = w
            break

    if hit is None:
        if changed:
            state["rev"] = rev + 1
            atomic_write(STATE, state, expected_rev=rev)
            print("due: 已把过期窗口标为 missed", file=sys.stderr)
        return 0

    # 3) 三个条件
    if hit["fireCount"] >= hit["expectFires"]:
        print(f"due: {hit['id']} 已达上限 {hit['expectFires']} 次，静默", file=sys.stderr)
        return 0
    if hit.get("lastFiredAt"):
        last = datetime.fromisoformat(hit["lastFiredAt"])
        gap = (now - last).total_seconds() / 60
        if gap < hit["intervalMin"] - 1:
            print(f"due: 距上次 {gap:.1f} 分钟，未到间隔，静默", file=sys.stderr)
            return 0

    hit["state"] = "ringing"
    hit["fireCount"] += 1
    hit["lastFiredAt"] = now.isoformat()
    state["rev"] = rev + 1
    try:
        atomic_write(STATE, state, expected_rev=rev)
    except Conflict as e:
        # 宁可重复响一次，也不漏响
        print(f"due: 写入冲突（{e}），仍输出文案", file=sys.stderr)

    n = hit["fireCount"]
    head = f"{hit['label']} —— {now.strftime('%H:%M')}"
    if n > 1:
        head += f"（第 {n} 次）"
    lines = [head]
    if n == 1 or n % 2 == 1:
        lines.append(f"回复「{hit['ackHint']}」就不响了。")
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------- 确认


def cmd_ack(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    state = load_state()
    if not state:
        print("ack: 今天还没有计划", file=sys.stderr)
        return 4

    rev = int(state["rev"])
    cands = []
    for w in state["windows"]:
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        grace_end = end + timedelta(minutes=GRACE_MIN)
        if args.window:
            if w["id"] != args.window:
                continue
        else:
            if not (start <= now <= grace_end):
                continue
        if w["state"] == "acked":
            continue
        cands.append(w)

    if not cands:
        print("ack: 没有匹配的窗口", file=sys.stderr)
        return 4

    hit = sorted(cands, key=lambda w: w["start"])[0]
    if hit["state"] not in {"pending", "ringing", "missed"}:
        print(f"ack: {hit['id']} 当前状态 {hit['state']}，不改写", file=sys.stderr)
        return 4

    hit["state"] = "acked"
    hit["ackedAt"] = now.isoformat()
    hit["ackText"] = args.text or ""
    state["rev"] = rev + 1
    try:
        atomic_write(STATE, state, expected_rev=rev)
    except Conflict as e:
        print(f"ack: 写入冲突（{e}）", file=sys.stderr)
        return 2
    print(f"ack: {hit['id']} 已确认", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 状态与指标


def cmd_status(args) -> int:
    routine = load_routine()
    state = load_state()
    if not state:
        print("尚无计划。先跑：python rtc.py plan")
        return 0
    rows = []
    for w in state["windows"]:
        rows.append(
            {
                "id": w["id"],
                "label": w["label"],
                "window": f"{w['start'][11:16]}-{w['end'][11:16]}",
                "state": w["state"],
                "fireCount": w["fireCount"],
                "expectFires": w["expectFires"],
                "ackedAt": w.get("ackedAt"),
            }
        )
    if args.json:
        print(json.dumps({"planDate": state["planDate"], "rev": state["rev"], "windows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"{state['planDate']}  {state['planKind']}  rev={state['rev']}")
    hb = read_heartbeat()
    if hb is None:
        print("  调度器心跳：无记录")
    else:
        gap = (now_tz(routine["tzOffset"]) - hb).total_seconds() / 60
        print(f"  调度器心跳：{gap:.0f} 分钟前")
    for r in rows:
        print(f"  {r['label']:<8} {r['window']}  {r['state']:<8} {r['fireCount']}/{r['expectFires']}")
    return 0


def cmd_metrics(args) -> int:
    state = load_state()
    if not state:
        print("metrics: 没有状态文件", file=sys.stderr)
        return 3
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS daily (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             expect INTEGER NOT NULL, fired INTEGER NOT NULL,
             acked_at TEXT, missed TEXT,
             PRIMARY KEY (date, window))"""
    )
    if args.action == "sync":
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
                    w.get("missedReason") if w["state"] == "missed" else None,
                ),
            )
        for h in state.get("history", []):
            con.execute(
                """INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(date, window) DO UPDATE SET
                     fired=excluded.fired, acked_at=excluded.acked_at, missed=excluded.missed""",
                (
                    h["date"],
                    h["id"],
                    h["id"],
                    None or 0,
                    h.get("fires", 0),
                    h.get("ackedAt"),
                    "history" if h.get("state") == "missed" else None,
                ),
            )
        con.commit()
        print("metrics: 已同步", file=sys.stderr)
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
    con.close()
    return 0


# ---------------------------------------------------------------- 入口


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rtc", description="RTC-kit 确定性核心")
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan")
    sp.add_argument("--now")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sd = sub.add_parser("due")
    sd.add_argument("--now")
    sd.set_defaults(func=cmd_due)

    sa = sub.add_parser("ack")
    sa.add_argument("--window")
    sa.add_argument("--text")
    sa.add_argument("--now")
    sa.set_defaults(func=cmd_ack)

    ss = sub.add_parser("status")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_status)

    sm = sub.add_parser("metrics")
    sm.add_argument("action", choices=["sync", "report"])
    sm.add_argument("--days", type=int, default=30)
    sm.set_defaults(func=cmd_metrics)

    args = p.parse_args(argv)
    if args.workspace:
        global WORKSPACE, STATE, ROUTINE, DB
        WORKSPACE = Path(args.workspace)
        STATE = WORKSPACE / "state" / "alarms.json"
        ROUTINE = WORKSPACE / "rules" / "routines.json"
        DB = WORKSPACE / "state" / "metrics.db"

    try:
        return args.func(args)
    except Validation as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 3
    except Conflict as e:
        print(f"状态冲突：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
