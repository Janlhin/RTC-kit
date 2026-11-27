"""不可变操作审计日志：``state/audit.jsonl``（JSON Lines，只追加）。

与 ``state/history.db`` 的分工：

- ``history.db`` 是执行历史的**派生索引**（可删可重建，参与统计聚合）；
- ``audit.jsonl`` 是只追加的**事件溯源流**：谁（``actor``）从哪个子命令
  （``source``）把哪个逻辑目标（``target``）从旧值（``old``）改成新值
  （``new``），何时（``ts``）、为什么（``reason``）。

语义口径（与 ``docs/behavior-contract.md`` 同一份契约的两处渲染）：

- **append-only**：本模块只暴露追加与读取，没有任何重写 / 删除 / 截断接口；
  既有行落盘后字节不变。
- **原子单行**：一条记录 = 一次 ``O_APPEND`` 写完整行 + 换行；多进程并发追加
  不允许出现半截行或撕裂缝。
- **失败不阻断**：写日志失败只打 stderr 一行，绝不冒泡成 Traceback，不影响主
  操作——审计不参与判定（与心跳、history.db 同一口径）。
- **隐私**：``target`` 只用逻辑 id（窗口 id / 命令作用对象），不记绝对路径、
  token、投递正文。
- **不是唯一真相源**：本批账本仍以 ``state/alarms.json`` /
  ``rules/routines.json`` 为准；审计流用于事后追溯与按时点回放重建。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import rtc_core as core

__all__ = [
    "AUDIT_NAME",
    "ACTOR_USER",
    "ACTOR_PROGRAM",
    "ACTOR_MODEL",
    "audit_path",
    "record",
    "read_events",
    "replay_state",
    "replay_routine",
]

AUDIT_NAME = "audit.jsonl"

# actor 取值：谁做的。
# - program：确定性 cron / 程序自动写（plan / due / recovery / 预排激活…）
# - user：本机交互命令直接调用（ack / check / cancel / postpone / set-time…）
# - model：模型或外部适配器代操作（如 ack --source external）
ACTOR_USER = "user"
ACTOR_PROGRAM = "program"
ACTOR_MODEL = "model"
ACTORS = frozenset({ACTOR_USER, ACTOR_PROGRAM, ACTOR_MODEL})


def audit_path() -> Path:
    """审计日志路径（跟随当前工作区）。"""
    return core.WORKSPACE / "state" / AUDIT_NAME


def _normalize_actor(actor: str) -> str:
    """未知 actor 归到 program，既不抛栈也不塞进非法枚举值。"""
    return actor if actor in ACTORS else ACTOR_PROGRAM


def _append_line(payload: dict[str, Any]) -> None:
    """把一条记录原子追加到日志：持 sidecar 互斥锁后，一次 O_APPEND 写完整行 + 换行。

    单行负载很小（几十到几百字节）。Windows 上 ``O_APPEND`` 跨进程不保证原子
    追加，因此先走核心同一套 sidecar + OS 锁协议把写入者串行化；临界区内单次
    ``os.write`` 写完整行，再 ``fsync``。既有行不会被打开后重写——本函数只以
    追加方式打开，模块也不提供任何重写 / 删除接口。
    """
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    with core._exclusive_file_lock(p):
        fd = os.open(
            str(p),
            os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            n = os.write(fd, data)
            if n != len(data):
                raise OSError(f"审计日志短写：{n}/{len(data)} 字节")
            os.fsync(fd)
        finally:
            os.close(fd)


def record(
    action: str,
    target: str,
    *,
    actor: str,
    source: str,
    old: Any = None,
    new: Any = None,
    reason: str | None = None,
    ts: datetime | None = None,
) -> None:
    """追加一条审计记录。任何失败都只打 stderr，不阻断主操作。

    ``action``：动作名（plan / due-fire / due-missed / ack / check / cancel /
    complete / disable / enable / postpone / set-time / add / edit /
    prune-expired / recovery / restore-backup …）。
    ``target``：逻辑作用对象（窗口 id；账本级事件用 ``"-"``）。
    ``old`` / ``new``：变更前后的关键值（窗口定义 / 窗口运行时字段 / 账本摘要）。
    ``reason``：可空备注（如撤销打勾、冲突回滚），不写投递正文与个人信息。
    """
    try:
        stamp = (ts or datetime.now().astimezone()).isoformat()
        payload: dict[str, Any] = {
            "ts": stamp,
            "action": action,
            "target": target,
            "actor": _normalize_actor(actor),
            "source": source,
            "old": deepcopy(old),
            "new": deepcopy(new),
            "reason": reason,
        }
        _append_line(payload)
    except (OSError, ValueError, TypeError) as e:
        print(f"audit: 写入失败（{type(e).__name__}: {e}），本轮审计可能少一条", file=sys.stderr)


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    """读回全部审计记录，按落盘顺序返回。空文件 / 不存在返回空列表。

    坏行（半截行 / 非法 JSON）抛 ``ValueError``——回放前应先确认日志完整；
    调用方需要容错扫描时自行逐行解析。
    """
    p = path or audit_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError as e:
            raise ValueError(f"audit.jsonl 第 {lineno} 行不是合法 JSON：{e}") from None
    return out


# 账本级事件：回放时直接用 new 里的顶层字段与 windows 整份替换。
_LEDGER_EVENTS = frozenset({"plan", "prebuilt-promote", "recovery"})

_STATE_TOP_KEYS = (
    "version",
    "rev",
    "planDate",
    "planKind",
    "timezone",
    "generatedBy",
    "generatedAt",
)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _events_up_to(events: list[dict[str, Any]], at: datetime | str | None) -> Iterator[dict[str, Any]]:
    """按落盘顺序筛出 ``ts <= at`` 的事件；``at`` 为空表示全部回放。"""
    cutoff = _as_datetime(at)
    for event in events:
        if cutoff is None:
            yield event
            continue
        ts = _as_datetime(event.get("ts"))
        if ts is not None and ts <= cutoff:
            yield event


def _find_window(state: dict[str, Any], wid: str) -> dict[str, Any] | None:
    for w in state.get("windows", []) or []:
        if isinstance(w, dict) and w.get("id") == wid:
            return w
    return None


def _apply_state_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    action = str(event.get("action") or "")
    new = event.get("new")
    if action in _LEDGER_EVENTS:
        if not isinstance(new, dict):
            return
        for key in _STATE_TOP_KEYS:
            if key in new:
                state[key] = deepcopy(new[key])
        if isinstance(new.get("windows"), list):
            state["windows"] = deepcopy(new["windows"])
        if "history" in new:
            state["history"] = deepcopy(new["history"])
        return
    wid = event.get("target")
    if not isinstance(wid, str) or not wid:
        return
    win = _find_window(state, wid)
    if win is None:
        # 事件对应的窗口不在当前账本（早于基线 / 已归档）：不凭空造窗口。
        return
    if isinstance(new, dict) and "window" in new:
        # 账本侧事件：{"rev": 账本 rev, "window": 窗口快照}。
        if isinstance(new["window"], dict):
            win.update(deepcopy(new["window"]))
        if "rev" in new:
            state["rev"] = deepcopy(new["rev"])
    elif isinstance(new, dict):
        win.update(deepcopy(new))
        if "rev" in new:
            state["rev"] = deepcopy(new["rev"])


def replay_state(
    events: list[dict[str, Any]],
    *,
    base: dict[str, Any] | None = None,
    at: datetime | str | None = None,
) -> dict[str, Any]:
    """按时间回放审计流，重建某时点的账本（``alarms.json`` 形态）。

    ``base``：回放起点快照（通常是更早的一份真实账本）；为空时从空骨架起步。
    ``at``：只回放 ``ts <= at`` 的事件；为空回放全部。

    回放是纯函数：不读磁盘、不写文件、不改入参。
    """
    state: dict[str, Any] = (
        deepcopy(base)
        if isinstance(base, dict)
        else {
            "version": 1,
            "rev": 0,
            "windows": [],
            "history": [],
        }
    )
    if not isinstance(state.get("windows"), list):
        state["windows"] = []
    if not isinstance(state.get("history"), list):
        state["history"] = []
    for event in _events_up_to(events, at):
        _apply_state_event(state, event)
    return state


def replay_routine(
    events: list[dict[str, Any]],
    *,
    base: dict[str, Any] | None = None,
    at: datetime | str | None = None,
) -> dict[str, Any]:
    """按时间回放审计流，重建某时点的时刻表窗口集合（``routines.json`` 形态）。

    只重建 ``alarmWindows``；顶层其它字段沿用 ``base``。窗口顺序不保证与原表
    一致（事件溯源不保留列表位置），逐字段比对时按 id 索引。
    """
    routine: dict[str, Any] = deepcopy(base) if isinstance(base, dict) else {"version": 1}
    windows: dict[str, dict[str, Any]] = {}
    for w in routine.get("alarmWindows") or []:
        if isinstance(w, dict) and isinstance(w.get("id"), str):
            windows[w["id"]] = deepcopy(w)
    for event in _events_up_to(events, at):
        action = str(event.get("action") or "")
        wid = event.get("target")
        new = event.get("new")
        if not isinstance(wid, str) or not wid:
            continue
        if action in {"add", "edit"} and isinstance(new, dict):
            windows[wid] = deepcopy(new)
        elif action in {"set-time", "postpone", "cancel", "complete", "disable", "enable"}:
            if wid in windows and isinstance(new, dict):
                windows[wid].update(deepcopy(new))
        elif action == "prune-expired" and isinstance(new, dict):
            for removed in new.get("removed") or []:
                windows.pop(str(removed), None)
    routine["alarmWindows"] = [windows[k] for k in sorted(windows)]
    return routine
