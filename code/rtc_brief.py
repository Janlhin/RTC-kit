"""行程简报与查询展示：brief 文案写回 AGENTS.md，schedule / history / status 的输出。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import rtc_core as core
import rtc_history as history

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

    先读 `state/history.db`（唯一真相源）；库里没有这一天时才退回旧快照——
    删掉整个 `state/schedule/` 目录，只要库在，历史照样查得到。
    """
    if getattr(args, "range", None):
        try:
            start, _, end = args.range.partition("..")
            if not end:
                start, end = start, start
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            print("history: --range 用法 <起>..<止>（YYYY-MM-DD）", file=sys.stderr)
            return 4
        rows = history.rows_for_range(start, end)
        if not rows:
            print(f"history: {start}..{end} 没有执行结果", file=sys.stderr)
            return 4
        if args.json:
            out = [
                {"date": d, "results": history.results_for_day(d)} for d in sorted({r["date"] for r in rows})
            ]
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        for d in sorted({r["date"] for r in rows}):
            res = history.results_for_day(d)
            done = sum(1 for r in res.values() if r.get("state") in {"acked", "delivered"})
            print(f"{d} {len(res)} 项，完成 {done}，未成 {len(res) - done}")
        return 0

    if args.month:
        files = sorted(core.SCHEDULE_DIR.glob(f"{args.month}-*.json"))
        db_days = history.days_in_month(args.month)
        if not files and not db_days:
            print(f"history: 没有 {args.month} 的快照", file=sys.stderr)
            return 4
        if args.json:
            out = []
            for day_iso in db_days:
                res = history.results_for_day(day_iso)
                if res:
                    out.append({"date": day_iso, "results": res})
            if not out:
                for f in files:
                    s = json.loads(f.read_text(encoding="utf-8"))
                    res = s.get("results") or {}
                    if res:
                        out.append({"date": s["date"], "results": res})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        any_row = False
        for day_iso in db_days or [json.loads(f.read_text(encoding="utf-8"))["date"] for f in files]:
            res = history.results_for_day(day_iso)
            if not res:
                path = core.SCHEDULE_DIR / f"{day_iso}.json"
                if not path.exists():
                    continue
                res = json.loads(path.read_text(encoding="utf-8")).get("results") or {}
            if not res:
                continue
            any_row = True
            d = date.fromisoformat(day_iso)
            dow = "一二三四五六日"[d.weekday()]
            done = sum(1 for r in res.values() if r.get("state") in {"acked", "delivered"})
            print(f"{day_iso}（周{dow}）{len(res)} 项，完成 {done}，未成 {len(res) - done}")
            for wid, r in res.items():
                print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
        if not any_row:
            print(f"history: {args.month} 没有已归档的执行结果")
        return 0

    if not args.date:
        print("用法：rtc.py history <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    res = history.results_for_day(args.date)
    path = core.SCHEDULE_DIR / f"{args.date}.json"
    if not res and path.exists():
        res = json.loads(path.read_text(encoding="utf-8")).get("results") or {}
    if not res:
        if history.has_day(args.date):
            print(f"history: {args.date} 还没有归档的执行结果")
            return 0
        if not path.exists():
            print(f"history: 没有 {args.date} 的快照", file=sys.stderr)
            return 4
        print(f"history: {args.date} 还没有归档的执行结果（快照只有计划——结果在次日归档时回填）")
        return 0

    if args.json:
        print(json.dumps({"date": args.date, "results": res}, ensure_ascii=False, indent=2))
        return 0
    print(f"{args.date} 的执行结果（{len(res)} 项）：")
    for wid, r in res.items():
        print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
    return 0


def cmd_events(args) -> int:
    """看某个窗口（或全部）的状态迁移轨迹。"""
    rows = history.events(args.window, args.since, args.until)
    if not rows:
        print("events: 没有事件", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        extra = " ".join(
            x for x in (r.get("item_id"), r.get("from_date"), r.get("to_date"), r.get("note")) if x
        )
        print(f"{r['ts']}  {r['kind']:<9}{r['window_id']:<22}{extra}".rstrip())
    return 0


def cmd_stats(args) -> int:
    """按窗口 / 日期 / 月份聚合执行结果。"""
    try:
        rows = history.stats(args.group_by, args.missed_only)
    except ValueError as e:
        print(f"stats: {e}", file=sys.stderr)
        return 4
    if not rows:
        print("stats: 库里还没有执行结果", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        print(
            f"{r['grp']:<24}{r['days']:>4} 天  计划 {r['expect']:>3}  响 {r['fired']:>3}"
            f"  确认 {r['acked']:>3}  漏 {r['missed']:>3}"
        )
    return 0


# ---------------------------------------------------------------- 状态与指标


def _backup_status() -> dict | None:
    """备份是附属品：读不到/读坏了都返回 None，绝不抛，也绝不影响退出码。"""
    mark = core.WORKSPACE / "state" / ".backup-last.json"
    if not mark.is_file():
        return None
    try:
        data = json.loads(mark.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _backup_line(backup: dict | None) -> str:
    """把「到底备了没」压成一行，用户不用去翻文件。"""
    if not backup:
        return "  备份  尚未备份（python rtc.py archive backup 可手动跑一次）"
    at = str(backup.get("at") or "")[5:16].replace("T", " ")
    written = backup.get("written", 0)
    g = backup.get("git")
    if g is True:
        git = "git 已推送"
    elif g is False:
        git = "git 未推送"
    else:
        git = "git 未跑（这次只做本地）"
    errors = backup.get("errors") or []
    if errors:
        return f"  备份  {at}  本地 {written} 处 · {git} · 错误 {len(errors)}：{str(errors[0])[:60]}"
    return f"  备份  {at}  本地 {written} 处 · {git} · 最近无错误"


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
    timetable = _timetable_summary(routine)
    backup = _backup_status()
    paused = core.load_pause()
    delivery = core.delivery_summary(state["planDate"])
    if args.json:
        print(
            json.dumps(
                {
                    "planDate": state["planDate"],
                    "rev": state["rev"],
                    "timetable": timetable,
                    "backup": backup,
                    "paused": paused,
                    "delivery": delivery,
                    "windows": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"{state['planDate']}  {state['planKind']}  rev={state['rev']}")
    if paused:
        print(f"  ⏸ 已暂停至 {paused['until']}（期间不会响；恢复用 resume）")
    if timetable["recurring"] == 0:
        print(
            f"  ⚠ 时刻表里没有日常窗口（一次性 {timetable['oneOff']} 条）——"
            "今天能响，明天不会自动排；改时刻表见 rules/routines.md"
        )
    hb = core.read_heartbeat()
    hb_attempt = core.read_heartbeat_attempt()
    now = core.now_tz(routine["tzOffset"])
    if hb is None:
        extra = (
            f"（{int((now - hb_attempt).total_seconds() // 60)} 分钟前调过，但没判完）"
            if hb_attempt is not None
            else ""
        )
        print(f"  调度器心跳：无成功判定{extra}")
    else:
        gap = (now - hb).total_seconds() / 60
        line = f"  调度器心跳：上次成功判定 {gap:.0f} 分钟前"
        if hb_attempt is not None and hb_attempt > hb:
            line += f"（其后又调过 {int((now - hb_attempt).total_seconds() // 60)} 分钟前）"
        print(line)
    print(_backup_line(backup))
    if delivery["total"]:
        line = f"  投递  {delivery['total']} 条 · 成功 {delivery['ok']} · 失败 {delivery['failed']}"
        if delivery["failed"]:
            first = delivery["failures"][0]
            line += f"（{str(first['at'])[11:16]} {first['window']}：{first['error'] or '没说原因'}）"
        print(line)
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


def _recurring_count(routine: dict) -> int:
    """时刻表里「每天会响」的窗口数：一次性（`oneOff` / `once-*`）不算。"""
    n = 0
    for w in routine.get("alarmWindows") or []:
        if w.get("oneOff") or str(w.get("id") or "").startswith("once-"):
            continue
        n += 1
    return n


def _timetable_summary(routine: dict) -> dict:
    """时刻表体检：日常窗口几个、一次性几个。日常 0 个 = 明天不会自动排。"""
    one_off = 0
    total = 0
    for w in routine.get("alarmWindows") or []:
        total += 1
        if w.get("oneOff") or str(w.get("id") or "").startswith("once-"):
            one_off += 1
    return {"total": total, "recurring": total - one_off, "oneOff": one_off}


def render_brief(routine: dict, state: dict | None, now: datetime) -> list[str]:
    """一页行程：现在在响什么、有没有欠着没结的、接下来是什么、今天的账。"""
    weekday = "一二三四五六日"[now.weekday()]
    head = f"今日行程（{now.strftime('%m-%d')} 周{weekday}）"
    alert = (
        []
        if _recurring_count(routine)
        else ["注意：时刻表里只有一次性提醒，没有日常窗口；明天不会按日常计划响。"]
    )
    if not state:
        return [
            head,
            *alert,
            "今天还没生成计划（cron 每天 06:00 生成，也可以手动 plan --force）",
        ]
    windows = state.get("windows") or []
    if not windows:
        return [head, *alert, "今天没有安排。"]

    def key(w: dict) -> str:
        return w.get("start") or ""

    ringing = [w for w in windows if w.get("state") == "ringing"]
    pending = [w for w in windows if w.get("state") == "pending" and key(w) > now.isoformat()]
    # 欠着的：过期没确认的，以及已经到点但系统还没标 missed 的。
    unsettled = [
        w
        for w in windows
        if w.get("state") == "missed"
        or (w.get("state") == "pending" and (w.get("start") or "") <= now.isoformat())
    ]
    done = [w for w in windows if w.get("state") in ("acked", "delivered", "missed", "skipped", "cancelled")]

    lines = [head, *alert]
    if ringing:
        w = ringing[0]
        hints = "、".join(f"「{h}」" for h in (w.get("ackHints") or []))
        lines.append(
            f"现在：{w['label']} 正在响（第 {w.get('fireCount', 1)} 次，"
            f"{_hhmm(w.get('start'))}-{_hhmm(w.get('end'))}）" + (f"；回 {hints} 就停" if hints else "")
        )
    else:
        lines.append("现在：没有在响的提醒。")

    if unsettled:
        words = []
        for w in sorted(unsettled, key=key)[:3]:
            why = "已过期" if w.get("state") == "missed" else "到点后没结"
            words.append(f"{_hhmm(w.get('start'))} {w['label']}（响 {w.get('fireCount', 0)} 次，{why}）")
        more = "" if len(unsettled) <= 3 else f"，等 {len(unsettled)} 条"
        lines.append("欠着的：" + "、".join(words) + more)

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

    fires = sum(int(w.get("fireCount") or 0) for w in windows)
    settled = sum(1 for w in windows if w.get("state") in ("acked", "delivered"))
    missed = sum(1 for w in windows if w.get("state") == "missed")
    lines.append(f"今天的账：计划 {len(windows)} 条 · 响 {fires} 次 · 确认 {settled} 条 · 漏 {missed} 条")
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


# ---------------------------------------------------------------- 摘要窗口
#
# 「19:00 晚报」这类需求，快照里其实全都有：响过几次、哪条确认了、哪条漏了、
# tasks.md 里还欠什么。全是本地确定性数据，所以文案由代码在到点那一刻算，
# 不需要模型在线，也不会把实时性外包给用户。

_TASKS_SECTION = "## Today"


def _tasks_today() -> list[str]:
    """读 tasks.md 的 Today 段里还没打勾的条目。缺失 = 空列表，不算错误。"""
    path = core.WORKSPACE / "tasks.md"
    if not path.exists():
        return []
    items = []
    in_today = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_today = stripped == _TASKS_SECTION
            continue
        if in_today and stripped.startswith(("- [ ]", "* [ ]")):
            text = stripped[5:].strip()
            if text:
                items.append(text)
    return items


def _window_tally(state: dict) -> dict:
    wins = state.get("windows") or []
    tally: dict = {"windows": len(wins), "fires": 0, "acked": [], "missed": [], "pending": []}
    for w in wins:
        # 早报兜底窗口是技术性守卫（防模型没交付），不是给人看的提醒，不进统计
        if w.get("format") == "report-fallback":
            tally["windows"] -= 1
            continue
        tally["fires"] += int(w.get("fireCount") or 0)
        st = w.get("state")
        if st in ("acked", "delivered"):
            tally["acked"].append(w)
        elif st == "missed":
            tally["missed"].append(w)
        elif st != "skipped":
            tally["pending"].append(w)
    return tally


def _tomorrow_line(routine: dict, today: date) -> str:
    """明日预告：复用 simulate 的「给定一天算窗口」，没有窗口就说不响。"""
    data = core.simulate_day(routine, today + timedelta(days=1))
    if not data["windows"]:
        return "明天没有窗口（不响）"
    parts = [f"{_hhmm(w['start'])} {w['label']}" for w in data["windows"]]
    return "、".join(parts)


def _label_plain(w: dict, icon: str) -> str:
    """实机窗口的 label 常自带同样的表情（「📋晚总结」），别让它和版式头撞成两个。"""
    label = str(w.get("label") or w.get("id") or "")
    if label.startswith(icon):
        label = label[len(icon) :].lstrip()
    return label or str(w.get("id") or "")


_REASON_WORD = {"window_expired": "没确认", "late_plan": "生成得太晚", "paused": "休假静音"}


def _reason_text(w: dict) -> str:
    reason = w.get("missedReason")
    return _REASON_WORD.get(str(reason), str(reason or "没确认"))


def _report_path(day: date) -> Path:
    return core.WORKSPACE / "state" / "reports" / f"{day.isoformat()}.md"


def _read_report(day: date) -> list[str] | None:
    """读当天模型写的汇报正文。文件不存在 / 空 / 读不动 → None（交给静态兜底）。"""
    p = _report_path(day)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # 坏编码不是「文已写好」：把它当没写，静态兜底照常发。
        # 以前这里只 catch OSError，一个乱码文件会让 due 每次抛异常，
        # 连带当天的其它提醒全部不响（外部审计 2026-11 发现的阻塞面）。
        return None
    return text.splitlines() if text else None


REPORT_EVIDENCE_WINDOW = "report"


def report_delivered(day: date) -> bool:
    """「今天的汇报已经真的送到用户手里」的唯一证据：投递回执 result=ok。

    文件存在只证明模型写完了，不证明用户收到——写完了但投递失败时如果静默，
    用户就整天没有汇报。所以兜底窗口只认回执，不认文件。
    """
    day_iso = day.isoformat()
    try:
        rows = core.read_delivery(day_iso)
    except (OSError, UnicodeDecodeError) as e:
        # 回执日志读不动（坏编码 / 权限 / IO）时，不能把「没有证据」升级成
        # 「整个 due 抛异常」：那会同时压住兜底和当天其他提醒。按未送达处理，
        # 让兜底照常发；诊断留在 stderr。
        print(f"due: 投递回执读取失败（{e}），按未送达处理", file=sys.stderr)
        return False
    for row in rows:
        if str(row.get("result")) != "ok":
            continue
        if str(row.get("window") or "") in {REPORT_EVIDENCE_WINDOW, "rtc-report", "brief-morning"}:
            return True
    return False


def _static_report(routine: dict, state: dict, now: datetime) -> list[str]:
    """模型没写出汇报时的静态版本：只说账本里确凿的事，不推测、不编造。

    这是 issue #60 的「零回归」要求——模型失败 / 没写 / 写空，9 点照常有话说。
    """
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    entries = [h for h in (state.get("history") or []) if str(h.get("date")) == yesterday]
    done = [h for h in entries if str(h.get("state")) in ("acked", "delivered")]
    undone = [h for h in entries if str(h.get("state")) == "missed"]
    tasks = _tasks_today()

    lines = [f"📊 昨日汇报 — {yesterday}"]
    lines.append("✅ 已完成：")
    lines.extend(_bullets([str(h.get("label") or h.get("id") or "") for h in done], "（昨天没有确认的）"))
    lines.append("❌ 未完成：")
    lines.extend(
        _bullets(
            [f"{h.get('label') or h.get('id') or ''}（{_reason_text(h)}）" for h in undone],
            "（昨天没有漏的）",
        )
    )
    lines.append("📝 备注：")
    notes = _read_notes(now.date() - timedelta(days=1))
    if notes:
        lines.extend(f"- {x}" for x in notes[:5])
    elif tasks:
        lines.append(f"tasks.md 今天还剩 {len(tasks)} 条待办")
    else:
        lines.append("（无）")
    lines.append("💬 遗留建议：")
    lines.extend(_bullets(tasks, "（今天没有要顺延的）"))
    return lines


def _read_notes(day: date) -> list[str]:
    """读用户口述（state/notes/<日期>.md）。缺失 = 空列表，不算错误。"""
    p = core.WORKSPACE / "state" / "notes" / f"{day.isoformat()}.md"
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    return [ln.strip("- ").strip() for ln in text.splitlines() if ln.strip()]


def build_digest(routine: dict, state: dict, hit: dict, now: datetime) -> list[str]:
    """按 rules/formats.md 的「跟进」/「总结」/「汇报」版式现算文案。只读，不改任何文件。"""
    tally = _window_tally(state)
    stamp = _hhmm(hit.get("start")) or now.strftime("%H:%M")
    tasks = _tasks_today()

    if hit["format"] == "report":
        # 单窗口模式：due 直接出稿，模型文件优先，读不到用静态汇报兜底（绝不允许因此不响）。
        written = _read_report(now.date())
        if written is not None:
            return written
        return _static_report(routine, state, now)

    if hit["format"] == "report-fallback":
        # 双窗口模式里的最后一棒：只在「没有投递回执」时开口。
        # 模型写好了但投递失败 / 根本没写 → 有文件就补发文件内容，没文件就发静态版。
        # 两者都失败时，用户至少还能收到静态汇报，而不是一片安静。
        written = _read_report(now.date())
        if written is not None:
            return written
        return _static_report(routine, state, now)

    if hit["format"] == "follow":
        lines = [f"🔔 {_label_plain(hit, '🔔')} — {now.date().isoformat()} {stamp}"]
        lines.append("📋 今日待办：")
        lines.extend(_bullets(tasks, "（tasks.md 里今天没有待办）"))
        lines.append("💬 进展：")
        lines.extend(
            _bullets([f"{_span_text(w)} 已确认" for w in tally["acked"]], "（还没有确认过任何一条）")
        )
        lines.append("❓ 需要做什么：")
        todo = []
        for w in tally["pending"]:
            todo.append(f"{_hhmm(w['start'])} {w['label']} 还没到点")
        for w in tally["missed"]:
            todo.append(f"{_hhmm(w['start'])} {w['label']} 漏了（{_reason_text(w)}）")
        lines.extend(_bullets(todo, "今天的窗口都处理完了"))
    else:
        lines = [f"📋 {_label_plain(hit, '📋')} — {now.date().isoformat()}"]
        lines.append("✅ 已完成：")
        lines.extend(_bullets([f"{_span_text(w)} {w['label']}" for w in tally["acked"]], "（没有确认的）"))
        lines.append("❌ 未完成：")
        lines.extend(
            _bullets(
                [f"{_hhmm(w['start'])} {w['label']}（{_reason_text(w)}）" for w in tally["missed"]],
                "（没有漏的）",
            )
        )
        lines.append("📝 备注：")
        if tasks:
            lines.append(f"tasks.md 今天还剩 {len(tasks)} 条待办")
        else:
            lines.append("tasks.md 今天没有待办")
        lines.append(f"📅 明日预告：{_tomorrow_line(routine, now.date())}")
        lines.append(
            f"今日闹钟：{tally['windows']} 个窗口，响 {tally['fires']} 次，"
            f"确认 {len(tally['acked'])} / 漏 {len(tally['missed'])} / 未到 {len(tally['pending'])}"
        )
        if str(state.get("planDate")) != now.date().isoformat():
            lines.append(f"⚠️ 窗口表异常（planDate={state.get('planDate')}，不是今天）")
    return lines


def _span_text(w: dict) -> str:
    return _hhmm(w.get("start"))


def _bullets(items: list[str], empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    out = [f"- {x}" for x in items[:5]]
    if len(items) > 5:
        out.append(f"- …还有 {len(items) - 5} 条")
    return out
