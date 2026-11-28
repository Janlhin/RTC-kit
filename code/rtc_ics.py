#!/usr/bin/env python3
"""rtc_ics.py — iCalendar（RFC 5545）序列化 / 解析，L2 P2 的源真相文件层。

本模块分两段，互不影响：

1. **旧只读导出**（``export_calendar`` / ``main``）：把当天 ``alarms.json`` 渲染成
   标准日历流到 stdout / 文件。这是跨代可读的出口，行为由 ``test_ics.py`` 既有
   用例守住，P2 不改它。
2. **P2 双向层**（文件层 + 事件映射）：``.ics`` 是 L2 行程本的**源真相**，
   SQLite（``rtc_calindex``）只是可删可重建的只读索引。一年一个文件：
   ``<workspace>/calendar/<YYYY>.ics``，按事件 ``DTSTART`` 年份归文件；跨年事件
   （12/31 起、次日结束）在相邻年份文件里各留一段交叉引用。

时区口径（#91 / D-012）：每条事件都必须带时区，绝不输出浮动时间：

- 瞬时是 UTC（偏移 0）→ ``DTSTART:...Z``；
- 事件带可加载的 IANA 时区名 → ``DTSTART;TZID=<名>:<本地墙钟>``，文件内附
  对应 ``VTIMEZONE``；
- 其余（固定偏移、缺时区信息）→ 换算成 UTC 瞬时，用 ``Z`` 后缀输出。

纯标准库；不在 ``plan`` / ``due`` / ``ack`` 热路径上。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc_core as core  # noqa: E402


def ics_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """RFC 5545 折行：>75 字节折行，续行以空格开头。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 74:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def local_to_ics(iso: str) -> str:
    """2026-11-20T15:00:00+08:00 -> 20261120T150000（本地墙钟，仅供旧版 UID 拼串用）。"""
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%Y%m%dT%H%M%S")


def uid_for(w: dict[str, Any], start: str) -> str:
    """日历 UID：优先用窗口的 ocid，这样推迟/改时刻只是「同一条的新版本」。

    老账本没有 ocid 时退回旧算法（id + 开始时间）——它会在改期后换 UID，
    但那种账本本来就是老版本导出的，一致性只能这样保。
    """
    ocid = w.get("ocid")
    if ocid:
        return f"{ocid}@rtc-kit"
    return f"{w.get('id', 'win')}-{local_to_ics(start)}@rtc-kit"


def _parse_aware(iso: str) -> datetime:
    """解析 ISO 时间；裸本地时间（理论上不该出现）先按 UTC 兜住，不落成浮动时间。"""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _offset_hhmm(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    sign = "+" if off >= timedelta(0) else "-"
    off = abs(off)
    hh, mm = off.seconds // 3600, (off.seconds // 60) % 60
    return f"{sign}{hh:02d}{mm:02d}"


def _obs_block(kind: str, dt: datetime) -> list[str]:
    """一条 STANDARD/DAYLIGHT 观测记录（最小可归档形态：固定观测，不带 RRULE）。"""
    off = _offset_hhmm(dt)
    return [
        f"BEGIN:{kind}",
        f"DTSTART:{dt.strftime('%Y%m%dT%H%M%S')}",
        f"TZOFFSETFROM:{off}",
        f"TZOFFSETTO:{off}",
        f"TZNAME:{dt.tzname() or ''}",
        f"END:{kind}",
    ]


def _vtimezone_lines(tzname: str, year: int) -> list[str]:
    """为某个 IANA 时区生成最小 VTIMEZONE 块：年初/年中两个采样点覆盖标准/夏令偏移。"""
    zi = ZoneInfo(tzname)
    std = datetime(year, 1, 1, 12, tzinfo=zi)
    summ = datetime(year, 7, 1, 12, tzinfo=zi)
    blocks: list[list[str]] = []
    if std.utcoffset() == summ.utcoffset():
        blocks.append(_obs_block("STANDARD", std))
    else:
        blocks.append(_obs_block("DAYLIGHT", summ))
        blocks.append(_obs_block("STANDARD", std))
    return ["BEGIN:VTIMEZONE", f"TZID:{tzname}", *[x for b in blocks for x in b], "END:VTIMEZONE"]


def _render_dt(prefix: str, dt: datetime, zi: ZoneInfo | None, tzid: str | None) -> tuple[str, str | None]:
    """渲染 DTSTART/DTEND。返回 (属性行, 用到的 TZID；Z 形式为 None)。绝不输出浮动时间。"""
    if dt.utcoffset() == timedelta(0):
        return f"{prefix}:{dt.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}", None
    if zi is not None and tzid:
        local = dt.astimezone(zi)
        return f"{prefix};TZID={tzid}:{local.strftime('%Y%m%dT%H%M%S')}", tzid
    return f"{prefix}:{dt.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}", None


def export_calendar(workspace: Path) -> str:
    """读当天状态，产出标准 VCALENDAR。源真相仍在 routines.json + alarms.json。"""
    state = None
    sp = workspace / "state" / "alarms.json"
    if sp.exists():
        state = json_load(sp)

    label = str((state or {}).get("timezone") or "").strip()
    zi = core._load_zoneinfo(label) if label else None
    tzid = label if zi is not None else None

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RTC-kit//Archive Export//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:RTC 行程归档",
    ]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    lines.append(f"DTSTAMP:{stamp}")

    windows = (state or {}).get("windows") or []
    vtz: dict[str, list[str]] = {}
    events: list[list[str]] = []
    for w in windows:
        start, end = w.get("start"), w.get("end")
        if not start:
            continue
        start_dt = _parse_aware(str(start))
        start_line, used_tz = _render_dt("DTSTART", start_dt, zi, tzid)
        ve = [
            "BEGIN:VEVENT",
            f"UID:{uid_for(w, start)}",
            f"DTSTAMP:{stamp}",
            f"SEQUENCE:{int(w.get('seq') or 0)}",
            start_line,
        ]
        if end:
            end_dt = _parse_aware(str(end))
            end_line, end_tz = _render_dt("DTEND", end_dt, zi, tzid)
            ve.append(end_line)
            used_tz = used_tz or end_tz
        if used_tz and used_tz not in vtz:
            vtz[used_tz] = _vtimezone_lines(used_tz, start_dt.year)
        ve.append(f"SUMMARY:{ics_escape(w.get('label') or w.get('id', '提醒'))}")
        status = w.get("state", "")
        # VEVENT 的 STATUS 只有 TENTATIVE / CONFIRMED / CANCELLED 三个值。
        # COMPLETED 是 VTODO（待办）的语义，日历客户端会当成陌生值。
        # 所以「已响过」不能写进 STATUS，改用自定义字段保住原状态。
        if status == "cancelled":
            ve.append("STATUS:CANCELLED")
        else:
            ve.append("STATUS:CONFIRMED")
        ve.append(f"X-RTC-STATE:{status or 'unknown'}")
        note = f"RTC 状态: {status}; 响铃 {w.get('fireCount', 0)} 次"
        if w.get("missedReason"):
            note += f"; 漏响原因: {w['missedReason']}"
        ve.append(f"DESCRIPTION:{ics_escape(note)}")
        ve.append("END:VEVENT")
        events.append(ve)

    for block in vtz.values():
        lines.extend(fold(x) for x in block)
    for ve in events:
        lines.extend(fold(x) for x in ve)

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def json_load(path: Path) -> dict[str, Any]:
    # json.loads 的返回类型是 Any；这里只读自己写出的 alarms.json，形状由 schema 保证
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rtc_ics", description="导出标准 iCalendar")
    ap.add_argument("--workspace", default=str(core.WORKSPACE))
    ap.add_argument("-o", "--output", help="输出文件；不填则 stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    content = export_calendar(ws)
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(f"已导出 {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(content)
    return 0


# ===========================================================================
# L2 P2 双向层：.ics 源真相文件（序列化 / 解析 / 一年一文件落盘）
# ===========================================================================

CALENDAR_DIR = Path("calendar")
PRODID = "-//rtc-kit//CN"

# Event status 枚举 → VEVENT STATUS（RFC 5545 §3.8.1.11 只认 TENTATIVE /
# CONFIRMED / CANCELLED；COMPLETED 是 VTODO 语义，日历客户端对 VEVENT 多宽容，
# 这里仅 done 映射 COMPLETED，其余终态留在 X-RTC-STATUS 保住原枚举）。
_STATUS_TO_ICS: dict[str, str] = {
    "done": "COMPLETED",
    "cancelled": "CANCELLED",
}
_ICS_TO_STATUS: dict[str, str] = {
    "CONFIRMED": "scheduled",
    "CANCELLED": "cancelled",
    "COMPLETED": "done",
    "TENTATIVE": "scheduled",
}

# 本模块认识的 VEVENT 属性名；其余属性原样进 ``unknown`` 带回，不丢。
_KNOWN_PROPS = frozenset(
    {
        "UID",
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "SUMMARY",
        "RRULE",
        "STATUS",
        "CATEGORIES",
        "X-RTC-KIND",
        "X-RTC-OWNER",
        "X-RTC-DEADLINE",
        "X-RTC-STATUS",
        "X-RTC-REMINDERPOLICY",
        "X-RTC-ALIASES",
        "X-RTC-REV",
        "X-RTC-TZID",
        "X-RTC-CROSS-YEAR",
    }
)


def calendar_dir(workspace: Path) -> Path:
    """源真相日历目录：``<workspace>/calendar``。"""
    return workspace / CALENDAR_DIR


def ics_path(workspace: Path, year: int) -> Path:
    """某一年对应的日历文件：``<workspace>/calendar/<YYYY>.ics``。"""
    return calendar_dir(workspace) / f"{year:04d}.ics"


def escape_text(text: str) -> str:
    """RFC 5545 §3.3.11 文本转义：反斜杠、分号、逗号、换行。"""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def unescape_text(value: str) -> str:
    """``escape_text`` 的逆运算；逐字符扫转义序列，未知反斜杠序列原样保留反斜杠。"""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt in ("\\", ";", ","):
            out.append(nxt)
        else:
            out.append(ch)
            out.append(nxt)
        i += 2
    return "".join(out)


def unfold(text: str) -> list[str]:
    """折行还原：CRLF 归一为 LF；续行（行首空格 / tab）并入上一行。"""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in norm.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """拆一条属性行：``NAME[;PARAM=VALUE]*:VALUE``。返回 (大写属性名, 参数, 值)。"""
    colon = line.find(":")
    if colon < 0:
        return line.upper(), {}, ""
    head, value = line[:colon], line[colon + 1 :]
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for param in parts[1:]:
        if "=" in param:
            k, v = param.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def _parse_dt(value: str, params: dict[str, str]) -> datetime:
    """解析 ICS 时刻：``...Z`` 为 UTC；``TZID=...`` 用 IANA 时区还原；裸本地时间钉 UTC。"""
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    tzid = params.get("TZID")
    dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
    if tzid:
        zi = core._load_zoneinfo(tzid)
        if zi is not None:
            return dt.replace(tzinfo=zi)
    return dt.replace(tzinfo=UTC)


def _format_dt(dt: datetime, tzid: str | None) -> tuple[str, str | None]:
    """渲染 DTSTART/DTEND。返回 (属性值串, 用到的 TZID；Z 形式为 None)。绝不浮动时间。"""
    if dt.utcoffset() == timedelta(0):
        return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"), None
    if tzid:
        zi = core._load_zoneinfo(tzid)
        if zi is not None:
            local = dt.astimezone(zi)
            return local.strftime("%Y%m%dT%H%M%S"), tzid
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"), None


def _dt_prop(prefix: str, dt: datetime, tzid: str | None) -> tuple[str, str | None]:
    val, used = _format_dt(dt, tzid)
    if used:
        return f"{prefix};TZID={used}:{val}", used
    return f"{prefix}:{val}", None


def _prop(name: str, value: str, params: dict[str, str] | None = None) -> str:
    if params:
        param_str = ";".join(f"{k}={v}" for k, v in params.items())
        return f"{name};{param_str}:{value}"
    return f"{name}:{value}"


def event_to_vevent(event: dict[str, Any]) -> list[str]:
    """Event 字典 → VEVENT 属性行（未折行；落盘前统一过 ``fold``）。"""
    start = datetime.fromisoformat(str(event["start"]))
    end = datetime.fromisoformat(str(event["end"]))
    tzid = str(event.get("tzid") or "").strip() or None

    lines = ["BEGIN:VEVENT"]
    lines.append(_prop("UID", str(event["uid"])))
    dtstamp = str(event.get("dtstamp") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    lines.append(_prop("DTSTAMP", dtstamp))

    start_line, used_tz = _dt_prop("DTSTART", start, tzid)
    lines.append(start_line)
    end_line, end_tz = _dt_prop("DTEND", end, tzid)
    lines.append(end_line)
    used_tz = used_tz or end_tz

    summary = str(event.get("summary") or event.get("uid"))
    lines.append(_prop("SUMMARY", escape_text(summary)))

    rrule = event.get("rrule")
    if rrule:
        lines.append(_prop("RRULE", str(rrule)))

    status = str(event.get("status") or "scheduled")
    lines.append(_prop("STATUS", _STATUS_TO_ICS.get(status, "CONFIRMED")))
    lines.append(_prop("X-RTC-STATUS", escape_text(status)))

    categories = event.get("categories") or []
    if categories:
        lines.append(_prop("CATEGORIES", ",".join(escape_text(str(c)) for c in categories)))

    lines.append(_prop("X-RTC-KIND", str(event.get("kind") or "once")))
    lines.append(_prop("X-RTC-OWNER", str(event.get("owner") or "self")))

    deadline = event.get("deadline")
    if deadline:
        lines.append(_prop("X-RTC-DEADLINE", str(deadline)))

    policy = event.get("reminderPolicy")
    if policy is not None:
        lines.append(_prop("X-RTC-REMINDERPOLICY", escape_text(json.dumps(policy, ensure_ascii=False))))

    aliases = event.get("aliases") or []
    if aliases:
        lines.append(_prop("X-RTC-ALIASES", ",".join(escape_text(str(a)) for a in aliases)))

    rev = event.get("rev")
    if rev is not None:
        lines.append(_prop("X-RTC-REV", str(rev)))

    if used_tz:
        lines.append(_prop("X-RTC-TZID", used_tz))

    cross = event.get("crossYear")
    if cross:
        lines.append(_prop("X-RTC-CROSS-YEAR", str(cross)))

    for raw in event.get("unknown") or []:
        lines.append(str(raw))

    lines.append("END:VEVENT")
    return lines


def _collect_vtz(events: list[dict[str, Any]], year: int) -> dict[str, list[str]]:
    """收集事件用到的 IANA 时区，生成最小 VTIMEZONE 块（复用旧导出口径）。"""
    vtz: dict[str, list[str]] = {}
    for ev in events:
        tzid = str(ev.get("tzid") or "").strip()
        if tzid and tzid not in vtz and core._load_zoneinfo(tzid) is not None:
            vtz[tzid] = _vtimezone_lines(tzid, year)
    return vtz


def render_calendar(events: list[dict[str, Any]], *, year: int) -> str:
    """把一年的事件渲染成完整 VCALENDAR 文本（CRLF、75 octet 折行、UTF-8 无 BOM）。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    for block in _collect_vtz(events, year).values():
        lines.extend(block)
    for ev in events:
        lines.extend(event_to_vevent(ev))
    lines.append("END:VCALENDAR")
    folded = [fold(x) for x in lines]
    return "\r\n".join(folded) + "\r\n"


def parse_calendar(text: str) -> dict[str, Any]:
    """解析日历流。返回 ``{"headers": dict, "events": list[dict]}``。

    每个事件含 ``props``（结构化属性：属性名 → [(参数, 值)]）与 ``raw``
    （折行还原后的原始属性行）；未知属性不丢，原样留在 ``raw``。
    """
    headers: dict[str, list[str]] = {}
    events: list[dict[str, Any]] = []
    in_cal = False
    current: dict[str, list[tuple[dict[str, str], str]]] | None = None
    raw_current: list[str] = []

    for line in unfold(text):
        if not line:
            continue
        name, params, value = _split_property(line)
        if name == "BEGIN":
            if value == "VCALENDAR":
                in_cal = True
            elif value == "VEVENT":
                current = {}
                raw_current = []
            continue
        if name == "END":
            if value == "VEVENT" and current is not None:
                events.append({"props": current, "raw": raw_current})
                current = None
                raw_current = []
            elif value == "VCALENDAR":
                in_cal = False
            continue
        if not in_cal:
            continue
        if current is None:
            headers.setdefault(name, []).append(value)
        else:
            current.setdefault(name, []).append((params, value))
            raw_current.append(line)

    return {"headers": headers, "events": events}


def _first(props: dict[str, list[tuple[dict[str, str], str]]], name: str) -> str | None:
    hits = props.get(name)
    if not hits:
        return None
    return hits[0][1]


def event_from_vevent(vevent: dict[str, Any]) -> dict[str, Any]:
    """把一个 VEVENT 还原成 Event 字典；未识别属性进 ``unknown`` 原样带回。"""
    props: dict[str, list[tuple[dict[str, str], str]]] = vevent["props"]

    start_raw = _first(props, "DTSTART")
    end_raw = _first(props, "DTEND")
    start_params = props["DTSTART"][0][0] if "DTSTART" in props else {}
    end_params = props["DTEND"][0][0] if "DTEND" in props else {}
    start = _parse_dt(start_raw or "", start_params)
    end = _parse_dt(end_raw or start_raw or "", end_params)

    tzid = start_params.get("TZID") or _first(props, "X-RTC-TZID")
    status_raw = _first(props, "X-RTC-STATUS")
    if not status_raw:
        status_raw = _ICS_TO_STATUS.get(_first(props, "STATUS") or "", "scheduled")

    aliases_raw = _first(props, "X-RTC-ALIASES")
    aliases = [unescape_text(a) for a in aliases_raw.split(",")] if aliases_raw else []

    categories_raw = _first(props, "CATEGORIES")
    categories = [unescape_text(c) for c in categories_raw.split(",")] if categories_raw else []

    policy_raw = _first(props, "X-RTC-REMINDERPOLICY")
    policy: Any = None
    if policy_raw:
        try:
            policy = json.loads(unescape_text(policy_raw))
        except json.JSONDecodeError:
            policy = None

    rev_raw = _first(props, "X-RTC-REV")
    cross = _first(props, "X-RTC-CROSS-YEAR")

    unknown = [line for line in vevent["raw"] if _split_property(line)[0] not in _KNOWN_PROPS]

    event: dict[str, Any] = {
        "uid": _first(props, "UID") or "",
        "kind": _first(props, "X-RTC-KIND") or "once",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rrule": _first(props, "RRULE"),
        "owner": _first(props, "X-RTC-OWNER") or "self",
        "deadline": _first(props, "X-RTC-DEADLINE"),
        "status": status_raw or "scheduled",
        "reminderPolicy": policy,
        "aliases": aliases,
        "summary": unescape_text(_first(props, "SUMMARY") or ""),
        "categories": categories,
        "tzid": tzid,
        "dtstamp": _first(props, "DTSTAMP"),
        "unknown": unknown,
    }
    if rev_raw is not None:
        event["rev"] = int(rev_raw) if rev_raw.isdigit() else rev_raw
    if cross:
        event["crossYear"] = cross
    return event


def parse_events(text: str) -> list[dict[str, Any]]:
    """解析日历流，返回 Event 字典列表。"""
    return [event_from_vevent(v) for v in parse_calendar(text)["events"]]


def read_file(path: Path) -> list[dict[str, Any]]:
    """读一个年份 .ics 文件；文件缺失返回空列表。"""
    if not path.exists():
        return []
    return parse_events(path.read_text(encoding="utf-8"))


def _year_of(iso: str) -> int:
    return datetime.fromisoformat(iso).year


def write_events(workspace: Path, events: list[dict[str, Any]]) -> list[Path]:
    """按 DTSTART 年份分组落盘；跨年事件在结束年份文件里留一段交叉引用。

    旧文件不删：本函数只写 ``calendar/`` 目录下的年份文件；写入走临时文件 +
    ``os.replace`` 原子改名。返回本次写出的文件路径列表。
    """
    by_year: dict[int, list[dict[str, Any]]] = {}
    cross: dict[int, list[dict[str, Any]]] = {}
    for ev in events:
        start_year = _year_of(str(ev["start"]))
        by_year.setdefault(start_year, []).append(ev)
        end_year = _year_of(str(ev["end"]))
        if end_year > start_year:
            cross.setdefault(end_year, []).append({**ev, "crossYear": str(start_year)})

    written: list[Path] = []
    cal_dir = calendar_dir(workspace)
    cal_dir.mkdir(parents=True, exist_ok=True)

    years = set(by_year) | set(cross)
    for year in sorted(years):
        merged = list(by_year.get(year, []))
        merged.extend(cross.get(year, []))
        path = ics_path(workspace, year)
        payload = render_calendar(merged, year=year)
        fd, tmp = tempfile.mkstemp(dir=str(cal_dir), prefix=f".{year:04d}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        written.append(path)
    return written


def load_all_events(workspace: Path) -> list[dict[str, Any]]:
    """直扫 ``calendar/*.ics`` 全部事件；按 uid 合并去重，交叉段不重复计数。

    这是「直接扫 .ics」的基准读路径；索引查询必须与它逐条一致。
    """
    cal_dir = calendar_dir(workspace)
    merged: dict[str, dict[str, Any]] = {}
    if not cal_dir.exists():
        return []
    for path in sorted(cal_dir.glob("*.ics")):
        for ev in read_file(path):
            uid = str(ev.get("uid") or "")
            if not uid:
                continue
            if ev.get("crossYear"):
                # 交叉段只作次年文件可检索；主记录在 start 年份文件里，不覆盖它。
                merged.setdefault(uid, ev)
                continue
            merged[uid] = ev
    return list(merged.values())


if __name__ == "__main__":
    raise SystemExit(main())
