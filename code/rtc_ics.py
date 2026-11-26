#!/usr/bin/env python3
"""rtc_ics.py — 把 RTC 的窗口导出成标准 iCalendar（RFC 5545）。

为什么存在：RTC 的状态存在私有 JSON/db 里，30 年后软件停更就读不出来。
.ics 是 1998 年的开放标准，纯文本，任何日历软件都能打开 —— 这是跨代可读的出口。

定位：**只读导出，不是源真相**。源真相仍是 routines.json + alarms.json。
本模块不写任何状态文件，只是把它们渲染成标准日历流。

时区口径（#91）：每条事件都必须带时区，绝不输出浮动时间（无 TZID 的裸本地时间）：

- 瞬时是 UTC（偏移 0）→ ``DTSTART:...Z``；
- 状态里带可加载的 IANA 时区名 → ``DTSTART;TZID=<名>:<本地墙钟>``，并在日历流里
  附一份对应的 ``VTIMEZONE``；
- 其余（旧固定偏移账本、缺时区信息）→ 换算成 UTC 瞬时，用 ``Z`` 后缀输出。

用法：
    python rtc_ics.py --workspace <dir>            # 导出到 stdout
    python rtc_ics.py --workspace <dir> -o cal.ics # 写到文件
"""

from __future__ import annotations

import argparse
import json
import sys
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


if __name__ == "__main__":
    raise SystemExit(main())
