#!/usr/bin/env python3
"""rtc_report.py — 早报素材生成器（方案 B：09:00 现场叫模型）。

宿主按 agent 模式跑本脚本时，stdout 会被注入模型的 prompt，模型据此写「汇报」。
所以这里只做三件事，全部是确定性数据，不判断、不措辞：

1. 昨日执行结果（`state/alarms.json` 的 history 里 `date == 昨天`）
2. 今日待办（`tasks.md` 的 Today 段未完成项）
3. 用户口述（`state/notes/<昨天>.md`，模型据此写「备注」段）

输出是一段紧凑的 Key-Value 素材；模型拿它 + `rules/formats.md` 的「汇报」版式
就能写出人话。**本脚本失败时退出码非 0，由宿主的 failure-deliver 兜底**；
模型没写文件时，`brief-morning-fallback` 窗口照常发静态汇报（见 rtc_brief）。

用法（宿主 cron 的 --script 直接调用，不带参数）：
    python rtc_report.py --workspace <dir> [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rtc_brief as brief  # noqa: E402
import rtc_core as core  # noqa: E402


def _yesterday_entries(state: dict, day: str) -> list[dict]:
    """取 history 里指定日期的条目；没有就返回空列表（合法空日，不是错误）。"""
    return [h for h in (state.get("history") or []) if str(h.get("date")) == day]


def _render_entries(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for h in entries:
        label = str(h.get("label") or h.get("id") or "")
        st = str(h.get("state") or "")
        fires = int(h.get("fires") or 0)
        reason = h.get("missedReason")
        word = brief._REASON_WORD.get(str(reason), str(reason or "")) if reason else ""
        extra = f"（{word}）" if word else ""
        lines.append(f"  - {label} = {st}，响 {fires} 次{extra}")
    return lines


def build_material(workspace: Path, day: date) -> list[str]:
    """产出注入模型 prompt 的素材。只读，绝不写任何文件。"""
    core.set_workspace(Path(workspace))
    core.load_routine()  # 先校验时刻表可读：坏文件要在这里报 Validation，而不是写出半份素材
    state = core.load_state() or {}
    yesterday = (day - timedelta(days=1)).isoformat()

    lines: list[str] = []
    lines.append(f"# 早报素材（{day.isoformat()} 生成，供写「汇报」）")
    lines.append("")
    lines.append(f"##  Yesterday = {yesterday}")
    entries = _yesterday_entries(state, yesterday)
    if entries:
        lines.extend(_render_entries(entries))
    else:
        lines.append("  （昨日没有任何执行记录）")

    lines.append("")
    lines.append("## Today 待办（tasks.md 的 Today 段，未完成）")
    tasks = brief._tasks_today()
    if tasks:
        for t in tasks:
            lines.append(f"  - {t}")
    else:
        lines.append("  （今天没有未完成的待办）")

    notes_path = core.WORKSPACE / "state" / "notes" / f"{yesterday}.md"
    lines.append("")
    lines.append(f"## 用户口述（state/notes/{yesterday}.md，可为空）")
    if notes_path.exists():
        text = notes_path.read_text(encoding="utf-8").strip()
        lines.append(text if text else "  （文件存在但为空）")
    else:
        lines.append("  （没有当天备注）")

    lines.append("")
    lines.append("## 写入要求")
    lines.append(f"  把「汇报」正文写到 state/reports/{day.isoformat()}.md，正文首行为")
    lines.append(f"  `📊 昨日汇报 — {yesterday}`；段序固定：✅ 已完成 / ❌ 未完成 / 📝 备注 / 💬 遗留建议。")
    lines.append("  写不出就什么都别写——9:10 的兜底窗口会发静态汇报，绝不影响你收到。")
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="生成早报素材（注入模型 prompt）")
    p.add_argument("--workspace", required=True)
    p.add_argument("--date", help="覆盖「今天」，默认取系统当天（测试用）")
    args = p.parse_args(argv)

    try:
        day = date.fromisoformat(args.date) if args.date else datetime.now().date()
    except ValueError:
        print(f"rtc_report: 日期看不懂：{args.date}（要 YYYY-MM-DD）", file=sys.stderr)
        return 3

    try:
        for line in build_material(Path(args.workspace), day):
            print(line)
    except core.Validation as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
