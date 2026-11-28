#!/usr/bin/env python3
"""rtc_rrule.py — RFC 5545 RRULE 解析与 occurrence 展开引擎（纯确定性，零第三方依赖）。

本模块只做「规则 → 时刻列表」这一件事：不接存储、不接 NLU、不接 plan/due/ack。
事件怎么存、到点怎么投递，是后续 phase 的事。

公开 API：
    parse_rrule(text) -> Rrule
        解析并校验 RFC 5545 RRULE 字符串；非法输入抛 RruleError。

    expand_occurrences(dtstart, rrule, window_from, window_to) -> list[datetime]
        展开半开区间 ``[window_from, window_to)`` 内的 occurrence，按时间升序，
        保留 ``dtstart`` 的 tzinfo；COUNT / UNTIL 是全局预算，不按窗口重算。

支持的 RRULE 键：
    FREQ       DAILY / WEEKLY / MONTHLY / YEARLY（必填）
    INTERVAL   正整数，默认 1
    COUNT      正整数；与 UNTIL 互斥
    UNTIL      RFC 5545 时间戳，``YYYYMMDDTHHMMSSZ``（UTC）或 ``YYYYMMDDTHHMMSS``（本地浮动）
    BYDAY      MO/TU/WE/TH/FR/SA/SU，可带序号前缀 ``1MO`` / ``-1FR``（序号仅 MONTHLY/YEARLY 有效）
    BYMONTHDAY 1..31 或负数（-1 = 当月最后一天）
    BYMONTH    1..12
    WKST       MO/TU/WE/TH/FR/SA/SU，默认 MO

未支持（如需要另开提案）：BYSETPOS、BYHOUR/BYMINUTE/BYSECOND、BYSECOND、
BYWEEKNO、BYYEARDAY、EXRULE、RDATE、半日时长/时长派生。

防御：条数与最大扫描年数双硬上限，超限抛 ExpansionLimit；任何非法输入抛 RruleError，
不静默返回空列表。
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal, NoReturn

__all__ = ["RruleError", "ExpansionLimit", "ByDay", "Rrule", "parse_rrule", "expand_occurrences"]

FREQ_VALUES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# 硬上限：展开超过 MAX_OCCURRENCES 条、或扫描跨度超过 MAX_SCAN_YEARS 年仍未结束，
# 抛 ExpansionLimit。两者都是兜底，正常规则（COUNT/UNTIL/窄窗口）触不到。
MAX_OCCURRENCES = 100_000
MAX_SCAN_YEARS = 100

_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")


class RruleError(ValueError):
    """RRULE 字符串非法、键值不合法，或入参时区口径不一致。"""


class ExpansionLimit(RruleError):
    """展开触及硬上限（条数 / 扫描年数），拒绝继续扫描，避免死循环。"""


@dataclass(frozen=True)
class ByDay:
    """单个 BYDAY 条目。ordinal 为 None 表示裸周几；1 = 当月/当年第 n 个，-1 = 最后一个。"""

    ordinal: int | None
    weekday: int


@dataclass(frozen=True)
class Rrule:
    """解析后的周期规则。字段与 RFC 5545 同名；未出现的键取默认值。"""

    freq: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    byday: tuple[ByDay, ...] = ()
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    wkst: int = 0


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _fail(message: str) -> NoReturn:
    raise RruleError(message)


def _parse_int(raw: str, key: str) -> int:
    try:
        return int(raw)
    except ValueError:
        _fail(f"{key} 必须是整数，收到 {raw!r}")


def _parse_until(raw: str) -> datetime:
    """解析 UNTIL：支持紧凑 RFC 形态 ``YYYYMMDDTHHMMSS[Z]`` 与 ISO 形态。"""
    s = raw.strip()
    if not s:
        _fail("UNTIL 值为空")
    if s.endswith("Z"):
        body = s[:-1]
        try:
            dt = datetime.strptime(body, "%Y%m%dT%H%M%S")
        except ValueError:
            try:
                dt = datetime.fromisoformat(body)
            except ValueError:
                _fail(f"UNTIL 时间戳格式非法：{raw!r}（应为 YYYYMMDDTHHMMSSZ）")
        return dt.replace(tzinfo=UTC)
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S")
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            _fail(f"UNTIL 时间戳格式非法：{raw!r}（应为 YYYYMMDDTHHMMSS 或 ISO 格式）")


def _parse_byday(raw: str, freq: str) -> tuple[ByDay, ...]:
    out: list[ByDay] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            _fail("BYDAY 出现空项（多余的逗号）")
        match = _BYDAY_RE.match(token)
        if not match:
            _fail(f"BYDAY 项非法：{token!r}（周几须为 MO/TU/WE/TH/FR/SA/SU）")
        ordinal_raw, wd_name = match.group(1), match.group(2)
        weekday = _WEEKDAYS[wd_name]
        ordinal = int(ordinal_raw) if ordinal_raw else None
        if ordinal is not None:
            if ordinal == 0 or not (-5 <= ordinal <= 5):
                _fail(f"BYDAY 序号越界：{token!r}（允许 -5..5，0 非法）")
            if freq in ("DAILY", "WEEKLY"):
                _fail(f"BYDAY 序号前缀 {token!r} 只在 MONTHLY/YEARLY 下有效")
        if weekday in seen:
            _fail(f"BYDAY 重复出现 {wd_name}")
        seen.add(weekday)
        out.append(ByDay(ordinal=ordinal, weekday=weekday))
    return tuple(out)


def _parse_int_list(raw: str, key: str, lo: int, hi: int) -> tuple[int, ...]:
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            _fail(f"{key} 出现空项（多余的逗号）")
        value = _parse_int(token, key)
        if not (lo <= value <= hi):
            _fail(f"{key} 取值 {value} 越界（允许 {lo}..{hi}）")
        out.append(value)
    return tuple(out)


def parse_rrule(text: str) -> Rrule:
    """解析并校验 RFC 5545 RRULE 字符串。

    抛出：
        RruleError：缺 FREQ、未知键、重复键、数值越界、COUNT 与 UNTIL 同给等。
    """
    if not text or not text.strip():
        _fail("RRULE 为空：至少需要 FREQ=DAILY/WEEKLY/MONTHLY/YEARLY")

    pairs: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            _fail(f"RRULE 片段缺少 '='：{part!r}")
        key, value = part.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if key in pairs:
            _fail(f"RRULE 键重复：{key}")
        pairs[key] = value

    if "FREQ" not in pairs:
        _fail("RRULE 缺少必填键 FREQ")
    freq_raw = pairs.pop("FREQ").upper()
    if freq_raw not in FREQ_VALUES:
        _fail(f"未知 FREQ={freq_raw}（仅支持 {'/'.join(FREQ_VALUES)}）")
    freq: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] = freq_raw  # type: ignore[assignment]

    interval = 1
    if "INTERVAL" in pairs:
        interval = _parse_int(pairs.pop("INTERVAL"), "INTERVAL")
        if interval < 1:
            _fail(f"INTERVAL 必须 ≥ 1，收到 {interval}")

    count: int | None = None
    if "COUNT" in pairs:
        count = _parse_int(pairs.pop("COUNT"), "COUNT")
        if count < 1:
            _fail(f"COUNT 必须 ≥ 1，收到 {count}")

    until: datetime | None = None
    if "UNTIL" in pairs:
        until = _parse_until(pairs.pop("UNTIL"))
    if count is not None and until is not None:
        _fail("COUNT 与 UNTIL 不能同时给出（RFC 5545 §3.3.10 二选一）")

    byday = _parse_byday(pairs.pop("BYDAY", ""), freq) if "BYDAY" in pairs else ()
    bymonthday = (
        _parse_int_list(pairs.pop("BYMONTHDAY", ""), "BYMONTHDAY", -31, 31) if "BYMONTHDAY" in pairs else ()
    )
    bymonth = _parse_int_list(pairs.pop("BYMONTH", ""), "BYMONTH", 1, 12) if "BYMONTH" in pairs else ()

    wkst = 0
    if "WKST" in pairs:
        wkst_raw = pairs.pop("WKST").strip().upper()
        if wkst_raw not in _WEEKDAYS:
            _fail(f"WKST 取值非法：{wkst_raw!r}（须为 MO/TU/WE/TH/FR/SA/SU）")
        wkst = _WEEKDAYS[wkst_raw]

    unknown = sorted(pairs)
    if unknown:
        _fail(f"RRULE 含未支持的键：{', '.join(unknown)}")

    return Rrule(
        freq=freq,
        interval=interval,
        count=count,
        until=until,
        byday=byday,
        bymonthday=bymonthday,
        bymonth=bymonth,
        wkst=wkst,
    )


# --------------------------------------------------------------------------- #
# 日期候选计算
# --------------------------------------------------------------------------- #


def _month_last_day(year: int, month: int) -> int:
    return monthrange(year, month)[1]


def _resolve_monthday(year: int, month: int, mday: int) -> int | None:
    """把 BYMONTHDAY 条目解析成当月实际日号；不存在（如 2/30）返回 None。"""
    last = _month_last_day(year, month)
    if mday > 0:
        return mday if mday <= last else None
    day = last + 1 + mday  # -1 → last；-2 → last-1
    return day if 1 <= day <= last else None


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> int | None:
    """当月第 ordinal 个周几；ordinal 正 = 从头数，负 = 从尾数；不存在返回 None。"""
    last = _month_last_day(year, month)
    occurrences = [day for day in range(1, last + 1) if date(year, month, day).weekday() == weekday]
    if not occurrences:
        return None
    index = ordinal - 1 if ordinal > 0 else len(occurrences) + ordinal
    return occurrences[index] if 0 <= index < len(occurrences) else None


def _month_candidates(year: int, month: int, rrule: Rrule, dtstart: datetime) -> list[date]:
    """算出某一月内命中规则的日期（升序）；无效日（平年 2/29 等）直接跳过。"""
    byday_days: set[int] = set()
    for spec in rrule.byday:
        if spec.ordinal is None:
            last = _month_last_day(year, month)
            byday_days.update(
                day for day in range(1, last + 1) if date(year, month, day).weekday() == spec.weekday
            )
        else:
            day = _nth_weekday(year, month, spec.weekday, spec.ordinal)
            if day is not None:
                byday_days.add(day)

    bymonthday_days: set[int] = set()
    for mday in rrule.bymonthday:
        resolved = _resolve_monthday(year, month, mday)
        if resolved is not None:
            bymonthday_days.add(resolved)

    if rrule.byday and rrule.bymonthday:
        days = byday_days & bymonthday_days
    elif rrule.byday:
        days = byday_days
    elif rrule.bymonthday:
        days = bymonthday_days
    else:
        days = {dtstart.day} if dtstart.day <= _month_last_day(year, month) else set()

    return [date(year, month, day) for day in sorted(days)]


def _plain_weekday_match(d: date, rrule: Rrule) -> bool:
    """DAILY / WEEKLY 下的裸 BYDAY 过滤（序号前缀已在解析期拒绝）。"""
    if not rrule.byday:
        return True
    return any(spec.weekday == d.weekday() for spec in rrule.byday)


def _monthday_match(d: date, rrule: Rrule) -> bool:
    if not rrule.bymonthday:
        return True
    last = _month_last_day(d.year, d.month)
    for mday in rrule.bymonthday:
        resolved = last + 1 + mday if mday < 0 else mday
        if resolved == d.day:
            return True
    return False


def _month_match(d: date, rrule: Rrule) -> bool:
    if not rrule.bymonth:
        return True
    return d.month in rrule.bymonth


def _filters_match(d: date, rrule: Rrule) -> bool:
    return _month_match(d, rrule) and _monthday_match(d, rrule) and _plain_weekday_match(d, rrule)


# --------------------------------------------------------------------------- #
# 展开
# --------------------------------------------------------------------------- #


def _check_tz_consistency(
    dtstart: datetime, window_from: datetime | None, window_to: datetime | None
) -> None:
    aware = dtstart.tzinfo is not None
    for label, bound in (("window_from", window_from), ("window_to", window_to)):
        if bound is None:
            continue
        bound_aware = bound.tzinfo is not None
        if aware != bound_aware:
            _fail(f"dtstart 与 {label} 时区口径不一致：一个带时区、一个朴素时间")


def _normalize_until(until: datetime | None, dtstart: datetime) -> datetime | None:
    if until is None:
        return None
    if until.tzinfo is None and dtstart.tzinfo is not None:
        return until.replace(tzinfo=dtstart.tzinfo)
    if until.tzinfo is not None and dtstart.tzinfo is None:
        _fail("dtstart 为朴素时间时，UNTIL 不能带时区")
    if until.tzinfo is not None and dtstart.tzinfo is not None:
        return until.astimezone(dtstart.tzinfo)
    return until


def _iter_dates(dtstart: datetime, rrule: Rrule) -> Iterator[date]:
    """按 FREQ 产出候选日期（升序）；越界日期跳过，不挪到次月/次日。"""
    start_date = dtstart.date()
    first_year = start_date.year

    def guard(year: int) -> None:
        if year > first_year + MAX_SCAN_YEARS:
            raise ExpansionLimit(
                f"RRULE 扫描跨度超过 {MAX_SCAN_YEARS} 年仍未结束，已停止以防死循环；"
                "请补 COUNT / UNTIL 或缩小查询窗口。"
            )

    if rrule.freq == "DAILY":
        cursor = start_date
        while True:
            guard(cursor.year)
            if _filters_match(cursor, rrule):
                yield cursor
            cursor += timedelta(days=rrule.interval)

    elif rrule.freq == "WEEKLY":
        anchor0 = start_date - timedelta(days=(start_date.weekday() - rrule.wkst) % 7)
        week = 0
        weekdays = sorted({spec.weekday for spec in rrule.byday} or [start_date.weekday()])
        while True:
            anchor = anchor0 + timedelta(weeks=week * rrule.interval)
            guard(anchor.year)
            for wd in weekdays:
                candidate = anchor + timedelta(days=(wd - rrule.wkst) % 7)
                if candidate >= start_date and _filters_match(candidate, rrule):
                    yield candidate
            week += 1

    elif rrule.freq == "MONTHLY":
        year, month = start_date.year, start_date.month
        while True:
            guard(year)
            if _month_match(date(year, month, 1), rrule):
                for candidate in _month_candidates(year, month, rrule, dtstart):
                    if candidate >= start_date:
                        yield candidate
            month += rrule.interval
            year += (month - 1) // 12
            month = (month - 1) % 12 + 1

    else:  # YEARLY
        year = start_date.year
        months = rrule.bymonth or (dtstart.month,)
        while True:
            guard(year)
            for month in months:
                for candidate in _month_candidates(year, month, rrule, dtstart):
                    if candidate >= start_date:
                        yield candidate
            year += rrule.interval


def expand_occurrences(
    dtstart: datetime,
    rrule: Rrule,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> list[datetime]:
    """展开半开区间 ``[window_from, window_to)`` 内的 occurrence。

    - 返回列表按时间升序，每个 occurrence 沿用 ``dtstart`` 的 tzinfo 与时刻。
    - COUNT / UNTIL 是**全局预算**：从 dtstart 起数满 COUNT 条或越过 UNTIL 即停，
      不会因为窗口只截一段就重新计数。
    - 非法 dtstart / 窗口时区口径不一致 → RruleError；触及条数或年数上限 → ExpansionLimit。
    """
    if not isinstance(dtstart, datetime):
        _fail(f"dtstart 必须是 datetime，收到 {type(dtstart).__name__}")
    _check_tz_consistency(dtstart, window_from, window_to)

    if dtstart.tzinfo is not None:
        if window_from is not None and window_from.tzinfo is None:
            window_from = window_from.replace(tzinfo=dtstart.tzinfo)
        if window_to is not None and window_to.tzinfo is None:
            window_to = window_to.replace(tzinfo=dtstart.tzinfo)

    until = _normalize_until(rrule.until, dtstart)
    upper = window_to if window_to is not None else until

    def in_window(occ: datetime) -> bool:
        if window_from is not None and occ < window_from:
            return False
        return not (upper is not None and occ >= upper)

    tz: tzinfo | None = dtstart.tzinfo
    tod: time = dtstart.time()
    first_year = dtstart.year
    result: list[datetime] = []

    for produced, candidate in enumerate(_iter_dates(dtstart, rrule), start=1):
        if candidate.year > first_year + MAX_SCAN_YEARS:
            raise ExpansionLimit(
                f"RRULE 扫描跨度超过 {MAX_SCAN_YEARS} 年仍未结束，已停止以防死循环；"
                "请补 COUNT / UNTIL 或缩小查询窗口。"
            )
        occ = datetime.combine(candidate, tod, tzinfo=tz)

        if until is not None and occ > until:
            break
        if produced > MAX_OCCURRENCES:
            raise ExpansionLimit(f"展开条数超过 {MAX_OCCURRENCES} 条硬上限，已停止；请补 COUNT / UNTIL。")

        if in_window(occ):
            result.append(occ)
        if rrule.count is not None and produced >= rrule.count:
            break
        if upper is not None and occ >= upper:
            break

    return result
