#!/usr/bin/env python3
"""rtc_query.py — L2 行程本只读查询：事件 + 旧窗口（P6）。

定位：**只读视图**，不写任何状态文件，不接 plan/due/ack 判定。回答「某段时间
有什么安排」，两条来源并排呈现、用 ``source`` 列区分：

- ``event``：影子账本 ``state/events.json`` 里的一等公民事件（P1 投影；P2 起
  真相源切到 ``.ics``，本模块读路径随之切换，见 D-015）。
- ``window``：既有 history.db / 快照里的旧窗口执行行（沿用 ``rtc_history``
  读路径，旧命令输出一字不动）。

边界：

- 纯标准库；**不被** ``plan`` / ``due`` / ``ack`` tick 热路径 import（与
  ``rtc_events`` 同一条静态守卫线）。
- 周期事件由 P3 ``rtc_rrule`` 引擎按查询区间逐条展开成 occurrence 实例；每条实例
  带 occurrence 起点与 ``ocid``。``show`` 仍按事件定义返回，不展开实例。
- ``--q`` 大小写不敏感子串匹配，中文按原字符、不分词；多条件 AND。
- ``export --ics``：序列化 / 折行 / 转义 / 跨年分组全部复用 P2 ``rtc_ics``，本模块只做路径路由。

退出码沿用既有集合：0 正常（含空结果与导出成功）/ 4 无匹配、无可导出事件或参数非法。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import rtc_core as core
import rtc_events as events
import rtc_history as history
import rtc_ics as ics
import rtc_rrule as rrule

__all__ = ["cmd_list", "cmd_show", "cmd_export"]

# 单次查询展开后的实例条数硬上限：先按区间过滤再聚合，超限明确报错而不是卡死
# 或静默截断。引擎侧另有 MAX_OCCURRENCES / MAX_SCAN_YEARS 双硬上限兜底死循环。
MAX_LIST_ITEMS = 2_000

_DEFAULT_SPACING_MIN = 15

DEFAULT_OFFSET = timezone(timedelta(hours=8))

_KIND_VALUES = ("once", "recurring")
_OWNER_SELF = "self"


class QueryValidation(ValueError):
    """查询参数非法（区间/筛选取值）；命令层转退出码 4，对齐 history/--range 口径。"""


# --------------------------------------------------------------------------- #
# 区间解析
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scope:
    """查询区间（闭区间，按日历日）。"""

    mode: str
    start: date
    end: date


def _parse_day(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise QueryValidation(f"{label} 必须是 YYYY-MM-DD：{raw!r}") from e


def _parse_year(raw: str) -> tuple[date, date]:
    if not (len(raw) == 4 and raw.isdigit()):
        raise QueryValidation(f"--year 必须是四位年份 YYYY：{raw!r}")
    year = int(raw)
    return date(year, 1, 1), date(year, 12, 31)


def _scope_from_args(args: argparse.Namespace) -> Scope:
    """三选一：``--date`` / ``--year`` / ``--from``+``--to``；互斥，缺一报错。"""
    modes = [args.date, args.year, args.from_date]
    given = [m for m in modes if m]
    if len(given) > 1:
        raise QueryValidation("--date / --year / --from+--to 三种区间只能给一种")
    if args.from_date or args.to_date:
        if not (args.from_date and args.to_date):
            raise QueryValidation("--from 必须和 --to 成对给")
        start = _parse_day(args.from_date, "--from")
        end = _parse_day(args.to_date, "--to")
        if start > end:
            raise QueryValidation(f"--from {start} 晚于 --to {end}")
        return Scope("range", start, end)
    if args.date:
        day = _parse_day(args.date, "--date")
        return Scope("date", day, day)
    if args.year:
        start, end = _parse_year(args.year)
        return Scope("year", start, end)
    raise QueryValidation("请给一种区间：--date YYYY-MM-DD / --year YYYY / --from A --to B")


# --------------------------------------------------------------------------- #
# 事件匹配
# --------------------------------------------------------------------------- #


def _parse_event_dt(raw: Any) -> datetime | None:
    """解析事件时刻；裸本地时间按 +08:00 兜住，不落成浮动时间。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_OFFSET)
    return dt


def _duration_minutes(start: datetime, end: Any) -> int:
    """事件起止时长（分钟）；缺 end / 非法值退回默认间隔，与 occurrence 适配层口径一致。"""
    if end is None:
        return _DEFAULT_SPACING_MIN
    try:
        parsed = datetime.fromisoformat(str(end))
    except ValueError:
        return _DEFAULT_SPACING_MIN
    minutes = (parsed.astimezone(start.tzinfo) - start).total_seconds() / 60
    return max(1, int(minutes)) if minutes > 0 else _DEFAULT_SPACING_MIN


def _scope_window(start_dt: datetime, scope: Scope) -> tuple[datetime, datetime]:
    """查询区间在事件时区下的半开窗口 ``[from, to)``。"""
    tz = start_dt.tzinfo or DEFAULT_OFFSET
    wfrom = datetime.combine(scope.start, time.min, tzinfo=tz)
    wto = datetime.combine(scope.end + timedelta(days=1), time.min, tzinfo=tz)
    return wfrom, wto


def _expand_event(ev: dict[str, Any], scope: Scope, errors: list[str]) -> list[dict[str, Any]]:
    """把事件展开成区间内的 occurrence 实例列表。

    - ``once``：start 落在区间内才出一条，实例起点即事件 start。
    - ``recurring``：交 P3 引擎 ``expand_occurrences`` 按半开区间逐条展开；
      跨年事件在两个年份的查询里各自出现对应实例。
    - 单条事件解析 / 展开失败只跳过它自己并记一条诊断，不拖垮整份查询。
    """
    start_dt = _parse_event_dt(ev.get("start"))
    if start_dt is None:
        return []
    uid = str(ev.get("uid") or "")
    duration = _duration_minutes(start_dt, ev.get("end"))

    if ev.get("kind") == "once":
        if not (scope.start <= start_dt.date() <= scope.end):
            return []
        occs = [start_dt]
    else:
        text = ev.get("rrule")
        if not text:
            return []
        wfrom, wto = _scope_window(start_dt, scope)
        try:
            parsed = rrule.parse_rrule(str(text))
            occs = rrule.expand_occurrences(start_dt, parsed, wfrom, wto)
        except (rrule.RruleError, ValueError, TypeError, KeyError) as e:
            errors.append(f"事件 {uid or '?'}：{e}")
            return []

    return [_occurrence_item(ev, occ, duration) for occ in occs]


def _occurrence_item(ev: dict[str, Any], occ: datetime, duration_min: int) -> dict[str, Any]:
    """一条 occurrence 实例：继承事件定义字段，覆盖实例起点 / 终点与 ``ocid``。"""
    uid = str(ev.get("uid") or "")
    item = dict(ev)
    item["source"] = "event"
    item["ocid"] = f"{uid}@{occ.date().isoformat()}"
    item["start"] = occ.isoformat()
    item["end"] = (occ + timedelta(minutes=duration_min)).isoformat()
    return item


def _event_haystack(ev: dict[str, Any]) -> str:
    """关键词检索文本池：标题（前向兼容字段）/ uid / owner / 别名 / 来源 routineId。"""
    source = ev.get("source") or {}
    parts: list[Any] = [
        ev.get("title"),
        ev.get("label"),
        ev.get("summary"),
        ev.get("uid"),
        ev.get("owner"),
        ev.get("status"),
        ev.get("kind"),
        ev.get("deadline"),
        ev.get("rrule"),
        source.get("routineId"),
    ]
    parts.extend(ev.get("aliases") or [])
    return " ".join(str(p) for p in parts if p).lower()


def _event_matches(ev: dict[str, Any], query: str | None, kind: str | None, owner: str | None) -> bool:
    kind_ok = not kind or ev.get("kind") == kind
    owner_ok = not owner or ev.get("owner") == owner
    query_ok = not query or query.lower() in _event_haystack(ev)
    return kind_ok and owner_ok and query_ok


# --------------------------------------------------------------------------- #
# 旧窗口行匹配
# --------------------------------------------------------------------------- #


def _window_kind(row: dict[str, Any]) -> str:
    """旧 daily 行类型 → 事件 kind 口径；oneoff/appointment 归 once，其余 recurring。"""
    return "once" if str(row.get("type") or "") in ("oneoff", "appointment") else "recurring"


def _window_haystack(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(k) or "") for k in ("window_id", "label", "missed")).lower()


def _window_matches(row: dict[str, Any], query: str | None, kind: str | None, owner: str | None) -> bool:
    # 旧窗口投影口径一律 self；替别人记的事件只出现在事件来源里。
    owner_ok = not owner or owner == _OWNER_SELF
    kind_ok = not kind or _window_kind(row) == kind
    query_ok = not query or query.lower() in _window_haystack(row)
    return owner_ok and kind_ok and query_ok


def _window_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "window",
        "date": row.get("date"),
        "windowId": row.get("window_id"),
        "label": row.get("label"),
        "kind": _window_kind(row),
        "owner": _OWNER_SELF,
        "state": row.get("state"),
        "expect": row.get("expect"),
        "fires": row.get("fired"),
        "ackedAt": row.get("acked_at"),
        "missedReason": row.get("missed"),
    }


def _old_window_rows(scope: Scope) -> list[dict[str, Any]]:
    if scope.mode == "date":
        return history.rows_for_day(scope.start.isoformat())
    return history.rows_for_range(scope.start.isoformat(), scope.end.isoformat())


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #


def _validate_filters(kind: str | None, owner: str | None) -> None:
    if kind and kind not in _KIND_VALUES:
        raise QueryValidation(f"--kind 取值：{' / '.join(_KIND_VALUES)}，收到 {kind!r}")
    if owner and owner != _OWNER_SELF and not owner.startswith("other:"):
        raise QueryValidation("--owner 取值：self 或 other:<name>")


# --------------------------------------------------------------------------- #
# 命令：list
# --------------------------------------------------------------------------- #


def _gather_items(
    scope: Scope, query: str | None, kind: str | None, owner: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    envelope = events.load_events(core.WORKSPACE)
    event_items: list[dict[str, Any]] = []
    for ev in events.all_events(envelope):
        # 筛选在事件定义层做；命中后再交引擎展开成区间内的 occurrence 实例。
        if not _event_matches(ev, query, kind, owner):
            continue
        event_items.extend(_expand_event(ev, scope, errors))
    event_items.sort(key=lambda x: (str(x.get("start") or ""), str(x.get("ocid") or "")))

    window_items: list[dict[str, Any]] = []
    for row in _old_window_rows(scope):
        if _window_matches(row, query, kind, owner):
            window_items.append(_window_item(row))
    window_items.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("windowId") or "")))
    return event_items, window_items, errors


def cmd_list(args: argparse.Namespace) -> int:
    try:
        _validate_filters(args.kind, args.owner)
        scope = _scope_from_args(args)
    except QueryValidation as e:
        print(f"list: {e}", file=sys.stderr)
        return 4

    event_items, window_items, errors = _gather_items(scope, args.q, args.kind, args.owner)

    for note in errors:
        print(f"list: 跳过 {note}", file=sys.stderr)

    if len(event_items) + len(window_items) > MAX_LIST_ITEMS:
        print(
            f"list: 结果超过上限 {MAX_LIST_ITEMS} 条，请收窄日期 / 年份范围后再查",
            file=sys.stderr,
        )
        return 4

    if args.json:
        payload = {
            "scope": {"mode": scope.mode, "from": scope.start.isoformat(), "to": scope.end.isoformat()},
            "filters": {"q": args.q, "kind": args.kind, "owner": args.owner},
            "count": {"events": len(event_items), "windows": len(window_items)},
            "items": [*event_items, *window_items],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_list_text(scope, event_items, window_items)
    return 0


def _print_list_text(
    scope: Scope, event_items: list[dict[str, Any]], window_items: list[dict[str, Any]]
) -> None:
    range_label = f"{scope.start} ~ {scope.end}" if scope.start != scope.end else f"{scope.start}"
    print(f"行程查询（{scope.mode} {range_label}）：事件 {len(event_items)} · 旧窗口 {len(window_items)}")
    if not event_items and not window_items:
        print("（无匹配）")
        return
    for ev in event_items:
        start = str(ev.get("start") or "")
        day = start[:10]
        stamp = start[11:16] or "--:--"
        aliases = ", ".join(ev.get("aliases") or [])
        ident = ev.get("uid") or ""
        if aliases:
            ident = f"{ident}（别名 {aliases}）"
        ocid = str(ev.get("ocid") or "")
        tail = f"  ocid={ocid}" if ocid else ""
        print(
            f"  事件  {day} {stamp}  {str(ev.get('kind') or ''):<10} "
            f"{str(ev.get('owner') or ''):<11} {str(ev.get('status') or ''):<10} {ident}{tail}"
        )
    for row in window_items:
        label = str(row.get("label") or row.get("windowId") or "")
        print(
            f"  旧窗口 {str(row.get('date') or '')}  {label:<16} "
            f"{row.get('kind'):<10} {_OWNER_SELF:<11} {str(row.get('state') or ''):<10} "
            f"{row.get('windowId')}"
        )


# --------------------------------------------------------------------------- #
# 命令：show
# --------------------------------------------------------------------------- #


def cmd_show(args: argparse.Namespace) -> int:
    target = str(args.target or "").strip()
    if not target:
        print("show: 缺目标 uid 或旧 id", file=sys.stderr)
        return 4

    envelope = events.load_events(core.WORKSPACE)
    ev = events.get_event(envelope, target)
    if ev is None:
        alias_uid = events.find_uid(envelope, target)
        if alias_uid:
            ev = events.get_event(envelope, alias_uid)

    if ev is None:
        print(f"show: 找不到事件 {target!r}（uid 或旧 id 均未命中）", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(ev, ensure_ascii=False, indent=2))
        return 0

    print(f"uid: {ev.get('uid')}")
    print(f"kind: {ev.get('kind')}")
    print(f"start: {ev.get('start')}")
    print(f"end: {ev.get('end')}")
    print(f"rrule: {ev.get('rrule')}")
    print(f"owner: {ev.get('owner')}")
    print(f"deadline: {ev.get('deadline')}")
    print(f"status: {ev.get('status')}")
    print(f"aliases: {', '.join(ev.get('aliases') or []) or '（无）'}")
    policy = ev.get("reminderPolicy") or {}
    print(
        "reminderPolicy: "
        f"preRemind={policy.get('preRemind')} ddlLadder={policy.get('ddlLadder')} "
        f"snooze={policy.get('snooze')}"
    )
    print(f"updatedAt: {ev.get('updatedAt')}")
    source = ev.get("source") or {}
    print(f"source: routineId={source.get('routineId')} ledgerDate={source.get('ledgerDate')}")
    return 0


# --------------------------------------------------------------------------- #
# 命令：export（序列化复用 P2 rtc_ics，不自写第二份）
# --------------------------------------------------------------------------- #


def _export_events() -> list[dict[str, Any]]:
    """导出事件清单：P2 真相优先（直扫 ``calendar/*.ics``），无 .ics 时回退 P1 影子账本。"""
    source = ics.load_all_events(core.WORKSPACE)
    if source:
        return source
    envelope = events.load_events(core.WORKSPACE)
    return events.all_events(envelope)


def _event_year(event: dict[str, Any]) -> int:
    return datetime.fromisoformat(str(event["start"])).year


def cmd_export(args: argparse.Namespace) -> int:
    """把事件导出为 RFC 5545 `.ics`；序列化 / 折行 / 转义 / 跨年分组全部走 P2 层。

    - 不给路径：整份日历写到 stdout。
    - 路径以 ``.ics`` 结尾：单文件导出全部事件。
    - 其余路径按目录处理：复用 P2 ``write_events`` 的按年分组与跨年交叉段，
      先落临时目录再复制到目标目录，不写工作区真相目录。
    """
    target = str(getattr(args, "ics", "") or "").strip()
    evs = _export_events()
    if not evs:
        print("export: 没有可导出的事件", file=sys.stderr)
        return 4

    if not target:
        year = min(_event_year(ev) for ev in evs)
        sys.stdout.write(ics.render_calendar(evs, year=year))
        return 0

    out = Path(target)
    if out.suffix.lower() == ".ics":
        year = min(_event_year(ev) for ev in evs)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(ics.render_calendar(evs, year=year), encoding="utf-8", newline="")
        print(f"已导出 {out}", file=sys.stderr)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="rtc-export-"))
    try:
        written = ics.write_events(tmp, evs)
        out.mkdir(parents=True, exist_ok=True)
        for src in written:
            shutil.copyfile(src, out / src.name)
        print(f"已导出 {len(written)} 个年份文件到 {out}", file=sys.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0
