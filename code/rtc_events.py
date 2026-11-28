#!/usr/bin/env python3
"""rtc_events.py — L2 行程本事件模型：存储、旧窗口投影、双向别名表（P1）。

定位（P1）：**派生影子副本**。旧真相源仍是 ``rules/routines.json`` +
``state/alarms.json``；本模块只把旧窗口投影成一等公民 Event 写进
``state/events.json``，读路径仍走旧链路。**P2 起 ``.ics`` 为源真相，
本文件对应的 JSON 转为只读缓存**（见 ``contract/event.schema.json`` 头注）。

边界：

- 纯标准库；**不被** ``plan`` / ``due`` / ``ack`` 热路径 import（有静态断言测试守住）。
- 不修改旧 ``routines.json`` / ``alarms.json``；派生永不反写真相
  （``docs/architecture.md`` §5 规则 1）。
- RRULE 只生成标准字符串，不做实例展开（展开属 P3）。
- 事件级时区、提醒策略、投递渠道沿用既有各自字段，本模块不重复建模。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

UID_DOMAIN = "rtc-kit"
DEFAULT_OWNER = "self"
DEFAULT_OFFSET = "+08:00"
EVENTS_RELATIVE = Path("state") / "events.json"

__all__ = [
    "UID_DOMAIN",
    "DEFAULT_OWNER",
    "DEFAULT_OFFSET",
    "EVENTS_RELATIVE",
    "events_path",
    "new_uid",
    "compose_local_iso",
    "rrule_for_window",
    "map_window",
    "empty_envelope",
    "load_events",
    "save_events",
    "all_events",
    "find_uid",
    "get_event",
    "upsert_event",
]

# 旧账本 state 枚举 → 事件 status 枚举（口径见 ADR D-013 五维映射表）。
# skipped 是生成期派生状态，不进目标枚举，回退 scheduled。
_LEDGER_STATUS: dict[str, str] = {
    "pending": "scheduled",
    "ringing": "ringing",
    "acked": "done",
    "delivered": "done",
    "cancelled": "cancelled",
    "missed": "missed",
    "skipped": "scheduled",
}

# 窗口 dateTypes（字符串枚举）→ RRULE BYDAY。holiday 走外挂日期类型表，不进 RRULE。
_DATE_TYPE_DAYS: dict[str, list[str]] = {
    "workday": ["MO", "TU", "WE", "TH", "FR"],
    "weekend": ["SA", "SU"],
}

_ALL_WEEK = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


def events_path(workspace: Path) -> Path:
    """影子账本路径：``<workspace>/state/events.json``。"""
    return workspace / EVENTS_RELATIVE


def new_uid() -> str:
    """生成 RFC 5545 §3.8.4.7 风格稳定 UID：UUIDv4 + ``@rtc-kit`` 后缀。"""
    return f"{uuid.uuid4()}@{UID_DOMAIN}"


def _parse_offset(offset: str) -> timezone:
    """把 ``+HH:MM`` 固定偏移解析成 timezone；非法值回退 ``+08:00``。"""
    text = (offset or DEFAULT_OFFSET).strip() or DEFAULT_OFFSET
    try:
        sign = 1 if text[0] == "+" else -1
        hh, mm = int(text[1:3]), int(text[4:6])
        return timezone(sign * timedelta(hours=hh, minutes=mm))
    except (ValueError, IndexError):
        return timezone(timedelta(hours=8))


def compose_local_iso(day: str, hhmm: str, offset: str = DEFAULT_OFFSET) -> str:
    """把 ``YYYY-MM-DD`` + ``HH:MM`` 拼成带固定偏移的 ISO 8601 时刻。

    模板级投影拿不到账本实例时刻时用；账本实例自带的 ISO 时刻原样保留，不走这里。
    """
    d = date.fromisoformat(day)
    t = time.fromisoformat(hhmm)
    return datetime.combine(d, t, tzinfo=_parse_offset(offset)).isoformat()


def _until_date_time(until: str, offset: str) -> str:
    """``validUntil``（``YYYY-MM-DD``）→ 当日 23:59:59 带偏移的 RRULE UNTIL 时刻。

    P3 展开器只认 date-time；date-only ``UNTIL=YYYYMMDD`` 会被兜成当天 00:00:00，
    截止日当天的晚间实例会被误判过期。取当日收尾时刻，保证截止日当天实例仍落在
    窗口内、次日实例起不再排。日期解析失败时退回旧的 date-only 形态，不抛栈。
    """
    try:
        d = date.fromisoformat(until)
    except ValueError:
        return until.replace("-", "")
    dt = datetime.combine(d, time(23, 59, 59), tzinfo=_parse_offset(offset))
    return dt.strftime("%Y%m%dT%H%M%S%z")


def rrule_for_window(w: dict[str, Any], *, tz_offset: str = DEFAULT_OFFSET) -> str | None:
    """旧窗口 → RFC 5545 RRULE 字符串；一次性窗口返回 ``None``。

    只覆盖旧模型能表达的 DAILY / WEEKLY + BYDAY；YEARLY / MONTHLY / INTERVAL /
    BYSETPOS 等由后续 phase 接入。``validUntil`` 收敛为 ``UNTIL``，且按事件时区
    产出 date-time（当日 23:59:59 + 偏移），不用 date-only。
    """
    if w.get("oneOff"):
        return None

    byday: list[str] = []
    for dt in w.get("dateTypes") or []:
        byday.extend(_DATE_TYPE_DAYS.get(str(dt), []))

    parts: list[str]
    if not byday or set(byday) >= _ALL_WEEK:
        parts = ["FREQ=DAILY"]
    else:
        seen: list[str] = []
        for day_code in byday:
            if day_code not in seen:
                seen.append(day_code)
        parts = ["FREQ=WEEKLY", f"BYDAY={','.join(seen)}"]

    until = w.get("validUntil")
    if until:
        parts.append(f"UNTIL={_until_date_time(str(until), tz_offset)}")
    return ";".join(parts)


def _kind(w: dict[str, Any], ledger: dict[str, Any] | None) -> str:
    if w.get("oneOff"):
        return "once"
    ledger_type = str((ledger or {}).get("type") or "")
    if ledger_type in ("oneoff", "appointment"):
        return "once"
    return "recurring"


def _status(w: dict[str, Any], ledger: dict[str, Any] | None) -> str:
    if ledger and ledger.get("state"):
        return _LEDGER_STATUS.get(str(ledger["state"]), "scheduled")
    if w.get("canceled") or w.get("disabled"):
        return "cancelled"
    return "scheduled"


def _aliases(w: dict[str, Any], ledger: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for cand in (w.get("id"), (ledger or {}).get("ocid"), (ledger or {}).get("id")):
        if cand and str(cand) not in out:
            out.append(str(cand))
    return out


def map_window(
    w: dict[str, Any],
    ledger: dict[str, Any] | None = None,
    *,
    as_of: date,
    tz_offset: str = DEFAULT_OFFSET,
    ledger_date: str | None = None,
    existing_uid: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把一条旧 routine 窗口（可叠加当天账本实例）投影成 Event 字典。

    - ``as_of``：模板级投影缺 ``validFrom`` 时取的参考日期（幂等由调用方保证：
      已存在的别名命中旧 uid，不重新生成）。
    - ``ledger``：当天账本里同 id 的实例；缺省时按模板投影。
    - ``existing_uid``：别名表命中的旧 uid；不传则新生成一个。
    """
    stamp = (now or datetime.now().astimezone()).isoformat()
    kind = _kind(w, ledger)

    if ledger and ledger.get("start"):
        start = str(ledger["start"])
        end = str(ledger.get("end") or ledger["start"])
    else:
        day = str(w.get("validFrom") or as_of.isoformat())
        end_day = str(w.get("validUntil") or day)
        start = compose_local_iso(day, str(w.get("start") or "09:00"), tz_offset)
        end = compose_local_iso(end_day, str(w.get("end") or w.get("start") or "09:00"), tz_offset)

    deadline = w.get("dueDate") or (ledger or {}).get("dueDate") or None

    event: dict[str, Any] = {
        "uid": existing_uid or new_uid(),
        "kind": kind,
        "start": start,
        "end": end,
        "rrule": None if kind == "once" else rrule_for_window(w, tz_offset=tz_offset),
        "owner": DEFAULT_OWNER,
        "deadline": str(deadline) if deadline else None,
        "status": _status(w, ledger),
        "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
        "aliases": _aliases(w, ledger),
        "summary": str(w.get("label") or w.get("id") or "") or None,
        "categories": None,
        "tzid": None,
        "dtstamp": stamp,
        "unknown": None,
        "rev": 0,
        "crossYear": None,
        "createdAt": stamp,
        "updatedAt": stamp,
        "source": {"routineId": str(w.get("id") or ""), "ledgerDate": ledger_date},
    }
    return event


def empty_envelope(now: datetime | None = None) -> dict[str, Any]:
    """空影子账本信封；``events`` 与 ``aliases`` 双向索引就位。"""
    return {
        "version": 1,
        "generatedAt": (now or datetime.now().astimezone()).isoformat(),
        "events": [],
        "aliases": {},
    }


def load_events(workspace: Path) -> dict[str, Any]:
    """读影子账本；文件缺失 / 损坏 / 形状不对时返回空信封，绝不抛栈给调用方。"""
    path = events_path(workspace)
    if not path.exists():
        return empty_envelope()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return empty_envelope()
    if not isinstance(raw, dict):
        return empty_envelope()
    raw.setdefault("version", 1)
    raw.setdefault("events", [])
    raw.setdefault("aliases", {})
    return raw


def all_events(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    return list(envelope.get("events") or [])


def find_uid(envelope: dict[str, Any], alias: str) -> str | None:
    hit = (envelope.get("aliases") or {}).get(alias)
    return str(hit) if hit else None


def get_event(envelope: dict[str, Any], uid: str) -> dict[str, Any] | None:
    for ev in all_events(envelope):
        if ev.get("uid") == uid:
            return ev
    return None


def upsert_event(envelope: dict[str, Any], event: dict[str, Any]) -> None:
    """按 uid 落一条事件，并刷新旧 id → uid 正向别名索引。

    同一 uid 再次写入视为一次修订：``rev`` 在旧值上 +1；新事件 ``rev`` 缺省 0。
    """
    events = envelope.setdefault("events", [])
    uid = str(event["uid"])
    for i, existing in enumerate(events):
        if existing.get("uid") == uid:
            event["rev"] = int(existing.get("rev") or 0) + 1
            events[i] = event
            break
    else:
        event.setdefault("rev", 0)
        events.append(event)
    aliases = envelope.setdefault("aliases", {})
    for alias in event.get("aliases") or []:
        aliases[str(alias)] = uid


def save_events(workspace: Path, envelope: dict[str, Any]) -> None:
    """原子落盘影子账本：临时文件 + fsync + ``os.replace``。只碰自己的影子文件。"""
    envelope["generatedAt"] = datetime.now().astimezone().isoformat()
    path = events_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".events.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
