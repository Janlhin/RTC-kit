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
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class ContractViolation(Validation):
    """配置与契约不符（未知字段等），不是文件损坏——不能拿底稿覆盖用户刚写的意图。"""


class _DueConflict(Exception):
    """内部信号：本次 due 撞上了状态写入冲突，账本没写成。

    不对外冒泡——cmd_due 接住它，按契约保持「空 stdout + 退出 0」，
    且**不跑自检、不投递告警**：自检读的是冲突前的旧账本，此时的告警是假的。
    """


# ---------------------------------------------------------------- 基础设施


DEFAULT_TZ_NAME = "Asia/Shanghai"
DEFAULT_OFFSET = "+08:00"


def _fixed_offset(offset: str) -> timezone:
    """旧格式：把 ``+HH:MM`` 解析成固定偏移时区。新配置走 IANA 名，这里只做回退兼容。"""
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def parse_iso(s: str, fallback_tz: tzinfo) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz)
    return dt


def _load_zoneinfo(name: str) -> ZoneInfo | None:
    """按 IANA 名取时区；缺 tzdata / 键不存在 / 名字非法时返回 None，不抛栈。"""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        return None


def resolve_tz(routine: dict[str, Any]) -> tzinfo:
    """解析时刻表运行时区：优先 IANA 名，回退旧固定偏移。

    回退链（顺序即优先级）：

    1. ``timezone``（IANA 名，如 ``America/New_York``）能加载 → 跟随 DST 的 ZoneInfo；
    2. 否则 ``tzOffset``（``+08:00`` 旧格式）→ 固定偏移，行为与改造前逐字节一致；
    3. 两者都缺 → 默认 ``+08:00``。

    Windows 等环境缺 tzdata 时 ZoneInfo 找不到键；此时不抛栈、也不静默吞掉：
    记一条 NOTICES（plan/brief 播一次），再回退到固定偏移。
    """
    name = str(routine.get("timezone") or "").strip()
    if name:
        zi = _load_zoneinfo(name)
        if zi is not None:
            return zi
        offset = routine.get("tzOffset")
        fallback = offset if isinstance(offset, str) and offset else DEFAULT_OFFSET
        NOTICES.append(f"时区 {name} 不可用（缺 tzdata 或不是合法 IANA 名），回退固定偏移 {fallback}")
        return _fixed_offset(fallback)
    offset = routine.get("tzOffset")
    if isinstance(offset, str) and offset:
        return _fixed_offset(offset)
    return _fixed_offset(DEFAULT_OFFSET)


def now_for(routine: dict[str, Any]) -> datetime:
    return datetime.now(resolve_tz(routine))


def zone_label(routine: dict[str, Any]) -> str:
    """写进状态/快照的时区标识：优先 IANA 名，旧配置退回偏移字符串。"""
    return str(routine.get("timezone") or routine.get("tzOffset") or DEFAULT_TZ_NAME)


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


def load_json(path: Path) -> Any:
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
def _exclusive_file_lock(path: Path) -> Iterator[None]:
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
    state: dict[str, Any] | None,
    routine: dict[str, Any] | None = None,
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


def atomic_write(
    path: Path, data: dict[str, Any], expected_rev: int | None, expected_digest: str | None = None
) -> str:
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


_ROUTINE_KEYS = ("version", "alarmWindows", "dateTypes")

# contract/routine.schema.json 的 alarmWindow.properties 全集（additionalProperties: false）。
# 运行时只认这些键：多出来的字段是「空气开关」——写了不报错、也永远不生效。
_WINDOW_KEYS = frozenset(
    {
        "ackHints",
        "canceled",
        "checklist",
        "dateTypes",
        "disabled",
        "dueDate",
        "end",
        "expectFires",
        "format",
        "id",
        "intervalMin",
        "label",
        "oneOff",
        "seq",
        "skipDates",
        "start",
        "validFrom",
        "validUntil",
    }
)


def _check_window_keys(index: int, w: object) -> None:
    """未知字段一律拒绝：它们不会被排程读取，写了等于没写，比漏响更难查。"""
    if not isinstance(w, dict):
        raise ContractViolation(f"alarmWindows[{index}] 应是对象，实际是 {type(w).__name__}")
    unknown = sorted(set(w) - _WINDOW_KEYS)
    if not unknown:
        return
    raise ContractViolation(
        f"alarmWindows[{index}]（{w.get('id') or '无 id'}）含契约未定义字段：{', '.join(unknown)}。"
        "这些字段不会被排程读取——写了等于没写。"
        "合法字段：" + "、".join(sorted(_WINDOW_KEYS)) + "。"
        "要停止提醒请用命令：ack（今天结束）/ cancel（取消安排）/ disable（永久停用），不要自造 status 字段。"
    )


# 运行中攒下的「要跟人说一句」的话（自愈、手改），由 plan/due 播一次就清空
NOTICES: list[str] = []


def load_routine(read_only: bool = False) -> dict[str, Any]:
    """读时刻表并做最小形状校验；坏了就先想办法自愈。

    实机教训：模型把 routines.json 写成了 0 字节，plan/due 双双失败，
    用户一整天收不到任何提醒——而程序手里明明还留着上一份好文件。
    所以校验失败时先尝试从程序底稿恢复，恢复了就在输出里说明一句。

    `read_only=True` 用于「只读素材」类调用（早报素材生成）：坏文件照常报
    Validation，**绝不写任何文件**——修复时刻表是 plan / 用户交互命令的职责，
    一个被描述为只读的脚本不能顺手改配置。

    契约违规（未知字段）不触发自愈：那不是文件损坏，拿底稿覆盖只会把用户
    刚写下的意图一起抹掉，而用户还以为自己已经关掉了提醒。
    """
    try:
        return _validate_routine()
    except ContractViolation:
        raise
    except Validation:
        if read_only:
            raise
        notice = _restore_from_last_backup()
        if not notice:
            raise
        NOTICES.append(notice)
        return _validate_routine()


def _validate_routine() -> dict[str, Any]:
    """读时刻表并做最小形状校验。

    「JSON 合法但结构不对」（整个文件被写成 `[]`、缺时区标识）同样会让
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
    if not routine.get("timezone") and not routine.get("tzOffset"):
        raise Validation(
            f"{ROUTINE} 缺时区：至少给 timezone（IANA 名，如 Asia/Shanghai）"
            "或 tzOffset（旧格式固定偏移，如 +08:00）；参考 rules/routines.example.md"
        )
    if not isinstance(routine.get("alarmWindows"), list):
        raise Validation(f"{ROUTINE} 的 alarmWindows 应是数组")
    for i, w in enumerate(routine["alarmWindows"]):
        _check_window_keys(i, w)
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
    if not data.get("timezone") and not data.get("tzOffset"):
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


def load_state() -> dict[str, Any] | None:
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
        prev: dict[str, Any] = {}
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


def self_check(
    state: dict[str, Any],
    now: datetime,
    first_run: bool = False,
    heartbeat: tuple[datetime | None, datetime | None] | None = None,
) -> list[str]:
    """只报告「任务在跑但可能没送达」的信号，正常时不输出任何东西。

    `heartbeat` 用于在 due 内部调用时传入「本次刷新之前」的心跳——否则
    due 刚写下的 attemptAt 会让「从没成功判定」这条检查永远看不到问题。
    """
    probs: list[str] = []
    if heartbeat is None:
        hb, hb_attempt = read_heartbeat(), read_heartbeat_attempt()
    else:
        hb, hb_attempt = heartbeat

    if hb is None:
        # attempt 新鲜 = due 正在判定中（它会在出口补写 lastOkAt）或持续被调用，
        # 此时不报——pause 期 due 每 tick 都在跑、只是判为静默，属健康。
        # 若 due 因状态损坏失败退出，lastOkAt 永远不写，attemptAt 会随时间变旧，
        # 次日这里就会告警——这正是要抓的场景。
        # attempt 也是空 = 全新部署的第一次运行：还没机会成功，不报噪音。
        if hb_attempt is not None:
            attempt_fresh = 0 <= (now - hb_attempt).total_seconds() / 60 <= HEARTBEAT_STALE_MIN
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


def alerts_path() -> Path:
    return WORKSPACE / "state" / "guard.json"


def emit_alert_once(key: str, line: str, now: datetime) -> bool:
    """同一问题同一天只投递一次。

    due 每 5 分钟跑一趟：同一个毛病反复刷屏，用户会把它当噪音关掉，
    然后真的坏掉那次也看不见了。guard.json 写失败时按「不投递」处理——
    宁可少一条告警，也不在坏盘上反复尝试。
    """
    p = alerts_path()
    today = now.date().isoformat()
    keys: list[str] = []
    try:
        if p.exists():
            data = load_json(p)
            if isinstance(data, dict) and data.get("date") == today and isinstance(data.get("keys"), list):
                keys = [str(k) for k in data["keys"]]
    except (Validation, ValueError, TypeError, OSError):
        keys = []
    fp = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    if fp in keys:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"date": today, "keys": (keys + [fp])[-200:]}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError:
        return False
    print(line)
    return True


# ---------------------------------------------------------------- 日程快照


def schedule_snapshot(
    routine: dict[str, Any],
    kind: str,
    windows: list[dict[str, Any]],
    day_iso: str,
    generated_at: str,
    results: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "timezone": zone_label(routine),
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


def fill_snapshot_results(day_iso: str, results: dict[str, Any]) -> bool:
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


def existing_snapshot_results(day_iso: str) -> dict[str, Any]:
    """读回当天快照已有的 results：重建剩余计划时不抹掉无关的执行结果。"""
    path = SCHEDULE_DIR / f"{day_iso}.json"
    if not path.exists():
        return {}
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    results = snap.get("results")
    return results if isinstance(results, dict) else {}


def write_schedule_snapshot(snap: dict[str, Any]) -> Path:
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


def plan_kind(d: date, routine: dict[str, Any]) -> str:
    overrides = routine.get("dateTypes", {}).get("holidayOverrides", [])
    iso = d.isoformat()
    for o in overrides:
        if o.get("date") == iso:
            return cast(str, o["type"])
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
    if interval <= 0:
        # 零/负间隔是配置错误，不是运行时意外：除零会把整份 plan 炸掉，
        # 连带旁边健康窗口一起不响。抛 Validation 让单窗口边界降级为
        # 「跳过该窗口 + 告警」，与 #75 的降级口径一致。
        raise Validation(f"intervalMin 必须是正整数，当前是 {interval}")
    return minutes // interval + 1


def in_valid_range(w: dict[str, Any], day_iso: str) -> bool:
    """窗口是否在有效期内（闭区间）。任一端省略 = 无限期。"""
    vf = w.get("validFrom")
    vu = w.get("validUntil")
    if vf and day_iso < vf:
        return False
    return not (vu and day_iso > vu)


def day_disabled(routine: dict[str, Any], kind: str) -> bool:
    """该日期类型被契约显式禁用（weekendAlarms / holidayAlarms = false）。

    禁用日 = 合法空日：静默，不报配置错误。
    """
    return (kind == "weekend" and routine.get("weekendAlarms") is False) or (
        kind == "holiday" and routine.get("holidayAlarms") is False
    )


def window_type(defn: dict[str, Any]) -> str:
    """四类日程的身份：预约 = 一次性 + 倒计时；倒计时不是独立类型，是 fixed 的一种活法。

    `cancelled` 不是第四种类型，是覆盖在任意类型之上的粘性状态（见 issue #9 §10.2）。
    """
    if defn.get("oneOff"):
        return "appointment" if defn.get("dueDate") else "oneoff"
    return "fixed"


def countdown_review_day(defn: dict[str, Any]) -> str | None:
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


def _window_bounds(w: dict[str, Any]) -> tuple[time, time]:
    """解析并校验单窗口的 start/end；格式或取值错统一转成 Validation。

    降级与严格两条路径都必须先过这里：裸 `int(x)` 抛的是 ValueError，
    既绕过了 errors 通道，也让 `schedule` 这类只读视图直接抛栈（issue #82）。
    """
    try:
        sh, sm = (int(x) for x in str(w["start"]).split(":"))
        eh, em = (int(x) for x in str(w["end"]).split(":"))
        return time(sh, sm), time(eh, em)
    except (KeyError, ValueError, TypeError) as e:
        raise Validation(f"start/end 格式不对（{e}）") from e


def _check_window_arithmetic(w: dict[str, Any]) -> None:
    """单窗口的数值校验：只拦「算错」这类，避免一条坏配置拖垮整天。"""
    if w.get("oneOff") or w.get("dueDate") or (w.get("checklist") or []):
        return
    start, end = _window_bounds(w)
    try:
        interval = int(w["intervalMin"])
    except (KeyError, ValueError, TypeError) as e:
        raise Validation(f"intervalMin 格式不对（{e}）") from e
    exp = expect_fires(start, end, interval)
    if w.get("expectFires") and int(w["expectFires"]) != exp:
        raise Validation(f"expectFires={w['expectFires']} 与算术值 {exp} 不符")


def occurrence_id(w: dict[str, Any], day_iso: str) -> str:
    """这一次提醒的稳定身份（ocid），日历导出拿它当 UID。

    两类窗口身份不同：

    - `oneOff`：整个模板只活一次，身份跟着模板走（`id@rtc`）。改日期是「同一条
      提醒换了时间」，日历里该是一次更新，而不是旧的消失、新的凭空出现。
    - 其余（周期 / 倒计时 / 清单）：每天都算一次独立事件，身份带日期
      （`id@YYYY-MM-DD`），否则日历会把昨天和今天当成同一条的后一次修订。

    身份只在 plan 生成时定一次，之后靠 `carry_forward` 带过去——推迟、改时刻都不换身份。
    """
    if w.get("oneOff"):
        return f"{w['id']}@rtc"
    return f"{w['id']}@{day_iso}"


def build_windows(
    routine: dict[str, Any],
    d: date,
    kind: str,
    now: datetime,
    strict: bool = True,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按时刻表生成某天的窗口。

    `errors` 传给「必须继续跑」的调用方（plan）：单个窗口的数值错误降级为
    「跳过该窗口 + 记一条告警」，而不是让整份计划校验失败——一条写错的
    提醒不该把一整天的账本都清空（issue #75）。其他调用方不传，保持 fail fast：
    抛的也是 Validation（不是裸 ValueError），所以只读视图只会「报错」不会抛栈。
    """
    tz = resolve_tz(routine)
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
        # 单窗口边界：解析/算术失败只跳它自己；调用方没给 errors 就 fail fast
        # （预演要「你必须修」，不能被静默跳过糊过去）。
        try:
            t_start, t_end = _window_bounds(w)
            try:
                interval = int(w["intervalMin"])
            except (KeyError, ValueError, TypeError) as e:
                raise Validation(f"intervalMin 格式不对（{e}）") from e
            _check_window_arithmetic(w)
        except Validation as e:
            if errors is None:
                raise
            errors.append(f"{w.get('id', '?')}：{e}")
            continue
        start = datetime.combine(d, t_start, tzinfo=tz)
        end = datetime.combine(d, t_end, tzinfo=tz)
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
            exp = expect_fires(t_start, t_end, interval)
            if w.get("expectFires") and int(w["expectFires"]) != exp:
                raise Validation(f"窗口 {w['id']} 的 expectFires={w['expectFires']} 与算术值 {exp} 不符")
        expired = now > end
        out.append(
            {
                "id": w["id"],
                "ocid": occurrence_id(w, day_iso),
                "seq": int(w.get("seq") or 0) if one_off else 0,
                "label": label,
                "type": window_type(w),
                "review": is_review,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "intervalMin": interval,
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
        if errors:
            # 有窗口被跳过：账本为空这件事必须由调用方告警，不能被「空日」掩盖
            return []
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


def expected_today_ids(
    routine: dict[str, Any], now: datetime, errors: list[str] | None = None
) -> set[str] | None:
    """今天按时刻表应该出现在账本里的窗口 id；配置整份算不出来时返回 None。

    这是「设了提醒却没响」的入口检查：时刻表有、账本没有，due 就不可能响它。

    `errors` 传进来就按「单窗口坏了只跳过它」算：坏窗口进 errors，其余窗口的
    id 照常返回。否则一条写错的窗口会让整本对账关掉（`None`），把另一个真缺失
    的提醒一起掩盖——一个已知缺陷不该藏住另一个缺陷。

    只有「整份时刻表读不懂」（配置语法/结构错）才是 `None`：那种情况下连
    「今天该有哪几个窗口」都无从谈起。
    """
    try:
        kind = plan_kind(now.date(), routine)
        if day_disabled(routine, kind):
            return set()
        skipped: list[str] = []
        ids = {w["id"] for w in build_windows(routine, now.date(), kind, now, strict=False, errors=skipped)}
        if errors is not None:
            errors.extend(skipped)
        return ids
    except (Validation, KeyError, ValueError, TypeError):
        return None


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


def _checklist_defs(items_def: list[Any]) -> list[dict[str, Any]]:
    """把法则里的清单定义（字符串或 {id,label}）统一成 {id,label}。"""
    out: list[dict[str, Any]] = []
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


def _migrate_checklist(prev: dict[str, Any], w: dict[str, Any]) -> None:
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


def carry_forward(old: dict[str, Any] | None, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            _carry_occurrence_identity(p, w)
            merged.append(w)
    return merged


def _carry_occurrence_identity(old: dict[str, Any], new: dict[str, Any]) -> None:
    """同日重建时把 ocid 带过去；开始时间变了就 seq+1（日历据此认「同一条的新版本」）。"""
    if not old.get("ocid"):
        return
    new["ocid"] = old["ocid"]
    seq = int(old.get("seq") or 0)
    if new.get("start") != old.get("start"):
        seq += 1
    new["seq"] = seq


def rollover(old: dict[str, Any] | None, today_iso: str) -> list[dict[str, Any]]:
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
    dedup: dict[tuple[Any, Any], dict[str, Any]] = {}
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


def simulate_day(routine: dict[str, Any], d: date, paused: dict[str, Any] | None = None) -> dict[str, Any]:
    """预演某一天会怎样：只读，绝不落盘。

    判定全部复用 plan 的那一套（有效期 / skipDates / disabled / dateTypes / 算术校验），
    唯一差别是把"现在"钉在那天的 00:00——这样计划里的窗口不会被判成"已过期"。
    """
    kind = plan_kind(d, routine)
    tz = resolve_tz(routine)
    day_start = datetime.combine(d, time(0, 0), tzinfo=tz)
    day_iso = d.isoformat()

    windows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
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


def render_simulate(data: dict[str, Any]) -> list[str]:
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


def cmd_simulate(args: argparse.Namespace) -> int:
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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


def load_pause() -> dict[str, Any] | None:
    """读暂停标记。坏文件当作没暂停——宁可按原样响，不能因为标记坏了集体静默。"""
    p = pause_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        date.fromisoformat(data["until"])
        return cast(dict[str, Any], data)
    except (OSError, ValueError, TypeError, KeyError) as e:
        print(f"pause: 暂停标记看不懂（{type(e).__name__}: {e}），当作没暂停", file=sys.stderr)
        return None


def _pause_range(p: dict[str, Any]) -> tuple[date, date]:
    until = date.fromisoformat(p["until"])
    start = date.fromisoformat(p.get("from") or p["until"])
    return start, until


def pause_covers(p: dict[str, Any] | None, d: date) -> bool:
    """这一天是否落在暂停期（左闭右开：until 那天已经恢复）。"""
    if not p:
        return False
    try:
        f, t = _pause_range(p)
    except (ValueError, TypeError):
        return False
    return f <= d < t


def pause_summary(p: dict[str, Any]) -> str:
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


def cmd_pause(args: argparse.Namespace) -> int:
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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


def cmd_resume(args: argparse.Namespace) -> int:
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


def read_delivery(day: str | None = None) -> list[dict[str, Any]]:
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


def delivery_summary(day: str) -> dict[str, Any]:
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


def cmd_delivery(args: argparse.Namespace) -> int:
    day = args.date or datetime.now(_fixed_offset(DEFAULT_OFFSET)).date().isoformat()
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


def cmd_delivery_record(args: argparse.Namespace) -> int:
    if args.result not in ("ok", "fail"):
        print(f"delivery-record: --result 只能是 ok / fail，收到 {args.result}", file=sys.stderr)
        return 3
    tz = _fixed_offset(DEFAULT_OFFSET)
    at = parse_iso(args.at, tz) if args.at else datetime.now(_fixed_offset(DEFAULT_OFFSET))
    record_delivery(args.window or "-", args.target, args.result, args.error, at)
    print(f"delivery-record: 已记 {at.isoformat()} {args.result}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    routine = load_routine()
    # 早报只能选一种输出方式：单窗口（due 直接出稿）或双窗口（模型先写 + 兜底）。
    # 两种同时启用会让 9:00 和 9:10 各发一次同一份汇报——外部审计实测到过重复输出。
    fmts = {w.get("format") for w in routine["alarmWindows"] if not w.get("disabled")}
    if "report" in fmts and "report-fallback" in fmts:
        raise Validation(
            "早报只能启用一种输出方式：format=report（due 直接出稿）或 "
            "format=report-fallback（模型先写 + 回执兜底），不能同时配置"
        )
    hand_edit = audit_routine(routine)
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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
    # 单个窗口算错 → 跳过该窗口 + 记告警，而不是让整份计划失败（issue #75）
    plan_errors: list[str] = []
    fresh = (
        []
        if day_disabled(routine, kind)
        else build_windows(routine, now.date(), kind, now, errors=plan_errors)
    )

    if old and old.get("planDate") == today:
        windows = carry_forward(old, fresh)
    else:
        # 跨日：窗口全部重建，但清单的勾选状态要带过去
        prev_windows = {x["id"]: x for x in (old or {}).get("windows", [])}
        for w in fresh:
            prev = prev_windows.get(w["id"]) or {}
            _migrate_checklist(prev, w)
            # 一次性窗口跨日身份不变（id@rtc）：换了日期就是同一条的新版本，seq 要跟着涨
            if prev.get("ocid") and prev["ocid"] == w.get("ocid"):
                w["seq"] = int(prev.get("seq") or 0) + (1 if prev.get("start") != w.get("start") else 0)
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
        "timezone": zone_label(routine),
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
    # 被跳过的窗口要吼一声：静默少一个提醒，比漏响更难查（issue #75）
    for msg in plan_errors:
        print(f"⚠️ 时刻表里有窗口被跳过（{msg}）：修好后跑 plan --force，才会补进今天的账本")

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

    # 自检已随 #78 移到 due（plan 一天一次，崩了没人看；due 每 5 分钟活着）。
    # 这里只播自愈与手改的提示。
    for line in take_notices() + hand_edit:
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


def cmd_due(args: argparse.Namespace) -> int:
    """due 入口：先进心跳 attemptAt，判定完整走完才补 lastOkAt。

    `_due_eval` 的所有 return 0（含「正确判定为静默」）都算判定完整走完；
    异常（Validation / Conflict 冒泡到 main 的非 0 退出）不会走到 ok 标记，
    于是 lastOkAt 停在最后一次真正成功的判定上——这正是外部看门狗要读的值。
    """
    routine = load_routine()
    for line in take_notices():
        print(line)
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
    # 先记下「本次刷新之前」的心跳：自检要看的是上一次到底有没有判完，
    # 不能拿自己刚写的 attemptAt 冒充证据。
    hb_before = read_heartbeat()
    hb_attempt_before = read_heartbeat_attempt()
    touch_heartbeat(now)  # attemptAt：进入即记，失败也留痕
    try:
        rc = _due_eval(args, routine, tz, now)
    except _DueConflict:
        # 冲突时判定确实跑完了（只是账本没写成）：按 #67 口径，心跳仍记 ok——
        # 心跳回答的是「调度器还在不在判」，不是「这次写没写成」。
        # 但自检不能借这次机会投递告警：它对账的是旧账本，喊出来的多半是假警报。
        # 对外仍是空 stdout + 退出 0。
        touch_heartbeat(now, ok=True)
        return 0
    if rc == 0:
        # 自检跟着 due 走：plan 一天只跑一次，崩了 due 还活着。
        # 同一问题每天只投一次，不刷屏。
        state = load_state()
        if state is not None:
            for line in self_check(state, now, heartbeat=(hb_before, hb_attempt_before)):
                emit_alert_once("self:" + line, line, now)
        touch_heartbeat(now, ok=True)  # lastOkAt：判定完整走完（含静默）
    return rc


def _due_eval(args: argparse.Namespace, routine: dict[str, Any], tz: tzinfo, now: datetime) -> int:
    """due 的判定主体。返回 0 表示「判定完整走完」（含正确判为静默）。"""
    state = load_state()
    today = now.date().isoformat()
    if not state or state.get("planDate") != today:
        try:
            rc = cmd_plan(argparse.Namespace(now=now.isoformat(), force=False))
        except Validation as e:
            # 配置坏了 → plan 抛错。due 不能跟着静默：今天一条都不会响，
            # 用户必须收到一条可投递的告警（每天一次，诊断细节在 stderr）。
            print(f"due: 今日计划生成失败：{e}", file=sys.stderr)
            rc = 3
        if rc != 0:
            emit_alert_once(
                "plan-failed",
                f"⚠️ 今天（{today}）的计划没生成，所有提醒都不会响；先按诊断修配置，再跑一次 plan",
                now,
            )
            return 0
        state = load_state()
    if state is None:
        print("due: 计划生成后仍没有状态文件，本 tick 放弃", file=sys.stderr)
        emit_alert_once(
            "no-state",
            "⚠️ 今天的计划没有落盘，所有提醒都不会响；检查 state/alarms.json 与磁盘权限",
            now,
        )
        return 3

    # 对账：时刻表有、账本没有 → 这些提醒今天物理上不可能响
    #
    # 但告警先攒着、不立刻投递：状态写入可能冲突，而契约（#4 §11）要求冲突时
    # stdout 必须为空。先喊一嗓子「有事」再发现压根没写进账本，等于对用户撒谎。
    pending: list[tuple[str, str]] = []

    def flush_alerts() -> None:
        """状态操作定论之后再投递；冲突路径不调用它（PR #79 复审 1）。"""
        for key, line in pending:
            emit_alert_once(key, line, now)
        pending.clear()

    if not pause_covers(load_pause(), now.date()):
        skipped: list[str] = []
        expected = expected_today_ids(routine, now, skipped)
        # 单窗口坏掉只跳过它，其余窗口的 id 照常对账——一条已知缺陷不该
        # 把另一个真缺失的提醒一起掩盖（PR #79 复审 2）。
        for line in skipped:
            print(f"due: 时刻表有窗口被跳过（{line}）", file=sys.stderr)
        if expected is not None:
            ledger_ids = {w["id"] for w in state.get("windows", [])}
            missing = sorted(expected - ledger_ids)
            if missing:
                pending.append(
                    (
                        "missing-" + ",".join(missing),
                        f"⚠️ 时刻表有 {'、'.join(missing)}，但今天账本里没有：这些提醒不会响；"
                        "检查 validFrom / dateTypes，或重建 plan",
                    )
                )

    rev = int(state["rev"])
    changed = False

    # 1) 过期未确认 → missed
    # 兜底窗口例外：过了点的兜底窗口，只要当天有「汇报已送达」的投递回执，
    # 就算交付完成（9:00 的 agent cron 已经把汇报发出去了），不该记成「漏响」。
    # 只认回执，不认文件存在——文件只能证明模型写了，不能证明用户收到了。
    for w in state["windows"]:
        end = datetime.fromisoformat(w["end"])
        if now > end and w["state"] in {"pending", "ringing"}:
            if w.get("format") == "report-fallback" and brief.report_delivered(now.date()):
                # 没响过才叫 skipped；已经响过（fireCount>0）的兜底不能因为
                # 回执晚到就被抹成「从未投递」，否则历史会失真。
                if int(w.get("fireCount", 0)) > 0:
                    w["state"] = "delivered"
                    w.pop("missedReason", None)
                else:
                    w["state"] = "skipped"
                    w["missedReason"] = "covered_by_receipt"
            else:
                w["state"] = "missed"
                w["missedAt"] = now.isoformat()
                w["missedReason"] = "window_expired"
            changed = True

    # 2) 找命中窗口
    # 早报兜底窗口（format=report-fallback）：当天已有「汇报送达」的回执时，
    # 这一窗不响、直接判 delivered（不是 skipped——它的任务确实完成了，只是由
    # 9:00 的 agent cron 完成）。必须在选 hit 之前处理，否则会重复投递。
    # 没有回执（写好了但没发出去 / 根本没写）→ 照常投递，内容见 build_digest。
    hit = None
    for w in state["windows"]:
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        if start <= now <= end and w["state"] in {"pending", "ringing"}:
            if w.get("format") == "report-fallback" and brief.report_delivered(now.date()):
                # 没响过才叫 skipped；已经响过（fireCount>0）的兜底不能因为
                # 回执晚到就被抹成「从未投递」，否则历史会失真。
                if int(w.get("fireCount", 0)) > 0:
                    w["state"] = "delivered"
                    w.pop("missedReason", None)
                else:
                    w["state"] = "skipped"
                    w["missedReason"] = "covered_by_receipt"
                changed = True
                continue
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
                # 下一次 due 重试即可。攒着的告警也不投递——没写成的事不提。
                print(f"due: 写入冲突（{e}），missed 标记延后到下一 tick", file=sys.stderr)
                raise _DueConflict() from e
        flush_alerts()
        return 0

    # 3) 三个条件
    if hit["fireCount"] >= hit["expectFires"]:
        print(f"due: {hit['id']} 已达上限 {hit['expectFires']} 次，静默", file=sys.stderr)
        flush_alerts()
        return 0
    if hit.get("lastFiredAt"):
        last = datetime.fromisoformat(hit["lastFiredAt"])
        gap = (now - last).total_seconds() / 60
        if gap < hit["intervalMin"] - 1:
            print(f"due: 距上次 {gap:.1f} 分钟，未到间隔，静默", file=sys.stderr)
            flush_alerts()
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
        raise _DueConflict() from e
    _record_history(state, routine, ts=now)
    # 状态确实写下去了，才轮到投递攒着的对账告警
    flush_alerts()

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


def cmd_ack(args: argparse.Namespace) -> int:
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
    state = load_state()
    if not state:
        print("ack: 今天还没有计划", file=sys.stderr)
        return 4

    if args.source == "external":
        if not args.date:
            print("ack: 外部回调必须带 --date（只允许结束当天该窗口）", file=sys.stderr)
            return 3
        if not args.event_id:
            print("ack: 外部回调必须带 --event-id（重复回调要幂等）", file=sys.stderr)
            return 3
    elif args.event_id:
        print("ack: --event-id 只能配合 --source external", file=sys.stderr)
        return 3

    if args.date:
        if not args.window:
            print("ack: --date 需要配合 --window", file=sys.stderr)
            return 3
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print(f"ack: --date 应为 YYYY-MM-DD，收到 {args.date}", file=sys.stderr)
            return 3
        if state.get("planDate") != args.date:
            print(
                f"ack: 当前计划日期是 {state.get('planDate')}，不能按 {args.date} 结束窗口",
                file=sys.stderr,
            )
            return 4

    if args.event_id:
        for w in state["windows"]:
            if w.get("ackEventId") != args.event_id:
                continue
            if args.window and w["id"] != args.window:
                print(
                    f"ack: 事件 {args.event_id} 已绑定到窗口 {w['id']}，不能改绑到 {args.window}",
                    file=sys.stderr,
                )
                return 4
            print(f"ack: 事件 {args.event_id} 已处理，跳过", file=sys.stderr)
            return 0

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
    if args.source:
        hit["ackSource"] = args.source
    if args.event_id:
        hit["ackEventId"] = args.event_id
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


def cmd_check(args: argparse.Namespace) -> int:
    """勾选 / 取消勾选清单里的某一项。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    结果看退出码（0 成功），细节在 stderr。
    """
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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


# 账本与今天的关系分类。postpone 只允许动「今天」的账本；口径见 cmd_postpone
# 的陈旧账本分支（issue #87 第 2 条，方案 A）。换备选方案 B 时只改这一段分类与出口。
_LEDGER_CURRENT = "current"
_LEDGER_STALE = "stale"
_LEDGER_INVALID = "invalid"


def _classify_postpone_ledger_day(state: dict[str, Any], today_iso: str) -> str:
    """账本属于今天、更早一天，还是日期字段不可用。

    缺失/空账本由调用方按 None 处理，不进这里。
    """
    plan_date = state.get("planDate")
    if not isinstance(plan_date, str) or not plan_date:
        return _LEDGER_INVALID
    try:
        date.fromisoformat(plan_date)
    except ValueError:
        return _LEDGER_INVALID
    if plan_date > today_iso:
        return _LEDGER_INVALID
    if plan_date < today_iso:
        return _LEDGER_STALE
    return _LEDGER_CURRENT


def _is_postponed_today(window: dict[str, Any]) -> bool:
    """当天被 postpone 推迟的窗口。

    快照的 windows 是「剩余计划」：推迟的窗口从这里统一排除；它的执行结果仍
    留在 results / history，证据不抹（issue #87 第 3 条）。只认 postponed 原因，
    其它 skipped（停用 / 日期类型 / 过期等）保持可见。
    """
    return window.get("state") == "skipped" and window.get("missedReason") == POSTPONE_REASON


def _load_postpone_state(today_iso: str | None = None) -> dict[str, Any] | None:
    """Validate only ledger fields postpone consumes; leave other commands unchanged.

    传 `today_iso` 时连带校验 `planDate`：缺字段 / 非法日期 / 相对今天是未来的账本，
    在时刻表写入之前就拒绝，绝不被当成「昨天的旧账」静默放过（issue #87 第 2 条）。
    """
    state = load_state()
    if not state:
        return None
    rev = state.get("rev")
    try:
        if isinstance(rev, bool) or not isinstance(rev, (int, str)) or int(rev) < 0:
            raise ValueError
    except ValueError as e:
        raise Validation("ledger rev must be a nonnegative integer") from e
    windows = state.get("windows")
    if not isinstance(windows, list):
        raise Validation("ledger windows must be a list")
    for window in windows:
        if (
            not isinstance(window, dict)
            or not isinstance(window.get("id"), str)
            or not window["id"]
            or not isinstance(window.get("state"), str)
        ):
            raise Validation("ledger windows require string id and state fields")
    if today_iso is not None:
        plan_date = state.get("planDate")
        if not isinstance(plan_date, str) or not plan_date:
            raise Validation("ledger planDate must be a YYYY-MM-DD string")
        try:
            date.fromisoformat(plan_date)
        except ValueError as e:
            raise Validation("ledger planDate must be YYYY-MM-DD") from e
        if plan_date > today_iso:
            raise Validation("ledger planDate is in the future relative to today")
    return state


def cmd_postpone(args: argparse.Namespace) -> int:
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
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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
    changes: dict[str, Any] = {}

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

    # 一次性窗口换了日期 = 同一事件的新版本：seq 要写回时刻表。
    # 否则中间隔几天不排它，窗口重新出现时 seq 又从 0 开始，
    # 日历会当成「从没更新过」（复现 #90 发现的 SEQUENCE 不涨）。
    if w.get("oneOff") and ({"validFrom", "validUntil", "dueDate"} & set(changes)):
        changes["seq"] = int(w.get("seq") or 0) + 1

    # Validate the ledger before committing any schedule change.
    try:
        _load_postpone_state(today.isoformat())
    except (OSError, Validation) as e:
        print(f"postpone: ledger preflight failed; schedule unchanged: {e}", file=sys.stderr)
        return 3

    def partial_failure(error: Exception) -> int:
        mode = " --deadline" if args.deadline else ""
        print(
            f"postpone: partially applied: schedule moved to {target.isoformat()}, "
            f"but today's state update could not be confirmed ({error}); "
            "today's occurrence may still fire. Inspect state/alarms.json and repair "
            "storage/state first, then retry the same explicit target: "
            f"postpone --window {args.window} --to {target.isoformat()}{mode}",
            file=sys.stderr,
        )
        return 3

    old_values = {k: (k in w, w.get(k)) for k in changes}

    # --- 1. 改时刻表（先改这个：明天有没有提醒取决于它）
    for k, v in changes.items():
        w[k] = v
    try:
        post_digest = _write_routine(routine, digest)
    except Conflict as e:
        print(f"postpone: 时刻表已被改动（{e}），未写入", file=sys.stderr)
        return 2

    # Reload after the routine commit: plan/due may have created or changed the ledger.
    try:
        state = _load_postpone_state(today.isoformat())
    except (OSError, Validation) as e:
        return partial_failure(e)

    # --- 2. 今天这一次不再响
    # 账本必须属于今天才允许改当日条目：更早一天的旧账连 occurrence 带它那天的快照
    # 都原样保留，不能把昨天的记录改成 skipped（issue #87 第 2 条）。
    outcome = "not_planned"
    history_error: Exception | None = None
    ledger_day = (
        _classify_postpone_ledger_day(state, today.isoformat()) if state is not None else _LEDGER_CURRENT
    )
    if state is not None and ledger_day == _LEDGER_STALE:
        outcome = "stale_ledger"
    if state is not None and ledger_day == _LEDGER_CURRENT:
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
            except (OSError, Validation, ValueError, TypeError, AttributeError) as e:
                # A competing malformed replacement can fail the writer's rev read.
                return partial_failure(e)
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
                    _write_routine(routine, post_digest)
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
            # The state write succeeded. A later audit error cannot undo that fact.
            outcome = "skipped"
            try:
                _record_history(
                    state,
                    routine,
                    kind="postpone",
                    window=args.window,
                    note=f"{args.to} <- {state.get('planDate')}",
                    ts=now,
                )
            except (OSError, Validation, ValueError, TypeError, AttributeError) as e:
                history_error = e
        else:
            outcome = "already_fired"

    # --- 3. 今天的快照同步：被跳过的那一次不再出现在当天安排里
    snapshot_error: OSError | None = None
    if outcome == "skipped":
        # skipped 只在「今天有计划且该窗口还在跑」时才出现
        assert state is not None
        try:
            rest = [x for x in state.get("windows", []) if not _is_postponed_today(x)]
            write_schedule_snapshot(
                schedule_snapshot(
                    routine,
                    state.get("planKind", "workday"),
                    rest,
                    state["planDate"],
                    now.isoformat(),
                    results=existing_snapshot_results(state["planDate"]),
                )
            )
        except OSError as e:
            snapshot_error = e
            print(f"postpone: 快照更新失败 {e}", file=sys.stderr)

    if history_error is not None:
        snapshot_status = (
            f"snapshot update failed ({snapshot_error})" if snapshot_error else "snapshot synchronized"
        )
        print(
            "postpone: partially applied: schedule moved and state committed as skipped; "
            f"history update failed ({history_error}); {snapshot_status}. "
            "Inspect and repair history/snapshot storage; verify the committed ledger "
            "before recovery. Repeating postpone alone does not repair missing history.",
            file=sys.stderr,
        )
        return 3

    stale_date = state.get("planDate") if state is not None else "?"
    notes = {
        "skipped": "今天这一次已跳过",
        "already_fired": "今天这一轮已经结束，明天会再提醒一次",
        "not_planned": "今天没有这一次的计划，明天第一次响",
        "stale_ledger": (
            f"今日账本还是 {stale_date} 的旧账，未改动当日条目；时刻表已调整，等 plan 生成今天的安排"
        ),
    }
    print(
        f"postpone: {args.window} → {target.isoformat()}（同一时刻），{notes[outcome]}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------- 取消 / 完成 / 停用

CANCELLED_REASON = "cancelled"


def _write_routine(routine: dict[str, Any], digest: str | None) -> str:
    """写时刻表。内容摘要做守卫——routines.json 是人会手改的文件。

    写完必须顺手盖章（摘要 + 底稿副本）。盖章漏了，下一次 plan 就会把程序
    自己的改动当成「有人手改」来报警，手改真发生时反倒没人信了。
    返回本次写出的内容摘要，供调用方在回滚时当守卫用。
    """
    new_digest = atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=digest)
    seal_routine(new_digest)
    return new_digest


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


def audit_routine(routine: dict[str, Any]) -> list[str]:
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


def _prune_expired_oneoffs(routine: dict[str, Any], today: str) -> list[str]:
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


def _find_window(routine: dict[str, Any], wid: str) -> dict[str, Any] | None:
    for w in routine.get("alarmWindows", []) or []:
        if w["id"] == wid:
            return cast(dict[str, Any], w)
    return None


def _mark_state(state: dict[str, Any] | None, wid: str, new_state: str, now: datetime, reason: str) -> str:
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


def cmd_set_time(args: argparse.Namespace) -> int:
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

    def parse_hhmm(s: str, what: str) -> time | None:
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
    end: time | None
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
        _write_routine(routine, digest)
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


def _parse_hhmm(value: str, what: str) -> time | None:
    try:
        h, m = (int(x) for x in value.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return time(h, m)
    except ValueError:
        print(f"add-window: --{what} 应为 HH:MM，收到 {value}", file=sys.stderr)
        return None


def cmd_add_window(args: argparse.Namespace) -> int:
    """新增或更新一个周期窗口；写入走 digest 守卫 + 统一印章。

    只改 rules/routines.json，不碰 state/alarms.json。默认下次 plan 生效；
    `--dry-run` 只打印将要写入的窗口，不落盘。

    `--oneoff` 把窗口标成一次性（当天只响一次、expectFires 固定 1）；再配
    `--date YYYY-MM-DD` 就等价于「只在这一天生效」——这是「明天 9 点提醒我」
    这类一句话诉求的最短路径，日期不该混进 `--start`。
    """
    digest = file_digest(ROUTINE)
    # dry-run 是「预览」，必须是纯只读：时刻表坏了就直说，不能借自愈顺手改文件。
    # 否则用户以为只是看一眼，磁盘上的 routines.json / 印章 / 备份已经变了。
    routine = load_routine(read_only=bool(args.dry_run))
    defs = routine.get("alarmWindows") or []
    idx = next((i for i, x in enumerate(defs) if x.get("id") == args.id), None)

    if idx is not None and not args.update:
        print(f"add-window: 窗口 {args.id} 已存在；要改它请加 --update", file=sys.stderr)
        return 4
    if idx is None and args.update:
        print(f"add-window: 窗口 {args.id} 不存在，--update 不能凭空创建", file=sys.stderr)
        return 4

    w = dict(defs[idx]) if idx is not None else {"id": args.id}
    if args.label is not None:
        w["label"] = args.label
    if args.start is not None:
        w["start"] = args.start
    if args.end is not None:
        w["end"] = args.end
    if args.interval is not None:
        w["intervalMin"] = args.interval
    if args.ack:
        w["ackHints"] = list(args.ack)
    if args.format is not None:
        if args.format == "none":
            w.pop("format", None)
        else:
            w["format"] = args.format
    if args.valid_from is not None:
        w["validFrom"] = args.valid_from
    if args.valid_until is not None:
        w["validUntil"] = args.valid_until
    if getattr(args, "oneoff", False):
        w["oneOff"] = True
    # 「明天 9 点提醒我」这种一句话诉求：--oneoff + --date 就是它的最短写法。
    # 日期只能落到 validFrom/validUntil，绝不能混进 --start（那是 #80 的现场事故）。
    if getattr(args, "date", None) is not None:
        text = str(args.date).strip()
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            print(f"add-window: --date 必须是 YYYY-MM-DD（带短横线），收到 {text}", file=sys.stderr)
            return 3
        try:
            date.fromisoformat(text)
        except ValueError:
            print(f"add-window: --date 不是有效日期：{text}", file=sys.stderr)
            return 3
        if not w.get("oneOff"):
            print(
                "add-window: --date 只能配 --oneoff（那天只响一次）；"
                "要限定一段日期请用 --valid-from / --valid-until",
                file=sys.stderr,
            )
            return 3
        w["validFrom"] = text
        w["validUntil"] = text

    for key in ("label", "start", "end", "intervalMin"):
        if not w.get(key):
            if key == "intervalMin" and w.get("oneOff"):
                # 一次性窗口的间隔没有意义（expectFires 固定 1），别逼调用方瞎填一个数
                w["intervalMin"] = 1
                continue
            print(f"add-window: 新窗口缺少 --{key}", file=sys.stderr)
            return 3

    start = _parse_hhmm(str(w["start"]), "start")
    end = _parse_hhmm(str(w["end"]), "end")
    if start is None or end is None:
        return 3
    if end <= start:
        print(f"add-window: end ({w['end']}) 必须晚于 start ({w['start']})", file=sys.stderr)
        return 3

    interval = int(w["intervalMin"])
    if interval <= 0:
        print("add-window: --interval 必须是正整数", file=sys.stderr)
        return 3
    # 一次性 / 倒计时 / 清单窗口每天只响一次，expectFires 由类型固定为 1；
    # 只改文案时如果按时刻算术重算，会把「响一次」改写成「响五次」。
    fixed_once = bool(w.get("oneOff") or w.get("dueDate") or w.get("checklist"))
    computed = 1 if fixed_once else expect_fires(start, end, interval)
    if args.expect_fires is not None and int(args.expect_fires) != computed:
        why = "一次性/倒计时/清单窗口固定为 1" if fixed_once else f"应为 {computed}"
        print(
            f"add-window: --expect-fires={args.expect_fires} 与窗口类型不符（{why}）",
            file=sys.stderr,
        )
        return 3
    w["expectFires"] = computed

    for key in ("validFrom", "validUntil"):
        if w.get(key):
            text = str(w[key]).strip()
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                print(
                    f"add-window: --{key} 必须是 YYYY-MM-DD（带短横线）；"
                    f"紧凑写法 {text} 存进去后排程按字符串比较，窗口会认不出来",
                    file=sys.stderr,
                )
                return 3
            try:
                date.fromisoformat(text)
            except ValueError:
                print(f"add-window: --{key} 不是有效日期：{text}", file=sys.stderr)
                return 3
            w[key] = text
    if w.get("validFrom") and w.get("validUntil") and w["validFrom"] > w["validUntil"]:
        print("add-window: validFrom 不能晚于 validUntil", file=sys.stderr)
        return 3

    # 规范要求 ackHints 必填；省略 --ack 时写显式合法默认，别让候选配置带着洞过门。
    if not w.get("ackHints"):
        w["ackHints"] = ["知道了"]

    active_formats = {
        str(x.get("format"))
        for x in defs
        if x.get("id") != args.id and not x.get("disabled") and x.get("format")
    }
    # 候选窗口自己若是停用状态，就不参与「早报二选一」——它今天本来就不会响。
    if w.get("format") and not w.get("disabled"):
        active_formats.add(str(w["format"]))
    if "report" in active_formats and "report-fallback" in active_formats:
        print(
            "add-window: report 与 report-fallback 只能二选一，不能同时启用",
            file=sys.stderr,
        )
        return 3

    if args.dry_run:
        print(json.dumps(w, ensure_ascii=False, indent=2))
        return 0

    if idx is None:
        defs.append(w)
    else:
        defs[idx] = w
    routine["alarmWindows"] = defs
    try:
        _write_routine(routine, digest)
    except Conflict as e:
        print(f"add-window: 时刻表已被改动（{e}），未写入", file=sys.stderr)
        return 2
    print(
        f"add-window: {args.id} 已写入（{w['start']}–{w['end']}，每 {interval} 分钟，"
        f"共 {computed} 次）；下次 plan 生效",
        file=sys.stderr,
    )
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
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
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)
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


def cmd_complete(args: argparse.Namespace) -> int:
    """完成一件事。

    语义 = 「做完了，以后不用提醒我」：记录确认 + 永久停用这个窗口。
    这与 `ack` 的区别：`ack` 只停今天这一次，`complete` 连以后也不再提醒。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)

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


def cmd_disable(args: argparse.Namespace) -> int:
    """永久停用一个窗口（「以后别提醒我了」）。

    与 `cancel` 的区别：cancel 取消一个安排，disable 关掉一类提醒。
    disable 可以随时用 `enable` 撤销。

    stdout 保持为空：它由用户交互触发，不是定时任务，不该被投递出去。
    """
    digest = file_digest(ROUTINE)
    routine = load_routine()
    tz = resolve_tz(routine)
    now = parse_iso(args.now, tz) if args.now else now_for(routine)

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


def cmd_enable(args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
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
    sa.add_argument("--date", help="只结束 YYYY-MM-DD 这一天的窗口（外部回调必填）")
    sa.add_argument("--source", choices=["external"], help="来源；外部平台适配器用 external")
    sa.add_argument("--event-id", dest="event_id", help="幂等键；同一事件重复回调只写一次")
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

    saw = sub.add_parser("add-window", help="新增或更新周期窗口（不再手改 routines.json）")
    saw.add_argument("--id", required=True, help="窗口 id")
    saw.add_argument("--label", help="窗口文案")
    saw.add_argument("--start", help="起始时刻 HH:MM")
    saw.add_argument("--end", help="结束时刻 HH:MM")
    saw.add_argument("--interval", type=int, help="间隔分钟")
    saw.add_argument(
        "--expect-fires",
        dest="expect_fires",
        type=int,
        help="显式覆盖响铃次数（需与窗口算术一致）",
    )
    saw.add_argument("--ack", action="append", help="确认词，可重复")
    saw.add_argument("--valid-from", dest="valid_from", help="生效日期 YYYY-MM-DD")
    saw.add_argument("--valid-until", dest="valid_until", help="失效日期 YYYY-MM-DD")
    saw.add_argument(
        "--oneoff",
        action="store_true",
        help="一次性窗口：当天只响一次（expectFires 固定 1）",
    )
    saw.add_argument(
        "--date",
        help="只在这一天生效 YYYY-MM-DD（必须配 --oneoff，等价 validFrom=validUntil）",
    )
    saw.add_argument(
        "--format",
        choices=["follow", "summary", "report", "report-fallback", "none"],
        help="窗口类型；none = 清除已有类型",
    )
    saw.add_argument("--update", action="store_true", help="更新已有窗口（字段级合并）")
    saw.add_argument("--dry-run", action="store_true", help="只打印将要写入的窗口，不落盘")
    saw.set_defaults(func=cmd_add_window)

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
        return cast(int, args.func(args))
    except Validation as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 3
    except Conflict as e:
        print(f"状态冲突：{e}", file=sys.stderr)
        return 2
