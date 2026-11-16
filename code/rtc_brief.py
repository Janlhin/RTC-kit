"""行程简报与查询展示：brief 文案写回 AGENTS.md，schedule / history / status 的输出。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date, datetime, time

import rtc_core as core

# ---------------------------------------------------------------- 历史日程查询


def preview_schedule(routine: dict, day_iso: str) -> dict | None:
    """按配置推算某天的安排——用于未来日期（那时还没有快照）。

    快照回答「那天实际是什么」，推算回答「那天将会是什么」。两者不混。
    """
    try:
        d = date.fromisoformat(day_iso)
    except ValueError:
        return None
    kind = core.plan_kind(d, routine)
    if core.day_disabled(routine, kind):
        return None
    tz = core.tz_of(routine["tzOffset"])
    now = datetime.combine(d, time(0, 0), tzinfo=tz)
    windows = core.build_windows(routine, d, kind, now, strict=False)
    return core.schedule_snapshot(routine, kind, windows, day_iso, "")


def outcome_text(r: dict) -> str:
    """把一条执行结果说成人话。"""
    st = str(r.get("state") or "")
    reason = str(r.get("missedReason") or "")
    fires = int(r.get("fires") or 0)
    names = {
        "delivered": "已送达（一次性，无需确认）",
        "acked": "已确认",
        "cancelled": "已取消",
        "skipped": "已跳过",
        "missed": "漏响",
        "pending": "没响成",
        "ringing": "响过但没确认",
    }
    base = names.get(st, st or "?")
    if reason:
        reasons = {
            "postponed": "推迟到别处",
            "cancelled": "被取消",
            "window_expired": "窗口过期",
            "day_rollover": "跨日未确认",
            "late_plan": "补建时已过",
        }
        base += f"（{reasons.get(reason, reason)}）"
    if fires:
        base += f"，响 {fires} 次"
    return base


def cmd_schedule(args) -> int:
    """查历史某天（或某月）的日程安排。

    读的是每日快照目录 state/schedule/，永久保留，不受 14 天截断影响。
    """
    if args.month:
        files = sorted(core.SCHEDULE_DIR.glob(f"{args.month}-*.json"))
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
            print(
                f"{s['date']}（周{dow}）{s['planKind']:<8} "
                f"{len(s['windows'])} 窗口 / {fires} 次  "
                f"播报 {len(s['rollcalls'])}  周专项 {len(s['weeklyChores'])}"
            )
        return 0

    if not args.date:
        print("用法：rtc.py schedule <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    path = core.SCHEDULE_DIR / f"{args.date}.json"
    if path.exists():
        s = json.loads(path.read_text(encoding="utf-8"))
        source = "快照"
    else:
        # 没有快照：该日期还没到（或那天没跑 plan）。按配置推算。
        try:
            s = preview_schedule(core.load_routine(), args.date)
        except core.Validation:
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
        results = s.get("results") or {}
        print("窗口：")
        for w in s["windows"]:
            t0 = w["start"][11:16]
            t1 = w["end"][11:16]
            tag = "  [一次性]" if w.get("oneOff") else ""
            span = ""
            if w.get("validFrom") or w.get("validUntil"):
                span = f"  有效期 {w.get('validFrom') or '不限'} ~ {w.get('validUntil') or '不限'}"
            line = (
                f"  {t0}–{t1}  {w['label']:<12} 每 {w['intervalMin']} 分钟 × {w['expectFires']} 次"
                f"  确认词：{w.get('ackHint')}{tag}{span}"
            )
            # 结果：那天实际怎么了
            r = results.get(w["id"])
            if r:
                line += "  → " + outcome_text(r)
            print(line)
        # 快照里可能有「结果」，但窗口已不在计划里（如当天被推迟走了）
        extra = [k for k in results if k not in {w["id"] for w in s["windows"]}]
        for k in extra:
            print(f"  {'—':<13} {'':<12} （计划里没有，但有结果）  → {outcome_text(results[k])}")
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


# ---------------------------------------------------------------- 执行结果查询


def cmd_history(args) -> int:
    """查某天（或某月）每个提醒的实际结果。

    快照的 `results` 层——推过、取消、没做成、响了几次、确认没确认。
    这是「那天到底发生了什么」的答案，与 `schedule`（那天计划了什么）互补。
    """
    if args.month:
        files = sorted(core.SCHEDULE_DIR.glob(f"{args.month}-*.json"))
        if not files:
            print(f"history: 没有 {args.month} 的快照", file=sys.stderr)
            return 4
        if args.json:
            out = []
            for f in files:
                s = json.loads(f.read_text(encoding="utf-8"))
                res = s.get("results") or {}
                if res:
                    out.append({"date": s["date"], "results": res})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        any_row = False
        for f in files:
            s = json.loads(f.read_text(encoding="utf-8"))
            res = s.get("results") or {}
            if not res:
                continue
            any_row = True
            d = date.fromisoformat(s["date"])
            dow = "一二三四五六日"[d.weekday()]
            done = sum(1 for r in res.values() if r.get("state") in {"acked", "delivered"})
            print(f"{s['date']}（周{dow}）{len(res)} 项，完成 {done}，未成 {len(res) - done}")
            for wid, r in res.items():
                print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
        if not any_row:
            print(f"history: {args.month} 没有已归档的执行结果")
        return 0

    if not args.date:
        print("用法：rtc.py history <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    path = core.SCHEDULE_DIR / f"{args.date}.json"
    if not path.exists():
        print(f"history: 没有 {args.date} 的快照", file=sys.stderr)
        return 4
    s = json.loads(path.read_text(encoding="utf-8"))
    res = s.get("results") or {}
    if args.json:
        print(json.dumps({"date": s["date"], "results": res}, ensure_ascii=False, indent=2))
        return 0
    if not res:
        print(f"history: {args.date} 还没有归档的执行结果（快照只有计划——结果在次日归档时回填）")
        return 0
    print(f"{args.date} 的执行结果（{len(res)} 项）：")
    for wid, r in res.items():
        print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
    return 0


# ---------------------------------------------------------------- 状态与指标


def cmd_status(args) -> int:
    routine = core.load_routine()
    state = core.load_state()
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
        print(
            json.dumps(
                {"planDate": state["planDate"], "rev": state["rev"], "windows": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"{state['planDate']}  {state['planKind']}  rev={state['rev']}")
    hb = core.read_heartbeat()
    if hb is None:
        print("  调度器心跳：无记录")
    else:
        gap = (core.now_tz(routine["tzOffset"]) - hb).total_seconds() / 60
        print(f"  调度器心跳：{gap:.0f} 分钟前")
    for r in rows:
        print(f"  {r['label']:<8} {r['window']}  {r['state']:<8} {r['fireCount']}/{r['expectFires']}")
    return 0


# ---------------------------------------------------------------- 入口


# ---------------------------------------------------------------- 行程简报
#
# 模型与 RTC 之间原本有一道墙：模型只在用户开口时被调用，它不知道今天排了什么，
# 于是用户每次都得重新讲一遍「我现在要干嘛」。墙不是靠「让模型多跑一条命令」
# 拆掉的——弱模型不保证每次都愿意跑。真正的解法是把行程推到模型每次会话都会
# 读到的位置（AGENTS.md），并给出一条零解析成本的纯文本。

_BRIEF_BEGIN = "<!-- RTC:BRIEF:BEGIN 自动生成，勿手改本区块；改时刻表请用 rtc.py -->"

_BRIEF_END = "<!-- RTC:BRIEF:END -->"

_STATE_WORD = {
    "acked": "已确认",
    "delivered": "已送达",
    "skipped": "跳过",
    "missed": "过期未确认",
    "cancelled": "已取消",
    "pending": "待响",
    "ringing": "正在响",
}


def _hhmm(iso: str | None) -> str:
    if not iso:
        return "--:--"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "--:--"


def _span(minutes: int) -> str:
    if minutes <= 0:
        return "已到点"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} 小时 {m} 分钟"
    if h:
        return f"{h} 小时"
    return f"{m} 分钟"


def render_brief(routine: dict, state: dict | None, now: datetime) -> list[str]:
    """一页行程：现在在响什么、接下来是什么、今天已经过了什么。"""
    weekday = "一二三四五六日"[now.weekday()]
    head = f"今日行程（{now.strftime('%m-%d')} 周{weekday}）"
    if not state:
        return [head, "今天还没生成计划（cron 每天 06:00 生成，也可以手动 plan --force）"]
    windows = state.get("windows") or []
    if not windows:
        return [head, "今天没有安排。"]

    def key(w: dict) -> str:
        return w.get("start") or ""

    ringing = [w for w in windows if w.get("state") == "ringing"]
    pending = [w for w in windows if w.get("state") == "pending" and key(w) > now.isoformat()]
    done = [w for w in windows if w.get("state") in ("acked", "delivered", "missed", "skipped", "cancelled")]

    lines = [head]
    if ringing:
        w = ringing[0]
        lines.append(
            f"现在：{w['label']} 正在响（第 {w.get('fireCount', 1)} 次，"
            f"{_hhmm(w.get('start'))}-{_hhmm(w.get('end'))}）"
        )
    else:
        lines.append("现在：没有在响的提醒。")

    if pending:
        rest = sorted(pending, key=key)
        nxt = rest[0]
        try:
            gap = int((datetime.fromisoformat(nxt["start"]) - now).total_seconds() // 60)
        except ValueError:
            gap = 0
        lines.append(f"接下来：{_hhmm(nxt['start'])} {nxt['label']}（还有 {_span(gap)}）")
        if len(rest) > 1:
            lines.append("再往后：" + "、".join(f"{_hhmm(w['start'])} {w['label']}" for w in rest[1:4]))
    else:
        lines.append("接下来：今天没有更晚的安排了。")

    if done:
        lines.append(
            "今天已过："
            + "、".join(
                f"{_hhmm(w['start'])} {w['label']} {_STATE_WORD.get(w.get('state'), w.get('state'))}"
                for w in sorted(done, key=key)
            )
        )
    return lines


def brief_text(routine: dict, state: dict | None, now: datetime) -> str:
    return "\n".join(render_brief(routine, state, now))


def sync_brief_block(routine: dict, state: dict | None, now: datetime) -> str:
    """把简报写进 AGENTS.md 的标记区块，返回结果供调用方打印。

    只替换两个标记之间的内容，标记外的用户原文一个字都不动；没有标记就
    什么都不做（返回 `no-block`），绝不整文件覆盖。
    """
    path = core.WORKSPACE / "AGENTS.md"
    if not path.exists():
        return "no-file"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"read-failed: {e}"
    if _BRIEF_BEGIN not in text or _BRIEF_END not in text:
        return "no-block"
    start = text.index(_BRIEF_BEGIN) + len(_BRIEF_BEGIN)
    end = text.index(_BRIEF_END)
    if start > end:
        return "no-block"
    body = brief_text(routine, state, now)
    new = text[:start] + "\n" + body + "\n" + text[end:]
    if new == text:
        return "unchanged"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return f"write-failed: {e}"
    return "updated"


def cmd_brief(args) -> int:
    routine = core.load_routine()
    tz = core.tz_of(routine["tzOffset"])
    now = core.parse_iso(args.now, tz) if args.now else core.now_tz(routine["tzOffset"])
    state = core.load_state()
    if args.sync:
        result = sync_brief_block(routine, state, now)
        print(f"brief: AGENTS.md {result}", file=sys.stderr)
    print(brief_text(routine, state, now))
    return 0
