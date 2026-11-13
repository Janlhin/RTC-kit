#!/usr/bin/env python3
"""rtc.py — 提醒主链路的确定性执行核心

设计要点：
  1. 不调用模型。判断"该不该响"是纯布尔逻辑。
  2. 状态只有一份：<workspace>/state/alarms.json，符合 contract/alarm-state.schema.json。
  3. 时刻表只有一份：<workspace>/rules/routines.json，符合 contract/routine.schema.json。
  4. 写入是原子的：临时文件 + os.replace，并在写入前比对 rev。
  5. plan 只增不改：已有 acked/missed/skipped 的窗口原样保留。
  6. 窗口可以是一次性的（oneOff）或有有效期（validFrom/validUntil）。
     一次性窗口送达即完成（state=delivered），不需要确认。
     有效期内没有任何窗口 = 合法空日，静默。

宿主契约（这是与运行时唯一的耦合面）：
  - stdout 只有两种形态：要投递的正文，或空（空 = 静默）
  - stderr 承载全部诊断信息
  - 退出码 0 正常 / 2 状态冲突 / 3 校验失败 / 4 无匹配窗口

用法：
  python rtc.py --workspace <dir> plan   [--now ISO] [--force]
  python rtc.py --workspace <dir> due    [--now ISO]
  python rtc.py --workspace <dir> ack    [--window ID] [--text TXT] [--now ISO]
  python rtc.py --workspace <dir> status [--json]
  python rtc.py --workspace <dir> schedule <YYYY-MM-DD> | --month YYYY-MM [--json]
  python rtc.py --workspace <dir> check --window <ID> --item <序号或名称> [--undo]
  python rtc.py --workspace <dir> metrics sync|report|schedule [--date YYYY-MM-DD]

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
SCHEDULE_DIR = WORKSPACE / "state" / "schedule"
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


def set_workspace(path: Path) -> None:
    """切换工作区。

    所有派生路径必须在这里一起更新——漏掉任何一个，命令就会写到别处
    （曾经踩过：快照目录漏更新，结果写到进程 cwd 下）。
    """
    global WORKSPACE, STATE, ROUTINE, DB, SCHEDULE_DIR
    WORKSPACE = path
    STATE = WORKSPACE / "state" / "alarms.json"
    ROUTINE = WORKSPACE / "rules" / "routines.json"
    DB = WORKSPACE / "state" / "metrics.db"
    SCHEDULE_DIR = WORKSPACE / "state" / "schedule"


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
            probs.append(f"⚠️ 昨天「{h.get('id')}」一次都没响过，当时调度器可能没在跑")
    return probs


# ---------------------------------------------------------------- 日程快照

def schedule_snapshot(routine: dict, kind: str, windows: list[dict],
                      day_iso: str, generated_at: str) -> dict:
    """当天完整计划的快照。写一次，留永久——回答「那天到底是什么安排」。

    窗口的 start/end 用生成时的实际值（含日期），有效期从时刻表定义取回。
    """
    defs = {w["id"]: w for w in routine.get("alarmWindows", [])}
    calls = []
    for r in routine.get("rollcalls") or []:
        act = (r.get("kindByDateType") or {}).get(kind)
        if not act or act == "none":
            continue
        calls.append({"hour": r["hour"], "minute": r.get("minute", 0), "kind": act})
    dow = (date.fromisoformat(day_iso).weekday() + 1) % 7
    chores = [
        {"label": c.get("label"), "attachToRollcallHour": c.get("attachToRollcallHour")}
        for c in (routine.get("weeklyChores") or [])
        if int(c.get("dayOfWeek", -1)) == dow
    ]
    return {
        "date": day_iso,
        "planKind": kind,
        "timezone": routine.get("timezone"),
        "generatedAt": generated_at,
        "windows": [
            {
                "id": w["id"],
                "label": w["label"],
                "start": w["start"],
                "end": w["end"],
                "intervalMin": w["intervalMin"],
                "expectFires": w["expectFires"],
                "oneOff": bool(w.get("oneOff")),
                "dueDate": w.get("dueDate"),
                "ackHint": w.get("ackHint"),
                "validFrom": defs.get(w["id"], {}).get("validFrom"),
                "validUntil": defs.get(w["id"], {}).get("validUntil"),
            }
            for w in windows
        ],
        "rollcalls": calls,
        "weeklyChores": chores,
    }


def write_schedule_snapshot(snap: dict) -> Path:
    """原子写一份每日快照。永久保留，不做清理。"""
    d = SCHEDULE_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{snap['date']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".schedule.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


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


def in_valid_range(w: dict, day_iso: str) -> bool:
    """窗口是否在有效期内（闭区间）。任一端省略 = 无限期。"""
    vf = w.get("validFrom")
    vu = w.get("validUntil")
    if vf and day_iso < vf:
        return False
    if vu and day_iso > vu:
        return False
    return True


def day_disabled(routine: dict, kind: str) -> bool:
    """该日期类型被契约显式禁用（weekendAlarms / holidayAlarms = false）。

    禁用日 = 合法空日：静默，不报配置错误。
    """
    if kind == "weekend" and routine.get("weekendAlarms") is False:
        return True
    if kind == "holiday" and routine.get("holidayAlarms") is False:
        return True
    return False


def build_windows(routine: dict, d: date, kind: str, now: datetime,
                  strict: bool = True) -> list[dict]:
    tz = tz_of(routine["tzOffset"])
    day_iso = d.isoformat()
    # 第一步：按有效期筛选。全部落空 = 这段时间本来就没有该响的东西（合法空日）
    ranged = [w for w in routine["alarmWindows"] if in_valid_range(w, day_iso)]
    out = []
    for w in ranged:
        allowed = w.get("dateTypes") or []
        if allowed and kind not in allowed:
            continue
        sh, sm = (int(x) for x in w["start"].split(":"))
        eh, em = (int(x) for x in w["end"].split(":"))
        start = datetime.combine(d, time(sh, sm), tzinfo=tz)
        end = datetime.combine(d, time(eh, em), tzinfo=tz)
        one_off = bool(w.get("oneOff"))
        due = w.get("dueDate")
        items_def = w.get("checklist") or []
        if due or items_def:
            # 倒计时 / 清单窗口：每天只提醒一次。
            # 倒计时的剩余天数由代码算；清单要逐项打勾，都不该每 5 分钟响。
            exp = 1
        elif one_off:
            # 一次性提醒只响一次，不做算术校验（end 可比 start 晚，用于容忍错过 start 那一分钟）
            exp = 1
        else:
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
                "oneOff": one_off,
                "dueDate": due,
                "checklist": [
                    {"id": str(i + 1), "label": str(label)}
                    for i, label in enumerate(w.get("checklist") or [])
                ],
                "checked": [],
            }
        )
    if not out:
        if not ranged:
            # 所有窗口都不在有效期内：还没开始，或已经结束。合法空日，静默。
            return []
        # 有窗口在有效期内，却被 dateTypes 全部排除 → 多半是 dateTypes 写错了
        if strict:
            raise Validation("今天没有任何窗口：检查 routines.json 的 dateTypes")
        return []
    return out


TERMINAL = {"acked", "missed", "skipped"}


def _migrate_checklist(prev: dict, w: dict) -> None:
    """把旧窗口的勾选状态与「已打完」终态迁移到新窗口上。

    清单不会因为日切而重置——昨天打了 3 项，今天还剩 15 项。
    """
    if not w.get("checklist"):
        return
    w["checked"] = list(prev.get("checked") or [])
    items = w["checklist"]
    done = set(w["checked"])
    if prev.get("state") == "acked" and len(done) >= len(items):
        # 已经全部打完 → 保持完成状态，之后不再提醒
        w["state"] = "acked"
        w["ackedAt"] = prev.get("ackedAt")
        w["fireCount"] = int(prev.get("fireCount", 0))


def carry_forward(old: dict | None, fresh: list[dict]) -> list[dict]:
    """只增不改：同日同 id 的终态窗口原样带回，避免重建冲掉确认。"""
    if not old:
        return fresh
    prev = {w["id"]: w for w in old.get("windows", [])}
    merged = []
    for w in fresh:
        p = prev.get(w["id"])
        keep = bool(p) and p.get("state") in TERMINAL
        if keep and w.get("dueDate"):
            keep = False          # 倒计时：每天重新开始，直到截止日
        if keep and w.get("checklist"):
            # 清单：只有全部打完才保留终态；没打完就次日继续（勾选状态带过去）
            done = set(w.get("checked") or [])
            keep = p.get("state") == "acked" and len(done) >= len(w["checklist"])
        if keep:
            merged.append(p)
        else:
            _migrate_checklist(p or {}, w)
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
    if day_disabled(routine, kind):
        # 契约里显式禁用的日期类型 → 合法空日：静默，但照常落盘 state
        fresh = []
    else:
        fresh = build_windows(routine, now.date(), kind, now)

    if old and old.get("planDate") == today:
        windows = carry_forward(old, fresh)
    else:
        # 跨日：窗口全部重建，但清单的勾选状态要带过去
        prev_windows = {x["id"]: x for x in (old or {}).get("windows", [])}
        for w in fresh:
            _migrate_checklist(prev_windows.get(w["id"], {}), w)
        windows = fresh
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

    # 每日快照：主链路写，写一次留永久。失败不阻断提醒，但必须留下诊断。
    try:
        snap = schedule_snapshot(routine, kind, windows, today, now.isoformat())
        write_schedule_snapshot(snap)
    except OSError as e:
        print(f"schedule: 快照写入失败 {e}", file=sys.stderr)

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
    if hit.get("oneOff"):
        # 一次性提醒：送达即完成，不需要等确认（对应旧系统 deleteAfterRun 语义）
        hit["state"] = "delivered"
    state["rev"] = rev + 1
    try:
        atomic_write(STATE, state, expected_rev=rev)
    except Conflict as e:
        # 宁可重复响一次，也不漏响
        print(f"due: 写入冲突（{e}），仍输出文案", file=sys.stderr)

    n = hit["fireCount"]
    head = f"{hit['label']} —— {now.strftime('%H:%M')}"
    if hit.get("dueDate"):
        days = (date.fromisoformat(hit["dueDate"]) - now.date()).days
        if days == 0:
            head += "（最后一天！）"
        elif days == 1:
            head += "（明天截止）"
        elif days > 1:
            head += f"（距截止还有 {days} 天）"
        else:
            head += f"（已过期 {-days} 天）"
    if n > 1:
        head += f"（第 {n} 次）"
    lines = [head]
    items = hit.get("checklist") or []
    if items:
        done = set(hit.get("checked") or [])
        left = [i for i in items if i["id"] not in done]
        head = lines[0]
        head += f"（还剩 {len(left)} 项）"
        lines[0] = head
        row = []
        for it in items:
            mark = "☑" if it["id"] in done else "☐"
            row.append(f"{mark} {it['label']}")
        for i in range(0, len(row), 6):
            lines.append(" ".join(row[i:i + 6]))
        lines.append(f"逐项打勾说「{items[0]['label']}拿到了」；全部拿到说「{hit['ackHint']}」")
    elif n == 1 or n % 2 == 1:
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
        if w["state"] in {"acked", "delivered"}:
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


# ---------------------------------------------------------------- 清单打勾


def cmd_check(args) -> int:
    """勾选 / 取消勾选清单里的某一项。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    结果看退出码（0 成功），细节在 stderr。
    """
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    state = load_state()
    if not state:
        print("check: 今天还没有计划", file=sys.stderr)
        return 4
    hit = None
    for w in state["windows"]:
        if args.window and w["id"] == args.window:
            hit = w
            break
    if hit is None:
        print(f"check: 没有匹配的窗口 {args.window}", file=sys.stderr)
        return 4
    items = hit.get("checklist") or []
    if not items:
        print("check: 该窗口没有清单", file=sys.stderr)
        return 4

    target = None
    for it in items:
        if args.item == it["id"] or args.item == it["label"]:
            target = it
            break
    if target is None:
        try:
            idx = int(args.item) - 1
            if 0 <= idx < len(items):
                target = items[idx]
        except ValueError:
            pass
    if target is None:
        print(f"check: 清单里没有「{args.item}」", file=sys.stderr)
        return 4

    checked = set(hit.get("checked") or [])
    if args.undo:
        checked.discard(target["id"])
    else:
        checked.add(target["id"])
    hit["checked"] = sorted(checked, key=lambda x: int(x) if x.isdigit() else 0)

    rev = int(state["rev"])
    state["rev"] = rev + 1
    # 全部打完 → 该窗口视为完成
    if len(checked) == len(items) and not args.undo:
        hit["state"] = "acked"
        hit["ackedAt"] = now.isoformat()
    atomic_write(STATE, state, expected_rev=rev)
    verb = "已取消" if args.undo else "已打勾"
    print(f"check: {target['label']} {verb}（{len(checked)}/{len(items)}）", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 历史日程查询


def preview_schedule(routine: dict, day_iso: str) -> dict | None:
    """按配置推算某天的安排——用于未来日期（那时还没有快照）。

    快照回答「那天实际是什么」，推算回答「那天将会是什么」。两者不混。
    """
    try:
        d = date.fromisoformat(day_iso)
    except ValueError:
        return None
    kind = plan_kind(d, routine)
    if day_disabled(routine, kind):
        return None
    tz = tz_of(routine["tzOffset"])
    now = datetime.combine(d, time(0, 0), tzinfo=tz)
    windows = build_windows(routine, d, kind, now, strict=False)
    return schedule_snapshot(routine, kind, windows, day_iso, "")


def cmd_schedule(args) -> int:
    """查历史某天（或某月）的日程安排。

    读的是每日快照目录 state/schedule/，永久保留，不受 14 天截断影响。
    """
    if args.month:
        files = sorted(SCHEDULE_DIR.glob(f"{args.month}-*.json"))
        if not files:
            print(f"schedule: 没有 {args.month} 的快照", file=sys.stderr)
            return 4
        if args.json:
            out = []
            for f in files:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        for f in files:
            s = json.loads(f.read_text(encoding="utf-8"))
            d = date.fromisoformat(s["date"])
            dow = "一二三四五六日"[d.weekday()]
            fires = sum(w["expectFires"] for w in s["windows"])
            print(f"{s['date']}（周{dow}）{s['planKind']:<8} "
                  f"{len(s['windows'])} 窗口 / {fires} 次  "
                  f"播报 {len(s['rollcalls'])}  周专项 {len(s['weeklyChores'])}")
        return 0

    if not args.date:
        print("用法：rtc.py schedule <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    path = SCHEDULE_DIR / f"{args.date}.json"
    if path.exists():
        s = json.loads(path.read_text(encoding="utf-8"))
        source = "快照"
    else:
        # 没有快照：该日期还没到（或那天没跑 plan）。按配置推算。
        try:
            s = preview_schedule(load_routine(), args.date)
        except Validation:
            s = None
        if s is None:
            print(f"schedule: 没有 {args.date} 的快照，也推算不出安排", file=sys.stderr)
            return 4
        source = "配置推算"
    if args.json:
        if not args.json_only:
            s = dict(s)
            s["_source"] = source
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    d = date.fromisoformat(s["date"])
    dow = "一二三四五六日"[d.weekday()]
    tag = "" if source == "快照" else "  （未来日期，按配置推算）"
    print(f"{s['date']}（周{dow}）{s['planKind']}  时区 {s.get('timezone')}{tag}")
    if s["windows"]:
        print("窗口：")
        for w in s["windows"]:
            t0 = w["start"][11:16]
            t1 = w["end"][11:16]
            tag = "  [一次性]" if w.get("oneOff") else ""
            span = ""
            if w.get("validFrom") or w.get("validUntil"):
                span = f"  有效期 {w.get('validFrom') or '不限'} ~ {w.get('validUntil') or '不限'}"
            print(f"  {t0}–{t1}  {w['label']:<12} 每 {w['intervalMin']} 分钟 × {w['expectFires']} 次"
                  f"  确认词：{w.get('ackHint')}{tag}{span}")
    else:
        print("窗口：（无）")
    if s["rollcalls"]:
        print("播报：")
        for c in s["rollcalls"]:
            print(f"  {c['hour']:02d}:{c.get('minute', 0):02d}  {c['kind']}")
    else:
        print("播报：（无）")
    if s["weeklyChores"]:
        print("周专项：")
        for c in s["weeklyChores"]:
            print(f"  {c.get('label')}  挂在 {c.get('attachToRollcallHour')}:00 播报末尾")
    else:
        print("周专项：（无）")
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
    # schedule 表：每日快照的 SQL 索引。快照文件是权威存档，这张表只为查询方便。
    con.execute(
        """CREATE TABLE IF NOT EXISTS schedule (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             start TEXT, end TEXT, expect INTEGER NOT NULL,
             one_off INTEGER NOT NULL DEFAULT 0,
             valid_from TEXT, valid_until TEXT,
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
        # schedule 表：从每日快照目录补齐。快照文件是权威存档，这里只做索引。
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
        con.commit()
        print(f"metrics: 已同步（schedule 索引 {snap_count} 行）", file=sys.stderr)
        con.close()
        return 0

    if args.action == "schedule":
        if args.date:
            rows = con.execute(
                """SELECT date, window, label, start, end, expect, one_off,
                          valid_from, valid_until
                   FROM schedule WHERE date=?""", (args.date,)).fetchall()
            if not rows:
                print(f"metrics: schedule 表里没有 {args.date}", file=sys.stderr)
                con.close()
                return 4
            for r in rows:
                print(f"{r[0]}  {r[1]:<12} {r[2]:<12} {r[3][11:16]}–{r[4][11:16]}"
                      f"  ×{r[5]}" + ("  [一次性]" if r[6] else "")
                      + (f"  {r[7]}~{r[8]}" if r[7] or r[8] else ""))
        else:
            rows = con.execute(
                "SELECT date, COUNT(*) FROM schedule GROUP BY date ORDER BY date").fetchall()
            print(f"schedule 表覆盖 {len(rows)} 天"
                  + (f"：{rows[0][0]} ~ {rows[-1][0]}" if rows else ""))
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

    schk = sub.add_parser("check")
    schk.add_argument("--window", required=True, help="窗口 id")
    schk.add_argument("--item", required=True, help="清单项：序号或名称")
    schk.add_argument("--undo", action="store_true", help="取消勾选")
    schk.add_argument("--now")
    schk.set_defaults(func=cmd_check)

    ssch = sub.add_parser("schedule")
    ssch.add_argument("date", nargs="?", help="YYYY-MM-DD")
    ssch.add_argument("--month", help="YYYY-MM")
    ssch.add_argument("--json", action="store_true")
    ssch.add_argument("--json-only", action="store_true", help=argparse.SUPPRESS)
    ssch.set_defaults(func=cmd_schedule)

    ss = sub.add_parser("status")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_status)

    sm = sub.add_parser("metrics")
    sm.add_argument("action", choices=["sync", "report", "schedule"])
    sm.add_argument("--days", type=int, default=30)
    sm.add_argument("--date", help="YYYY-MM-DD（用于 metrics schedule）")
    sm.set_defaults(func=cmd_metrics)

    args = p.parse_args(argv)
    if args.workspace:
        set_workspace(Path(args.workspace))

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
