"""多盘本地备份与 git 归档。

备份：把档案压成 zip 写到其它本地盘，每盘保留若干份；归档：把档案按天提交进私人
git 仓库并推送（GitHub / Gitee / 自建都行）。被 core 的 plan / main 调用。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import rtc_core as core

# ---------------------------------------------------------------- 多盘本地备份
# 每日快照是永久档案，但只存在一块盘上——那块盘坏了，一年后什么都查不到。
# 这里每天把它们压成一个 zip 写到另外的本地盘（不写源盘，同盘不算备份），
# 按天数滚动删除；成功时完全静默，失败才说话。

BACKUP_CFG = "rules/backup.json"

BACKUP_MARK = "state/.backup-last.json"

BACKUP_GLOB = "RTC-*.zip"

BACKUP_KEEP = 30

BACKUP_FILES = (
    "rules/routines.json",
    "rules/backup.json",
    "state/alarms.json",
    "state/routine-seal.json",
    "state/metrics.sql",
    "state/metrics.db",
    "AGENTS.md",
    "tasks.md",
)

# state/backups 只认程序自己写的三类；模型/人随手 cp 的 routines.<时间>.json 不进归档
BACKUP_ROUTINE_PATTERNS = (
    "routines.program-last.json",
    "routines.handedit-*.json",
    "routines.corrupt-*.json",
)
# tasks.md 的底稿：同样只认程序写的（tasks.<时间戳>.md）
TASKS_BACKUP_PATTERN = "tasks.*.md"


def backup_config() -> tuple[list[Path], int]:
    """只认 rules/backup.json —— 不自己扫盘。


    自动扫盘会在别人的机器上悄悄往不认识的盘写文件；测试也会因此污染真实磁盘。
    目标盘由安装器（install.py）一次性写好，用户随时可改。
    """
    cfg = core.WORKSPACE / BACKUP_CFG
    targets: list[Path] = []
    keep = BACKUP_KEEP
    if cfg.exists():
        try:
            data = core.load_json(cfg)
            if isinstance(data, dict):
                raw = data.get("targets") or []
                if isinstance(raw, list):
                    targets = [Path(str(t)) for t in raw if str(t).strip()]
                # 可选异地层：cloud / nas 是单个路径（本地挂载点），填了才加。
                # 不绑定厂商——云盘/NAS 在你机器上就是个目录，主流程一视同仁。
                for layer in ("cloud", "nas"):
                    val = data.get(layer)
                    if isinstance(val, str) and val.strip():
                        p = Path(val.strip())
                        if p not in targets:
                            targets.append(p)
                if isinstance(data.get("keep"), int) and data["keep"] > 0:
                    keep = int(data["keep"])
        except (core.Validation, ValueError, TypeError, OSError):
            pass
    return targets, keep


def snapshot_tasks(keep: int | None = None) -> Path | None:
    """给 tasks.md 留一份底稿；内容没变就什么都不做。

    tasks.md 是唯一没有程序守护的活文件：routines 有印章和底稿、alarms 有 rev
    校验和原子写、schedule 有每日快照，只有它靠编辑器直接改。写坏了就是真丢。
    """
    src = core.WORKSPACE / "tasks.md"
    if not src.is_file():
        return None
    content = src.read_bytes()
    backups = core.WORKSPACE / "state" / "backups"
    existing = sorted(backups.glob("tasks.*.md"))
    if existing and existing[-1].read_bytes() == content:
        return None
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    path = backups / f"tasks.{stamp}.md"
    n = 2
    while path.exists():  # 同一秒里改两次：加序号，别覆盖上一份
        path = backups / f"tasks.{stamp}-{n}.md"
        n += 1
    path.write_bytes(content)
    if keep is None:
        _, keep = backup_config()
    for old in sorted(backups.glob("tasks.*.md"))[:-keep] if keep > 0 else []:
        with contextlib.suppress(OSError):
            old.unlink()
    return path


def cmd_touch_tasks(args: argparse.Namespace) -> int:
    path = snapshot_tasks()
    if path is None:
        print("tasks: 没有 tasks.md，或内容和上一份底稿一样——不留新底稿")
        return 0
    print(f"tasks: 已留底稿 state/backups/{path.name}")
    return 0


def archive_members() -> list[tuple[Path, str]]:
    """归档要带哪些文件。

    `state/backups` 只认程序自己写的那三类（底稿 / 手改证据 / 坏件），
    模型或人随手 `cp` 出来的 `routines.<时间>.json` 不进归档——否则翻历史时
    会看到两份 routines，一份 10 个窗口一份 7 个，没人分得清哪份是真的。
    """
    members: list[tuple[Path, str]] = []
    for rel in BACKUP_FILES:
        p = core.WORKSPACE / rel
        if p.is_file():
            members.append((p, rel))
    schedule = core.WORKSPACE / "state" / "schedule"
    if schedule.is_dir():
        for f in sorted(schedule.glob("*.json")):
            members.append((f, f"state/schedule/{f.name}"))
    backups = core.WORKSPACE / "state" / "backups"
    if backups.is_dir():
        seen: set[Path] = set()
        for pat in BACKUP_ROUTINE_PATTERNS + (TASKS_BACKUP_PATTERN,):
            for f in sorted(backups.glob(pat)):
                if f in seen:
                    continue
                seen.add(f)
                members.append((f, f"state/backups/{f.name}"))
    return members


def backup_now(targets: list[Path], keep: int, day: str) -> tuple[list[Path], list[Path], list[str]]:
    """把工作区档案压成一份 zip 写到每个目标盘。

    返回（本次新写的, 今天已经在那儿、沿用的, 错误列表）。
    两类分开报：沿用的那一份可能是别人写进来的，说成"刚写好"就是在骗用户。
    """
    members = archive_members()
    if not members:
        return [], [], ["工作区里没有可备份的档案（还没生成过数据？）"]

    written: list[Path] = []
    existing: list[Path] = []
    errors: list[str] = []
    for t in targets:
        dst = t / f"RTC-{day.replace('-', '')}.zip"
        if dst.exists():
            existing.append(dst)
            continue
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            t.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for src, arc in members:
                    z.write(src, arc)
            with zipfile.ZipFile(tmp) as z:  # 回读，让 zip 自己验 CRC
                if z.testzip() is not None:
                    raise OSError(f"回读校验失败：{dst}")
                if len(z.namelist()) != len(members):
                    raise OSError(f"条目数不符：{len(z.namelist())}/{len(members)}")
            os.replace(tmp, dst)
            written.append(dst)
        except (OSError, ValueError) as e:
            errors.append(f"{t} 写入失败：{e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            continue

    # 每个目标盘只留最近 keep 份
    for t in targets:
        try:
            files = sorted(t.glob(BACKUP_GLOB))
            for old in files[:-keep] if keep > 0 else []:
                old.unlink()
        except OSError:
            pass
    return written, existing, errors


def record_mark(day: str, at: datetime, written: int, git: bool | None, errors: list[str]) -> None:
    """把「这次备份到底做了什么」落到 state/.backup-last.json。

    status 那一行读的就是这里，所以**手动 backup 也必须写**——否则用户 15:09 跑完，
    status 还在报凌晨那次，等于告诉他"没跑过"。
    `git` 用 None 表示"这一趟没碰 git"，和"推了但失败"要分开。
    """
    mark = core.WORKSPACE / BACKUP_MARK
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(
            json.dumps(
                {
                    "date": day,
                    "at": at.isoformat(timespec="seconds"),
                    "written": written,
                    "git": git,
                    "errors": errors,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def daily_backup(now: datetime) -> list[str]:
    """每天跑一次（由 plan 调）：本地多盘 zip + git 私人仓库，两种都配了就都做。

    成功静默，失败给一行话。
    """
    today = now.date().isoformat()
    mark = core.WORKSPACE / BACKUP_MARK
    try:
        if mark.exists() and (core.load_json(mark) or {}).get("date") == today:
            return []
    except (core.Validation, ValueError, TypeError, OSError):
        pass

    targets, keep = backup_config()
    has_git = bool(git_archive_config())
    if not targets and not has_git:
        return []  # 一样都没配，静默；手动 backup / gitarchive 会明确告诉你

    written: list[Path] = []
    existing: list[Path] = []
    errors: list[str] = []
    if targets:
        written, existing, errors = backup_now(targets, keep, today)
    did_git = False
    if has_git:
        did_git, git_errors = git_archive(now)
        errors = list(errors) + [f"git: {e}" for e in git_errors]

    record_mark(today, now, len(written) + len(existing), did_git if has_git else None, errors)
    if errors:
        return [f"⚠️ 备份部分失败：{'；'.join(errors[:3])}"]
    return []


def cmd_backup(args: argparse.Namespace) -> int:
    today = date.today().isoformat()
    targets, keep = backup_config()
    if args.to:
        targets = [Path(p) for p in args.to]
    if args.keep:
        keep = args.keep
    if not targets:
        print("backup: 没找到可写的第二块盘；用 --to 指定一个目录", file=sys.stderr)
        return 3
    written, existing, errors = backup_now(targets, keep, today)
    for w in written:
        size = w.stat().st_size / 1024
        print(f"backup: {w}  {size:.1f} KB")
    for w in existing:
        size = w.stat().st_size / 1024
        print(f"backup: {w}  {size:.1f} KB（今天已存在，未覆盖）")
    for e in errors:
        print(f"backup: {e}", file=sys.stderr)
    # 手动备份同样要记账：status 那一行读的就是这份记录
    record_mark(today, datetime.now(), len(written) + len(existing), None, errors)
    if not written and not existing:
        return 3
    return 0


# ---------------------------------------------------------------- git 存档
# 压缩包只防「盘坏」，不防「机器没了」。这条路把档案按天提交进一个私人 git 仓库，
# 远端随便写：GitHub / Gitee / GitLab / 自建 / 本地裸库都行（URL 或路径原样交给 git）。


def git_archive_config() -> dict[str, Any]:
    """读 rules/backup.json 里的 git 段。没有就返回空 dict（=不启用）。"""
    cfg = core.WORKSPACE / BACKUP_CFG
    if not cfg.exists():
        return {}
    try:
        data = core.load_json(cfg)
    except (core.Validation, ValueError, TypeError, OSError):
        return {}
    g = (data or {}).get("git")
    return g if isinstance(g, dict) else {}


def _git(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            [
                "git",
                "-c",
                "user.name=RTC",
                "-c",
                "user.email=rtc@localhost",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"git 调用失败：{e}"


def git_archive(now: datetime) -> tuple[bool, list[str]]:
    """把今天的档案复制进本地镜像仓库并推送到远端。

    返回 (有没有做事, 错误行)。没有配置 / 没有变化 都算「没做事」，静默。
    """
    g = git_archive_config()
    if not g:
        return False, []

    repo = Path(g.get("dir") or (Path.home() / "RTC-archive"))
    branch = str(g.get("branch") or "main")
    remotes: list[str] = []
    if isinstance(g.get("remotes"), list):
        remotes = [str(x) for x in g["remotes"] if str(x).strip()]
    elif str(g.get("remote") or "").strip():
        remotes = [str(g["remote"]).strip()]
    remotes = remotes or ["origin"]

    if _git(["--version"], Path.cwd())[0] != 0:
        return True, ["git 不可用：先装 git 再来（装好后重跑一次即自动补推）"]

    errors: list[str] = []
    try:
        repo.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return True, [f"git 存档目录建不出来（{repo}）：{e}"]

    if not (repo / ".git").exists():
        rc, out = _git(["init"], repo)
        if rc != 0:
            return True, [f"git init 失败：{out}"]
        _git(["branch", "-M", branch], repo)

    today = now.date().isoformat()
    day_dir = repo / "archive" / today
    members = archive_members()
    if not members:
        return False, []

    try:
        for src, rel in members:
            dst = day_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    except OSError as e:
        return True, [f"复制档案进 git 目录失败：{e}"]

    rc, out = _git(["add", "-A"], repo)
    if rc != 0:
        return True, [f"git add 失败：{out}"]
    rc, out = _git(["status", "--porcelain"], repo)
    if rc != 0:
        return True, [f"git status 失败：{out}"]
    if not out.strip():
        return False, []  # 今天已经存过，且内容没变

    rc, out = _git(["commit", "-m", f"archive {today}"], repo)
    if rc != 0:
        return True, [f"git commit 失败：{out}"]

    for remote in remotes:
        target = remote
        if remote not in ("origin",) and (
            "://" in remote
            or remote.startswith("git@")
            or remote.startswith("ssh:")
            or remote.endswith(".git")
        ):
            rc, out = _git(["remote", "add", "origin", remote], repo)
            if rc != 0:  # 已经有 origin 了，改掉
                _git(["remote", "set-url", "origin", remote], repo)
            target = "origin"
        rc, out = _git(["push", target, f"HEAD:{branch}"], repo)
        if rc != 0:
            # 远端被别的机器推过（或 force-push 过）：先 rebase 再试一次
            _git(["pull", "--rebase", target, branch], repo)
            rc2, out2 = _git(["push", target, f"HEAD:{branch}"], repo)
            if rc2 != 0:
                errors.append(f"推送到 {remote} 失败：{out2[-300:] or out[-300:]}")
    return True, errors


def cmd_gitarchive(args: argparse.Namespace) -> int:
    g = git_archive_config()
    if args.dir or args.remote:
        g = dict(g)
        if args.dir:
            g["dir"] = args.dir
        if args.remote:
            g["remotes"] = [args.remote]
    if not g:
        print(
            'gitarchive: 还没配置。在 rules/backup.json 里写 "git": '
            '{"dir": "...", "remotes": ["git@github.com:you/rtc-data.git"]}',
            file=sys.stderr,
        )
        return 3
    cfg = core.WORKSPACE / BACKUP_CFG
    data: dict[str, Any] = {}
    if cfg.exists():
        try:
            loaded = core.load_json(cfg)
            data = loaded if isinstance(loaded, dict) else {}
        except (core.Validation, ValueError, TypeError, OSError):
            data = {}
    data["git"] = g
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"gitarchive: 配置写不进去：{e}", file=sys.stderr)
        return 3

    did, errors = git_archive(datetime.now().astimezone())
    for msg in errors:
        print(f"gitarchive: {msg}", file=sys.stderr)
    if errors:
        return 3
    print(f"gitarchive: {'已提交并推送' if did else '内容没变化，跳过'}")
    return 0
