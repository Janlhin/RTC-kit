#!/usr/bin/env python3
"""RTC-kit 确定性核心：契约校验、状态写入、计划生成、到点判断与各命令。

设计要点（与 rtc.py 拆分前一致，逐字未改）：
  1. 不调用模型。判断"该不该响"是纯布尔逻辑。
  2. 状态只有一份：<workspace>/state/alarms.json。
  3. 时刻表只有一份：<workspace>/rules/routines.json。
  4. 写入是原子的：临时文件 + os.replace，并在写入前比对 rev。
  5. plan 只增不改：已有 acked/missed/skipped 的窗口原样保留。
  6. 窗口可以是一次性（oneOff）或有有效期（validFrom/validUntil）。

宿主契约（与运行时唯一的耦合面）：
  - stdout 只有两种形态：要投递的正文，或空（空 = 静默）
  - stderr 承载全部诊断信息
  - 退出码 0 正常 / 2 状态冲突 / 3 校验失败或部分应用 / 4 无匹配窗口
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import rtc_archive as archive
import rtc_brief as brief
import rtc_history as hist
import rtc_metrics as metrics

if hasattr(sys.stdout, "reconfigure"):
    # 标准库类型里 TextIO 没有 reconfigure；实际解释器上是 TextIOWrapper。
    cast(Any, sys.stdout).reconfigure(encoding="utf-8", errors="replace")
    cast(Any, sys.stderr).reconfigure(encoding="utf-8", errors="replace")

WORKSPACE = Path(os.environ.get("RTC_WORKSPACE") or Path.cwd())

STATE = WORKSPACE / "state" / "alarms.json"

ROUTINE = WORKSPACE / "rules" / "routines.json"

DB = WORKSPACE / "state" / "metrics.db"

SCHEDULE_DIR = WORKSPACE / "state" / "schedule"

GRACE_MIN = 15


class Conflict(Exception):
    """rev 不一致，放弃写入。"""


class Validation(Exception):
    """生成的计划不满足校验。"""


# ---------------------------------------------------------------- 基础设施


def now_tz(tz_offset: str) -> datetime:
    sign = 1 if tz_offset[0] == "+" else -1
    hh, mm = tz_offset[1:].split(":")
    return datetime.now(timezone(sign * timedelta(hours=int(hh), minutes=int(mm))))


def parse_iso(s: str, fallback_tz: timezone) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz)
    return dt


def tz_of(offset: str) -> timezone:
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def set_workspace(path: Path) -> None:
    """切换工作区。

    所有派生路径必须在这里一起更新——漏掉任何一个，命令就会写到别处
    （曾经踩过：快照目录漏更新，结果写到进程 cwd 下）。
    """
    global WORKSPACE, STATE, ROUTINE, DB, SCHEDULE_DIR
    WORKSPACE = path
    STATE = WORKSPACE / "state" / "alarms.json"
    ROUTINE = WORKSPACE / "rules" / "routines.json"
    DB = WORKSPACE / "state" / "metrics.db"
    SCHEDULE_DIR = WORKSPACE / "state" / "schedule"


def load_json(path: Path):
    """读 JSON；读坏了就给一行可读诊断，绝不抛 Traceback 给调度器。

    状态文件被手改坏、写到一半断电、编码被编辑器改掉，都会落到这里。以前
    `json.JSONDecodeError` 直接冒到 main 之上：`rtc-due` 每 5 分钟抛一次
    Traceback，宿主只看到「Script exited with code 1」，用户什么也看不懂。
    现在统一转成 Validation（退出码 3、stderr 一行说清哪个文件哪一行），
    并且只读不写——坏文件原样保留，留待人工或备份恢复。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Validation(
            f"{path} 不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列：{e.msg}）；"
            f"该文件未被改动，请从备份恢复或核对是否有手改"
        ) from None
    except UnicodeDecodeError as e:
        raise Validation(f"{path} 不是 UTF-8 编码（{e.reason}）；该文件未被改动") from None


def file_digest(path: Path) -> str | None:
    """文件内容摘要。检测「读取之后被手改过」——比 rev 更严，适合人会编辑的文件。"""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


_LOCK_WAIT_S = 10


def _sidecar_is_our_inode(lock: Path, fd: int) -> bool:
    """sidecar 是否仍是我们打开的那个 inode。

    POSIX 的 ``flock`` 锁的是 **inode**，不是路径。sidecar 被删掉再重建后，
    我们锁住的是旧 inode，对新文件没有任何约束——第二个写入者会毫无阻碍地
    进入临界区（审计补丁未覆盖的洞）。所以拿到锁后必须比对一次
    ``os.stat(path).st_ino`` 与 ``os.fstat(fd).st_ino``。

    Windows 上持锁期间文件根本删不掉（强制锁），且 st_ino 语义不同，
    因此不做比对；个别文件系统会返回 0，此时无法判定，按「是自己」处理，
    避免陷入无意义的空转。
    """
    if os.name == "nt":
        return True
    try:
        st_path = os.stat(str(lock))
        st_fd = os.fstat(fd)
    except OSError:
        return False
    if not st_path.st_ino or not st_fd.st_ino:
        return True
    return st_path.st_ino == st_fd.st_ino


def _unlock_fd(fd: int) -> None:
    """释放 fd 上的 OS 锁（与获取时的分支一一对应）。"""
    if os.name == "nt":
        import msvcrt

        # msvcrt 只在 Windows 存在；两平台都按实际运行环境取值，避开类型存根的裁剪。
        win = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        win.locking(fd, win.LK_UNLCK, 1)
    else:
        import fcntl

        posix = cast(Any, fcntl)
        posix.flock(fd, posix.LOCK_UN)


@contextlib.contextmanager
def _exclusive_file_lock(path: Path):
    """持久 sidecar + 操作系统持锁：互斥由内核保证，文件本身不按年龄替换或删除。

    协议要点：

    - sidecar = ``<文件名>.lock``，**长期存在**，谁都不删、不按 mtime 判陈旧；
    - 进程活着就持锁，进程一死内核自动释放（不需要「陈旧阈值」这个概念）；
    - 所有写入者必须走本协议；不遵守协议的编辑器 / 其它工具不被覆盖，
      仍需 ``expected_rev`` / ``expected_digest`` 的 CAS 兜底；
    - 仅限本地文件系统。网络盘上的 ``flock`` / ``msvcrt.locking`` 语义未验证。
    """
    import errno

    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = _time.monotonic() + _LOCK_WAIT_S
    fd = None
    acquired = False
    try:
        while True:
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
            os.set_inheritable(fd, False)
            try:
                if os.name == "nt":
                    import msvcrt

                    win = cast(Any, msvcrt)
                    os.lseek(fd, 0, os.SEEK_SET)
                    # Windows 允许锁住 EOF 之外的字节，因此不需要写入标记；
                    # 真正要的是「这个句柄持有一把锁」，不是文件内容。
                    win.locking(fd, win.LK_NBLCK, 1)
                else:
                    import fcntl

                    posix = cast(Any, fcntl)
                    posix.flock(fd, posix.LOCK_EX | posix.LOCK_NB)
            except OSError as ex:
                os.close(fd)
                fd = None
                if ex.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if _time.monotonic() >= deadline:
                    raise Conflict(f"{path.name} write-lock wait timed out") from ex
                _time.sleep(0.01)
                continue
            if _sidecar_is_our_inode(lock, fd):
                acquired = True
                break
            # sidecar 在 open 与加锁之间被删除/替换：锁的是旧 inode，等于没锁。
            _unlock_fd(fd)
            os.close(fd)
            fd = None
            if _time.monotonic() >= deadline:
                raise Conflict(f"{path.name} lock sidecar was replaced during acquire")
            _time.sleep(0.01)
        yield
    finally:
        if fd is not None:
            try:
                if acquired:
                    _unlock_fd(fd)
            finally:
                os.close(fd)


def _record_history(
    state: dict | None,
    routine: dict | None = None,
    *,
    kind: str | None = None,
    window: str | None = None,
    item: str | None = None,
    note: str | None = None,
    ts: datetime | None = None,
) -> None:
    """每次成功写状态之后，同步进 history.db。

    审计不参与判定，库写不进去也只是少一条轨迹——绝不能让提醒本身失败。
    """
    if not state:
        return
    try:
        hist.sync_state(state, routine)
        if kind:
            hist.log_event(
                kind,
                window or "-",
                ts=ts,
                day=state.get("planDate"),
                item_id=item,
                note=note,
            )
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        print(f"history: 写库失败（{type(e).__name__}: {e}），本轮历史可能少一条", file=sys.stderr)


def atomic_write(path: Path, data: dict, expected_rev: int | None, expected_digest: str | None = None) -> str:
    """写入前比对 rev 与内容摘要；不一致则抛 Conflict，绝不覆盖别人的写入。

    rev 适合程序自己写的账本；摘要适合人会手改的配置文件（时刻表）。
    返回本次写入内容的摘要——它是「这个操作写出的版本」，供调用方在
    后续回滚里做 CAS；不能改成重新读磁盘，那会把别人的改动当成自己的基线。
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    new_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    with _exclusive_file_lock(path):
        if expected_digest is not None and file_digest(path) != expected_digest:
            raise Conflict(f"{path.name} 在读取之后被改动过")
        if expected_rev is not None and path.exists():
            current = load_json(path)
            if int(current.get("rev", 0)) != expected_rev:
                raise Conflict(f"rev 已变：期望 {expected_rev}，实际 {current.get('rev')}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # 临门再验：把 check 与 replace 之间的窗口缩到一次原子系统调用
            if expected_digest is not None and file_digest(path) != expected_digest:
                raise Conflict(f"{path.name} 在写入期间被改动过")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return new_digest


_ROUTINE_KEYS = ("version", "timezone", "tzOffset", "alarmWindows", "dateTypes")

# 运行中攒下的「要跟人说一句」的话（自愈、手改），由 plan/due 播一次就清空
NOTICES: list[str] = []


def load_routine() -> dict:
    """读时刻表并做最小形状校验；坏了就先想办法自愈。

    实机教训：模型把 routines.json 写成了 0 字节，plan/due 双双失败，
    用户一整天收不到任何提醒——而程序手里明明还留着上一份好文件。
    所以校验失败时先尝试从程序底稿恢复，恢复了就在输出里说明一句。
    """
    try:
        return _validate_routine()
    except Validation:
        notice = _restore_from_last_backup()
        if not notice:
            raise
        NOTICES.append(notice)
        return _validate_routine()


def _validate_routine() -> dict:
    """读时刻表并做最小形状校验。

    「JSON 合法但结构不对」（整个文件被写成 `[]`、少了 tzOffset）同样会让
    plan/due 在后续几行抛 KeyError/TypeError。这里提前拦下来，给一句
    能照着修的诊断，而不是一个栈。
    """
    if not ROUTINE.exists():
        raise Validation(f"找不到时刻表：{ROUTINE}")
    routine = load_json(ROUTINE)
    if not isinstance(routine, dict):
        raise Validation(
            f"{ROUTINE} 顶层应是对象（{{…}}），实际是 {type(routine).__name__}；"
            f"参考 rules/routines.example.md"
        )
    missing = [k for k in _ROUTINE_KEYS if k not in routine]
    if missing:
        raise Validation(f"{ROUTINE} 缺字段：{', '.join(missing)}；参考 rules/routines.example.md")
    if not isinstance(routine.get("alarmWindows"), list):
        raise Validation(f"{ROUTINE} 的 alarmWindows 应是数组")
    return routine


def _restore_from_last_backup() -> str | None:
    """从程序最后写出的那份底稿恢复时刻表。成功返回要说的话，否则 None。"""
    last = WORKSPACE / "state" / BACKUP_DIR / "routines.program-last.json"
    if not last.exists():
        return None
    try:
        data = load_json(last)
    except (Validation, ValueError, TypeError, OSError):
        return None
    if not isinstance(data, dict) or any(k not in data for k in _ROUTINE_KEYS):
        return None

    bdir = WORKSPACE / "state" / BACKUP_DIR
    bad = None
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        if ROUTINE.exists():
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            bad = bdir / f"routines.corrupt-{stamp}.json"
            shutil.copyfile(ROUTINE, bad)
    except OSError:
        bad = None

    try:
        atomic_write(ROUTINE, data, expected_rev=None, expected_digest=None)
    except (OSError, Validation, ValueError):
        return None
    seal_routine()

    windows = len(data.get("alarmWindows") or [])
    msg = f"⚠️ {ROUTINE.name} 被写坏了，已从程序底稿自动恢复（{windows} 个窗口），提醒继续正常"
    if bad:
        msg += f"；坏件留在 {bad}"
    return msg


def take_notices() -> list[str]:
    """取走待播报的提示（自愈、手改等），只播一次。"""
    got = list(NOTICES)
    NOTICES.clear()
    return got


def load_state() -> dict | None:
    if not STATE.exists():
        return None
    state = load_json(STATE)
    if not isinstance(state, dict):
        raise Validation(
            f"{STATE} 顶层应是对象（{{…}}），实际是 {type(state).__name__}；"
            f"该文件未被改动，请从 state 备份恢复"
        )
    if not isinstance(state.get("windows") or [], list):
        raise Validation(f"{STATE} 的 windows 应是数组；该文件未被改动")
    return state


# ---------------------------------------------------------------- 心跳与自检

HEARTBEAT_STALE_MIN = 60


def hb_path() -> Path:
    return WORKSPACE / "state" / "heartbeat.json"


def touch_heartbeat(now: datetime, *, ok: bool = False) -> None:
    """写心跳。attemptAt 每次进入 due 都写；lastOkAt 只在判定完整走完后写。

    - ``attemptAt``：进程被调用过的证据（进入 due 即记，失败也留痕）。
    - ``lastOkAt``：判定完整走完（含正确判定为静默）的证据；失败路径不推进。
    - ``at``：lastOkAt 的镜像，兼容旧部署的 self_check / 读 ``at`` 的外部脚本。

    这样区分了「调过但没判完」（状态文件损坏时进程已失败退出，心跳却曾
    被刷成新鲜）和「判完了」两件事——前者不再冒充健康。写心跳失败只报
    stderr，绝不影响提醒本身。
    """
    p = hb_path()
    try:
        prev: dict = {}
        if p.exists():
            try:
                loaded = load_json(p)
                if isinstance(loaded, dict):
                    prev = loaded
            except (Validation, ValueError, OSError, TypeError):
                prev = {}
        attempt = now.isoformat()
        last_ok = attempt if ok else (prev.get("lastOkAt") or prev.get("at"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"attemptAt": attempt, "lastOkAt": last_ok, "at": last_ok}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"heartbeat: 写入失败 {e}", file=sys.stderr)


def read_heartbeat() -> datetime | None:
    """最后一次「判定完整走完」的时间（lastOkAt）；兼容旧格式的 ``at``。"""
    p = hb_path()
    if not p.exists():
        return None
    try:
        data = load_json(p)
        iso = None
        if isinstance(data, dict):
            iso = data.get("lastOkAt") or data.get("at")
        return datetime.fromisoformat(iso) if iso else None
    except (Validation, ValueError, KeyError, TypeError, OSError, AttributeError):
        return None


def read_heartbeat_attempt() -> datetime | None:
    """最后一次「进入 due」的时间（attemptAt）；区分「没调过」与「调了没判完」。"""
    p = hb_path()
    if not p.exists():
        return None
    try:
        data = load_json(p)
        iso = data.get("attemptAt") if isinstance(data, dict) else None
        return datetime.fromisoformat(iso) if iso else None
    except (Validation, ValueError, KeyError, TypeError, OSError, AttributeError):
        return None


def self_check(state: dict, now: datetime, first_run: bool = False) -> list[str]:
    """只报告「任务在跑但可能没送达」的信号，正常时不输出任何东西。"""
    probs: list[str] = []
    hb = read_heartbeat()
    hb_attempt = read_heartbeat_attempt()

    if hb is None:
        # attempt 新鲜 = due 正在判定中（它会在出口补写 lastOkAt）或持续被调用，
        # 此时不报——pause 期 due 每 tick 都在跑、只是判为静默，属健康。
        # 若 due 因状态损坏失败退出，lastOkAt 永远不写，attemptAt 会随时间变旧，
        # 次日这里就会告警——这正是要抓的场景。
        attempt_fresh = (
            hb_attempt is not None and 0 <= (now - hb_attempt).total_seconds() / 60 <= HEARTBEAT_STALE_MIN
        )
        if not first_run and not attempt_fresh:
            probs.append("⚠️ 没有成功判定心跳：rtc-due 可能从未完整跑完一次判定")
    else:
        gap_min = (now - hb).total_seconds() / 60
        if gap_min > HEARTBEAT_STALE_MIN:
            probs.append(f"⚠️ 调度器最近一次成功判定在 {gap_min / 60:.1f} 小时前，这段时间里的窗口可能没响")

    yesterday = (now.date() - timedelta(days=1)).isoformat()
    for h in state.get("history", []):
        if h.get("date") != yesterday:
            continue
        if h.get("state") == "missed" and int(h.get("fires", 0)) == 0:
            probs.append(f"⚠️ 昨天「{h.get('id')}」一次都没响过，当时调度器可能没在跑")
    return probs


# ---------------------------------------------------------------- 日程快照


def schedule_snapshot(
    routine: dict,
    kind: str,
    windows: list[dict],
    day_iso: str,
    generated_at: str,
    results: dict | None = None,
) -> dict:
    """当天完整计划的快照。写一次，留永久——回答「那天到底是什么安排」。

    窗口的 start/end 用生成时的实际值（含日期），有效期从时刻表定义取回。

    `results` 是「实际怎么了」：窗口 id → {state, fires, ackedAt, missedReason}。
    plan 时还没有结果（空），rollover 归档时回填。
    两者合起来才回答得了「那天发生了什么」——只记计划，推迟/取消/未完成就永远查不出来。
    """
    defs = {w["id"]: w for w in routine.get("alarmWindows", [])}
    calls = []
    for r in routine.get("rollcalls") or []:
        act = (r.get("kindByDateType") or {}).get(kind)
        if not act or act == "none":
            continue
        calls.append({"hour": r["hour"], "minute": r.get("minute", 0), "kind": act})
    dow = (date.fromisoformat(day_iso).weekday() + 1) % 7
    chores = [
        {"label": c.get("label"), "attachToRollcallHour": c.get("attachToRollcallHour")}
        for c in (routine.get("weeklyChores") or [])
        if int(c.get("dayOfWeek", -1)) == dow
    ]
    return {
        "date": day_iso,
        "planKind": kind,
        "timezone": routine.get("timezone"),
        "generatedAt": generated_at,
        "results": results or {},
        "windows": [
            {
                "id": w["id"],
                "label": w["label"],
                "type": w.get("type"),
                "review": bool(w.get("review")),
                "start": w["start"],
                "end": w["end"],
                "intervalMin": w["intervalMin"],
                "expectFires": w["expectFires"],
                "oneOff": bool(w.get("oneOff")),
                "dueDate": w.get("dueDate"),
                "ackHint": w.get("ackHint"),
                "format": w.get("format"),
                "validFrom": defs.get(w["id"], {}).get("validFrom"),
                "validUntil": defs.get(w["id"], {}).get("validUntil"),
            }
            for w in windows
        ],
        "rollcalls": calls,
        "weeklyChores": chores,
    }


def fill_snapshot_results(day_iso: str, results: dict) -> bool:
    """把「实际结果」回填进某天的快照。返回是否写入成功。

    快照是永久存档：plan 时写计划、rollover 时补结果。
    回填失败不阻断主链路——快照补不回来，下次还能再试。
    """
    path = SCHEDULE_DIR / f"{day_iso}.json"
    if not path.exists():
        return False
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    existing = snap.get("results") or {}
    merged = dict(existing)
    merged.update(results or {})
    if merged == existing:
        return True
    snap["results"] = merged
    try:
        write_schedule_snapshot(snap)
    except OSError:
        return False
    return True


def write_schedule_snapshot(snap: dict) -> Path:
    """原子写一份每日快照。永久保留，不做清理。"""
    d = SCHEDULE_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{snap['date']}.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".schedule.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


# ---------------------------------------------------------------- 计划生成


def plan_kind(d: date, routine: dict) -> str:
    overrides = routine.get("dateTypes", {}).get("holidayOverrides", [])
    iso = d.isoformat()
    for o in overrides:
        if o.get("date") == iso:
            return o["type"]
    # 契约里 0=周日，Python 里 weekday() 0=周一，必须换算
    dow = (d.weekday() + 1) % 7
    if dow in routine["dateTypes"]["workday"]:
        return "workday"
    if dow in routine["dateTypes"]["weekend"]:
        return "weekend"
    return "holiday"


def expect_fires(start: time, end: time, interval: int) -> int:
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes < 0:
        raise Validation("窗口跨午夜，当前版本不支持")
    return minutes // interval + 1


def in_valid_range(w: dict, day_iso: str) -> bool:
    """窗口是否在有效期内（闭区间）。任一端省略 = 无限期。"""
    vf = w.get("validFrom")
    vu = w.get("validUntil")
    if vf and day_iso < vf:
        return False
    return not (vu and day_iso > vu)


def day_disabled(routine: dict, kind: str) -> bool:
    """该日期类型被契约显式禁用（weekendAlarms / holidayAlarms = false）。

    禁用日 = 合法空日：静默，不报配置错误。
    """
    return (kind == "weekend" and routine.get("weekendAlarms") is False) or (
        kind == "holiday" and routine.get("holidayAlarms") is False
    )


def window_type(defn: dict) -> str:
    """四类日程的身份：预约 = 一次性 + 倒计时；倒计时不是独立类型，是 fixed 的一种活法。

    `cancelled` 不是第四种类型，是覆盖在任意类型之上的粘性状态（见 issue #9 §10.2）。
    """
    if defn.get("oneOff"):
        return "appointment" if defn.get("dueDate") else "oneoff"
    return "fixed"


def countdown_review_day(defn: dict) -> str | None:
    """倒计时（fixed + dueDate）的复盘日 = 截止日次日。

    截止日之后再无提醒（复盘日只响一次，响完永停）。显式给了 `validUntil` 且
    正好等于截止日的老配置保持旧行为——它们本来就不该多活一天。
    """
    due = defn.get("dueDate")
    if not due or defn.get("oneOff") or defn.get("canceled"):
        return None
    review = (date.fromisoformat(due) + timedelta(days=1)).isoformat()
    vu = defn.get("validUntil")
    if vu and vu < review:
        return None
    return review


def build_windows(routine: dict, d: date, kind: str, now: datetime, strict: bool = True) -> list[dict]:
    tz = tz_of(routine["tzOffset"])
    day_iso = d.isoformat()
    # 第一步：按有效期筛选。全部落空 = 这段时间本来就没有该响的东西（合法空日）
    # 永久停用的窗口：任何日期都不生成（「以后别提醒我了」/「这件事完成了」）
    enabled = [w for w in routine["alarmWindows"] if not w.get("disabled")]
    ranged = [w for w in enabled if in_valid_range(w, day_iso)]
    # 倒计时活到「截止日 + 1（复盘日）」为止：再往后就是已经收尾的过去，永不重生
    ranged = [
        w
        for w in ranged
        if not (
            w.get("dueDate")
            and not w.get("oneOff")
            and str(w["dueDate"]) < day_iso
            and countdown_review_day(w) != day_iso
        )
    ]

    # 显式跳过的日期（推迟产生的）——这一天不响
    skipped = [w for w in ranged if day_iso in (w.get("skipDates") or [])]
    dated = [w for w in ranged if day_iso not in (w.get("skipDates") or [])]
    out = []
    for w in dated:
        allowed = w.get("dateTypes") or []
        if allowed and kind not in allowed:
            continue
        sh, sm = (int(x) for x in w["start"].split(":"))
        eh, em = (int(x) for x in w["end"].split(":"))
        start = datetime.combine(d, time(sh, sm), tzinfo=tz)
        end = datetime.combine(d, time(eh, em), tzinfo=tz)
        one_off = bool(w.get("oneOff"))
        due = w.get("dueDate")
        items_def = w.get("checklist") or []
        review_day = countdown_review_day(w)
        is_review = bool(review_day) and day_iso == review_day
        label = w.get("label", w["id"])
        if is_review:
            label = f"{label}（复盘）"
        if due or items_def or is_review:
            # 倒计时 / 清单 / 复盘：每天只提醒一次。
            # 倒计时的剩余天数由代码算；清单要逐项打勾，都不该每 5 分钟响。
            exp = 1
        elif one_off:
            # 一次性提醒只响一次，不做算术校验（end 可比 start 晚，用于容忍错过 start 那一分钟）
            exp = 1
        else:
            exp = expect_fires(time(sh, sm), time(eh, em), int(w["intervalMin"]))
            if w.get("expectFires") and int(w["expectFires"]) != exp:
                raise Validation(f"窗口 {w['id']} 的 expectFires={w['expectFires']} 与算术值 {exp} 不符")
        expired = now > end
        out.append(
            {
                "id": w["id"],
                "label": label,
                "type": window_type(w),
                "review": is_review,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "intervalMin": int(w["intervalMin"]),
                "expectFires": exp,
                "fireCount": 0,
                "state": "skipped" if expired else "pending",
                "lastFiredAt": None,
                "ackedAt": None,
                "ackText": None,
                "missedAt": None,
                "missedReason": "late_plan" if expired else None,
                "ackHint": (w.get("ackHints") or ["知道了"])[0],
                "format": w.get("format"),
                "oneOff": one_off or is_review,
                "dueDate": due,
                "disabled": False,
                "cancelledAt": None,
                "checklist": _checklist_defs(items_def),
                "checked": [],
            }
        )
    if not out:
        if not ranged and len(enabled) < len(routine["alarmWindows"]):
            # 所有窗口都被永久停用：合法空日，静默。
            return []
        if not dated:
            # 所有窗口都不在有效期内，或都被显式跳过：合法空日，静默。
            return []
        if skipped:
            # 今天有窗口被显式跳过，其余的又被 dateTypes 排除 → 合法空日
            return []
        # 有窗口在有效期内，却被 dateTypes 全部排除 → 多半是 dateTypes 写错了
        if strict:
            raise Validation("今天没有任何窗口：检查 routines.json 的 dateTypes")
        return []
    return out


TERMINAL = {"acked", "missed", "skipped", "cancelled", "delivered"}


def _item_id(label: str, explicit: str | None = None) -> str:
    """清单项的稳定身份：有显式 id 就用（不合规的退回内容哈希），否则用标签哈希。

    身份必须跨日不变——顺序会变、显示序号只是给人看的（issue #9 §9）。
    """
    if explicit:
        cand = str(explicit).strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", cand):
            return cand
    norm = " ".join(str(label).split())
    return "i" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _checklist_defs(items_def: list) -> list[dict]:
    """把法则里的清单定义（字符串或 {id,label}）统一成 {id,label}。"""
    out: list[dict] = []
    seen: dict[str, int] = {}
    for raw in items_def:
        if isinstance(raw, dict):
            label = str(raw.get("label") or raw.get("id") or "").strip()
            explicit = raw.get("id")
        else:
            label = str(raw).strip()
            explicit = None
        if not label:
            continue
        iid = _item_id(label, explicit)
        if iid in seen:
            seen[iid] += 1
            iid = f"{iid}-{seen[iid]}"
        else:
            seen[iid] = 1
        out.append({"id": iid, "label": label})
    return out


def _migrate_checklist(prev: dict, w: dict) -> None:
    """把旧窗口的勾选状态与「已打完」终态迁移到新窗口上。

    清单不会因为日切而重置——昨天打了 3 项，今天还剩 15 项。
    迁移按**稳定 id** 对齐：老状态里存的可能还是序号（"1"），也认得出来；
    新清单里已经没有的项，勾选随之作废并记一条 `orphaned` 事件，不静默丢。
    """
    if not w.get("checklist"):
        return
    items = w["checklist"]
    by_id = {it["id"]: it for it in items}
    old = list(prev.get("checked") or [])
    keep: list[str] = []
    orphaned: list[str] = []
    for token in old:
        token = str(token)
        resolved: str | None = None
        if token in by_id:
            resolved = token
        elif token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(items):
                resolved = items[idx]["id"]
        else:
            for it in items:
                if it["label"] == token:
                    resolved = it["id"]
                    break
        if resolved is None:
            orphaned.append(token)
        else:
            keep.append(resolved)
    w["checked"] = sorted(set(keep))
    if orphaned and prev.get("id"):
        try:
            for token in orphaned:
                hist.log_event(
                    "orphaned",
                    prev["id"],
                    item_id=token,
                    note="清单项已不存在，勾选作废",
                )
        except (sqlite3.Error, OSError, ValueError, TypeError):
            pass
    if prev.get("state") == "acked" and len(w["checked"]) >= len(items):
        # 已经全部打完 → 保持完成状态，之后不再提醒
        w["state"] = "acked"
        w["ackedAt"] = prev.get("ackedAt")
        w["fireCount"] = int(prev.get("fireCount", 0))


def carry_forward(old: dict | None, fresh: list[dict]) -> list[dict]:
    """只增不改：同日同 id 的终态窗口原样带回，避免重建冲掉确认。

    `ringing`（正在响）也算：它带着 fireCount 与 lastFiredAt。
    同一天重建（`plan --force`）时若把它重置成 pending，下一次 tick 会
    再响一轮，而 fireCount 是从 0 重新数——上限形同虚设。
    `rtc_once.py` 每加一条提醒都跑一次 `plan --force`，这条路径是常走的。
    """
    if not old:
        return fresh
    prev = {w["id"]: w for w in old.get("windows", [])}
    merged = []
    for w in fresh:
        p = prev.get(w["id"])
        if p is None:
            _migrate_checklist({}, w)
            merged.append(w)
            continue
        pstate = p.get("state")
        if pstate == "ringing":
            merged.append(p)
            continue
        keep = bool(pstate) and pstate in TERMINAL
        if keep and w.get("dueDate") and pstate != "cancelled":
            keep = False  # 倒计时：每天重新开始，直到截止日（但被取消的不重开）
        if keep and w.get("checklist"):
            # 清单：只有全部打完才保留终态；没打完就次日继续（勾选状态带过去）
            done = set(w.get("checked") or [])
            keep = pstate == "acked" and len(done) >= len(w["checklist"])
        if keep:
            merged.append(p)
        else:
            _migrate_checklist(p, w)
            merged.append(w)
    return merged


def rollover(old: dict | None, today_iso: str) -> list[dict]:
    """日切归档：每个窗口都进历史，不只是 pending/ringing。

    只压未终结的窗口会丢掉 acked 的记录，账本会永久缺一天（原设计的一处缺口）。
    """
    hist = list((old or {}).get("history", []))
    if not old:
        return hist
    plan_date = old.get("planDate", today_iso)
    for w in old.get("windows", []):
        state = w.get("state")
        reason = w.get("missedReason")
        if state in {"pending", "ringing"}:
            state = "missed"
            reason = reason or "day_rollover"
        hist.append(
            {
                "date": plan_date,
                "id": w["id"],
                "label": w.get("label"),
                "expect": w.get("expectFires", 0),
                "state": state,
                "fires": w.get("fireCount", 0),
                "ackedAt": w.get("ackedAt"),
                "missedReason": reason if state in {"missed", "skipped", "cancelled"} else None,
            }
        )
    # 同 date+id 去重，后写覆盖先写
    dedup: dict[tuple, dict] = {}
    for h in hist:
        dedup[(h.get("date"), h.get("id"))] = h
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=14)).isoformat()

    # 回填前一天的快照：把「实际怎么了」写进永久存档。
    # 快照只有计划没有结果，推迟/取消/未完成就永远查不出来。
    if plan_date and plan_date != today_iso:
        results = {
            w["id"]: {
                "state": w.get("state"),
                "fires": w.get("fireCount", 0),
                "ackedAt": w.get("ackedAt"),
                "missedReason": w.get("missedReason"),
                "label": w.get("label"),
                "expect": w.get("expectFires", 0),
            }
            for w in old.get("windows", [])
        }
        if results:
            fill_snapshot_results(plan_date, results)

    return [h for k, h in sorted(dedup.items()) if h.get("date", "") >= cutoff]


def simulate_day(routine: dict, d: date, paused: dict | None = None) -> dict:
    """预演某一天会怎样：只读，绝不落盘。

    判定全部复用 plan 的那一套（有效期 / skipDates / disabled / dateTypes / 算术校验），
    唯一差别是把"现在"钉在那天的 00:00——这样计划里的窗口不会被判成"已过期"。
    """
    kind = plan_kind(d, routine)
    tz = tz_of(routine["tzOffset"])
    day_start = datetime.combine(d, time(0, 0), tzinfo=tz)
    day_iso = d.isoformat()

    windows: list[dict] = []
    skipped: list[dict] = []
    day_off = day_disabled(routine, kind)
    if pause_covers(paused, d):
        until = (paused or {}).get("until", "?")
        skipped.append({"id": "<paused>", "reason": f"这段时间已暂停（到 {until}）"})
    elif day_off:
        skipped.append({"id": f"<{kind}>", "reason": f"{kind} 在契约里被禁用（整天空日）"})
    else:
        for w in build_windows(routine, d, kind, day_start):
            windows.append(
                {
                    "id": w["id"],
                    "label": w["label"],
                    "start": w["start"],
                    "end": w["end"],
                    "expectFires": w["expectFires"],
                    "oneOff": w["oneOff"],
                }
            )
        for w in routine["alarmWindows"]:
            if any(x["id"] == w["id"] for x in windows):
                continue
            if w.get("disabled"):
                skipped.append({"id": w["id"], "reason": "已停用（disable）"})
            elif not in_valid_range(w, day_iso):
                skipped.append({"id": w["id"], "reason": "不在有效期"})
            elif day_iso in (w.get("skipDates") or []):
                skipped.append({"id": w["id"], "reason": "这一天被推迟（postpone）"})
            elif w.get("dateTypes") and kind not in w["dateTypes"]:
                skipped.append({"id": w["id"], "reason": f"只在这些日期类型响：{'/'.join(w['dateTypes'])}"})
    windows.sort(key=lambda x: x["start"])
    return {
        "date": day_iso,
        "kind": kind,
        "dayDisabled": day_off,
        "windows": windows,
        "skipped": skipped,
        "totalFires": sum(x["expectFires"] for x in windows),
        "totalAttempts": sum(x["expectFires"] for x in windows),
    }


def render_simulate(data: dict) -> list[str]:
    """预演的人读版：几号、几次、几点、重叠、跳过。"""
    d = date.fromisoformat(data["date"])
    weekday = "一二三四五六日"[d.weekday()]
    lines = [f"{data['date']}（周{weekday}）预计响 {data['totalFires']} 次"]
    if data["dayDisabled"]:
        lines.append(f"  （{data['kind']} 按契约不响：整天空日）")
        return lines
    if not data["windows"]:
        lines.append("  没有任何窗口——这一天不会响")
    for w in data["windows"]:
        kind = "一次性" if w["oneOff"] else "每天"
        lines.append(f"  {w['start'][11:16]}–{w['end'][11:16]}  {w['label']}  {w['expectFires']} 次  {kind}")
    if data["windows"]:
        starts = [w["start"][11:16] for w in data["windows"]]
        dupes = sorted({s for s in starts if starts.count(s) > 1})
        overlap = "、".join(dupes) if dupes else "无"
        lines.append(f"最早 {min(starts)}   最晚 {max(starts)}   同一时刻重叠：{overlap}")
    if data["skipped"]:
        lines.append("跳过：" + "、".join(f"{s['id']}（{s['reason']}）" for s in data["skipped"]))
    return lines


def cmd_simulate(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            print(f"simulate: 日期看不懂：{args.date}（要 YYYY-MM-DD）", file=sys.stderr)
            return 3
    else:
        d = now.date() + timedelta(days=1)
    try:
        data = simulate_day(routine, d, load_pause())
    except Validation as e:
        print(f"simulate: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for line in render_simulate(data):
        print(line)
    return 0


def pause_path() -> Path:
    """暂停标记放在独立文件，不碰 routines.json。

    时刻表是用户作息（有印章、有审计），临时静音是另一回事；混在一起，
    印章会把「暂停」当成「有人手改了时刻表」。
    """
    return WORKSPACE / "state" / "paused.json"


def load_pause() -> dict | None:
    """读暂停标记。坏文件当作没暂停——宁可按原样响，不能因为标记坏了集体静默。"""
    p = pause_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        date.fromisoformat(data["until"])
        return data
    except (OSError, ValueError, TypeError, KeyError) as e:
        print(f"pause: 暂停标记看不懂（{type(e).__name__}: {e}），当作没暂停", file=sys.stderr)
        return None


def _pause_range(p: dict) -> tuple[date, date]:
    until = date.fromisoformat(p["until"])
    start = date.fromisoformat(p.get("from") or p["until"])
    return start, until


def pause_covers(p: dict | None, d: date) -> bool:
    """这一天是否落在暂停期（左闭右开：until 那天已经恢复）。"""
    if not p:
        return False
    try:
        f, t = _pause_range(p)
    except (ValueError, TypeError):
        return False
    return f <= d < t


def pause_summary(p: dict) -> str:
    """恢复时说清「停了几天、跳过多少条」，数字从每日快照里数。"""
    f, t = _pause_range(p)
    days = (t - f).days
    skipped = 0
    d = f
    while d < t:
        path = SCHEDULE_DIR / f"{d.isoformat()}.json"
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
            skipped += len(snap.get("windows") or [])
        except (OSError, ValueError):
            pass
        d += timedelta(days=1)
    return f"{f:%m-%d} ~ {t - timedelta(days=1):%m-%d} 暂停了 {days} 天，跳过 {skipped} 次提醒"


def cmd_pause(args) -> int:
    tz = tz_of(load_routine()["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(load_routine()["tzOffset"])
    until = None
    if args.until:
        try:
            until = date.fromisoformat(args.until)
        except ValueError:
            print(f"pause: 日期看不懂：{args.until}（要 YYYY-MM-DD）", file=sys.stderr)
            return 3
    elif args.days:
        if args.days < 1:
            print("pause: --days 至少 1 天", file=sys.stderr)
            return 3
        until = now.date() + timedelta(days=args.days)
    if until is None:
        print("pause: 要说停到哪天：--days N 或 --until YYYY-MM-DD", file=sys.stderr)
        return 3
    if until <= now.date():
        print(f"pause: {until} 已经过去了（今天 {now.date()}）", file=sys.stderr)
        return 3
    data = {"from": now.date().isoformat(), "until": until.isoformat(), "since": now.isoformat()}
    if args.reason:
        data["reason"] = args.reason
    pause_path().parent.mkdir(parents=True, exist_ok=True)
    pause_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"pause: 已暂停到 {until.isoformat()}（{(until - now.date()).days} 天），期间不会响")
    # 今天已经排好的窗口要立刻收掉，否则暂停之后当天还会响
    cmd_plan(argparse.Namespace(now=now.isoformat(), force=True))
    return 0


def cmd_resume(args) -> int:
    p = load_pause()
    if not p:
        print("resume: 本来就没暂停")
        return 0
    print(pause_summary(p))
    pause_path().unlink(missing_ok=True)
    print("resume: 已恢复，下一条 plan 会把窗口排回来")
    return 0


def delivery_log() -> Path:
    return WORKSPACE / "state" / "delivery.log"


def record_delivery(window: str, target: str, result: str, error: str | None, at: datetime) -> None:
    """宿主（或包装脚本）投完一条就在日志里补一行。

    RTC 脚本只能把话交给 stdout，投递发生在退出之后——「说出去了」和「收到了」
    之间有一段脚本看不见的路。这一行就是那段路的回执：没有它，昨天的提醒到底
    发出去没有，谁都答不上来。
    """
    path = delivery_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(
            json.dumps(
                {
                    "at": at.isoformat(),
                    "window": window,
                    "target": target,
                    "result": result,
                    "error": error,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def read_delivery(day: str | None = None) -> list[dict]:
    """读回执；坏行跳过，不让一行坏 JSON 毁掉整天的账。"""
    path = delivery_log()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or "result" not in row:
            continue
        if day and str(row.get("at", ""))[:10] != day:
            continue
        rows.append(row)
    return rows


def delivery_summary(day: str) -> dict:
    rows = read_delivery(day)
    fails = [r for r in rows if r.get("result") != "ok"]
    return {
        "date": day,
        "total": len(rows),
        "ok": len(rows) - len(fails),
        "failed": len(fails),
        "failures": [
            {
                "at": r.get("at"),
                "window": r.get("window"),
                "target": r.get("target"),
                "error": r.get("error"),
            }
            for r in fails
        ],
    }


def cmd_delivery(args) -> int:
    day = args.date or now_tz("+08:00").date().isoformat()
    if not delivery_log().exists():
        print(
            "delivery: 还没有投递回执（state/delivery.log 不存在）——"
            "宿主投完一条要调 delivery-record 写一行，见 docs/troubleshooting.md §2.2",
            file=sys.stderr,
        )
        return 4
    data = delivery_summary(day)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"{day}  投递 {data['total']} 条：成功 {data['ok']} · 失败 {data['failed']}")
    for f in data["failures"]:
        when = str(f["at"] or "")[11:16]
        print(f"  {when}  {f['window']} → {f['target']}  {f['error'] or '（没说原因）'}")
    return 0


def cmd_delivery_record(args) -> int:
    if args.result not in ("ok", "fail"):
        print(f"delivery-record: --result 只能是 ok / fail，收到 {args.result}", file=sys.stderr)
        return 3
    tz = tz_of("+08:00")
    at = parse_iso(args.at, tz) if args.at else now_tz("+08:00")
    record_delivery(args.window or "-", args.target, args.result, args.error, at)
    print(f"delivery-record: 已记 {at.isoformat()} {args.result}")
    return 0


def cmd_plan(args) -> int:
    routine = load_routine()
    hand_edit = audit_routine(routine)
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    today = now.date().isoformat()

    old = load_state()
    if old and old.get("planDate") == today and not args.force:
        print(f"plan: {today} 已存在，跳过（rev={old.get('rev')}）", file=sys.stderr)
        return 0

    # 暂停到期（或当天已过 until）就自动恢复，并把「停了多少」补报一次
    paused = load_pause()
    if paused:
        try:
            if now.date() >= date.fromisoformat(paused["until"]):
                print(pause_summary(paused))
                pause_path().unlink(missing_ok=True)
                paused = None
        except (ValueError, TypeError):
            paused = None

    kind = plan_kind(now.date(), routine)
    # 契约里显式禁用的日期类型 → 合法空日：静默，但照常落盘 state
    fresh = [] if day_disabled(routine, kind) else build_windows(routine, now.date(), kind, now)

    if old and old.get("planDate") == today:
        windows = carry_forward(old, fresh)
    else:
        # 跨日：窗口全部重建，但清单的勾选状态要带过去
        prev_windows = {x["id"]: x for x in (old or {}).get("windows", [])}
        for w in fresh:
            _migrate_checklist(prev_windows.get(w["id"], {}), w)
        windows = fresh
    history = (
        rollover(old, today) if not (old and old.get("planDate") == today) else list(old.get("history", []))
    )
    if len(windows) != len(fresh):
        raise Validation(f"窗口数 {len(windows)} 与时刻表 {len(fresh)} 不符")

    # 暂停期：今天该响的都算出来（快照里留底，恢复时好数），但状态里一条也不排，
    # 于是 due 自然静默——不写 routines.json、不删窗口、不碰印章。
    paused_today = pause_covers(paused, now.date())
    planned = windows
    if paused_today:
        windows = []
    total = sum(w["expectFires"] for w in windows)

    state = {
        "version": 1,
        "rev": int((old or {}).get("rev", 0)) + 1,
        "timezone": routine.get("timezone", "Asia/Shanghai"),
        "planDate": today,
        "planKind": kind,
        "generatedBy": "rtc-plan",
        "generatedAt": now.isoformat(),
        "windows": windows,
        "history": history,
    }
    atomic_write(STATE, state, expected_rev=int((old or {}).get("rev", 0)) if old else None)
    # 第一次建库时把老快照与 alarms.json 里的历史摘要灌进去——换仓库形态，历史不丢。
    if not hist.history_db().exists():
        try:
            hist.backfill_from_snapshots()
            hist.import_state_history(state, routine)
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            print(f"history: 回填失败（{type(e).__name__}: {e}），库从今天开始记", file=sys.stderr)
    _record_history(state, routine, kind="plan", ts=now)
    print(
        f"plan: {today} {kind} {len(windows)} 窗口 / {total} 次，rev={state['rev']}"
        + ("（已暂停）" if paused_today else ""),
        file=sys.stderr,
    )

    # 表里只剩一次性提醒时，今天的计划会「生成成功但明天不再有日常窗口」——
    # 这类状态最容易被误判成脚本坏了，所以在这里显式报一句（退出码仍为 0）。
    recurring = sum(
        1
        for w in routine.get("alarmWindows") or []
        if not w.get("oneOff") and not str(w.get("id") or "").startswith("once-")
    )
    if recurring == 0:
        print(
            "plan: 时刻表里只有一次性提醒，没有日常窗口——今天不会有日常提醒；改时刻表见 rules/routines.md",
            file=sys.stderr,
        )

    # 顺手清掉过期的一次性提醒：只删「oneOff 且 validUntil 已过」的。
    # 放在 state 写成功之后，且任何冲突都只提示不阻断——清理失败不影响今天照响。
    try:
        rdigest = file_digest(ROUTINE)
        rnow = load_routine()
        removed = _prune_expired_oneoffs(rnow, today)
        if removed:
            _write_routine(rnow, rdigest)
            print(f"plan: 已清理过期的一次性提醒 {', '.join(removed)}", file=sys.stderr)
    except (Conflict, Validation) as e:
        print(f"plan: 清理过期一次性提醒失败（{e}），不影响本次计划", file=sys.stderr)

    # 每日快照：主链路写，写一次留永久。失败不阻断提醒，但必须留下诊断。
    # 暂停日写的是「本来会响的那些窗口」+ paused 标记——恢复时的「跳过 N 次」靠它数。
    try:
        snap = schedule_snapshot(routine, kind, planned, today, now.isoformat())
        if paused_today:
            snap["paused"] = True
        write_schedule_snapshot(snap)
    except OSError as e:
        print(f"schedule: 快照写入失败 {e}", file=sys.stderr)

    # 补跑 plan 时也把前一天的「实际结果」回填进它的快照。
    # 机器关机跨过日切、次日才第一次开机时，rollover 只发生一次；
    # 靠这里保证前一天的快照最终也带上结果。
    if old and old.get("planDate") and old.get("planDate") != today:
        prev_results = {
            w["id"]: {
                "state": w.get("state"),
                "fires": w.get("fireCount", 0),
                "ackedAt": w.get("ackedAt"),
                "missedReason": w.get("missedReason"),
                "label": w.get("label"),
                "expect": w.get("expectFires", 0),
            }
            for w in old.get("windows", [])
        }
        if prev_results:
            fill_snapshot_results(old["planDate"], prev_results)

    # SQL 索引：每天顺手同步一次，让「跨月统计」真的有数据可查。
    # 失败不阻断提醒——metrics.db 只是派生索引，丢了下次还能重建。
    try:
        metrics.metrics_sync(state)
        metrics.metrics_dump()
    except (OSError, ValueError, TypeError) as e:
        print(f"metrics: 自动同步失败 {type(e).__name__}: {e}", file=sys.stderr)

    # 每天跑一次本地多盘备份：真档案在 state/schedule/*.json，丢了就是永久丢失
    try:
        for line in archive.daily_backup(now):
            print(line)
    except Exception as e:  # 备份失败绝不能拖垮提醒本身
        print(f"backup: 备份异常 {type(e).__name__}: {e}")

    # 自检结果走 stdout：有异常才输出，宿主照常投递；正常时一个字都不打印
    for line in take_notices() + hand_edit + self_check(state, now, first_run=(old is None)):
        print(line)

    # 行程推到会话入口：模型不跑命令也知道今天排了什么
    res = brief.sync_brief_block(routine, state, now)
    if res.startswith(("read-failed", "write-failed")):
        print(f"brief: AGENTS.md 同步失败（{res}）", file=sys.stderr)

    # 周一早上那一页小账：用 metrics 现成数据回答「上周靠谱吗」。
    # 只挂在 plan 上——宿主定时任务条数必须恒定，不许为它多开一条 cron。
    try:
        for line in metrics.weekly_report(now) or []:
            print(line)
    except (OSError, ValueError, TypeError, sqlite3.Error) as e:
        print(f"weekly: 周报生成失败 {type(e).__name__}: {e}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 到点判断


def cmd_due(args) -> int:
    """due 入口：先进心跳 attemptAt，判定完整走完才补 lastOkAt。

    `_due_eval` 的所有 return 0（含「正确判定为静默」）都算判定完整走完；
    异常（Validation / Conflict 冒泡到 main 的非 0 退出）不会走到 ok 标记，
    于是 lastOkAt 停在最后一次真正成功的判定上——这正是外部看门狗要读的值。
    """
    routine = load_routine()
    for line in take_notices():
        print(line)
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    touch_heartbeat(now)  # attemptAt：进入即记，失败也留痕
    rc = _due_eval(args, routine, tz, now)
    if rc == 0:
        touch_heartbeat(now, ok=True)  # lastOkAt：判定完整走完（含静默）
    return rc


def _due_eval(args, routine, tz, now) -> int:
    """due 的判定主体。返回 0 表示「判定完整走完」（含正确判为静默）。"""
    state = load_state()
    if not state or state.get("planDate") != now.date().isoformat():
        rc = cmd_plan(argparse.Namespace(now=now.isoformat(), force=False))
        if rc != 0:
            return rc
        state = load_state()
    if state is None:
        print("due: 计划生成后仍没有状态文件，本 tick 放弃", file=sys.stderr)
        return 3

    rev = int(state["rev"])
    changed = False

    # 1) 过期未确认 → missed
    for w in state["windows"]:
        end = datetime.fromisoformat(w["end"])
        if now > end and w["state"] in {"pending", "ringing"}:
            w["state"] = "missed"
            w["missedAt"] = now.isoformat()
            w["missedReason"] = "window_expired"
            changed = True

    # 2) 找命中窗口
    hit = None
    for w in state["windows"]:
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        if start <= now <= end and w["state"] in {"pending", "ringing"}:
            hit = w
            break

    if hit is None:
        if changed:
            state["rev"] = rev + 1
            try:
                atomic_write(STATE, state, expected_rev=rev)
                _record_history(state, routine, ts=now)
                print("due: 已把过期窗口标为 missed", file=sys.stderr)
            except Conflict as e:
                # 契约（issue #4 §11）：due 冲突保持静默，诊断走 stderr，
                # 下一次 due 重试即可。
                print(f"due: 写入冲突（{e}），missed 标记延后到下一 tick", file=sys.stderr)
        return 0

    # 3) 三个条件
    if hit["fireCount"] >= hit["expectFires"]:
        print(f"due: {hit['id']} 已达上限 {hit['expectFires']} 次，静默", file=sys.stderr)
        return 0
    if hit.get("lastFiredAt"):
        last = datetime.fromisoformat(hit["lastFiredAt"])
        gap = (now - last).total_seconds() / 60
        if gap < hit["intervalMin"] - 1:
            print(f"due: 距上次 {gap:.1f} 分钟，未到间隔，静默", file=sys.stderr)
            return 0

    hit["state"] = "ringing"
    hit["fireCount"] += 1
    hit["lastFiredAt"] = now.isoformat()
    if hit.get("oneOff"):
        # 一次性提醒：送达即完成，不需要等确认（对应旧系统 deleteAfterRun 语义）
        hit["state"] = "delivered"
    state["rev"] = rev + 1
    try:
        atomic_write(STATE, state, expected_rev=rev)
    except Conflict as e:
        # 契约（issue #4 §11）：due 冲突必须保持静默——空 stdout、退出 0，
        # 诊断走 stderr。下一次 due 会重试并由成功那次记录 fireCount，绝不重复
        # 投递文案（空 stdout 即「不投递」）。这是与旧实现「宁可重复响一次」的
        # 显式背离：重复投递比晚一 tick 响更糟，而宿主把空 stdout 当作不响。
        print(f"due: 写入冲突（{e}），本次静默，下一 tick 重试", file=sys.stderr)
        return 0
    _record_history(state, routine, ts=now)

    # 状态变了就连带刷新 AGENTS.md 里的行程：模型下次开口时读到的是刚响过什么，
    # 而不是 06:00 生成时的旧版本。写失败只提示，绝不影响这次投递。
    res = brief.sync_brief_block(routine, state, now)
    if res.startswith(("read-failed", "write-failed")):
        print(f"brief: AGENTS.md 同步失败（{res}）", file=sys.stderr)

    n = hit["fireCount"]
    if hit.get("format"):
        # 摘要窗口：文案现算（今天完成什么、还欠什么、响了几次），不靠模型在线。
        # 算不出来就退回普通提醒——宁可少一段内容，不能因此不响。
        try:
            print("\n".join(brief.build_digest(routine, state, hit, now)))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            print(f"digest: 摘要生成失败（{type(e).__name__}: {e}），本次用普通提醒", file=sys.stderr)
        else:
            if hit.get("expectFires", 1) > 1 and n < hit["expectFires"]:
                print(f"回复「{hit['ackHint']}」就不响了。")
            return 0
    head = f"{hit['label']} —— {now.strftime('%H:%M')}"
    if hit.get("review"):
        head += "（复盘：昨天已结束）"
    if hit.get("dueDate"):
        days = (date.fromisoformat(hit["dueDate"]) - now.date()).days
        if days == 0:
            head += "（最后一天！）"
        elif days == 1:
            head += "（明天截止）"
        elif days > 1:
            head += f"（距截止还有 {days} 天）"
        else:
            head += f"（已过期 {-days} 天）"
    if n > 1:
        head += f"（第 {n} 次）"
    lines = [head]
    items = hit.get("checklist") or []
    if items:
        done = set(hit.get("checked") or [])
        left = [i for i in items if i["id"] not in done]
        head = lines[0]
        head += f"（还剩 {len(left)} 项）"
        lines[0] = head
        row = []
        for it in items:
            mark = "☑" if it["id"] in done else "☐"
            row.append(f"{mark} {it['label']}")
        for i in range(0, len(row), 6):
            lines.append(" ".join(row[i : i + 6]))
        # 清单只在第一次响时教一遍怎么勾，之后靠「还剩 N 项」自己看
        if n == 1:
            lines.append(f"逐项说「{items[0]['label']}拿到了」；全部打完说「{hit['ackHint']}」")
    elif hit.get("expectFires", 1) > 1 and (n == 1 or n == hit["expectFires"]):
        # 只响一次的提醒：送达即完成，说「回复就不响了」纯属添乱。
        # 会反复响的：第一次教一遍，最后一次再提醒一遍怎么停，中间靠「第 N 次」自己看。
        lines.append(f"回复「{hit['ackHint']}」就不响了。")
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------- 确认


def cmd_ack(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    state = load_state()
    if not state:
        print("ack: 今天还没有计划", file=sys.stderr)
        return 4

    rev = int(state["rev"])
    cands = []
    for w in state["windows"]:
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        grace_end = end + timedelta(minutes=GRACE_MIN)
        if args.window:
            if w["id"] != args.window:
                continue
        else:
            same_day = state.get("planDate") == now.date().isoformat()
            if not (start <= now <= grace_end or (w["state"] == "missed" and same_day)):
                continue
        if w["state"] in {"acked", "delivered"}:
            continue
        cands.append(w)

    if not cands:
        print("ack: 没有匹配的窗口", file=sys.stderr)
        return 4

    # 优先确认当前在响/待响的窗口，避免把回复误绑到当天更早的 missed 窗口
    active = [w for w in cands if w["state"] in {"pending", "ringing"}]
    pool = active if active else cands
    hit = sorted(pool, key=lambda w: w["start"])[0]
    if hit["state"] not in {"pending", "ringing", "missed"}:
        print(f"ack: {hit['id']} 当前状态 {hit['state']}，不改写", file=sys.stderr)
        return 4

    hit["state"] = "acked"
    hit["ackedAt"] = now.isoformat()
    hit["ackText"] = args.text or ""
    state["rev"] = rev + 1
    try:
        atomic_write(STATE, state, expected_rev=rev)
    except Conflict as e:
        print(f"ack: 写入冲突（{e}）", file=sys.stderr)
        return 2
    print(f"ack: {hit['id']} 已确认", file=sys.stderr)
    _record_history(state, routine, kind="ack", window=hit["id"], note=args.text, ts=now)
    res = brief.sync_brief_block(routine, state, now)
    if res.startswith(("read-failed", "write-failed")):
        print(f"brief: AGENTS.md 同步失败（{res}）", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 清单打勾


def cmd_check(args) -> int:
    """勾选 / 取消勾选清单里的某一项。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    结果看退出码（0 成功），细节在 stderr。
    """
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    state = load_state()
    if not state:
        print("check: 今天还没有计划", file=sys.stderr)
        return 4
    hit = None
    for w in state["windows"]:
        if args.window and w["id"] == args.window:
            hit = w
            break
    if hit is None:
        print(f"check: 没有匹配的窗口 {args.window}", file=sys.stderr)
        return 4
    items = hit.get("checklist") or []
    if not items:
        print("check: 该窗口没有清单", file=sys.stderr)
        return 4

    target = None
    for it in items:
        if args.item == it["id"] or args.item == it["label"]:
            target = it
            break
    if target is None:
        try:
            idx = int(args.item) - 1
            if 0 <= idx < len(items):
                target = items[idx]
        except ValueError:
            pass
    if target is None:
        print(f"check: 清单里没有「{args.item}」", file=sys.stderr)
        return 4

    checked = set(hit.get("checked") or [])
    if args.undo:
        checked.discard(target["id"])
    else:
        checked.add(target["id"])
    hit["checked"] = sorted(checked)

    rev = int(state["rev"])
    state["rev"] = rev + 1
    # 全部打完 → 该窗口视为完成
    if len(checked) == len(items) and not args.undo:
        hit["state"] = "acked"
        hit["ackedAt"] = now.isoformat()
    elif args.undo and hit.get("state") == "acked":
        # 撤销打勾必须把窗口一起退回「没完成」，否则会留下「已确认但清单没勾完」
        # 的矛盾状态：窗口不再响，没勾完的项永远提不到（审计 #8 的遗留问题）
        hit["state"] = "pending"
        hit["ackedAt"] = None
    atomic_write(STATE, state, expected_rev=rev)
    _record_history(
        state,
        routine,
        kind="check",
        window=hit["id"],
        item=target["id"],
        note="undo" if args.undo else None,
        ts=now,
    )
    verb = "已取消" if args.undo else "已打勾"
    print(f"check: {target['label']} {verb}（{len(checked)}/{len(items)}）", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 推迟改期

POSTPONE_REASON = "postponed"


def cmd_postpone(args) -> int:
    """把一个窗口推迟到目标日期。

    一次做完三件事：
      1. 时刻表的日期字段平移到目标日 —— 时刻（HH:MM）不动，所以是「同一时间」
      2. 今天已生成的那一次标为 skipped —— 否则今天仍会响
      3. 明天由 plan 从时刻表重新生成 —— 自动创建，不需要动任何定时任务

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    # 摘要先取，再读内容：这样「读取之后被改过」一定会被发现
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    today = now.date()

    defs = routine.get("alarmWindows") or []
    w = next((x for x in defs if x["id"] == args.window), None)
    if w is None:
        print(f"postpone: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4

    # --- 目标日期
    if args.to:
        try:
            target = date.fromisoformat(args.to)
        except ValueError:
            print(f"postpone: --to 应为 YYYY-MM-DD，收到 {args.to}", file=sys.stderr)
            return 3
    else:
        target = today + timedelta(days=1 if args.days is None else int(args.days))
    if target <= today:
        print(f"postpone: 目标日期 {target.isoformat()} 不晚于今天", file=sys.stderr)
        return 3

    # --- 目标日该窗口会不会被过滤掉（否则明天它根本不会响）
    kind = plan_kind(target, routine)
    allowed = w.get("dateTypes") or []
    if allowed and kind not in allowed:
        names = {"workday": "工作日", "weekend": "周末", "holiday": "节假日"}
        print(
            f"postpone: {target.isoformat()} 是{names.get(kind, kind)}，"
            f"而该窗口的 dateTypes 只在 {'/'.join(allowed)} 生效",
            file=sys.stderr,
        )
        return 3
    if (kind == "weekend" and routine.get("weekendAlarms") is False) or (
        kind == "holiday" and routine.get("holidayAlarms") is False
    ):
        print(
            f"postpone: {target.isoformat()} 是"
            f"{'周末' if kind == 'weekend' else '节假日'}，该日期类型被显式禁用",
            file=sys.stderr,
        )
        return 3

    vf = w.get("validFrom")
    vu = w.get("validUntil")
    due = w.get("dueDate")
    changes: dict = {}

    def _shift(d: str, delta: int) -> str:
        return (date.fromisoformat(d) + timedelta(days=delta)).isoformat()

    def _too_early(what: str) -> int:
        print(f"postpone: 目标日期早于当前的{what}，这是提前不是推迟", file=sys.stderr)
        return 3

    if due:
        # 倒计时窗口：目标日期 = 新的截止日。剩余天数由代码重算。
        old_due = date.fromisoformat(due)
        if target < old_due:
            return _too_early("截止日")
        changes["dueDate"] = target.isoformat()
        if vu and date.fromisoformat(vu) == old_due:
            changes["validUntil"] = target.isoformat()
    elif vf and vu:
        # 一次性窗口（validFrom == validUntil）或区间窗口
        old_from, old_to = date.fromisoformat(vf), date.fromisoformat(vu)
        if args.deadline:
            # 移动截止日：整体平移，时长不变
            if target < old_to:
                return _too_early("截止日")
            delta = (target - old_to).days
            changes["validUntil"] = target.isoformat()
            changes["validFrom"] = _shift(vf, delta)
        else:
            # 移动生效日：整体平移，时长不变
            if target < old_from:
                return _too_early("生效日")
            delta = (target - old_from).days
            changes["validFrom"] = target.isoformat()
            changes["validUntil"] = _shift(vu, delta)
    elif vf:
        # 只有生效日：整个窗口挪过去
        if target < date.fromisoformat(vf):
            return _too_early("生效日")
        changes["validFrom"] = target.isoformat()
    else:
        # 周期窗口（每天都响）：推迟 = 跳过今天这几天，之后照常
        skip = set(w.get("skipDates") or [])
        for i in range((target - today).days):
            skip.add((today + timedelta(days=i)).isoformat())
        changes["skipDates"] = sorted(skip)

    if not changes:
        print(f"postpone: {args.window} 无需改动", file=sys.stderr)
        return 0

    old_values = {k: (k in w, w.get(k)) for k in changes}

    # --- 1. 改时刻表（先改这个：明天有没有提醒取决于它）
    for k, v in changes.items():
        w[k] = v
    try:
        post_digest = atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=digest)
    except Conflict as e:
        print(f"postpone: 时刻表已被改动（{e}），未写入", file=sys.stderr)
        return 2

    # --- 2. 今天这一次不再响
    state = load_state()
    outcome = "not_planned"
    if state:
        srev = int(state["rev"])
        hit = next((x for x in state.get("windows", []) if x["id"] == args.window), None)
        if hit is None:
            outcome = "not_planned"
        elif hit["state"] in {"pending", "ringing"}:
            hit["state"] = "skipped"
            hit["missedAt"] = now.isoformat()
            hit["missedReason"] = POSTPONE_REASON
            # 倒计时窗口：缓存里的截止日也要跟着改，否则今天的文案还写「最后一天」
            if "dueDate" in changes:
                hit["dueDate"] = changes["dueDate"]
            state["rev"] = srev + 1
            try:
                atomic_write(STATE, state, expected_rev=srev)
                _record_history(
                    state,
                    routine,
                    kind="postpone",
                    window=args.window,
                    note=f"{args.to} <- {state.get('planDate')}",
                    ts=now,
                )
                outcome = "skipped"
            except Conflict as e:
                # 回滚时刻表：宁可今天照响，也不要明天没有。
                # 守卫必须是「本次第 1 步写入的那个版本」(post_digest)：
                # 推迟前的 digest 必然不匹配（回滚永远失败），回滚时重读的
                # 当前值又会把别人的并发改动当成自己的基线而覆盖掉。
                for k, (present, val) in old_values.items():
                    if present:
                        w[k] = val
                    else:
                        w.pop(k, None)
                try:
                    atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=post_digest)
                except Conflict as rb_err:
                    # 部分应用（退出码 3，与「校验失败」共用 3，但语义不同）：
                    # 时刻表已成功推迟到 {target}，但今日状态因并发编辑回滚失败、
                    # 未更新——今日这一轮仍可能再响一次。
                    # 恢复：直接重跑同一条 postpone（时刻表已是目标日、幂等），
                    # 或对今日该窗口执行 ack/cancel 收口。诊断已在 stderr 点名冲突。
                    print(
                        f"postpone: 状态冲突（{e}），回滚被拒（{rb_err}）——"
                        f"部分应用：时刻表已推迟到 {target.isoformat()}，"
                        f"今日状态未更新；恢复：重跑同一条 postpone，"
                        f"或对今日该窗口 ack/cancel",
                        file=sys.stderr,
                    )
                    return 3
                print(f"postpone: 状态冲突（{e}），已回滚，未推迟", file=sys.stderr)
                return 2
        else:
            outcome = "already_fired"

    # --- 3. 今天的快照同步：被跳过的那一次不再出现在当天安排里
    if outcome == "skipped":
        # skipped 只在「今天有计划且该窗口还在跑」时才出现
        assert state is not None
        try:
            rest = [
                x
                for x in state.get("windows", [])
                if not (x["id"] == args.window and x.get("state") == "skipped")
            ]
            write_schedule_snapshot(
                schedule_snapshot(
                    routine, state.get("planKind", "workday"), rest, state["planDate"], now.isoformat()
                )
            )
        except OSError as e:
            print(f"postpone: 快照更新失败 {e}", file=sys.stderr)

    notes = {
        "skipped": "今天这一次已跳过",
        "already_fired": "今天这一轮已经结束，明天会再提醒一次",
        "not_planned": "今天没有这一次的计划，明天第一次响",
    }
    print(
        f"postpone: {args.window} → {target.isoformat()}（同一时刻），{notes[outcome]}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------- 取消 / 完成 / 停用

CANCELLED_REASON = "cancelled"


def _write_routine(routine: dict, digest: str | None) -> None:
    """写时刻表。内容摘要做守卫——routines.json 是人会手改的文件。"""
    new_digest = atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=digest)
    seal_routine(new_digest)


# ---------------------------------------------------------------- 手改探测
# 为什么要这个：技能里写着「绝不手改 routines.json」，但那是**文字约束**。
# 弱模型会照着技能里给的 JSON 模板直接写文件（实机就这样把 4 个默认窗口
# 换成了 1 个自己编的），而文字约束拦不住它。这里给一条**可验证**的约束：
# 程序每次写完都记一份摘要，下次 plan 发现摘要对不上，就说明这文件被
# RTC 之外的东西改过——备份原件 + 当场说出来。不阻断提醒，但不再沉默。

ROUTINE_SEAL = "routine-seal.json"

BACKUP_DIR = "backups"


def seal_path() -> Path:
    return WORKSPACE / "state" / ROUTINE_SEAL


def seal_routine(digest: str | None = None) -> None:
    """记下「程序最后一次写出的 routines.json 长什么样」。

    除了摘要，还留一份内容副本：手改把文件写坏时，至少还有一份能照着修的底稿。
    """
    try:
        data = {
            "digest": digest or file_digest(ROUTINE),
            "windows": len((load_json(ROUTINE) or {}).get("alarmWindows") or []),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        p = seal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        last = WORKSPACE / "state" / BACKUP_DIR / "routines.program-last.json"
        last.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROUTINE, last)
    except (OSError, ValueError, TypeError):
        pass  # 印章写不进去不影响提醒本身


def audit_routine(routine: dict) -> list[str]:
    """时刻表被程序之外的东西改过吗？返回要报告给人的行（正常时为空）。"""
    try:
        if not seal_path().exists():
            seal_routine()
            return []
        seal = load_json(seal_path())
        want = seal.get("digest")
        was = seal.get("windows")
    except (Validation, ValueError, TypeError, OSError, AttributeError):
        seal_routine()
        return []

    got = file_digest(ROUTINE)
    if want and got == want:
        return []

    now_windows = len(routine.get("alarmWindows") or [])
    backup = None
    try:
        bdir = WORKSPACE / "state" / BACKUP_DIR
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = bdir / f"routines.handedit-{stamp}.json"
        shutil.copyfile(ROUTINE, backup)
    except OSError:
        backup = None

    msg = (
        f"⚠️ {ROUTINE.name} 被 RTC 之外的东西改过"
        f"（上次程序写出 {was if was is not None else '?'} 个窗口，现在是 {now_windows} 个）"
    )
    if backup:
        msg += f"；原件已备份到 {backup}"
    msg += "。改时刻表请用 set-time / postpone / disable 命令，别手写 JSON。"
    return [msg]


def _prune_expired_oneoffs(routine: dict, today: str) -> list[str]:
    """删掉已经过期的一次性提醒条目，返回被删的 id。

    一次性窗口只存在于 validFrom~validUntil 那一天，过期后既不会再响、也不会
    自己消失——不清理就会一直躺在 routines.json 里（实机上还留着 11-14 的那条）。
    只认「oneOff 且 validUntil 早于今天」；周期窗口、倒计时窗口一律不碰。
    """
    defs = routine.get("alarmWindows") or []
    dead_ids = {
        w["id"] for w in defs if w.get("oneOff") and (w.get("validUntil") or w.get("validFrom") or "") < today
    }
    if not dead_ids:
        return []
    routine["alarmWindows"] = [w for w in defs if w.get("id") not in dead_ids]
    return sorted(dead_ids)


def _find_window(routine: dict, wid: str) -> dict | None:
    for w in routine.get("alarmWindows", []) or []:
        if w["id"] == wid:
            return w
    return None


def _mark_state(state: dict | None, wid: str, new_state: str, now: datetime, reason: str) -> str:
    """把今天 state 里某个窗口改成给定终态。返回结果标记。"""
    if not state:
        return "not_planned"
    hit = next((w for w in state.get("windows", []) if w["id"] == wid), None)
    if hit is None:
        return "not_planned"
    if hit["state"] in {"acked", "delivered", "cancelled"}:
        return "already_final"
    if new_state == "cancelled":
        hit["state"] = "cancelled"
        hit["cancelledAt"] = now.isoformat()
        hit["missedAt"] = now.isoformat()
        hit["missedReason"] = reason
    elif new_state == "acked":
        hit["state"] = "acked"
        hit["ackedAt"] = now.isoformat()
        hit["ackText"] = reason or ""
        hit["missedAt"] = None
        hit["missedReason"] = None
    return "changed"


def cmd_set_time(args) -> int:
    """改窗口的起止时刻（HH:MM）。只写时刻表，下次 plan 生效（默认次日）。

    stdout 保持为空：由用户交互触发，不是定时任务，不该被投递出去。
    改的是「时刻」不是「日期」，不会改变今天已经生成的窗口。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    defs = routine.get("alarmWindows") or []
    w = next((x for x in defs if x["id"] == args.window), None)
    if w is None:
        print(f"set-time: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4

    def parse_hhmm(s, what):
        try:
            h, m = (int(x) for x in s.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return time(h, m)
        except ValueError:
            print(f"set-time: --{what} 应为 HH:MM，收到 {s}", file=sys.stderr)
            return None

    start = parse_hhmm(args.start, "start")
    if start is None:
        return 3
    if args.end is None:
        eh, em = (int(x) for x in w["end"].split(":"))
        end = time(eh, em)
    else:
        end = parse_hhmm(args.end, "end")
        if end is None:
            return 3

    if end <= start:
        print(f"set-time: end ({args.end or w['end']}) 必须晚于 start ({args.start})", file=sys.stderr)
        return 3

    w["start"] = args.start
    if args.end is not None:
        w["end"] = args.end

    # 窗口长度变化 -> 重算 expectFires（与 plan 的算术校验保持一致）
    if not (w.get("dueDate") or w.get("checklist") or w.get("oneOff")):
        w["expectFires"] = expect_fires(start, end, int(w["intervalMin"]))

    try:
        atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=digest)
    except Conflict as e:
        print(f"set-time: 时刻表已被改动（{e}），未写入", file=sys.stderr)
        return 2
    print(
        f"set-time: {args.window} 改为 {args.start}"
        + (f"–{args.end}" if args.end else "")
        + "，下次 plan 生效（默认次日）",
        file=sys.stderr,
    )
    return 0


def cmd_cancel(args) -> int:
    """取消一个安排。

    两层含义，按窗口类型自动判断：
      - 有期限的窗口（一次性 / 倒计时 / 有效期区间）→ 取消整个安排：
        有效期截断到今天，今天这次标 `cancelled`，以后不再生成
      - 无限期的周期窗口 → 只取消今天这一次（明天照常）。
        周期窗口没有「结束」，想永久停用请用 `disable`。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    today = now.date().isoformat()

    w = _find_window(routine, args.window)
    if w is None:
        print(f"cancel: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4
    if w.get("disabled"):
        print(f"cancel: {args.window} 已经停用了", file=sys.stderr)
        return 4

    # 未来某天的一次性安排 —— 直接停用那个窗口
    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"cancel: --date 应为 YYYY-MM-DD，收到 {args.date}", file=sys.stderr)
            return 3
        if target.isoformat() != today:
            # 先确认那天真的有这个窗口 —— 否则「取消」会静默无效
            if not in_valid_range(w, target.isoformat()):
                print(
                    f"cancel: {args.window} 在 {target.isoformat()} 不存在"
                    f"（有效期 {w.get('validFrom') or '不限'} ~ {w.get('validUntil') or '不限'}）",
                    file=sys.stderr,
                )
                return 4
            if w.get("oneOff") and w.get("validFrom") == target.isoformat():
                # 这个窗口只存在于那一天 → 整件事取消
                w["disabled"] = True
                _write_routine(routine, digest)
                print(f"cancel: {args.window} 在 {target.isoformat()} 的那次已取消", file=sys.stderr)
                return 0
            skip = set(w.get("skipDates") or [])
            skip.add(target.isoformat())
            w["skipDates"] = sorted(skip)
            _write_routine(routine, digest)
            print(f"cancel: {args.window} 在 {target.isoformat()} 不再响", file=sys.stderr)
            return 0

    # 取消今天这一次
    state = load_state()
    outcome = _mark_state(state, args.window, "cancelled", now, CANCELLED_REASON)
    if outcome == "changed":
        # _mark_state 只在 state 存在时返回 changed
        assert state is not None
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"cancel: 状态冲突（{e}）", file=sys.stderr)
            return 2
        _record_history(state, routine, kind="cancel", window=args.window, ts=now)

    # 安排本身的去留
    # 一次性窗口：取消 = 这件事不做了 → 彻底停用（它本来也只存在于某一天）
    # 有期限的安排（倒计时 / 有效期区间）：有效期截断到今天 → 以后不再生成
    # 无限期的周期窗口：没有「这件事」可取消，只取消今天这一次
    truncated = False
    if w.get("oneOff"):
        w["disabled"] = True
        truncated = True
    elif w.get("dueDate"):
        # 倒计时：取消 = 整段停（含复盘日）——标记 canceled，次日不再生成复盘
        w["dueDate"] = today
        w["canceled"] = True
        if w.get("validUntil") and w["validUntil"] > today:
            w["validUntil"] = today
        truncated = True
    elif w.get("validUntil"):
        if w["validUntil"] > today:
            w["validUntil"] = today
            truncated = True
        else:
            truncated = True  # 已经到期的安排，截断后等于不再生成
    if truncated:
        _write_routine(routine, digest)

    if outcome == "already_final":
        note = "这一轮已经结束了"
    elif truncated:
        note = "已取消，以后不再提醒"
    else:
        note = "今天这次已取消（明天照常；要永久停用请用 disable）"
    print(f"cancel: {args.window} {note}", file=sys.stderr)
    return 0


def cmd_complete(args) -> int:
    """完成一件事。

    语义 = 「做完了，以后不用提醒我」：记录确认 + 永久停用这个窗口。
    这与 `ack` 的区别：`ack` 只停今天这一次，`complete` 连以后也不再提醒。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])

    w = _find_window(routine, args.window)
    if w is None:
        print(f"complete: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4

    state = load_state()
    outcome = _mark_state(state, args.window, "acked", now, args.text or "")
    if outcome == "changed":
        # _mark_state 只在 state 存在时返回 changed
        assert state is not None
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"complete: 状态冲突（{e}）", file=sys.stderr)
            return 2
        _record_history(
            state,
            routine,
            kind="complete",
            window=args.window,
            note=args.text,
            ts=now,
        )

    w["disabled"] = True
    _write_routine(routine, digest)

    notes = {
        "changed": "已完成，以后不再提醒",
        "already_final": "已完成（这一轮已结束），以后不再提醒",
        "not_planned": "已完成，以后不再提醒",
    }
    print(f"complete: {args.window} {notes[outcome]}", file=sys.stderr)
    return 0


def cmd_disable(args) -> int:
    """永久停用一个窗口（「以后别提醒我了」）。

    与 `cancel` 的区别：cancel 取消一个安排，disable 关掉一类提醒。
    disable 可以随时用 `enable` 撤销。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])

    w = _find_window(routine, args.window)
    if w is None:
        print(f"disable: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4
    if w.get("disabled"):
        print(f"disable: {args.window} 已经停用了", file=sys.stderr)
        return 4

    # 今天已生成的那次也不能再响
    state = load_state()
    outcome = _mark_state(state, args.window, "cancelled", now, CANCELLED_REASON)
    if outcome == "changed":
        # _mark_state 只在 state 存在时返回 changed
        assert state is not None
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"disable: 状态冲突（{e}）", file=sys.stderr)
            return 2
        _record_history(state, routine, kind="disable", window=args.window, ts=now)

    w["disabled"] = True
    _write_routine(routine, digest)
    print(f"disable: {args.window} 已停用，以后不再提醒", file=sys.stderr)
    return 0


def cmd_enable(args) -> int:
    """重新启用一个被停用的窗口。"""
    digest = file_digest(ROUTINE)
    routine = load_routine()
    # --now 接受但不使用：启用不涉及时间判断，留着是为了与其它命令的参数约定一致
    w = _find_window(routine, args.window)
    if w is None:
        print(f"enable: 时刻表里没有窗口 {args.window}", file=sys.stderr)
        return 4
    if not w.get("disabled"):
        print(f"enable: {args.window} 本来就是启用的", file=sys.stderr)
        return 4
    w["disabled"] = False
    _write_routine(routine, digest)
    with contextlib.suppress(sqlite3.Error, OSError, ValueError, TypeError):
        hist.log_event("enable", args.window)
    print(f"enable: {args.window} 已重新启用", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rtc", description="RTC-kit 确定性核心")
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan")
    sp.add_argument("--now")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sd = sub.add_parser("due")
    sd.add_argument("--now")
    sd.set_defaults(func=cmd_due)

    ssim = sub.add_parser("simulate", help="预演某一天会响几次（只读，不改任何文件）")
    ssim.add_argument("--date", help="YYYY-MM-DD（默认明天）")
    ssim.add_argument("--json", action="store_true")
    ssim.add_argument("--now")
    ssim.set_defaults(func=cmd_simulate)

    spa = sub.add_parser("pause", help="临时全静音（不改时刻表）")
    spa.add_argument("--days", type=int, help="停几天")
    spa.add_argument("--until", help="停到哪天（YYYY-MM-DD，不含当天）")
    spa.add_argument("--reason", help="为什么停（可写「出差」这种词）")
    spa.add_argument("--now")
    spa.set_defaults(func=cmd_pause)

    srs = sub.add_parser("resume", help="结束暂停，窗口立刻回来")
    srs.add_argument("--now")
    srs.set_defaults(func=cmd_resume)

    stt = sub.add_parser("touch-tasks", help="改 tasks.md 之前先留底稿（内容没变就不写）")
    stt.set_defaults(func=archive.cmd_touch_tasks)

    sdl = sub.add_parser("delivery", help="查投递回执：某天到底发出去没有")
    sdl.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    sdl.add_argument("--json", action="store_true")
    sdl.set_defaults(func=cmd_delivery)

    sdr = sub.add_parser("delivery-record", help="宿主投完一条后写一行回执（给包装脚本用）")
    sdr.add_argument("--window", help="窗口 id（不知写 -）")
    sdr.add_argument("--target", required=True, help="投到哪个渠道 / 设备")
    sdr.add_argument("--result", required=True, help="ok / fail")
    sdr.add_argument("--error", help="失败原因，一行话")
    sdr.add_argument("--at", help="投递时刻 ISO（默认现在）")
    sdr.set_defaults(func=cmd_delivery_record)

    sa = sub.add_parser("ack")
    sa.add_argument("--window")
    sa.add_argument("--text")
    sa.add_argument("--now")
    sa.set_defaults(func=cmd_ack)

    schk = sub.add_parser("check")
    schk.add_argument("--window", required=True, help="窗口 id")
    schk.add_argument("--item", required=True, help="清单项：序号或名称")
    schk.add_argument("--undo", action="store_true", help="取消勾选")
    schk.add_argument("--now")
    schk.set_defaults(func=cmd_check)

    spo = sub.add_parser("postpone")
    spo.add_argument("--window", required=True, help="窗口 id")
    spo.add_argument("--to", help="目标日期 YYYY-MM-DD（默认明天）")
    spo.add_argument("--days", type=int, help="往后推几天（默认 1）")
    spo.add_argument("--deadline", action="store_true", help="移动截止日而不是生效日")
    spo.add_argument("--now")
    spo.set_defaults(func=cmd_postpone)

    scan = sub.add_parser("cancel")
    scan.add_argument("--window", required=True, help="窗口 id")
    scan.add_argument("--date", help="取消未来某天的那次（YYYY-MM-DD）")
    scan.add_argument("--now")
    scan.set_defaults(func=cmd_cancel)

    scom = sub.add_parser("complete")
    scom.add_argument("--window", required=True, help="窗口 id")
    scom.add_argument("--text", help="完成时的备注")
    scom.add_argument("--now")
    scom.set_defaults(func=cmd_complete)

    sdis = sub.add_parser("disable")
    sdis.add_argument("--window", required=True, help="窗口 id")
    sdis.add_argument("--now")
    sdis.set_defaults(func=cmd_disable)

    sen = sub.add_parser("enable")
    sen.add_argument("--window", required=True, help="窗口 id")
    sen.add_argument("--now")
    sen.set_defaults(func=cmd_enable)

    sst = sub.add_parser("set-time")
    sst.add_argument("--window", required=True, help="窗口 id")
    sst.add_argument("--start", required=True, help="新起始时刻 HH:MM")
    sst.add_argument("--end", help="新结束时刻 HH:MM（省略=保持不变）")
    sst.set_defaults(func=cmd_set_time)

    ssch = sub.add_parser("schedule")
    ssch.add_argument("date", nargs="?", help="YYYY-MM-DD")
    ssch.add_argument("--month", help="YYYY-MM")
    ssch.add_argument("--json", action="store_true")
    ssch.add_argument("--json-only", action="store_true", help=argparse.SUPPRESS)
    ssch.set_defaults(func=brief.cmd_schedule)

    shis = sub.add_parser("history")
    shis.add_argument("date", nargs="?", help="YYYY-MM-DD")
    shis.add_argument("--month", help="YYYY-MM")
    shis.add_argument("--range", help="<起>..<止>，两端都是 YYYY-MM-DD")
    shis.add_argument("--json", action="store_true")
    shis.set_defaults(func=brief.cmd_history)

    sev = sub.add_parser("events", help="看某个窗口的状态迁移轨迹（history.db 的 events 表）")
    sev.add_argument("window", nargs="?", help="窗口 ID；不写则看全部")
    sev.add_argument("--since", help="起始时间（ISO）")
    sev.add_argument("--until", help="结束时间（ISO）")
    sev.add_argument("--json", action="store_true")
    sev.set_defaults(func=brief.cmd_events)

    sst = sub.add_parser("stats", help="按窗口 / 日期 / 月份聚合执行结果")
    sst.add_argument("--group-by", choices=["window", "date", "month"], default="window")
    sst.add_argument("--missed-only", action="store_true")
    sst.add_argument("--json", action="store_true")
    sst.set_defaults(func=brief.cmd_stats)

    ss = sub.add_parser("status")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=brief.cmd_status)

    sb = sub.add_parser("brief")
    sb.add_argument("--now")
    sb.add_argument("--sync", action="store_true", help="同时刷新 AGENTS.md 的行程区块")
    sb.set_defaults(func=brief.cmd_brief)

    sm = sub.add_parser("metrics")
    sm.add_argument("action", choices=["sync", "report", "schedule"])
    sm.add_argument("--days", type=int, default=30)
    sm.add_argument("--date", help="YYYY-MM-DD（用于 metrics schedule）")
    sm.set_defaults(func=metrics.cmd_metrics)

    sb = sub.add_parser("backup", help="把档案压成 zip 写到其他本地盘（每日 plan 会自动跑一次）")
    sb.add_argument("--to", action="append", help="目标目录，可重复；不写则自动挑非源盘")
    sb.add_argument("--keep", type=int, default=0, help="每个盘保留几份（默认取配置，30）")
    sb.set_defaults(func=archive.cmd_backup)

    sg = sub.add_parser("gitarchive", help="把档案按天提交进私人 git 仓库并推送（GitHub/Gitee/自建都行）")
    sg.add_argument("--dir", help="本地镜像仓库目录，默认 ~/RTC-archive")
    sg.add_argument("--remote", help="远端地址：https / ssh / 本地裸库路径都可以")
    sg.set_defaults(func=archive.cmd_gitarchive)

    args = p.parse_args(argv)
    if args.workspace:
        set_workspace(Path(args.workspace))

    try:
        return args.func(args)
    except Validation as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 3
    except Conflict as e:
        print(f"状态冲突：{e}", file=sys.stderr)
        return 2
