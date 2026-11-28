#!/usr/bin/env python3
"""rtc_nlu.py — 确定性中文时间短语解析（NLU L0/L1），零模型、零第三方依赖。

本模块只做封闭词表内的中文时间解析，定位是交互入口的前端翻译层：

  - **确定性**：同一输入 + 同一注入的 ``now``，多次解析结果逐字节一致；模块内部
    不读系统时间（没有 ``datetime.now`` / ``date.today`` 兜底），现在时刻必须由
    调用方以 tz-aware datetime 注入。
  - **时区**：一律使用调用方给出的 IANA ``tzinfo``（``zoneinfo``）；日级运算按
    本地墙钟算，相对时长运算走 aware datetime，不写死固定偏移。
  - **一次性 / 周期**：默认（``recurring=False``）只解析一次性时间点，周期表达按
    既有契约拒绝；显式打开 ``recurring=True`` 后，封闭词表内的周期表达产出
    ``kind="recurring"`` + RFC 5545 ``rrule`` + 首个 occurrence 的 ``dtstart``。
    周期写路径尚未接入，入口命令仍按一次性契约拒绝周期表达。

依赖纪律：本模块只能被交互入口（``rtc_once``）import；``plan`` / ``due`` /
tick 判定链路与 ``rtc_core`` 不得依赖它（由测试做静态 + 运行时双重断言）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

# ----------------------------------------------------------------------------
# 词表与归一化
# ----------------------------------------------------------------------------

_WEEKDAY_CN: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

# 中文周几 → RFC 5545 BYDAY 缩写。
_WEEKDAY_RFC: dict[str, str] = {
    "一": "MO",
    "二": "TU",
    "三": "WE",
    "四": "TH",
    "五": "FR",
    "六": "SA",
    "日": "SU",
    "天": "SU",
}

# 时段词 → 是否把 12 小时制的小时数往下午拨 12。
_PM_PERIODS = ("下午", "晚上")
_AM_PERIODS = ("凌晨", "早上", "上午")

# 周期 / 集合类表达：未开周期开关时，命中即按既有契约拒绝。
_RECURRING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"每年|每月|每周|每天|每日|每逢|每个|每\s*\d"),
)

# 即使打开周期开关也不猜：需要工作日历 / BYSETPOS / 相对月末 / 第 N 周定位。
_RE_RECURRING_REJECTED = re.compile(r"工作日|节假日|最后一个|第\s*\d+\s*周|隔周|每两")

# 周期表达形状（封闭词表）。
_RE_WEEKLY = re.compile(r"每周(?P<wds>[一二三四五六日天]+)")
_RE_DAILY = re.compile(r"每天|每日")
_RE_MONTHLY = re.compile(r"每月(?P<day>\d{1,2})\s*(号|日)")
_RE_YEARLY = re.compile(r"每年(?P<mon>\d{1,2})\s*月(?P<day>\d{1,2})\s*(?:日|号)")
_RE_EVERY_N = re.compile(r"每(?P<n>\d+)\s*(?P<unit>个月|天|周|月)")

# 纯相对天数偏移词 → 天数；按词长从长到短排，避免「后天」抢先吃掉「大后天」。
_DAY_OFFSET_WORDS: list[tuple[str, int]] = [
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
    ("昨天", -1),
    ("昨日", -1),
    ("今天", 0),
    ("今日", 0),
]

_DURATION_RE = re.compile(r"(?P<half>一个半|半)小时|(?P<hours>\d+)\s*小时|(?P<mins>\d+)\s*分钟")
_DAY_DURATION_RE = re.compile(r"(?P<days>\d+)\s*天(?:后|以后)?")
_DAY_OFFSET_RE = re.compile("|".join(re.escape(w) for w, _ in _DAY_OFFSET_WORDS))
_WEEKDAY_RE = re.compile(r"(?P<prefix>下|本|这)?周(?P<wd>[一二三四五六日天])")
_MONTHDAY_RE = re.compile(r"(?P<next>下|下个)?(?:这个)?月(?P<day>\d{1,2})\s*号")
_CLOCK_RE = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
    r"(?P<hour>[0-9一两三四五六七八九十]+)\s*点(?:半|(?P<minute>[0-9一两三四五六七八九十]+)\s*分?)?"
)
_HHMM_RE = re.compile(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})")

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(token: str) -> int:
    """阿拉伯 / 中文数字串转整数；无法识别抛 ValueError。"""
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_DIGIT[left] if left else 1
        ones = _CN_DIGIT[right] if right else 0
        return tens * 10 + ones
    return _CN_DIGIT[token]


# 归一化：全角数字转半角、去空白、「星期 / 礼拜」统一成「周」。
_FULLWIDTH_DIGITS = {chr(0xFF10 + i): str(i) for i in range(10)}


class NluError(Exception):
    """解析失败基类：短语落在封闭词表之外，调用方按非零退出码上报。"""


class UnsupportedExpression(NluError):
    """现有契约表达不了的表达：``reason`` 是给用户看的明确原因。

    抛出时 ``reason`` 以「现有契约不支持：」开头，调用方原样透传到 stderr。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedTime:
    """一次解析的结构化结果。

    属性：
        moment: 目标绝对瞬时（tz-aware datetime）。一次性事件即目标时刻；
            周期事件即首个 occurrence 的 ``dtstart``。
        date: 目标本地日历日（所在时区墙钟）。
        clock: 目标本地时刻（不带时区）。
        crosses_midnight: 与注入的 ``now`` 是否不在同一本地日历日。
        raw: 归一化之前的原始短语。
        kind: ``once``（一次性）或 ``recurring``（周期）。
        rrule: 周期事件的 RFC 5545 RRULE 字符串；一次性为 ``None``。
        dtstart: 周期事件首个 occurrence 的 tz-aware datetime；一次性等于 ``moment``。
    """

    moment: datetime
    date: date
    clock: time
    crosses_midnight: bool
    raw: str
    kind: str = "once"
    rrule: str | None = None
    dtstart: datetime | None = None

    @property
    def hhmm(self) -> str:
        """``HH:MM`` 形式的本地时刻。"""
        return f"{self.clock.hour:02d}:{self.clock.minute:02d}"

    @property
    def date_iso(self) -> str:
        """``YYYY-MM-DD`` 形式的本地日期。"""
        return self.date.isoformat()


# ----------------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------------

_UNSUPPORTED_REASON_DISABLED = (
    "现有契约不支持：周期 / 重复事件（每周一三五、每天几点、每月 1 号等）的写路径"
    "待 L2 排程层接入；当前入口只登记一次性提醒。"
)
_UNSUPPORTED_REASON_SHAPE = (
    "现有契约不支持：需要工作日历、BYSETPOS、相对月末或「第 N 周的周几」等规则"
    "（如每月最后一个工作日、每年第 3 周的周三、工作日、节假日、隔周）；"
    "封闭词表内只支持天 / 周 / 月 / 年，以及每 N 天 / 周 / 月的确定性 RRULE。"
)


def _normalize(phrase: str) -> str:
    text = "".join(_FULLWIDTH_DIGITS.get(ch, ch) for ch in phrase)
    text = re.sub(r"\s+", "", text)
    text = text.replace("星期", "周").replace("礼拜", "周")
    return text


def _check_recurring_disabled(text: str) -> None:
    for pattern in _RECURRING_PATTERNS:
        if pattern.search(text):
            raise UnsupportedExpression(_UNSUPPORTED_REASON_DISABLED)


def _match_duration_minutes(text: str) -> int | None:
    """匹配「N 分钟 / N 小时 / N 天 / 半小时 / 一个半小时」这类相对时长。"""
    m = _DURATION_RE.search(text)
    if not m:
        return None
    if m.group("half") == "一个半":
        return 90
    if m.group("half") == "半":
        return 30
    if m.group("hours") is not None:
        return int(m.group("hours")) * 60
    if m.group("mins") is not None:
        return int(m.group("mins"))
    return None


def _match_day_offset(text: str) -> int | None:
    m = _DAY_OFFSET_RE.search(text)
    if not m:
        return None
    return dict(_DAY_OFFSET_WORDS)[m.group(0)]


def _match_day_duration(text: str) -> int | None:
    m = _DAY_DURATION_RE.search(text)
    return int(m.group("days")) if m else None


def _match_weekday(text: str, today: date) -> date | None:
    m = _WEEKDAY_RE.search(text)
    if not m:
        return None
    wd = _WEEKDAY_CN[m.group("wd")]
    monday = today - timedelta(days=today.weekday())
    prefix = m.group("prefix")
    if prefix == "下":
        # 「下周三」= 下一个自然周的周三（周一为一周起点），不是「7 天后碰到的周三」。
        return monday + timedelta(days=7 + wd)
    if prefix in ("本", "这"):
        return monday + timedelta(days=wd)
    # 裸「周三」：本周内还没到就取本周，已过则顺到下周；今天正好是周三就取今天。
    candidate = monday + timedelta(days=wd)
    if candidate < today:
        candidate += timedelta(days=7)
    return candidate


def _match_monthday(text: str, today: date) -> date | None:
    m = _MONTHDAY_RE.search(text)
    if not m:
        return None
    day = int(m.group("day"))
    year, month = today.year, today.month
    if m.group("next"):
        month += 1
        if month == 13:
            year, month = year + 1, 1
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise NluError(f"{year}-{month:02d}-{day:02d} 不是有效日期：{exc}") from exc


def _match_clock(text: str) -> time | None:
    clock_match = _CLOCK_RE.search(text)
    hhmm_match = None if clock_match else _HHMM_RE.search(text)
    m = clock_match or hhmm_match
    if not m:
        return None
    try:
        hour = _cn_to_int(m.group("hour"))
        minute_group = m.groupdict().get("minute")
        minute = (
            _cn_to_int(minute_group)
            if minute_group is not None
            else 30
            if clock_match and "半" in text
            else 0
        )
    except (KeyError, ValueError) as exc:
        raise NluError(f"时刻数字无法识别：{m.group(0)!r}") from exc
    period = m.groupdict().get("period") or ""
    if period in _PM_PERIODS and hour < 12:
        hour += 12
    if period in _AM_PERIODS and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise NluError(f"时刻 {hour}:{minute:02d} 越界（小时 0-23，分钟 0-59）")
    return time(hour, minute)


def _validate_now(now: datetime) -> tzinfo:
    if now.tzinfo is None:
        raise NluError("now 必须是带时区（tz-aware）的 datetime，不能是裸本地时间")
    return now.tzinfo


# ----------------------------------------------------------------------------
# 周期（recurring）分支
# ----------------------------------------------------------------------------


def _combine(day: date, clock: time, zone: tzinfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=zone)


def _future_today_or_next(today: date, clock: time, now_local: datetime, zone: tzinfo) -> date:
    """今天该槽位还没到就取今天；已过则顺到下一个自然日。"""
    if _combine(today, clock, zone) <= now_local:
        return today + timedelta(days=1)
    return today


def _next_weekly(today: date, wd_idx: int, clock: time, now_local: datetime, zone: tzinfo) -> date:
    """下一个命中给定周几的日期；今天命中且槽位未过就取今天。"""
    delta = (wd_idx - today.weekday()) % 7
    candidate = today + timedelta(days=delta)
    if candidate == today and _combine(candidate, clock, zone) <= now_local:
        candidate += timedelta(days=7)
    return candidate


def _next_monthly(today: date, day: int, clock: time, now_local: datetime, zone: tzinfo) -> date:
    """下一个 ``day`` 号；跳过没有该日的月份（如 2 月没有 31 号）。"""
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is not None and (candidate > today or _combine(candidate, clock, zone) > now_local):
            return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1
    raise NluError(f"每月 {day} 号在可行月份内找不到下一次 occurrence")


def _next_yearly(today: date, mon: int, day: int, clock: time, now_local: datetime, zone: tzinfo) -> date:
    """下一个周年纪念日；今年已过则顺到明年。"""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, mon, day)
        except ValueError as exc:
            raise NluError(f"{year}-{mon:02d}-{day:02d} 不是有效日期：{exc}") from exc
        if candidate > today or _combine(candidate, clock, zone) > now_local:
            return candidate
    raise NluError(f"每年 {mon} 月 {day} 日找不到下一次 occurrence")


def _recurring_result(
    rrule: str,
    anchor: date,
    clock: time,
    now_local: datetime,
    zone: tzinfo,
    raw: str,
) -> ParsedTime:
    moment = _combine(anchor, clock, zone)
    dtstart = moment
    return ParsedTime(
        moment=moment,
        date=anchor,
        clock=clock,
        crosses_midnight=anchor != now_local.date(),
        raw=raw,
        kind="recurring",
        rrule=rrule,
        dtstart=dtstart,
    )


def _match_recurring(text: str, now_local: datetime, zone: tzinfo, raw: str) -> ParsedTime | None:
    """识别封闭词表内的周期形状；不是周期短语返回 ``None``。"""
    today = now_local.date()
    clock = _match_clock(text) or time(9, 0)

    weekly = _RE_WEEKLY.search(text)
    if weekly:
        chars = weekly.group("wds")
        rfc: list[str] = []
        idxs: list[int] = []
        seen: set[str] = set()
        for char in chars:
            if char not in _WEEKDAY_CN or char in seen:
                continue
            seen.add(char)
            idxs.append(_WEEKDAY_CN[char])
            rfc.append(_WEEKDAY_RFC[char])
        if not idxs:
            return None
        anchor = min(_next_weekly(today, idx, clock, now_local, zone) for idx in idxs)
        return _recurring_result(f"FREQ=WEEKLY;BYDAY={','.join(rfc)}", anchor, clock, now_local, zone, raw)

    if _RE_DAILY.search(text):
        anchor = _future_today_or_next(today, clock, now_local, zone)
        return _recurring_result("FREQ=DAILY", anchor, clock, now_local, zone, raw)

    monthly = _RE_MONTHLY.search(text)
    if monthly:
        day = int(monthly.group("day"))
        if not 1 <= day <= 31:
            raise NluError(f"每月 {day} 号越界（日 1-31）")
        anchor = _next_monthly(today, day, clock, now_local, zone)
        return _recurring_result(f"FREQ=MONTHLY;BYMONTHDAY={day}", anchor, clock, now_local, zone, raw)

    yearly = _RE_YEARLY.search(text)
    if yearly:
        mon = int(yearly.group("mon"))
        day = int(yearly.group("day"))
        if not 1 <= mon <= 12 or not 1 <= day <= 31:
            raise NluError(f"每年 {mon} 月 {day} 日越界（月 1-12，日 1-31）")
        anchor = _next_yearly(today, mon, day, clock, now_local, zone)
        return _recurring_result(
            f"FREQ=YEARLY;BYMONTH={mon};BYMONTHDAY={day}",
            anchor,
            clock,
            now_local,
            zone,
            raw,
        )

    every_n = _RE_EVERY_N.search(text)
    if every_n:
        n = int(every_n.group("n"))
        unit = every_n.group("unit")
        if n < 1:
            raise NluError("每 N 次重复的 N 必须不小于 1")
        if unit == "天":
            anchor = _future_today_or_next(today, clock, now_local, zone)
            freq = "DAILY"
        elif unit == "周":
            anchor = _next_weekly(today, today.weekday(), clock, now_local, zone)
            freq = "WEEKLY"
        else:
            anchor = _next_monthly(today, today.day, clock, now_local, zone)
            freq = "MONTHLY"
        interval = f";INTERVAL={n}" if n > 1 else ""
        return _recurring_result(f"FREQ={freq}{interval}", anchor, clock, now_local, zone, raw)

    return None


# ----------------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------------


def parse_phrase(
    phrase: str,
    now: datetime,
    tz: tzinfo | None = None,
    *,
    recurring: bool = False,
) -> ParsedTime:
    """把一句中文时间短语解析成结构化结果。

    参数：
        phrase: 中文时间短语，如「明天下午三点」「下周三」「半小时后」。
        now: 注入的当前时刻，必须 tz-aware；模块内部不读系统时间。
        tz: 目标时区；省略时取 ``now`` 自带的时区。
        recurring: 是否解析周期表达。默认 ``False``（一次性契约：周期短语拒绝）；
            ``True`` 时封闭词表内的周期短语产出 ``kind="recurring"`` + ``rrule``。

    返回：
        :class:`ParsedTime`。

    抛出：
        UnsupportedExpression: 周期写路径未接入，或规则需要工作日历 / BYSETPOS 等。
        NluError: 短语落在封闭词表之外，或日期 / 时刻越界。
    """
    source_tz = _validate_now(now)
    zone = tz if tz is not None else source_tz
    local_now = now.astimezone(zone)
    text = _normalize(phrase)

    if _RE_RECURRING_REJECTED.search(text):
        raise UnsupportedExpression(_UNSUPPORTED_REASON_SHAPE)

    recurring_parsed: ParsedTime | None = None
    if recurring:
        recurring_parsed = _match_recurring(text, local_now, zone, phrase)
    else:
        _check_recurring_disabled(text)

    if recurring_parsed is not None:
        return recurring_parsed

    # 相对时长（半小时后 / N 小时后 / N 分钟后）：先落到 UTC 瞬时再加，
    # 避免 DST 边界上按本地墙钟加完再重算偏移导致瞬时漂移。
    duration = _match_duration_minutes(text)
    if duration is not None:
        moment = (now.astimezone(UTC) + timedelta(minutes=duration)).astimezone(zone)
        local = moment
        return ParsedTime(
            moment=moment,
            date=local.date(),
            clock=local.time().replace(second=0, microsecond=0),
            crosses_midnight=local.date() != local_now.date(),
            raw=phrase,
            kind="once",
            rrule=None,
            dtstart=moment,
        )

    today = local_now.date()
    anchor: date | None = None
    anchor_kind: str | None = None

    day_offset = _match_day_offset(text)
    day_duration = _match_day_duration(text)
    if day_offset is not None:
        anchor = today + timedelta(days=day_offset)
        anchor_kind = "day_offset"
    elif day_duration is not None:
        anchor = today + timedelta(days=day_duration)
        anchor_kind = "day_offset"
    else:
        weekday = _match_weekday(text, today)
        if weekday is not None:
            anchor = weekday
            anchor_kind = "weekday"
        else:
            monthday = _match_monthday(text, today)
            if monthday is not None:
                anchor = monthday
                anchor_kind = "monthday"

    clock = _match_clock(text)
    if anchor is None and clock is None:
        raise NluError(f"无法识别的时间短语：{phrase!r}（不在 L0/L1 支持范围内）")

    target_date = anchor if anchor is not None else today
    if clock is None:
        # 无时刻时的默认口径（写进文档，不许临场猜）：
        #   - 纯天数偏移（今天 / 明天 / N 天后）：继承 now 的当前时分；
        #   - 周 / 月锚点：默认上午 09:00。
        if anchor_kind in ("weekday", "monthday"):
            clock = time(9, 0)
        else:
            clock = local_now.time().replace(second=0, microsecond=0)

    moment = datetime.combine(target_date, clock, tzinfo=zone)
    return ParsedTime(
        moment=moment,
        date=target_date,
        clock=clock,
        crosses_midnight=target_date != today,
        raw=phrase,
        kind="once",
        rrule=None,
        dtstart=moment,
    )
