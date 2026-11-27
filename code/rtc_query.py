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
- 周期事件的 occurrence 逐条枚举属 P3 引擎；本阶段按「规则有效期与查询区间
  重叠」做归属匹配，不展开实例。接入 P3 ``rtc_rrule.expand_occurrences`` 后
  再补实例级过滤（见 D-019 复审条件）。
- ``--q`` 大小写不敏感子串匹配，中文按原字符、不分词；多条件 AND。

退出码沿用既有集合：0 正常（含空结果）/ 4 无匹配或参数非法；导出占位同 4。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import rtc_core as core
import rtc_events as events
import rtc_history as history

__all__ = ["cmd_list", "cmd_show", "cmd_export"]

# 单次查询返回条数硬上限：先按日期/年份过滤再聚合，超限明确报错而不是卡死。
# occurrence 枚举接入 P3 引擎后，条数预算由引擎的 ExpansionLimit 兜底。
MAX_LIST_ITEMS = 5_000

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


def _rrule_until_day(rrule: str | None) -> date | None:
    """从 RRULE 字符串取 UNTIL 的日历日；没有 UNTIL / 解析不出返回 None。"""
    if not rrule:
        return None
    for part in rrule.split(";"):
        if part.startswith("UNTIL="):
            value = part[6:]
            try:
                return datetime.strptime(value[:8], "%Y%m%d").date()
            except ValueError:
                return None
    return None


def _event_in_scope(ev: dict[str, Any], scope: Scope) -> bool:
    """事件是否落在查询区间。

    归属规则（P6 口径，D-019）：

    - ``once``：按 ``start`` 日期归属，只落在 start 那天所在的区间。
    - ``recurring``：按「规则有效期与区间重叠」归属——start 不晚于区间末、
      UNTIL 不早于区间始；跨年周期事件在两个年份都会列出。实例级逐条枚举
      待 P3 引擎接入。
    """
    start_dt = _parse_event_dt(ev.get("start"))
    if start_dt is None:
        return False
    start_day = start_dt.date()
    if ev.get("kind") == "once":
        return scope.start <= start_day <= scope.end
    until_day = _rrule_until_day(ev.get("rrule"))
    if start_day > scope.end:
        return False
    return until_day is None or until_day >= scope.start


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


def _event_item(ev: dict[str, Any]) -> dict[str, Any]:
    item = dict(ev)
    item["source"] = "event"
    return item


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    envelope = events.load_events(core.WORKSPACE)
    event_items: list[dict[str, Any]] = []
    for ev in events.all_events(envelope):
        if _event_in_scope(ev, scope) and _event_matches(ev, query, kind, owner):
            event_items.append(_event_item(ev))
    event_items.sort(key=lambda x: str(x.get("start") or ""))

    window_items: list[dict[str, Any]] = []
    for row in _old_window_rows(scope):
        if _window_matches(row, query, kind, owner):
            window_items.append(_window_item(row))
    window_items.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("windowId") or "")))
    return event_items, window_items


def cmd_list(args: argparse.Namespace) -> int:
    try:
        _validate_filters(args.kind, args.owner)
        scope = _scope_from_args(args)
    except QueryValidation as e:
        print(f"list: {e}", file=sys.stderr)
        return 4

    event_items, window_items = _gather_items(scope, args.q, args.kind, args.owner)

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
        print(
            f"  事件  {day} {stamp}  {str(ev.get('kind') or ''):<10} "
            f"{str(ev.get('owner') or ''):<11} {str(ev.get('status') or ''):<10} {ident}"
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
# 命令：export（占位，序列化层等 P2）
# --------------------------------------------------------------------------- #


def cmd_export(args: argparse.Namespace) -> int:
    """.ics 导出占位：序列化复用 P2 的 ``rtc_ics.py``，本阶段不自写一份。

    P2 分支合入后，此 handler 改为调用 P2 导出函数（一年一文件、RFC 5545），
    命令面与 ``--ics`` 参数保持不变。
    """
    print(
        "export --ics 暂未接入：等待 P2 的 rtc_ics.py 序列化层合入后复用，"
        "本 PR 不另写一份 .ics 序列化（见 D-019 已知缺口）",
        file=sys.stderr,
    )
    return 4
