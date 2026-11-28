#!/usr/bin/env python3
"""rtc_occurrence.py — L2 P3：影子事件账本里 recurring 事件的当天实例化（适配层）。

定位：P1 影子副本 ``state/events.json`` 是只读派生；本模块是 plan 热路径消费
事件账本的唯一适配层。``rtc_core`` 只调用本模块，不直接 import ``rtc_events`` /
``rtc_rrule``（热路径守卫口径见 P1 测试 ``TestHotPathImportGuard``）。

边界：

- 只把 ``kind=recurring`` 的事件按 RRULE 展开成**当天半开区间**内的 occurrence
  窗口；``once`` 事件仍走旧 ``routines.json`` 链路，本模块不投影。
- 去重键 = ``(uid, occurrence start)``；同一事件同一天多条实例按起点区分。
- ``COUNT`` / ``UNTIL`` 是引擎全局预算（从 DTSTART 起数）；本模块只截当天窗口，
  不重新计数。
- ``events.json`` 缺失 / 损坏 / 空信封 → 返回 ``[]``，旧链路输出逐字节不变。
- 事件级时区沿用事件 ``start`` 自带偏移；朴素时刻回退工作区时区。所有产出时刻
  一律带偏移，不写裸 naive。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

import rtc_events as events
import rtc_rrule as rrule

__all__ = ["occurrence_windows"]

_ACTIVE_STATUS = {"scheduled", "ringing"}
_DEFAULT_ACK_HINT = "知道了"
_DEFAULT_SPACING_MIN = 15
_ID_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(uid: str, alias: str | None) -> str:
    """uid（``<uuid>@rtc-kit``）收敛成账本 ``id`` 允许的 ``[a-z0-9-]`` 片段。"""
    base = uid.split("@", 1)[0]
    base = _ID_SLUG_RE.sub("-", base.lower()).strip("-")
    return base or (alias or "event")


def _label(ev: dict[str, Any], slug: str) -> str:
    aliases = ev.get("aliases") or []
    return str(aliases[0]) if aliases else slug


def _parse_dt(value: Any, fallback: tzinfo, what: str) -> datetime:
    """解析事件时刻；朴素时刻挂回退时区；非法值抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} 缺失")
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback)
    return dt


def _duration_minutes(start: datetime, end: Any) -> int:
    """事件起止时长（分钟）；缺 end / 非法值退回默认间隔。"""
    if end is None:
        return _DEFAULT_SPACING_MIN
    try:
        parsed = datetime.fromisoformat(str(end))
    except ValueError:
        return _DEFAULT_SPACING_MIN
    minutes = (parsed.astimezone(start.tzinfo) - start).total_seconds() / 60
    return max(1, int(minutes)) if minutes > 0 else _DEFAULT_SPACING_MIN


def _window_for(
    ev: dict[str, Any],
    uid: str,
    occ: datetime,
    duration_min: int,
    day_iso: str,
    now: datetime,
) -> dict[str, Any]:
    """把一条 occurrence 包装成旧账本窗口形状，供 due/ack 既有机器判定。"""
    slug = _slug(uid, str((ev.get("aliases") or [None])[0]))
    end = occ + timedelta(minutes=duration_min)
    expired = now > end
    return {
        "id": slug,
        "ocid": f"{slug}@{day_iso}",
        "seq": 0,
        "label": _label(ev, slug),
        "type": "fixed",
        "review": False,
        "start": occ.isoformat(),
        "end": end.isoformat(),
        "intervalMin": 1,
        "expectFires": 1,
        "fireCount": 0,
        "state": "skipped" if expired else "pending",
        "lastFiredAt": None,
        "ackedAt": None,
        "ackText": None,
        "missedAt": None,
        "missedReason": "late_plan" if expired else None,
        "ackHint": _DEFAULT_ACK_HINT,
        "format": None,
        "oneOff": False,
        "dueDate": ev.get("deadline"),
        "disabled": False,
        "cancelledAt": None,
        "checklist": [],
        "checked": [],
    }


def occurrence_windows(
    workspace: Path,
    tz: tzinfo,
    day: date,
    now: datetime,
    *,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """展开影子账本里 recurring 事件在 ``day`` 当天的 occurrence 窗口。

    - ``errors`` 传入后：单条事件解析/展开失败只跳过它自己并记一条诊断，
      不拖垮整天计划；不传则异常向上冒泡。
    - 返回列表按起点升序；事件账本缺失或为空时返回 ``[]``。
    """
    envelope = events.load_events(workspace)
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for ev in events.all_events(envelope):
        uid = str(ev.get("uid") or "")
        try:
            if ev.get("kind") != "recurring":
                continue
            if ev.get("status") not in _ACTIVE_STATUS:
                continue
            text = ev.get("rrule")
            if not text:
                continue

            dtstart = _parse_dt(ev.get("start"), tz, "事件 start")
            parsed = rrule.parse_rrule(str(text))
            occs = rrule.expand_occurrences(dtstart, parsed, day_start, day_end)
            duration = _duration_minutes(dtstart, ev.get("end"))

            for occ in occs:
                key = (uid, occ.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                out.append(_window_for(ev, uid, occ, duration, day.isoformat(), now))
        except (rrule.RruleError, ValueError, TypeError, KeyError) as e:
            if errors is None:
                raise
            errors.append(f"事件 {uid or '?'}：{e}")

    out.sort(key=lambda w: w["start"])
    return out
