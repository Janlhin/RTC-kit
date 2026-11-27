"""停摆恢复（host outage bootstrap）：确定性判定、过期窗口归档与恢复提示。

宿主长时间不被调用时，due 心跳的 ``attemptAt`` 会停止刷新。重启后第一个 due 读到
陈旧的 ``attemptAt``，就能判定「刚从停摆恢复」：把停摆期间错过的窗口归档为
``missed``（不静默丢弃，也不当成正常响应），补建当天计划，再产出一条可投递的
恢复提示。

全程不调模型、不猜用户意图；时间由调用方注入，测试不靠真实 sleep 或系统时钟硬等。
判定只认 ``attemptAt``（有没有被调过），不认 ``lastOkAt``（判完没有）——后者陈旧
属于崩溃循环，归自检路径，不混进停摆恢复。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import rtc_core as core
import rtc_history as hist

# 两个 due tick（5 分钟 × 2）没刷 attemptAt，判为停摆。阈值与 tick 间隔绑定，
# 不是拍脑袋的固定小时数。
RECOVERY_STALE_MIN = 10

# 归档原因：宿主停摆导致错过。与 day_rollover（日切自然归档）、window_expired
# （当天窗口过期）并列，查账时能分开看。
OUTAGE_REASON = "host_outage"


@dataclass
class RecoveryReport:
    """一次恢复核对的结果。纯数据，不带副作用。"""

    outage: bool
    attempt_at: datetime | None
    gap_minutes: int
    missed: list[dict[str, Any]] = field(default_factory=list)
    already_handled: bool = False


def detect_outage(now: datetime, attempt_at: datetime | None, *, bootstrap: bool, ledger_stale: bool) -> bool:
    """是否判定为「刚从停摆恢复」。

    证据要两条凑齐：心跳陈旧 **且** 账本滞后于今天（旧 planDate 还没补建）。
    只凭心跳陈旧不够——同一天里稀疏地手动跑几次 due、或有意静默，心跳也会
    陈旧，但账本是今天的，判成停摆就是误报。

    - ``bootstrap=True``：宿主重启钩子 / agent 显式唤醒，不等下一个 cron tick，
      现在就跑一次核对；账本新鲜时仍不伪造停摆。
    - 没有 ``attemptAt``（心跳文件缺失或损坏）：自动判定不猜；显式 bootstrap
      且账本滞后才当作「需要核对账本」。
    """
    if not ledger_stale:
        return False
    if attempt_at is None:
        return bool(bootstrap)
    return now - attempt_at > timedelta(minutes=RECOVERY_STALE_MIN)


def outage_identity(attempt_at: datetime | None) -> str | None:
    """同一次停摆的稳定身份：以停摆前最后一次 attemptAt 为准。"""
    return attempt_at.isoformat() if attempt_at is not None else None


def _already_handled(attempt_at: datetime | None) -> bool:
    """这次停摆是否已经归档过（同一身份不重复记账、不重复投递）。"""
    identity = outage_identity(attempt_at)
    if identity is None:
        return False
    try:
        for ev in hist.events():
            if ev.get("kind") == "recovery" and ev.get("note") == identity:
                return True
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # 历史库读不动时不挡恢复：宁可多记一次，不能把错过的窗口静默丢掉。
        return False
    return False


def human_span(minutes: int) -> str:
    """把停摆分钟数说成大白话时长。"""
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def collect_missed(
    routine: dict[str, Any], old: dict[str, Any], now: datetime, errors: list[str]
) -> list[dict[str, Any]]:
    """旧 planDate 之后、今天之前，错过的每个窗口归档为 missed。

    暂停期内的日子、显式禁用日、合法空日不算漏响；坏窗口降级跳过（进 errors），
    不连累同一天其余窗口。
    """
    start_iso = str(old.get("planDate") or "")
    try:
        start_day = date.fromisoformat(start_iso)
    except ValueError:
        return []
    today = now.date()
    paused = core.load_pause()
    missed: list[dict[str, Any]] = []
    d = start_day + timedelta(days=1)
    while d < today:
        if not core.pause_covers(paused, d):
            kind = core.plan_kind(d, routine)
            if not core.day_disabled(routine, kind):
                day_errors: list[str] = []
                windows = core.build_windows(
                    routine,
                    d,
                    kind,
                    datetime.combine(d, time.max, tzinfo=now.tzinfo),
                    strict=False,
                    errors=day_errors,
                )
                errors.extend(day_errors)
                for w in windows:
                    missed.append(
                        {
                            "date": d.isoformat(),
                            "id": w["id"],
                            "label": w["label"],
                            "expect": int(w.get("expectFires") or 0),
                            "state": "missed",
                            "fires": 0,
                            "ackedAt": None,
                            "missedReason": OUTAGE_REASON,
                        }
                    )
        d += timedelta(days=1)
    return missed


def build_report(
    routine: dict[str, Any],
    old: dict[str, Any] | None,
    now: datetime,
    attempt_at: datetime | None,
    *,
    bootstrap: bool,
    ledger_stale: bool,
    errors: list[str],
) -> RecoveryReport:
    """判定停摆并算出归档清单。只读，不落盘。"""
    outage = detect_outage(now, attempt_at, bootstrap=bootstrap, ledger_stale=ledger_stale)
    if not outage:
        return RecoveryReport(False, attempt_at, 0)
    gap = int((now - attempt_at).total_seconds() // 60) if attempt_at else 0
    report = RecoveryReport(True, attempt_at, max(gap, 0))
    report.already_handled = _already_handled(attempt_at)
    if old is None or report.already_handled:
        return report
    report.missed = collect_missed(routine, old, now, errors)
    return report


def format_message(report: RecoveryReport, *, rebuilt: bool) -> str:
    """拼那一条可投递的恢复提示。返回空串表示不投递。"""
    if not report.outage:
        return ""
    parts: list[str] = ["⏰ RTC 刚恢复"]
    if report.attempt_at is not None:
        parts.append(f"宿主约 {human_span(report.gap_minutes)} 没运行（上次 {report.attempt_at:%H:%M}）")
    else:
        parts.append("宿主刚被显式唤醒")
    n = len(report.missed)
    if n:
        labels = "、".join(f"{m['date'][5:]} {m['label']}" for m in report.missed[:3])
        tail = f" 等 {n} 个" if n > 3 else ""
        parts.append(f"期间 {n} 个提醒未送达（{labels}{tail}）")
    else:
        parts.append("期间没有错过的提醒")
    parts.append("今天的计划已补建。" if rebuilt else "账本已核对。")
    return parts[0] + "：" + "，".join(parts[1:])
