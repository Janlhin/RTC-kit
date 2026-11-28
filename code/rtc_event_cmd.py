#!/usr/bin/env python3
"""rtc_event_cmd.py — L2 P5 事件级 CRUD 命令层。

覆盖事件一等公民的写命令：``create`` / ``edit`` / ``complete`` / ``cancel``
（含 ``--undo`` 当天撤销）。存储读写复用 P1 ``rtc_events`` 公开 API，审计复用
``rtc_audit``，执行历史复用 ``rtc_history``；本模块**不进** ``plan`` / ``due`` /
``ack`` tick 热路径（热路径守卫见 ``test_events_p1.py``）。

边界（P5，不越界到其它 phase）：

- 不写 ``.ics``（P2 接管真相源）；不做 occurrence 展开（P3）；不接 NLU 短语
  （P4 解析器另 lane，本模块只收结构化参数）；不做 year/range 查询与导出（P6）。
- 旧 ``rules/routines.json`` / ``state/alarms.json`` 全程只读；事件命令只碰
  ``state/events.json`` 影子账本。
- 并发：读账本时记内容摘要，写盘前在排他锁内复核摘要，被别人改过就抛
  ``Conflict``（退出 2），绝不静默覆盖。
- 审计 / history 失败不阻断主流程，沿用 #110 口径：只打 stderr 一行。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from copy import deepcopy
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any

import rtc_audit as audit
import rtc_core as core
import rtc_events as events
import rtc_history as hist

REPO = Path(__file__).resolve().parents[1]
EVENT_SCHEMA = REPO / "contract" / "event.schema.json"

# 审计动作名（与 history events.kind 同名，便于对账）。
ACTION_CREATE = "create"
ACTION_EDIT = "edit"
ACTION_COMPLETE = "complete"
ACTION_CANCEL = "cancel"
ACTION_CANCEL_UNDO = "cancel-undo"

# 撤销恢复时保持现状的身份字段：uid 改期 / 取消 / 撤销都不换。
_UNDO_KEEP_KEYS = frozenset({"uid", "aliases", "createdAt"})


# ---------------------------------------------------------------- 基础工具


def _load_routine_raw() -> dict[str, Any]:
    """只读时刻表取时区；坏文件 / 缺失都回退空 dict，绝不借自愈改写旧真相源。"""
    p = core.ROUTINE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _resolve_now(args: argparse.Namespace, tz: tzinfo) -> datetime:
    return core.parse_iso(args.now, tz) if getattr(args, "now", None) else core.now_for(_load_routine_raw())


def _parse_owner(raw: str | None) -> str:
    owner = (raw or events.DEFAULT_OWNER).strip()
    if owner == events.DEFAULT_OWNER or owner.startswith("other:") and len(owner) > len("other:"):
        return owner
    raise core.Validation(f"owner 应为 self 或 other:<name>，收到 {raw!r}")


def _parse_iso_arg(raw: str, what: str, tz: tzinfo) -> datetime:
    try:
        return core.parse_iso(raw, tz)
    except ValueError as e:
        raise core.Validation(f"--{what} 应为带偏移的 ISO 8601 时刻，收到 {raw!r}") from e


def _parse_date_arg(raw: str, what: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as e:
        raise core.Validation(f"--{what} 应为 YYYY-MM-DD，收到 {raw!r}") from e
    return raw


def _positive_minutes(value: int | None, what: str) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise core.Validation(f"--{what} 必须是正整数分钟，收到 {value}")
    return value


def _nonnegative_ladder(values: list[int] | None) -> list[int] | None:
    if not values:
        return None
    for n in values:
        if n < 0:
            raise core.Validation(f"--ddl-ladder 必须 ≥ 0 分钟，收到 {n}")
    return sorted(set(values))


def _policy_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """把 CLI 提醒参数落成 reminderPolicy 形状（与 event.schema.json 对齐）。"""
    pre = _positive_minutes(getattr(args, "pre_remind", None), "pre-remind")
    ladder = _nonnegative_ladder(getattr(args, "ddl_ladder", None))
    snooze = _positive_minutes(getattr(args, "snooze", None), "snooze")
    return {
        "preRemind": {"minutesBefore": pre} if pre else None,
        "ddlLadder": ladder,
        "snooze": {"minutes": snooze} if snooze else None,
    }


def _validate_envelope(envelope: dict[str, Any]) -> None:
    """整包过 event schema；解释器没装 jsonschema / 宿主没带契约文件时跳过。"""
    try:
        import jsonschema
    except ImportError:
        return
    try:
        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
    except OSError:
        return
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    errs = list(validator.iter_errors(envelope))
    if errs:
        raise core.Validation(f"事件不符合契约：{errs[0].message}")


def _resolve_event(envelope: dict[str, Any], uid: str | None, alias: str | None) -> dict[str, Any] | None:
    """按 uid 命中；否则按旧 id / 别名查反向索引。"""
    if uid:
        return events.get_event(envelope, uid)
    if alias:
        hit = events.find_uid(envelope, alias)
        return events.get_event(envelope, hit) if hit else None
    return None


def _save_envelope(ws: Path, envelope: dict[str, Any], digest: str | None) -> None:
    """排他锁内复核摘要后原子落盘；摘要对不上抛 Conflict，不覆盖别人写入。"""
    path = events.events_path(ws)
    with core._exclusive_file_lock(path):
        if digest is not None and path.exists() and core.file_digest(path) != digest:
            raise core.Conflict(f"{path.name} 在读取之后被改动过")
        events.save_events(ws, envelope)


def _audit_record(
    action: str, target: str, old: Any, new: Any, now: datetime, reason: str | None = None
) -> None:
    """审计留痕；失败不阻断主流程（#110 口径）。"""
    audit.record(
        action,
        target,
        actor=audit.ACTOR_USER,
        source=f"event:{action}",
        old=old,
        new=new,
        reason=reason,
        ts=now,
    )


def _history(kind: str, target: str, now: datetime, note: str | None = None) -> None:
    try:
        hist.log_event(kind, target, actor="user", source="event-cli", note=note, ts=now)
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        print(f"history: 写库失败（{type(e).__name__}: {e}），本轮历史可能少一条", file=sys.stderr)


# ---------------------------------------------------------------- create


def cmd_event_create(args: argparse.Namespace) -> int:
    """结构化参数新建一条事件；写 events.json，``.ics`` 接管留 P2。"""
    routine = _load_routine_raw()
    tz = core.resolve_tz(routine)
    now = _resolve_now(args, tz)

    title = (args.title or "").strip()
    if not title:
        raise core.Validation("create: --title 不能为空")
    # P4 解析器输出 dtstart（tz-aware datetime）→ 落账映射：
    #   event.start = parsed.dtstart.isoformat()
    #   event.end   = 按窗口宽度 / 入参派生
    # 这是命名差异不是语义冲突：契约字段保持 `start`，解析器侧不改名（见 D-018）。
    start = _parse_iso_arg(args.start, "start", tz)
    end = _parse_iso_arg(args.end, "end", tz)
    if end <= start:
        raise core.Validation("create: --end 必须晚于 --start")
    owner = _parse_owner(args.owner)
    deadline = _parse_date_arg(args.deadline, "deadline") if args.deadline else None
    rrule = args.rrule or None
    if rrule is not None and not rrule.strip():
        rrule = None
    kind = "recurring" if rrule else "once"
    _positive_minutes(getattr(args, "pre_remind", None), "pre-remind")
    _positive_minutes(getattr(args, "snooze", None), "snooze")
    _nonnegative_ladder(getattr(args, "ddl_ladder", None))

    ws = core.WORKSPACE
    envelope = events.load_events(ws)
    digest = core.file_digest(events.events_path(ws))

    uid = args.uid or events.new_uid()
    if events.get_event(envelope, uid) is not None:
        # 幂等：同 uid 重复 create 不产生第二条，rev / 审计 / history 都不涨。
        print(f"create: {uid} 已存在，未重复创建", file=sys.stderr)
        return 0

    stamp = now.isoformat()
    event: dict[str, Any] = {
        "uid": uid,
        "title": title,
        "kind": kind,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rrule": rrule,
        "owner": owner,
        "deadline": deadline,
        "status": "scheduled",
        "reminderPolicy": _policy_from_args(args),
        "aliases": [],
        "createdAt": stamp,
        "updatedAt": stamp,
    }

    candidate = deepcopy(envelope)
    events.upsert_event(candidate, event)
    _validate_envelope(candidate)

    events.upsert_event(envelope, event)
    _save_envelope(ws, envelope, digest)

    _history(ACTION_CREATE, uid, now, note=title)
    _audit_record(ACTION_CREATE, uid, None, {"event": event}, now, reason=title)
    print(f"create: 已记录「{title}」（{uid}）", file=sys.stderr)
    return 0


# ----------------------------------------------------------------- edit


def _apply_policy_edit(policy: dict[str, Any], args: argparse.Namespace) -> None:
    """局部合并提醒策略：给了的参数才改；显式传空串 / 空列表表示清除。"""
    if getattr(args, "pre_remind", None) is not None:
        minutes = _positive_minutes(args.pre_remind, "pre-remind")
        policy["preRemind"] = {"minutesBefore": minutes} if minutes else None
    if getattr(args, "ddl_ladder", None) is not None:
        policy["ddlLadder"] = _nonnegative_ladder(args.ddl_ladder)
    if getattr(args, "snooze", None) is not None:
        minutes = _positive_minutes(args.snooze, "snooze")
        policy["snooze"] = {"minutes": minutes} if minutes else None


def cmd_event_edit(args: argparse.Namespace) -> int:
    """按 uid 或旧 id 局部更新；只改给了的参数，rev / 摘要冲突退出 2。"""
    routine = _load_routine_raw()
    tz = core.resolve_tz(routine)
    now = _resolve_now(args, tz)

    ws = core.WORKSPACE
    envelope = events.load_events(ws)
    digest = core.file_digest(events.events_path(ws))

    ev = _resolve_event(envelope, args.uid, args.alias)
    if ev is None:
        print("edit: 找不到对应事件（--uid 或旧 id）", file=sys.stderr)
        return 4
    prior = deepcopy(ev)

    if args.title is not None:
        title = args.title.strip()
        if not title:
            raise core.Validation("edit: --title 不能为空")
        ev["title"] = title
    if args.owner is not None:
        ev["owner"] = _parse_owner(args.owner)
    if args.deadline is not None:
        ev["deadline"] = _parse_date_arg(args.deadline, "deadline") if args.deadline else None
    if args.rrule is not None:
        rrule = args.rrule.strip() or None
        ev["rrule"] = rrule
        ev["kind"] = "recurring" if rrule else "once"

    start_dt = _parse_iso_arg(args.start, "start", tz) if args.start else None
    end_dt = _parse_iso_arg(args.end, "end", tz) if args.end else None
    if start_dt is not None:
        ev["start"] = start_dt.isoformat()
    if end_dt is not None:
        ev["end"] = end_dt.isoformat()
    if start_dt is not None or end_dt is not None:
        cur_start = datetime.fromisoformat(ev["start"])
        cur_end = datetime.fromisoformat(ev["end"])
        if cur_end <= cur_start:
            raise core.Validation("edit: --end 必须晚于 --start")

    _apply_policy_edit(
        ev.setdefault("reminderPolicy", {"preRemind": None, "ddlLadder": None, "snooze": None}), args
    )
    ev["updatedAt"] = now.isoformat()

    _validate_envelope(envelope)
    _save_envelope(ws, envelope, digest)

    _history(ACTION_EDIT, ev["uid"], now, note=ev.get("title"))
    _audit_record(ACTION_EDIT, ev["uid"], {"event": prior}, {"event": ev}, now)
    print(f"edit: 已更新事件 {ev['uid']}", file=sys.stderr)
    return 0


# --------------------------------------------------------------- complete


def cmd_event_complete(args: argparse.Namespace) -> int:
    """把事件标 ``done``；occurrence 粒度的完成留 P3 接线。"""
    routine = _load_routine_raw()
    tz = core.resolve_tz(routine)
    now = _resolve_now(args, tz)

    ws = core.WORKSPACE
    envelope = events.load_events(ws)
    digest = core.file_digest(events.events_path(ws))

    ev = _resolve_event(envelope, args.uid, args.alias)
    if ev is None:
        print("complete: 找不到对应事件（--uid 或旧 id）", file=sys.stderr)
        return 4
    if ev["status"] == "done":
        print(f"complete: {ev['uid']} 已经是完成状态", file=sys.stderr)
        return 0

    prior = deepcopy(ev)
    ev["status"] = "done"
    ev["updatedAt"] = now.isoformat()

    _validate_envelope(envelope)
    _save_envelope(ws, envelope, digest)

    _history(ACTION_COMPLETE, ev["uid"], now, note=ev.get("title"))
    _audit_record(ACTION_COMPLETE, ev["uid"], {"event": prior}, {"event": ev}, now)
    print(f"complete: 已完成事件 {ev['uid']}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- cancel


def cmd_event_cancel(args: argparse.Namespace) -> int:
    """取消事件（``--uid`` / ``--alias``）；``--undo`` 走当天撤销恢复。"""
    if getattr(args, "undo", False):
        return _cmd_event_cancel_undo(args)

    routine = _load_routine_raw()
    tz = core.resolve_tz(routine)
    now = _resolve_now(args, tz)

    ws = core.WORKSPACE
    envelope = events.load_events(ws)
    digest = core.file_digest(events.events_path(ws))

    ev = _resolve_event(envelope, args.uid, args.alias)
    if ev is None:
        print("cancel: 找不到对应事件（--uid 或旧 id）", file=sys.stderr)
        return 4
    if ev["status"] == "cancelled":
        print(f"cancel: {ev['uid']} 已经是取消状态", file=sys.stderr)
        return 4
    if ev["status"] in {"done", "missed"}:
        print(f"cancel: {ev['uid']} 已是终态 {ev['status']}，不能再取消", file=sys.stderr)
        return 4

    prior = deepcopy(ev)
    ev["status"] = "cancelled"
    ev["updatedAt"] = now.isoformat()

    _validate_envelope(envelope)
    _save_envelope(ws, envelope, digest)

    _history(ACTION_CANCEL, ev["uid"], now, note=ev.get("title"))
    _audit_record(ACTION_CANCEL, ev["uid"], {"event": prior}, {"event": ev}, now)
    print(f"cancel: 已取消事件 {ev['uid']}", file=sys.stderr)
    return 0


def _trace_day(event: dict[str, Any], tz: tzinfo) -> str | None:
    """审计时间戳换算成运行时区日历日；坏时间戳返回 None。"""
    stamp = event.get("ts")
    if not isinstance(stamp, str):
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date().isoformat()


def _find_cancel_trace(
    events_log: list[dict[str, Any]], uid: str, today_iso: str, tz: tzinfo
) -> tuple[str, dict[str, Any] | None]:
    """找该事件最近一次事件级 cancel 痕迹，取取消前快照。

    只认 ``source="event:cancel"`` 的事件级记录；返回 ``found`` / ``expired`` /
    ``absent``。跨日痕迹不拿来复活（口径同窗口级 #113）。
    """
    expired: dict[str, Any] | None = None
    for record in reversed(events_log):
        if record.get("action") != ACTION_CANCEL or record.get("target") != uid:
            continue
        if record.get("source") != f"event:{ACTION_CANCEL}":
            continue
        old = record.get("old")
        if not isinstance(old, dict) or not isinstance(old.get("event"), dict):
            continue
        snap = old["event"]
        day = _trace_day(record, tz)
        if day == today_iso:
            return "found", snap
        if expired is None:
            expired = snap
    return ("expired", expired) if expired is not None else ("absent", None)


def _cmd_event_cancel_undo(args: argparse.Namespace) -> int:
    """撤销今天那次事件取消：按审计快照逐字段还原；跨日拒绝、无痕迹失败。"""
    routine = _load_routine_raw()
    tz = core.resolve_tz(routine)
    now = _resolve_now(args, tz)
    today = now.date().isoformat()

    ws = core.WORKSPACE
    envelope = events.load_events(ws)
    digest = core.file_digest(events.events_path(ws))

    ev = _resolve_event(envelope, args.uid, args.alias)
    if ev is None:
        print("cancel: 找不到对应事件（--uid 或旧 id），不能撤销", file=sys.stderr)
        return 4

    try:
        records = audit.read_events()
    except (OSError, ValueError, TypeError) as e:
        print(f"cancel: 审计痕迹读不出来（{type(e).__name__}: {e}），按无痕迹处理", file=sys.stderr)
        return 4

    status, snapshot = _find_cancel_trace(records, ev["uid"], today, tz)
    if status == "absent":
        print(f"cancel: {ev['uid']} 今天没有可撤销的取消记录", file=sys.stderr)
        return 4
    if status == "expired":
        print(f"cancel: {ev['uid']} 的取消发生在昨天及更早，撤销痕迹只在当天有效", file=sys.stderr)
        return 4
    assert snapshot is not None

    if ev["status"] != "cancelled":
        print(f"cancel: {ev['uid']} 当前状态 {ev['status']}，不是取消状态，没有可撤销的取消", file=sys.stderr)
        return 4

    prior = deepcopy(ev)
    for key, value in snapshot.items():
        if key in _UNDO_KEEP_KEYS:
            continue
        ev[key] = deepcopy(value)
    ev["updatedAt"] = now.isoformat()

    _validate_envelope(envelope)
    _save_envelope(ws, envelope, digest)

    _history(ACTION_CANCEL_UNDO, ev["uid"], now, note="undo 撤销今天的取消")
    _audit_record(
        ACTION_CANCEL_UNDO,
        ev["uid"],
        {"event": prior},
        {"event": ev},
        now,
        reason="undo 撤销今天的取消",
    )
    print(f"cancel: {ev['uid']} 已撤销今天的取消，恢复到取消前状态", file=sys.stderr)
    return 0
