#!/usr/bin/env python3
"""RTC-kit 安装接地器（install grounding）。

把 RTC-kit 接到一个 Harness（如 Nous Hermes Agent）上，让模型从安装起就拿到
"真实路径 + 命令接口"，而不是去猜该改哪个文件、或手改 JSON。

用户视角只有一条命令：装完之后，两分钟内会在他的渠道里收到一条测试提醒。

职责（按 run() 里的五个步骤）：
  1. 把 code/rtc.py 与三个入口脚本拷进 Harness 的 scripts 目录（覆盖，幂等）；
  2. 把仓库里所有技能的 {{...}} 占位符填死，写进 Harness 的 skills 目录；
  3. 搭工作区骨架：routines.json / AGENTS.md / tasks.md / errors.md
     —— **已存在的文件一律不覆盖**（除非 --force），因为那是用户的真实数据；
  4. （可选 --with-host）把工作区登记成本宿主的一个项目，避免会话在别处找文件；
  5. （可选 --smoke）注入一条两分钟后的一次性提醒，轮询确认它真的触发了。

用法：
  python install.py --workspace C:/path/to/rtc-workspace [--smoke] [--with-host]
                   [--harness-scripts-dir DIR] [--harness-skills-dir DIR]
                   [--code-src DIR] [--tz-offset "+08:00"]
                   [--dry-run] [--force] [--setup-cron]

说明：
  - 本脚本只用标准库，跨平台。
  - --dry-run 只打印将要做什么，不写任何文件。
  - --setup-cron 会尝试调用 "hermes cron create"；需要网关在跑且已配对渠道，
    默认不执行（只打印命令供手动运行）。
  - --smoke 需要宿主的定时器在跑（Hermes 即网关 + 两条 cron）。超时不等于装坏，
    只是"提醒响了没"这件事无法在本次安装里被证实。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_OFFSET = "+08:00"
SMOKE_LABEL = "【安装自检】链路测试"
SMOKE_LEAD_MIN = 2  # 自检提醒定在「现在 + 2 分钟」
SMOKE_WINDOW_MIN = 4  # 窗口宽 4 分钟：无论当前是第几分钟，都必然覆盖到一个 5 分钟刻度
SMOKE_DEFAULT_WAIT = 330

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
        {
            "id": "wake",
            "label": "起床",
            "start": "08:10",
            "end": "08:40",
            "intervalMin": 5,
            "expectFires": 7,
            "ackHints": ["起了", "好了"],
        },
        {
            "id": "meds-am",
            "label": "早间事项",
            "start": "09:00",
            "end": "09:25",
            "intervalMin": 5,
            "expectFires": 6,
            "ackHints": ["好了", "办了"],
        },
        {
            "id": "meds-noon",
            "label": "午间事项",
            "start": "12:00",
            "end": "12:25",
            "intervalMin": 5,
            "expectFires": 6,
            "ackHints": ["好了", "办了"],
        },
        {
            "id": "meds-pm",
            "label": "晚间事项",
            "start": "19:00",
            "end": "19:25",
            "intervalMin": 5,
            "expectFires": 6,
            "ackHints": ["好了", "办了"],
        },
    ],
    "rollcalls": [
        {
            "hour": 9,
            "minute": 0,
            "kindByDateType": {"workday": "report", "weekend": "none", "holiday": "none"},
        },
        {
            "hour": 20,
            "minute": 0,
            "kindByDateType": {"workday": "summary", "weekend": "summary", "holiday": "summary"},
        },
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

ERRORS_HEADER = """# errors.md — 出过什么错

只追加，不改旧行。每条形如：

```
YYYY-MM-DD HH:MM  场景 → 后果 → 下次怎么做
```

"""


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


def _ensure_utf8_stdio() -> None:
    """控制台编码兜底。

    GitHub Actions 的 Windows runner 控制台是 cp1252，打印中文会直接抛
    UnicodeEncodeError——CI 第一次跑就死在一行 `print("[install] --dry-run：…")`。
    只在当前编码表示不了中文时才切 UTF-8；中文 Windows 的 cp936 控制台保持原样，
    免得把它本来正常的显示改成乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if not enc:
            continue
        try:
            "安装".encode(enc)
            continue
        except (UnicodeEncodeError, LookupError):
            pass
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


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

    路径类占位符已由本脚本填死；剩下的都是 `docs/contributing.md` 里「由部署 / 用户
    决定」的填充项（时区、作息时刻、项目名……），安装器不猜，只提示。
    """
    return sorted(set(re.findall(r"\{\{[A-Z][A-Z0-9_]*\}\}", text)))


def install_skills(
    repo: Path, skills_dir: Path, scripts_dir: Path, workspace: Path, dry_run: bool, category: str = "rtc"
) -> list:
    """把仓库里**所有**带 SKILL.md 的技能接地后写进 Harness。

    只装 alarm-clock 是不够的：daily-routine / daily-task-prep / daily-task-manager /
    deletion-gate 同样引用真实路径，漏装等于让模型手上只有一份能用的说明书。

    目录形状必须是 ``<skills>/<category>/<skill>/SKILL.md``：Hermes 按**父目录**识别
    技能归属（现有技能全都长成 ``productivity/pdf/SKILL.md``）。以前平铺写
    ``<skills>/<skill>/SKILL.md``，文件都在，宿主却看不见——这就是 D-01。
    """
    installed = []
    for tpl in sorted((repo / "skills").glob("*/SKILL.md")):
        name = tpl.parent.name
        text = substitute_skill(tpl.read_text(encoding="utf-8"), scripts_dir, workspace)
        leftovers = remaining_placeholders(text)
        dst = (skills_dir / category / name / "SKILL.md") if category else (skills_dir / name / "SKILL.md")
        if dry_run:
            print(f"[install] 将写入技能 {dst}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
            print(f"[install] 已写入技能 {category + '/' if category else ''}{name}")
        if leftovers:
            shown = "、".join(leftovers[:3])
            more = f" 等 {len(leftovers)} 处" if len(leftovers) > 3 else ""
            print(f"[install] 提示：{name} 有需按你作息填的占位符：{shown}{more}（装完再改，不影响提醒本身）")
        installed.append(name)
    return installed


def default_routine() -> dict:
    """开箱默认时刻表。**不是空白模板**——装完就能响，用户第一分钟能看到效果。"""
    return copy.deepcopy(DEFAULT_ROUTINE)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup_targets_for(workspace: Path) -> list:
    """除源盘以外的固定盘，各给一个备份目录。POSIX 上没有这个概念，返回空。"""
    src = workspace.drive.upper() if workspace.drive else ""
    out = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if root.exists() and root.drive.upper() != src:
                out.append(str(root / "RTC-Backup"))
        except OSError:
            continue
    return out


def scaffold_workspace(
    repo: Path, code_dir: Path, workspace: Path, dry_run: bool, force: bool = False
) -> dict:
    """搭工作区骨架。已存在的**用户文件**一律不覆盖（除非 force）。

    返回 {相对路径: "created" | "skipped" | "planned" | "overwritten"}，供打印与测试。

    为什么不能覆盖：`routines.json` / `tasks.md` / `AGENTS.md` 里是用户自己的
    作息与承诺，重装一次把它们清空，等于把用户的日程丢了——比装不上更糟。
    """
    files = {
        "rules/routines.json": json.dumps(default_routine(), ensure_ascii=False, indent=2) + "\n",
        "rules/backup.json": json.dumps(
            {"targets": backup_targets_for(workspace), "keep": 30}, ensure_ascii=False, indent=2
        )
        + "\n",
        "errors.md": ERRORS_HEADER,
        "tasks.md": "# tasks.md — 今天要做什么\n\n## Today\n\n### 我\n\n"
        "## 待安排（有日期）\n\n## 待安排（没日期）\n",
        "AGENTS.md": "",
    }

    agents_tpl = repo / "templates" / "AGENTS.example.md"
    tasks_tpl = repo / "templates" / "tasks.example.md"
    if agents_tpl.is_file():
        files["AGENTS.md"] = substitute_skill(agents_tpl.read_text(encoding="utf-8"), code_dir, workspace)
    if tasks_tpl.is_file():
        files["tasks.md"] = substitute_skill(tasks_tpl.read_text(encoding="utf-8"), code_dir, workspace)

    result = {}
    for rel, text in files.items():
        dest = workspace / rel
        if dest.exists() and not force:
            result[rel] = "skipped"
            continue
        if dry_run:
            result[rel] = "planned"
            continue
        existed = dest.exists()
        _write_text(dest, text)
        result[rel] = "overwritten" if existed else "created"
    return result


def smoke_target(now: datetime) -> str:
    """自检提醒的触发时刻 = 现在 + 2 分钟（HH:MM）。纯函数，便于测试。"""
    return (now + timedelta(minutes=SMOKE_LEAD_MIN)).strftime("%H:%M")


def timetable_summary(routine: dict) -> dict:
    """时刻表体检：日常窗口几个、一次性几个。纯函数，便于测试。"""
    total = 0
    one_off = 0
    for w in routine.get("alarmWindows") or []:
        total += 1
        if w.get("oneOff") or str(w.get("id") or "").startswith("once-"):
            one_off += 1
    return {"total": total, "recurring": total - one_off, "oneOff": one_off}


def smoke_blocked_reason(workspace: Path) -> str | None:
    """表里一个日常窗口都没有时，自检没有意义：一次性能响，明天照样不排。

    自检只证明「这一条能投递到手机」，用户真正怕的是「明天的还响不响」。
    所以日常窗口为 0 时直接判失败，而不是给一个虚假的绿灯。
    """
    routine_path = workspace / "rules" / "routines.json"
    if not routine_path.is_file():
        return "找不到 rules/routines.json，无法判断时刻表"
    try:
        routine = json.loads(routine_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"rules/routines.json 读不出来（{e}），无法判断时刻表"
    summary = timetable_summary(routine)
    if summary["recurring"] == 0:
        return (
            f"时刻表里没有日常窗口（一次性 {summary['oneOff']} 条）——"
            "自检会绿，但明天不会自动排；先把日常窗口补回 rules/routines.md"
        )
    return None


def _run_quiet(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_smoke(scripts_dir: Path, workspace: Path, wait_sec: int = SMOKE_DEFAULT_WAIT) -> int:
    """注入一条一次性提醒并盯到它真的响。

    返回 0 = 已触发；1 = 超时（宿主定时器没跑）；2 = 连注入都失败。
    这一步是「用户第一分钟能否看到效果」的验收，而不是锦上添花。
    """
    once = scripts_dir / "rtc_once.py"
    rtc = scripts_dir / "rtc.py"
    if not once.is_file() or not rtc.is_file():
        print(f"[install] 自检跳过：找不到 {once.name} 或 {rtc.name}", file=sys.stderr)
        return 2
    blocked = smoke_blocked_reason(workspace)
    if blocked:
        print(f"[install] 自检判失败：{blocked}", file=sys.stderr)
        return 1

    now = datetime.now()
    at = smoke_target(now)
    py = sys.executable or "python"
    proc = _run_quiet(
        [
            py,
            str(once),
            at,
            SMOKE_LABEL,
            "--date",
            now.date().isoformat(),
            "--window-min",
            str(SMOKE_WINDOW_MIN),
            "--workspace",
            str(workspace),
        ]
    )
    if proc.returncode != 0:
        print("[install] 自检注入失败：" + (proc.stderr or proc.stdout or "").strip()[-300:], file=sys.stderr)
        return 2

    print(f"[install] 已注入自检提醒：{at}（约 {SMOKE_LEAD_MIN} 分钟后）")
    print(f"[install] 正在等宿主触发，最多 {wait_sec} 秒……")
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        status = _run_quiet([py, str(rtc), "--workspace", str(workspace), "status", "--json"])
        try:
            data = json.loads(status.stdout or "{}")
        except ValueError:
            data = {}
        for w in data.get("windows", []):
            if w.get("label") != SMOKE_LABEL:
                continue
            if w.get("state") == "delivered" or int(w.get("fireCount") or 0) >= 1:
                print(f"[install] 自检通过：{at} 那条提醒已触发（state={w.get('state')}）。")
                print("[install] 如果你在同一条渠道里收到了它，说明投递也通了。")
                print("[install] 这条自检提醒留在时刻表里，明天 plan 会自动清掉。")
                return 0
        time.sleep(10)

    print(f"[install] 自检超时：{wait_sec} 秒内没等到触发。", file=sys.stderr)
    print("[install] 接下来的排查顺序（不是装坏了，多半是宿主没在跑）：", file=sys.stderr)
    print("  1) 宿主网关在跑吗：hermes gateway status", file=sys.stderr)
    print(
        "  2) cron 建了吗：hermes cron list（应有 rtc-plan / rtc-due，启用早报后另有 rtc-report）",
        file=sys.stderr,
    )
    print(
        "  3) 直接手跑一次判断：python " + str(rtc) + " --workspace " + str(workspace) + " due",
        file=sys.stderr,
    )
    return 1


def register_host_project(workspace: Path, dry_run: bool, name: str = "rtc") -> int:
    """把工作区登记成本宿主的一个项目（当前只支持 Hermes）。

    为什么需要：宿主默认按自己的目录找文件，模型会在别处翻到模板，然后告诉用户
    「你没配置」。把工作区登记成项目，会话的 cwd 才会落在真实数据旁边。

    返回 0 = 已就绪/已存在；1 = 跳过（宿主不认识这个概念）。
    """
    exe = shutil.which("hermes")
    if not exe:
        print("[install] 宿主登记跳过：没找到 hermes 命令（非 Hermes 宿主可忽略）")
        return 1
    listed = _run_quiet([exe, "project", "list"])
    if name in (listed.stdout or ""):
        print(f"[install] 宿主项目已存在：{name}（跳过）")
        return 0
    cmd = [exe, "project", "create", name, str(workspace), "--use"]
    if dry_run:
        print("[install] 将执行：" + " ".join(cmd))
        return 0
    proc = _run_quiet(cmd)
    if proc.returncode != 0:
        print("[install] 宿主项目登记失败：" + (proc.stderr or proc.stdout or "").strip()[-300:])
        print("[install] 可手动执行：" + " ".join(cmd))
        return 1
    print(f"[install] 已登记宿主项目 {name} → {workspace}（并设为当前）")
    print("[install] 注意：会话工作目录要重启网关后才生效（hermes gateway restart）")
    return 0


def run(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parent
    code_src = Path(args.code_src) if args.code_src else (repo / "code")
    scripts_dir = Path(args.harness_scripts_dir) if args.harness_scripts_dir else default_scripts_dir()
    skills_dir = Path(args.harness_skills_dir) if args.harness_skills_dir else default_skills_dir()
    workspace = Path(args.workspace)
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))

    print(f"[install] repo          = {repo}")
    print(f"[install] code_src       = {code_src}")
    print(f"[install] scripts_dir    = {scripts_dir}")
    print(f"[install] skills_dir     = {skills_dir}")
    print(f"[install] workspace      = {workspace}")

    if dry_run:
        print("[install] --dry-run：以下动作不会真正写入")

    # 1/5 脚本
    print("\n[install] 1/5 安装入口脚本")
    if not dry_run:
        scripts_dir.mkdir(parents=True, exist_ok=True)
    to_copy = [
        "rtc.py",
        "rtc_core.py",
        "rtc_archive.py",
        "rtc_metrics.py",
        "rtc_brief.py",
        "rtc_history.py",
        "rtc_plan.py",
        "rtc_due.py",
        "rtc_once.py",
        "rtc_report.py",  # 早报素材（agent 模式 cron 注入 prompt，issue #60 方案 B）
        "metrics.mjs",  # 可选旁路统计；不拷会让技能里的 node metrics.mjs 调用摸空
    ]
    for fname in to_copy:
        src = code_src / fname
        if not src.is_file():
            print(f"[install] 跳过（源码缺失）：{src}", file=sys.stderr)
            continue
        dst = scripts_dir / fname
        if dry_run:
            print(f"[install] 将拷贝 {src} -> {dst}")
        else:
            shutil.copyfile(src, dst)
            print(f"[install] 已拷贝 {fname}")

    # 2/5 技能
    print("\n[install] 2/5 接地技能")
    tpl_root = repo / "skills"
    if tpl_root.is_dir():
        names = install_skills(
            repo, skills_dir, scripts_dir, workspace, dry_run, str(getattr(args, "skills_category", "rtc"))
        )
        print(f"[install] 技能共 {len(names)} 个：{'、'.join(names)}")
    else:
        print(f"[install] 找不到技能模板目录：{tpl_root}", file=sys.stderr)

    # 3/5 工作区骨架
    print("\n[install] 3/5 搭工作区骨架")
    scaffold = scaffold_workspace(repo, scripts_dir, workspace, dry_run, force)
    for rel in ("rules/routines.json", "rules/backup.json", "AGENTS.md", "tasks.md", "errors.md"):
        state = scaffold.get(rel, "?")
        label = {
            "created": "已生成",
            "skipped": "已存在，跳过（不动你的数据）",
            "planned": "将生成",
            "overwritten": "已覆盖（--force）",
        }.get(state, state)
        print(f"[install] {rel}: {label}")
    if scaffold.get("rules/backup.json") == "created":
        tgts = backup_targets_for(workspace)
        if tgts:
            print(f"[install] 每日备份目标：{'、'.join(tgts)}（每个盘留最近 30 份）")
        else:
            print(
                "[install] 未找到第二块本地盘：每日备份暂不启用。"
                "接一块盘后在 rules/backup.json 里的 targets 写上路径即可。"
            )
    if scaffold.get("rules/routines.json") == "created":
        print(
            "[install] 默认时刻表已就位：起床 08:10、早间 09:00、午间 12:00、晚间 19:00"
            "（用 set-time 改，别手改 JSON）"
        )
    if not dry_run:
        routine_path = workspace / "rules" / "routines.json"
        try:
            summary = timetable_summary(json.loads(routine_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            summary = None
        if summary and summary["recurring"] == 0:
            print(
                f"[install] ⚠ 时刻表只有一次性提醒（{summary['oneOff']} 条），没有日常窗口："
                "明天不会自动排，先按 rules/routines.md 补回日常窗口",
                file=sys.stderr,
            )

    # 4/5 宿主登记（可选）
    if getattr(args, "with_host", False):
        print("\n[install] 4/5 登记宿主项目")
        register_host_project(workspace, dry_run)
    else:
        print("\n[install] 4/5 宿主项目登记：未要求（加 --with-host 可让验收更顺）")

    # 5/5 自检（可选）
    smoke_rc = None
    if getattr(args, "smoke", False):
        print("\n[install] 5/5 链路自检")
        if dry_run:
            print(f"[install] 将注入 {smoke_target(datetime.now())} 的一次性提醒并等待触发")
        else:
            smoke_rc = run_smoke(scripts_dir, workspace, int(getattr(args, "smoke_wait", SMOKE_DEFAULT_WAIT)))
    else:
        print("\n[install] 5/5 链路自检：未要求（加 --smoke 可当场验证提醒真的会响）")

    # cron 命令
    print(
        "\n[install] 三条 cron（网关运行时执行；或加 --setup-cron 让本脚本执行）：\n"
        "  前两条是确定性链路（不调模型）；第三条是早报的模型生成，"
        "会突破「cron 恒定两条」的既有约束（issue #60 方案 B，需用户批准）。"
    )
    cmd_plan = (
        f'hermes cron create "0 6 * * *" --name rtc-plan --no-agent '
        f"--script rtc_plan.py --deliver <target> --failure-deliver <target> "
        f"--catch-up on --timezone {args.tz_offset}"
    )
    cmd_due = (
        f'hermes cron create "*/5 * * * *" --name rtc-due --no-agent '
        f"--script rtc_due.py --deliver <target> --failure-deliver <target> "
        f"--catch-up off --timezone {args.tz_offset}"
    )
    cmd_report = (
        f'hermes cron create "0 9 * * *" --name rtc-report '
        f"--script rtc_report.py --skill alarm-clock "
        f"--deliver <target> --failure-deliver <target> "
        f"--catch-up off --timezone {args.tz_offset}"
    )
    print(f"  {cmd_plan}")
    print(f"  {cmd_due}")
    print(f"  {cmd_report}")
    print("  说明：<target> 换成你的投递渠道（如微信 iLink bot）；首次部署先 --deliver local 观察两天。")
    print(
        "  早报兜底：时刻表里配 09:10 的 `brief-morning-fallback` 窗口（format: report-fallback），"
        "别再配 09:00 的 format=report 窗口（plan 会拒绝同时启用）。"
        "09:00 模型写完并投递后记一条 `delivery-record --window report --result ok`；"
        "有回执兜底静默，没回执就补发（有文件发文件正文，没文件发静态汇报）。"
    )

    if getattr(args, "setup_cron", False) and not dry_run:
        for cmd in (cmd_plan, cmd_due, cmd_report):
            print(f"[install] 执行：{cmd}")
            rc = subprocess.run(cmd, shell=True).returncode
            if rc != 0:
                print(f"[install] 命令返回 {rc}，请检查网关与权限", file=sys.stderr)

    if smoke_rc == 1:
        return 1
    if smoke_rc == 2:
        return 1
    return 0


def main() -> int:
    _ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="RTC-kit 安装接地器")
    ap.add_argument("--workspace", required=True, help="RTC 工作区目录（rules/routines.json 将在此）")
    ap.add_argument(
        "--harness-scripts-dir", default=None, help="Harness 的 scripts 目录（默认 ~/.hermes/scripts）"
    )
    ap.add_argument(
        "--harness-skills-dir", default=None, help="Harness 的 skills 目录（默认 ~/.hermes/skills）"
    )
    ap.add_argument(
        "--skills-category",
        default="rtc",
        help="技能写在 skills/<类别>/<技能>/ 下的类别名；宿主按父目录识别，默认 rtc；传空串则平铺",
    )
    ap.add_argument("--code-src", default=None, help="源码 code/ 目录（默认脚本所在仓库的 code/）")
    ap.add_argument("--tz-offset", default=DEFAULT_OFFSET, help="工作区时区偏移，默认 +08:00")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的工作区文件（会丢用户数据，慎用）")
    ap.add_argument("--with-host", action="store_true", help="把工作区登记成宿主项目（当前支持 Hermes）")
    ap.add_argument("--smoke", action="store_true", help="注入一条两分钟后的提醒并确认它真的触发了")
    ap.add_argument(
        "--smoke-wait",
        type=int,
        default=SMOKE_DEFAULT_WAIT,
        help=f"自检最多等多少秒（默认 {SMOKE_DEFAULT_WAIT}）",
    )
    ap.add_argument("--setup-cron", action="store_true", help="尝试执行 hermes cron create（需网关在跑）")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
