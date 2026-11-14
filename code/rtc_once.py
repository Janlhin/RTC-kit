#!/usr/bin/env python3
"""rtc_once.py — 宿主侧的确定性「一次性提醒」入口，不调用任何模型。

用法:
    python rtc_once.py 11:30 "出门"                 # 今天 11:30 响一次
    python rtc_once.py 09:00 "交材料" --date 2026-11-15
    python rtc_once.py 23:45 --workspace <dir>      # 在副本上试跑

做三件事，全部由宿主完成，不需要 AI 理解任何自然语言:
  1. 往 routines.json 追加一个 oneOff 窗口（结构化字段）
  2. 调 rtc.py plan --force 重新生成当天计划
  3. 打印确认信息

不会跑 due（跑一次就会把响铃消耗掉）；到点由 cron 的 rtc-due 投递。
"""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RT = HERE / "rtc.py"
DEFAULT_WS = Path(r"C:\Users\Janlhin\RTC")


def fail(msg: str) -> int:
    print(f"拒绝执行：{msg}", file=sys.stderr)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="添加一个一次性提醒（纯确定性，无模型）")
    ap.add_argument("at", help="触发时刻 HH:MM，24 小时制")
    ap.add_argument("label", nargs="?", default="提醒", help="提醒文案")
    ap.add_argument("--date", default=None, help="生效日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--window-min", type=int, default=4, help="触发窗口宽度（分钟），默认 4")
    ap.add_argument("--workspace", default=str(DEFAULT_WS), help="RTC 工作区（测试时可指向副本）")
    ap.add_argument("--ack", default="知道了", help="确认词，默认「知道了」")
    args = ap.parse_args()

    try:
        hh, mm = (int(x) for x in args.at.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and len(args.at.split(":")) == 2):
            raise ValueError
    except ValueError:
        return fail("时间必须是 24 小时制 HH:MM，例如 11:30")

    target = date.today() if args.date is None else date.fromisoformat(args.date)
    ws = Path(args.workspace)
    routines = ws / "rules" / "routines.json"
    if not routines.is_file():
        return fail(f"找不到时刻表 {routines}")
    if not RT.is_file():
        return fail(f"找不到 rtc.py {RT}")
    if not 1 <= args.window_min <= 30:
        return fail("--window-min 必须在 1..30 之间")

    start_min = hh * 60 + mm
    end_min = min(start_min + args.window_min, 23 * 60 + 59)
    start = f"{hh:02d}:{mm:02d}"
    end = f"{end_min // 60:02d}:{end_min % 60:02d}"
    wid = f"once-{hh:02d}{mm:02d}-{target.strftime('%Y%m%d')}"
    iso = target.isoformat()

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

    windows.append({
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
    })
    routines.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    proc = subprocess.run(
        [sys.executable, str(RT), "--workspace", str(ws), "plan", "--force"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        # 回滚配置写入，避免留下未生效的脏配置
        routines.write_bytes(raw)
        print((proc.stderr or proc.stdout or "plan 失败").strip(), file=sys.stderr)
        return fail("plan --force 失败，已回滚 routines.json")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)

    # plan --force 会重建当天状态。把旧窗口的运行时字段合并回来，
    # 否则已送达/已确认的提醒会被复活成 pending（违反不复活 / 不误消）。
    if before and isinstance(before.get("windows"), list):
        runtime_keys = (
            "fireCount", "state", "lastFiredAt", "ackedAt", "ackText",
            "missedAt", "missedReason", "checked", "disabled", "cancelledAt",
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
            state_path.write_bytes(
                (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"警告：合并既有状态失败（{exc}），请检查 state/alarms.json", file=sys.stderr)

    print(f"已登记：{iso} {start}–{end} 「{args.label}」一次性，id={wid}")
    print("到点由 cron 的 rtc-due 自动投递；此处不会手动触发 due。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
