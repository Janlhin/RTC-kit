#!/usr/bin/env python3
"""RTC-kit 安装接地器（install grounding）。

把 RTC-kit 接到一个 Harness（如 Nous Hermes Agent）上，让模型从安装起就拿到
"真实路径 + 命令接口"，而不是去猜该改哪个文件、或手改 JSON。

职责：
  1. 把 code/rtc.py 与三个入口脚本拷进 Harness 的 scripts 目录；
  2. 读取 skills/alarm-clock/SKILL.md（带 {{...}} 占位符的模板），把
     {{CODE_DIR}} / {{WORKSPACE_DIR}} / {{STATE_DIR}} 替换成真实绝对路径，
     写到 Harness 的 skills 目录；
  3. 若工作区还没有 rules/routines.json，用一份内置的、无占位符的最小默认配置
     生成可运行的初始工作区；
  4. 打印（或可选执行）两条 hermes cron 命令（rtc-plan / rtc-due）。

用法：
  python install.py --workspace C:/path/to/rtc-workspace
                   [--harness-scripts-dir DIR] [--harness-skills-dir DIR]
                   [--code-src DIR] [--tz-offset "+08:00"]
                   [--dry-run] [--setup-cron]

说明：
  - 本脚本只用标准库，跨平台。
  - --dry-run 只打印将要做什么，不写任何文件。
  - --setup-cron 会尝试调用 "hermes cron create"；需要网关在跑且已配对渠道，
    默认不执行（只打印命令供手动运行）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_OFFSET = "+08:00"

DEFAULT_ROUTINE = {
    "version": 1,
    "timezone": DEFAULT_TZ,
    "tzOffset": DEFAULT_OFFSET,
    "dateTypes": {
        "workday": [1, 2, 3, 4, 5],
        "weekend": [0, 6],
        "holidayOverrides": [{"date": "2030-01-01", "type": "holiday", "note": "示例：新年"}],
    },
    "alarmWindows": [
        {"id": "wake", "label": "起床", "start": "08:10", "end": "08:40",
         "intervalMin": 5, "expectFires": 7, "ackHints": ["起了", "好了"]},
        {"id": "meds-am", "label": "例行事项 A", "start": "09:00", "end": "09:25",
         "intervalMin": 5, "expectFires": 6, "ackHints": ["好了", "办了"]},
        {"id": "meds-noon", "label": "例行事项 B", "start": "12:00", "end": "12:25",
         "intervalMin": 5, "expectFires": 6, "ackHints": ["好了", "办了"]},
        {"id": "meds-pm", "label": "例行事项 C", "start": "19:00", "end": "19:25",
         "intervalMin": 5, "expectFires": 6, "ackHints": ["好了", "办了"]},
    ],
    "rollcalls": [
        {"hour": 9, "minute": 0, "kindByDateType": {"workday": "report", "weekend": "none", "holiday": "none"}},
        {"hour": 20, "minute": 0, "kindByDateType": {"workday": "summary", "weekend": "summary", "holiday": "summary"}},
    ],
    "phases": [
        {"name": "active", "start": "08:00", "end": "23:00", "proactiveAllowed": True},
        {"name": "quiet", "start": "23:00", "end": "08:00", "proactiveAllowed": False, "exception": "仅 P0"},
    ],
    "weeklyChores": [
        {"dayOfWeek": 6, "label": "周六家务", "attachToRollcallHour": 14},
        {"dayOfWeek": 0, "label": "周日家务", "attachToRollcallHour": 14},
    ],
}


def default_scripts_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "hermes" / "scripts"
    return Path.home() / ".hermes" / "scripts"


def default_skills_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "hermes" / "skills"
    return Path.home() / ".hermes" / "skills"


def _placeholders_for(code_dir: Path, workspace: Path) -> dict:
    """路径类占位符 → 真实绝对路径。都是「由部署决定」的值，安装时确定性填死。"""
    return {
        "{{CODE_DIR}}": str(code_dir),
        "{{WORKSPACE_DIR}}": str(workspace),
        "{{STATE_DIR}}": str(workspace / "state"),
        "{{RULES_DIR}}": str(workspace / "rules"),
    }


def _normalize_separators(text: str, roots: list) -> str:
    """把 Windows 绝对路径里残留的正斜杠统一成 ``os.sep``。

    模板写作 ``{{CODE_DIR}}/rtc.py``，只做字符串替换会拼出
    ``C:\\Users\\...\\scripts/rtc.py`` 这种混斜线路径。多数情况下还能跑，
    但在 cmd、某些工具和日志里会断，也会让人误以为路径拼错了。
    """
    if os.sep == "/":
        return text
    for root in roots:
        text = re.sub(
            re.escape(str(root)) + r"[^ \t\n`\"'）|，。；]*",
            lambda m: m.group(0).replace("/", os.sep),
            text,
        )
    return text


def substitute_skill(template: str, code_dir: Path, workspace: Path) -> str:
    """替换路径占位符，产出「可直接照着跑」的现场文本（不留混斜线）。"""
    values = _placeholders_for(code_dir, workspace)
    # 先替「占位符 + 斜杠」，再替裸占位符——否则 `{{X}}/sub` 会变成 `dir/sub`
    for key, val in values.items():
        template = template.replace(key + "/", val.rstrip("/") + os.sep)
    for key, val in values.items():
        template = template.replace(key, val)
    return _normalize_separators(template, list(values.values()))


def remaining_placeholders(text: str) -> list:
    """还剩哪些 ``{{XXX}}``。

    路径类占位符已由本脚本填死；剩下的都是 `CONTRIBUTING.md` 里「由部署 / 用户
    决定」的填充项（时区、作息时刻、项目名……），安装器不猜，只提示。
    """
    return sorted(set(re.findall(r"\{\{[A-Z][A-Z0-9_]*\}\}", text)))


def install_skills(repo: Path, skills_dir: Path, scripts_dir: Path,
                   workspace: Path, dry_run: bool) -> list:
    """把仓库里**所有**带 SKILL.md 的技能接地后写进 Harness。

    只装 alarm-clock 是不够的：daily-routine / daily-task-prep / daily-task-manager /
    deletion-gate 同样引用真实路径，漏装等于让模型手上只有一份能用的说明书。
    """
    installed = []
    for tpl in sorted((repo / "skills").glob("*/SKILL.md")):
        name = tpl.parent.name
        text = substitute_skill(tpl.read_text(encoding="utf-8"), scripts_dir, workspace)
        leftovers = remaining_placeholders(text)
        dst = skills_dir / name / "SKILL.md"
        if dry_run:
            print(f"[install] 将写入技能 {dst}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
            print(f"[install] 已写入技能 {name}")
        if leftovers:
            print(f"[install] 提示：{name} 里还有需要人工填的占位符：{'、'.join(leftovers)}")
        installed.append(name)
    return installed


def run(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parent
    code_src = Path(args.code_src) if args.code_src else (repo / "code")
    scripts_dir = Path(args.harness_scripts_dir) if args.harness_scripts_dir else default_scripts_dir()
    skills_dir = Path(args.harness_skills_dir) if args.harness_skills_dir else default_skills_dir()
    workspace = Path(args.workspace)
    rules_dir = workspace / "rules"
    routine_path = rules_dir / "routines.json"

    print(f"[install] repo          = {repo}")
    print(f"[install] code_src       = {code_src}")
    print(f"[install] scripts_dir    = {scripts_dir}")
    print(f"[install] skills_dir     = {skills_dir}")
    print(f"[install] workspace      = {workspace}")

    if args.dry_run:
        print("[install] --dry-run：以下动作不会真正写入")
    else:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        skills_dir.mkdir(parents=True, exist_ok=True)
        rules_dir.mkdir(parents=True, exist_ok=True)
        (workspace / "state").mkdir(parents=True, exist_ok=True)

    to_copy = ["rtc.py", "rtc_plan.py", "rtc_due.py", "rtc_once.py"]
    for name in to_copy:
        src = code_src / name
        if not src.is_file():
            print(f"[install] 跳过（源码缺失）：{src}", file=sys.stderr)
            continue
        dst = scripts_dir / name
        if args.dry_run:
            print(f"[install] 将拷贝 {src} -> {dst}")
        else:
            shutil.copyfile(src, dst)
            print(f"[install] 已拷贝 {name}")

    tpl_root = repo / "skills"
    if tpl_root.is_dir():
        names = install_skills(repo, skills_dir, scripts_dir, workspace, args.dry_run)
        print(f"[install] 技能共 {len(names)} 个：{'、'.join(names)}")
    else:
        print(f"[install] 找不到技能模板目录：{tpl_root}", file=sys.stderr)

    if routine_path.exists():
        print(f"[install] 工作区已存在配置，跳过脚手架：{routine_path}")
    else:
        if args.dry_run:
            print(f"[install] 将生成默认 routines.json -> {routine_path}")
        else:
            routine_path.write_text(
                json.dumps(DEFAULT_ROUTINE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[install] 已生成默认 routines.json（请按需用 set-time/postpone 调整）")

    print("\n[install] 两条 cron（网关运行时执行；或加 --setup-cron 让本脚本执行）：")
    cmd_plan = (f'hermes cron create "0 6 * * *" --name rtc-plan --no-agent '
                f'--script rtc_plan.py --deliver <target> --failure-deliver <target> '
                f'--catch-up on --timezone {args.tz_offset}')
    cmd_due = (f'hermes cron create "*/5 * * * *" --name rtc-due --no-agent '
               f'--script rtc_due.py --deliver <target> --failure-deliver <target> '
               f'--catch-up off --timezone {args.tz_offset}')
    print(f"  {cmd_plan}")
    print(f"  {cmd_due}")
    print("  说明：<target> 换成你的投递渠道（如微信 iLink bot）；首次部署先 --deliver local 观察两天。")

    if args.setup_cron and not args.dry_run:
        for cmd in (cmd_plan, cmd_due):
            print(f"[install] 执行：{cmd}")
            rc = subprocess.run(cmd, shell=True).returncode
            if rc != 0:
                print(f"[install] 命令返回 {rc}，请检查网关与权限", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RTC-kit 安装接地器")
    ap.add_argument("--workspace", required=True, help="RTC 工作区目录（rules/routines.json 将在此）")
    ap.add_argument("--harness-scripts-dir", default=None, help="Harness 的 scripts 目录（默认 ~/.hermes/scripts）")
    ap.add_argument("--harness-skills-dir", default=None, help="Harness 的 skills 目录（默认 ~/.hermes/skills）")
    ap.add_argument("--code-src", default=None, help="源码 code/ 目录（默认脚本所在仓库的 code/）")
    ap.add_argument("--tz-offset", default=DEFAULT_OFFSET, help="工作区时区偏移，默认 +08:00")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--setup-cron", action="store_true", help="尝试执行 hermes cron create（需网关在跑）")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
