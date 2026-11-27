#!/usr/bin/env python3
"""rtc_nlu.py — 确定性中文时间短语解析（NLU L0/L1），零模型、零第三方依赖。

本模块只做「一次性时间点 / 日期」的封闭词表解析，定位是交互入口的前端翻译层：

  - **确定性**：同一输入 + 同一注入的 ``now``，多次解析结果逐字节一致；模块内部
    不读系统时间（没有 ``datetime.now`` / ``date.today`` 兜底），现在时刻必须由
    调用方以 tz-aware datetime 注入。
  - **时区**：一律使用调用方给出的 IANA ``tzinfo``（``zoneinfo``）；日级运算按
    本地墙钟算，相对时长运算走 aware datetime，不写死固定偏移。
  - **边界**：周期 / 重复 / 集合类表达（每年、每周一三五、每月最后一个工作日、
    每天 8 点等）现有契约表达不了，明确抛出 :class:`UnsupportedExpression`，
    不硬塞、不偷偷降级成某个单次时刻。

依赖纪律：本模块只能被交互入口（``rtc_once``）import；``plan`` / ``due`` /
``tick`` 判定链路与 ``rtc_core`` 不得依赖它（由测试做静态 + 运行时双重断言）。
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

# 时段词 → 是否把 12 小时制的小时数往下午拨 12。
_PM_PERIODS = ("下午", "晚上")
_AM_PERIODS = ("凌晨", "早上", "上午")

# 周期 / 集合类表达：命中即判「现有契约不支持」。
_RECURRING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"每年|每月|每周|每天|每日|每逢|每个"),
    re.compile(r"工作日|最后一个|节假日"),
)

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
        moment: 目标绝对瞬时（tz-aware datetime）。
        date: 目标本地日历日（所在时区墙钟）。
        clock: 目标本地时刻（不带时区）。
        crosses_midnight: 与注入的 ``now`` 是否不在同一本地日历日。
        raw: 归一化之前的原始短语。
    """

    moment: datetime
    date: date
    clock: time
    crosses_midnight: bool
    raw: str

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


def _normalize(phrase: str) -> str:
    text = "".join(_FULLWIDTH_DIGITS.get(ch, ch) for ch in phrase)
    text = re.sub(r"\s+", "", text)
    text = text.replace("星期", "周").replace("礼拜", "周")
    return text


def _check_recurring(text: str) -> None:
    for pattern in _RECURRING_PATTERNS:
        if pattern.search(text):
            raise UnsupportedExpression(
                "现有契约不支持：周期 / 重复 / 集合类表达（如每年、每周一三五、"
                "每月最后一个工作日、每天几点）；当前 L0/L1 只解析一次性时间点。"
            )


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
# 对外入口
# ----------------------------------------------------------------------------


def parse_phrase(phrase: str, now: datetime, tz: tzinfo | None = None) -> ParsedTime:
    """把一句中文时间短语解析成结构化结果。

    参数：
        phrase: 中文时间短语，如「明天下午三点」「下周三」「半小时后」。
        now: 注入的当前时刻，必须 tz-aware；模块内部不读系统时间。
        tz: 目标时区；省略时取 ``now`` 自带的时区。

    返回：
        :class:`ParsedTime`。

    抛出：
        UnsupportedExpression: 周期 / 重复 / 集合类表达，现有契约不支持。
        NluError: 短语落在封闭词表之外，或日期 / 时刻越界。
    """
    source_tz = _validate_now(now)
    zone = tz if tz is not None else source_tz
    local_now = now.astimezone(zone)
    text = _normalize(phrase)

    _check_recurring(text)

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
    )
