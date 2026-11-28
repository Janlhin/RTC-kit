#!/usr/bin/env python3
"""rtc_once.py — 宿主侧的确定性「一次性提醒」入口，不调用任何模型。

用法:
    python rtc_once.py 11:30 "出门"                  # 今天 11:30 响一次
    python rtc_once.py 09:00 "交材料" --date 2026-11-15
    python rtc_once.py "明天下午三点" "看牙"          # NLU L0/L1 中文短语（确定性解析）
    python rtc_once.py 23:45 --workspace <dir>       # 在副本上试跑

做三件事，全部由宿主完成:
  1. 往 routines.json 追加一个 oneOff 窗口（结构化字段）
  2. 调 rtc.py plan --force 重新生成当天计划
  3. 打印确认信息

时间入口两条路径:
  - ``HH:MM``：既有 24 小时制路径，行为逐字未改；
  - 中文短语：交 ``rtc_nlu`` 做封闭词表确定性解析（零模型、零第三方），
    周期 / 集合类表达明确拒绝，不偷偷降级。

不会跑 due（跑一次就会把响铃消耗掉）；到点由 cron 的 rtc-due 投递。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import rtc_nlu

if hasattr(sys.stdout, "reconfigure"):
    # 标准库类型里 TextIO 没有 reconfigure；实际解释器上是 TextIOWrapper。
    cast(Any, sys.stdout).reconfigure(encoding="utf-8", errors="replace")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
RT = HERE / "rtc.py"
DEFAULT_WS = Path(os.environ.get("RTC_WORKSPACE") or (Path.home() / "RTC"))


def seal_routine(ws: Path, routines: Path) -> None:
    """写完时刻表要盖章，否则下一次 plan 会把这条提醒报成「有人手改」。

    我们也是「程序」——只有 RTC 自己写的文件才不该触发手改告警。
    """
    try:
        payload = {
            "digest": hashlib.sha256(routines.read_bytes()).hexdigest(),
            "windows": len(
                (json.loads(routines.read_text(encoding="utf-8")) or {}).get("alarmWindows") or []
            ),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        seal = ws / "state" / "routine-seal.json"
        seal.parent.mkdir(parents=True, exist_ok=True)
        seal.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        last = ws / "state" / "backups" / "routines.program-last.json"
        last.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(routines, last)
    except (OSError, ValueError, TypeError):
        pass  # 盖章失败不影响提醒本身


def fail(msg: str) -> int:
    print(f"拒绝执行：{msg}", file=sys.stderr)
    return 2


def _fixed_offset(offset: str) -> timezone:
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def _resolve_tz(routine: dict[str, Any]) -> tzinfo:
    """与 ``rtc_core.resolve_tz`` 同一条回退链：IANA 名 → 旧固定偏移 → 默认 +08:00。"""
    name = str(routine.get("timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
            pass
    offset = routine.get("tzOffset")
    if isinstance(offset, str) and offset:
        return _fixed_offset(offset)
    return _fixed_offset("+08:00")


def main() -> int:
    ap = argparse.ArgumentParser(description="添加一个一次性提醒（纯确定性，无模型）")
    ap.add_argument("at", help="触发时刻 HH:MM，24 小时制")
    ap.add_argument("label", nargs="?", default="提醒", help="提醒文案")
    ap.add_argument("--date", default=None, help="生效日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--window-min", type=int, default=4, help="触发窗口宽度（分钟），默认 4")
    ap.add_argument("--workspace", default=str(DEFAULT_WS), help="RTC 工作区（测试时可指向副本）")
    ap.add_argument("--ack", default="知道了", help="确认词，默认「知道了」")
    args = ap.parse_args()

    ws = Path(args.workspace)
    routines = ws / "rules" / "routines.json"
    if not routines.is_file():
        return fail(f"找不到时刻表 {routines}")
    if not RT.is_file():
        return fail(f"找不到 rtc.py {RT}")
    if not 1 <= args.window_min <= 30:
        return fail("--window-min 必须在 1..30 之间")

    try:
        routine_data = json.loads(routines.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"时刻表读不出来：{exc}")
    tz = _resolve_tz(routine_data)

    hh: int
    mm: int
    target: date
    clock_str: str
    date_iso: str
    if re.fullmatch(r"\d{1,2}:\d{2}", args.at):
        # 既有 HH:MM 路径。「今天」按工作区时区算，与 plan 的清理口径一致；
        # 进程本地是 UTC 时不能用 date.today()，否则跨午夜后会被当成过期清掉。
        try:
            hh, mm = (int(x) for x in args.at.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except ValueError:
            return fail("时间必须是 24 小时制 HH:MM，例如 11:30")
        target = date.fromisoformat(args.date) if args.date is not None else datetime.now(tz).date()
        clock_str = f"{hh:02d}:{mm:02d}"
        date_iso = target.isoformat()
    else:
        # NLU L0/L1 路径：中文短语 → 结构化日期 + 时刻。
        if args.date is not None:
            return fail("自然语言短语与 --date 不能同时指定；日期已包含在短语里")
        now = datetime.now(tz)
        try:
            parsed = rtc_nlu.parse_phrase(args.at, now, tz)
        except rtc_nlu.UnsupportedExpression as exc:
            return fail(exc.reason)
        except rtc_nlu.NluError as exc:
            return fail(str(exc))
        target = parsed.date
        hh, mm = parsed.clock.hour, parsed.clock.minute
        clock_str = parsed.hhmm
        date_iso = parsed.date_iso
        if parsed.crosses_midnight:
            print("提示：该提醒落在另一个日历日（跨天）。", file=sys.stderr)

    start_min = hh * 60 + mm
    end_min = min(start_min + args.window_min, 23 * 60 + 59)
    start = clock_str
    end = f"{end_min // 60:02d}:{end_min % 60:02d}"
    wid = f"once-{hh:02d}{mm:02d}-{target.strftime('%Y%m%d')}"
    iso = date_iso

    state_path = ws / "state" / "alarms.json"
    before = None
    if state_path.is_file():
        try:
            before = json.loads(state_path.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            before = None

    raw = routines.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    windows = data.setdefault("alarmWindows", [])
    if any(w.get("id") == wid for w in windows):
        return fail(f"窗口 {wid} 已存在，如需修改请先删除 routines.json 里对应条目")

    windows.append(
        {
            "id": wid,
            "label": args.label,
            "start": start,
            "end": end,
            "intervalMin": 5,
            "expectFires": 1,
            "oneOff": True,
            "ackHints": [args.ack],
            "validFrom": iso,
            "validUntil": iso,
        }
    )
    routines.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    proc = subprocess.run(
        [sys.executable, str(RT), "--workspace", str(ws), "plan", "--force"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        # 回滚配置写入，避免留下未生效的脏配置
        routines.write_bytes(raw)
        print((proc.stderr or proc.stdout or "plan 失败").strip(), file=sys.stderr)
        return fail("plan --force 失败，已回滚 routines.json")

    # 我们也是程序：写完立刻盖章，否则下一次 plan 会把刚加的提醒报成「有人手改」。
    seal_routine(ws, routines)

    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)

    # plan --force 会重建当天状态。把旧窗口的运行时字段合并回来，
    # 否则已送达/已确认的提醒会被复活成 pending（违反不复活 / 不误消）。
    if before and isinstance(before.get("windows"), list):
        runtime_keys = (
            "fireCount",
            "state",
            "lastFiredAt",
            "ackedAt",
            "ackText",
            "missedAt",
            "missedReason",
            "checked",
            "disabled",
            "cancelledAt",
        )
        old_by_id = {w.get("id"): w for w in before["windows"]}
        try:
            current = json.loads(state_path.read_bytes().decode("utf-8"))
            for w in current.get("windows", []):
                old = old_by_id.get(w.get("id"))
                if old:
                    for key in runtime_keys:
                        if key in old:
                            w[key] = old[key]
            state_path.write_bytes((json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except (OSError, ValueError) as exc:
            print(f"警告：合并既有状态失败（{exc}），请检查 state/alarms.json", file=sys.stderr)

    print(f"已登记：{iso} {start}–{end} 「{args.label}」一次性，id={wid}")
    print("到点由 cron 的 rtc-due 自动投递；此处不会手动触发 due。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
