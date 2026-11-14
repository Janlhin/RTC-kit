#!/usr/bin/env python3
"""rtc.py — 提醒主链路的确定性执行核心

设计要点：
  1. 不调用模型。判断"该不该响"是纯布尔逻辑。
  2. 状态只有一份：<workspace>/state/alarms.json，符合 contract/alarm-state.schema.json。
  3. 时刻表只有一份：<workspace>/rules/routines.json，符合 contract/routine.schema.json。
  4. 写入是原子的：临时文件 + os.replace，并在写入前比对 rev。
  5. plan 只增不改：已有 acked/missed/skipped 的窗口原样保留。
  6. 窗口可以是一次性的（oneOff）或有有效期（validFrom/validUntil）。
     一次性窗口送达即完成（state=delivered），不需要确认。
     有效期内没有任何窗口 = 合法空日，静默。

宿主契约（这是与运行时唯一的耦合面）：
  - stdout 只有两种形态：要投递的正文，或空（空 = 静默）
  - stderr 承载全部诊断信息
  - 退出码 0 正常 / 2 状态冲突 / 3 校验失败或部分应用 / 4 无匹配窗口

用法：
  python rtc.py --workspace <dir> plan   [--now ISO] [--force]
  python rtc.py --workspace <dir> due    [--now ISO]
  python rtc.py --workspace <dir> ack    [--window ID] [--text TXT] [--now ISO]
  python rtc.py --workspace <dir> status [--json]
  python rtc.py --workspace <dir> schedule <YYYY-MM-DD> | --month YYYY-MM [--json]
  python rtc.py --workspace <dir> history  <YYYY-MM-DD> | --month YYYY-MM [--json]
  python rtc.py --workspace <dir> check --window <ID> --item <序号或名称> [--undo]
  python rtc.py --workspace <dir> postpone --window <ID> [--to YYYY-MM-DD | --days N] [--deadline]
  python rtc.py --workspace <dir> cancel   --window <ID> [--date YYYY-MM-DD]
  python rtc.py --workspace <dir> complete --window <ID> [--text TXT]
  python rtc.py --workspace <dir> disable  --window <ID>
  python rtc.py --workspace <dir> enable   --window <ID>
  python rtc.py --workspace <dir> metrics sync|report|schedule [--date YYYY-MM-DD]

环境变量 RTC_WORKSPACE 可代替 --workspace；都不给时用当前目录。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time as _time
import uuid as _uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str | None:
    """文件内容摘要。检测「读取之后被手改过」——比 rev 更严，适合人会编辑的文件。"""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


_LOCK_STALE_S = 30
_LOCK_WAIT_S = 10


def _lock_token() -> str:
    """本持有者的唯一令牌：进程号 + 随机串，确保全局唯一、不可预测。"""
    return f"{os.getpid()}.{_uuid.uuid4().hex}"


def _try_acquire_lock(lock: Path, token: str, candidate: Path) -> bool:
    """尝试获取一次锁（不阻塞）。成功返回 True 并令 lock 内容为 token。

    - 锁不存在：O_EXCL 干净创建，成功。
    - 锁存在且仍新鲜（未超 ``_LOCK_STALE_S``）：返回 False，由调用方等待。
    - 锁存在且已陈旧：用唯一候选文件做 ``os.replace`` 原子接管，再校验
      「lock 内容是否等于自己的 token」——只有接管的胜出者读到自己的 token、
      返回 True；落败的一方读到的是别人的 token，返回 False。这保证两个写入者
      不会同时认为自己持有锁、从而同时进入临界区（审计发现 ①：stale-lock 接管
      会丢失写入）。
    """
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        return True
    except (FileExistsError, PermissionError):
        # Windows 上锁文件被另一进程/线程短暂持有时，O_EXCL 会抛 PermissionError
        # （共享冲突）而非 FileExistsError；一律当作「锁被占用，稍后重试」。
        pass
    try:
        if _time.time() - lock.stat().st_mtime <= _LOCK_STALE_S:
            return False
    except OSError:
        return False
    try:
        fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, token.encode("ascii"))
        os.close(fd)
        os.replace(str(candidate), str(lock))
    except FileExistsError:
        pass  # 候选文件名唯一，理论上不会；忽略
    except OSError:
        pass
    try:
        return lock.read_bytes() == token.encode("ascii")
    except OSError:
        return False


@contextlib.contextmanager
def _exclusive_file_lock(path: Path):
    """协作式互斥：O_EXCL 原子创建 .lock，包住「比对 -> 写临时文件 -> replace」。

    遵守本机制的写入者被锁串行化；不遵守的外部写入（手改、别的工具）
    由 expected_digest 的 CAS 检出。锁文件带陈旧超时，进程崩溃不会永久卡死。

    锁文件内容 = 本持有者的唯一令牌（pid.随机）。写入者据此判断「我是否仍持有锁」，
    释放时只删自己的锁，绝不碰别人接管的锁。单次获取逻辑见 ``_try_acquire_lock``。
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = _lock_token()
    candidate = lock.with_name(lock.name + "." + token)
    deadline = _time.monotonic() + _LOCK_WAIT_S

    acquired = False
    while not acquired:
        if _try_acquire_lock(lock, token, candidate):
            acquired = True
            break
        if _time.monotonic() >= deadline:
            raise Conflict(f"{path.name} 写入锁等待超时")
        _time.sleep(0.01)

    try:
        yield
    finally:
        # 只删自己仍持有的锁；若临界区超过陈旧阈值被别人接管，绝不动别人的锁。
        # （临界区为毫秒级，远小于 _LOCK_STALE_S，故正常不会出现「别人接管」；
        #  此处的令牌校验是防御性的最后一道闸。）
        try:
            if lock.read_bytes() == token.encode("ascii"):
                lock.unlink()
        except OSError:
            pass
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


def atomic_write(path: Path, data: dict, expected_rev: int | None,
                 expected_digest: str | None = None) -> str:
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


def load_routine() -> dict:
    if not ROUTINE.exists():
        raise Validation(f"找不到时刻表：{ROUTINE}")
    return load_json(ROUTINE)


def load_state() -> dict | None:
    return load_json(STATE) if STATE.exists() else None


# ---------------------------------------------------------------- 心跳与自检

HEARTBEAT_STALE_MIN = 60


def hb_path() -> Path:
    return WORKSPACE / "state" / "heartbeat.json"


def touch_heartbeat(now: datetime) -> None:
    """每次 due 都记一笔，无论响不响。这是「调度器还活着」的唯一证据。"""
    p = hb_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"heartbeat: 写入失败 {e}", file=sys.stderr)


def read_heartbeat() -> datetime | None:
    p = hb_path()
    if not p.exists():
        return None
    try:
        return datetime.fromisoformat(load_json(p)["at"])
    except (ValueError, KeyError, TypeError, OSError):
        return None


def self_check(state: dict, now: datetime, first_run: bool = False) -> list[str]:
    """只报告「任务在跑但可能没送达」的信号，正常时不输出任何东西。"""
    probs: list[str] = []
    hb = read_heartbeat()

    if hb is None:
        if not first_run:
            probs.append("⚠️ 没有调度器心跳：rtc-due 可能从未成功运行过")
    else:
        gap_min = (now - hb).total_seconds() / 60
        if gap_min > HEARTBEAT_STALE_MIN:
            probs.append(
                f"⚠️ 调度器最近一次心跳在 {gap_min / 60:.1f} 小时前，这段时间里的窗口可能没响"
            )

    yesterday = (now.date() - timedelta(days=1)).isoformat()
    for h in state.get("history", []):
        if h.get("date") != yesterday:
            continue
        if h.get("state") == "missed" and int(h.get("fires", 0)) == 0:
            probs.append(f"⚠️ 昨天「{h.get('id')}」一次都没响过，当时调度器可能没在跑")
    return probs


# ---------------------------------------------------------------- 日程快照

def schedule_snapshot(routine: dict, kind: str, windows: list[dict],
                      day_iso: str, generated_at: str,
                      results: dict | None = None) -> dict:
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
                "start": w["start"],
                "end": w["end"],
                "intervalMin": w["intervalMin"],
                "expectFires": w["expectFires"],
                "oneOff": bool(w.get("oneOff")),
                "dueDate": w.get("dueDate"),
                "ackHint": w.get("ackHint"),
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
    if vu and day_iso > vu:
        return False
    return True


def day_disabled(routine: dict, kind: str) -> bool:
    """该日期类型被契约显式禁用（weekendAlarms / holidayAlarms = false）。

    禁用日 = 合法空日：静默，不报配置错误。
    """
    if kind == "weekend" and routine.get("weekendAlarms") is False:
        return True
    if kind == "holiday" and routine.get("holidayAlarms") is False:
        return True
    return False


def build_windows(routine: dict, d: date, kind: str, now: datetime,
                  strict: bool = True) -> list[dict]:
    tz = tz_of(routine["tzOffset"])
    day_iso = d.isoformat()
    # 第一步：按有效期筛选。全部落空 = 这段时间本来就没有该响的东西（合法空日）
    # 永久停用的窗口：任何日期都不生成（「以后别提醒我了」/「这件事完成了」）
    enabled = [w for w in routine["alarmWindows"] if not w.get("disabled")]
    ranged = [w for w in enabled if in_valid_range(w, day_iso)]
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
        if due or items_def:
            # 倒计时 / 清单窗口：每天只提醒一次。
            # 倒计时的剩余天数由代码算；清单要逐项打勾，都不该每 5 分钟响。
            exp = 1
        elif one_off:
            # 一次性提醒只响一次，不做算术校验（end 可比 start 晚，用于容忍错过 start 那一分钟）
            exp = 1
        else:
            exp = expect_fires(time(sh, sm), time(eh, em), int(w["intervalMin"]))
            if w.get("expectFires") and int(w["expectFires"]) != exp:
                raise Validation(
                    f"窗口 {w['id']} 的 expectFires={w['expectFires']} 与算术值 {exp} 不符"
                )
        expired = now > end
        out.append(
            {
                "id": w["id"],
                "label": w.get("label", w["id"]),
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
                "oneOff": one_off,
                "dueDate": due,
                "disabled": False,
                "cancelledAt": None,
                "checklist": [
                    {"id": str(i + 1), "label": str(label)}
                    for i, label in enumerate(w.get("checklist") or [])
                ],
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


TERMINAL = {"acked", "missed", "skipped", "cancelled"}


def _migrate_checklist(prev: dict, w: dict) -> None:
    """把旧窗口的勾选状态与「已打完」终态迁移到新窗口上。

    清单不会因为日切而重置——昨天打了 3 项，今天还剩 15 项。
    """
    if not w.get("checklist"):
        return
    w["checked"] = list(prev.get("checked") or [])
    items = w["checklist"]
    done = set(w["checked"])
    if prev.get("state") == "acked" and len(done) >= len(items):
        # 已经全部打完 → 保持完成状态，之后不再提醒
        w["state"] = "acked"
        w["ackedAt"] = prev.get("ackedAt")
        w["fireCount"] = int(prev.get("fireCount", 0))


def carry_forward(old: dict | None, fresh: list[dict]) -> list[dict]:
    """只增不改：同日同 id 的终态窗口原样带回，避免重建冲掉确认。"""
    if not old:
        return fresh
    prev = {w["id"]: w for w in old.get("windows", [])}
    merged = []
    for w in fresh:
        p = prev.get(w["id"])
        keep = bool(p) and p.get("state") in TERMINAL
        if keep and w.get("dueDate") and p.get("state") != "cancelled":
            keep = False          # 倒计时：每天重新开始，直到截止日（但被取消的不重开）
        if keep and w.get("checklist"):
            # 清单：只有全部打完才保留终态；没打完就次日继续（勾选状态带过去）
            done = set(w.get("checked") or [])
            keep = p.get("state") == "acked" and len(done) >= len(w["checklist"])
        if keep:
            merged.append(p)
        else:
            _migrate_checklist(p or {}, w)
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


def cmd_plan(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    today = now.date().isoformat()

    old = load_state()
    if old and old.get("planDate") == today and not args.force:
        print(f"plan: {today} 已存在，跳过（rev={old.get('rev')}）", file=sys.stderr)
        return 0

    kind = plan_kind(now.date(), routine)
    if day_disabled(routine, kind):
        # 契约里显式禁用的日期类型 → 合法空日：静默，但照常落盘 state
        fresh = []
    else:
        fresh = build_windows(routine, now.date(), kind, now)

    if old and old.get("planDate") == today:
        windows = carry_forward(old, fresh)
    else:
        # 跨日：窗口全部重建，但清单的勾选状态要带过去
        prev_windows = {x["id"]: x for x in (old or {}).get("windows", [])}
        for w in fresh:
            _migrate_checklist(prev_windows.get(w["id"], {}), w)
        windows = fresh
    history = rollover(old, today) if not (old and old.get("planDate") == today) else list(old.get("history", []))
    total = sum(w["expectFires"] for w in windows)
    if len(windows) != len(fresh):
        raise Validation(f"窗口数 {len(windows)} 与时刻表 {len(fresh)} 不符")

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
    print(
        f"plan: {today} {kind} {len(windows)} 窗口 / {total} 次，rev={state['rev']}",
        file=sys.stderr,
    )

    # 每日快照：主链路写，写一次留永久。失败不阻断提醒，但必须留下诊断。
    try:
        snap = schedule_snapshot(routine, kind, windows, today, now.isoformat())
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

    # 自检结果走 stdout：有异常才输出，宿主照常投递；正常时一个字都不打印
    for line in self_check(state, now, first_run=(old is None)):
        print(line)
    return 0


# ---------------------------------------------------------------- 到点判断


def cmd_due(args) -> int:
    routine = load_routine()
    tz = tz_of(routine["tzOffset"])
    now = parse_iso(args.now, tz) if args.now else now_tz(routine["tzOffset"])
    touch_heartbeat(now)

    state = load_state()
    if not state or state.get("planDate") != now.date().isoformat():
        rc = cmd_plan(argparse.Namespace(now=now.isoformat(), force=False))
        if rc != 0:
            return rc
        state = load_state()

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

    n = hit["fireCount"]
    head = f"{hit['label']} —— {now.strftime('%H:%M')}"
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
            lines.append(" ".join(row[i:i + 6]))
        lines.append(f"逐项打勾说「{items[0]['label']}拿到了」；全部拿到说「{hit['ackHint']}」")
    elif n == 1 or n % 2 == 1:
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
    hit["checked"] = sorted(checked, key=lambda x: int(x) if x.isdigit() else 0)

    rev = int(state["rev"])
    state["rev"] = rev + 1
    # 全部打完 → 该窗口视为完成
    if len(checked) == len(items) and not args.undo:
        hit["state"] = "acked"
        hit["ackedAt"] = now.isoformat()
    atomic_write(STATE, state, expected_rev=rev)
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
        post_digest = atomic_write(ROUTINE, routine, expected_rev=None,
                                  expected_digest=digest)
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
                    atomic_write(ROUTINE, routine, expected_rev=None,
                                 expected_digest=post_digest)
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
        try:
            rest = [x for x in state.get("windows", [])
                    if not (x["id"] == args.window and x.get("state") == "skipped")]
            write_schedule_snapshot(schedule_snapshot(
                routine, state.get("planKind", "workday"), rest,
                state["planDate"], now.isoformat()))
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
    atomic_write(ROUTINE, routine, expected_rev=None, expected_digest=digest)


def _find_window(routine: dict, wid: str) -> dict | None:
    for w in routine.get("alarmWindows", []) or []:
        if w["id"] == wid:
            return w
    return None


def _mark_state(state: dict | None, wid: str, new_state: str,
                now: datetime, reason: str) -> str:
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
        print(f"set-time: end ({args.end or w['end']}) 必须晚于 start ({args.start})",
              file=sys.stderr)
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
                print(f"cancel: {args.window} 在 {target.isoformat()} 的那次已取消",
                      file=sys.stderr)
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
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"cancel: 状态冲突（{e}）", file=sys.stderr)
            return 2

    # 安排本身的去留
    # 一次性窗口：取消 = 这件事不做了 → 彻底停用（它本来也只存在于某一天）
    # 有期限的安排（倒计时 / 有效期区间）：有效期截断到今天 → 以后不再生成
    # 无限期的周期窗口：没有「这件事」可取消，只取消今天这一次
    truncated = False
    if w.get("oneOff"):
        w["disabled"] = True
        truncated = True
    elif w.get("dueDate"):
        w["dueDate"] = today
        if w.get("validUntil") and w["validUntil"] > today:
            w["validUntil"] = today
        truncated = True
    elif w.get("validUntil"):
        if w["validUntil"] > today:
            w["validUntil"] = today
            truncated = True
        else:
            truncated = True      # 已经到期的安排，截断后等于不再生成
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
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"complete: 状态冲突（{e}）", file=sys.stderr)
            return 2

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
        rev = int(state["rev"])
        state["rev"] = rev + 1
        try:
            atomic_write(STATE, state, expected_rev=rev)
        except Conflict as e:
            print(f"disable: 状态冲突（{e}）", file=sys.stderr)
            return 2

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
    print(f"enable: {args.window} 已重新启用", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 历史日程查询


def preview_schedule(routine: dict, day_iso: str) -> dict | None:
    """按配置推算某天的安排——用于未来日期（那时还没有快照）。

    快照回答「那天实际是什么」，推算回答「那天将会是什么」。两者不混。
    """
    try:
        d = date.fromisoformat(day_iso)
    except ValueError:
        return None
    kind = plan_kind(d, routine)
    if day_disabled(routine, kind):
        return None
    tz = tz_of(routine["tzOffset"])
    now = datetime.combine(d, time(0, 0), tzinfo=tz)
    windows = build_windows(routine, d, kind, now, strict=False)
    return schedule_snapshot(routine, kind, windows, day_iso, "")


def outcome_text(r: dict) -> str:
    """把一条执行结果说成人话。"""
    st = r.get("state")
    reason = r.get("missedReason")
    fires = int(r.get("fires") or 0)
    names = {
        "delivered": "已送达（一次性，无需确认）",
        "acked": "已确认",
        "cancelled": "已取消",
        "skipped": "已跳过",
        "missed": "漏响",
        "pending": "没响成",
        "ringing": "响过但没确认",
    }
    base = names.get(st, st or "?")
    if reason:
        reasons = {
            "postponed": "推迟到别处",
            "cancelled": "被取消",
            "window_expired": "窗口过期",
            "day_rollover": "跨日未确认",
            "late_plan": "补建时已过",
        }
        base += f"（{reasons.get(reason, reason)}）"
    if fires:
        base += f"，响 {fires} 次"
    return base


def cmd_schedule(args) -> int:
    """查历史某天（或某月）的日程安排。

    读的是每日快照目录 state/schedule/，永久保留，不受 14 天截断影响。
    """
    if args.month:
        files = sorted(SCHEDULE_DIR.glob(f"{args.month}-*.json"))
        if not files:
            print(f"schedule: 没有 {args.month} 的快照", file=sys.stderr)
            return 4
        if args.json:
            out = []
            for f in files:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        for f in files:
            s = json.loads(f.read_text(encoding="utf-8"))
            d = date.fromisoformat(s["date"])
            dow = "一二三四五六日"[d.weekday()]
            fires = sum(w["expectFires"] for w in s["windows"])
            print(f"{s['date']}（周{dow}）{s['planKind']:<8} "
                  f"{len(s['windows'])} 窗口 / {fires} 次  "
                  f"播报 {len(s['rollcalls'])}  周专项 {len(s['weeklyChores'])}")
        return 0

    if not args.date:
        print("用法：rtc.py schedule <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    path = SCHEDULE_DIR / f"{args.date}.json"
    if path.exists():
        s = json.loads(path.read_text(encoding="utf-8"))
        source = "快照"
    else:
        # 没有快照：该日期还没到（或那天没跑 plan）。按配置推算。
        try:
            s = preview_schedule(load_routine(), args.date)
        except Validation:
            s = None
        if s is None:
            print(f"schedule: 没有 {args.date} 的快照，也推算不出安排", file=sys.stderr)
            return 4
        source = "配置推算"
    if args.json:
        if not args.json_only:
            s = dict(s)
            s["_source"] = source
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    d = date.fromisoformat(s["date"])
    dow = "一二三四五六日"[d.weekday()]
    tag = "" if source == "快照" else "  （未来日期，按配置推算）"
    print(f"{s['date']}（周{dow}）{s['planKind']}  时区 {s.get('timezone')}{tag}")
    if s["windows"]:
        results = s.get("results") or {}
        print("窗口：")
        for w in s["windows"]:
            t0 = w["start"][11:16]
            t1 = w["end"][11:16]
            tag = "  [一次性]" if w.get("oneOff") else ""
            span = ""
            if w.get("validFrom") or w.get("validUntil"):
                span = f"  有效期 {w.get('validFrom') or '不限'} ~ {w.get('validUntil') or '不限'}"
            line = (f"  {t0}–{t1}  {w['label']:<12} 每 {w['intervalMin']} 分钟 × {w['expectFires']} 次"
                    f"  确认词：{w.get('ackHint')}{tag}{span}")
            # 结果：那天实际怎么了
            r = results.get(w["id"])
            if r:
                line += "  → " + outcome_text(r)
            print(line)
        # 快照里可能有「结果」，但窗口已不在计划里（如当天被推迟走了）
        extra = [k for k in results if k not in {w["id"] for w in s["windows"]}]
        for k in extra:
            print(f"  {'—':<13} {'':<12} （计划里没有，但有结果）"
                  f"  → {outcome_text(results[k])}")
    else:
        print("窗口：（无）")
    if s["rollcalls"]:
        print("播报：")
        for c in s["rollcalls"]:
            print(f"  {c['hour']:02d}:{c.get('minute', 0):02d}  {c['kind']}")
    else:
        print("播报：（无）")
    if s["weeklyChores"]:
        print("周专项：")
        for c in s["weeklyChores"]:
            print(f"  {c.get('label')}  挂在 {c.get('attachToRollcallHour')}:00 播报末尾")
    else:
        print("周专项：（无）")
    return 0


# ---------------------------------------------------------------- 执行结果查询


def cmd_history(args) -> int:
    """查某天（或某月）每个提醒的实际结果。

    快照的 `results` 层——推过、取消、没做成、响了几次、确认没确认。
    这是「那天到底发生了什么」的答案，与 `schedule`（那天计划了什么）互补。
    """
    if args.month:
        files = sorted(SCHEDULE_DIR.glob(f"{args.month}-*.json"))
        if not files:
            print(f"history: 没有 {args.month} 的快照", file=sys.stderr)
            return 4
        if args.json:
            out = []
            for f in files:
                s = json.loads(f.read_text(encoding="utf-8"))
                res = s.get("results") or {}
                if res:
                    out.append({"date": s["date"], "results": res})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        any_row = False
        for f in files:
            s = json.loads(f.read_text(encoding="utf-8"))
            res = s.get("results") or {}
            if not res:
                continue
            any_row = True
            d = date.fromisoformat(s["date"])
            dow = "一二三四五六日"[d.weekday()]
            done = sum(1 for r in res.values() if r.get("state") in {"acked", "delivered"})
            print(f"{s['date']}（周{dow}）{len(res)} 项，完成 {done}，未成 {len(res) - done}")
            for wid, r in res.items():
                print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
        if not any_row:
            print(f"history: {args.month} 没有已归档的执行结果")
        return 0

    if not args.date:
        print("用法：rtc.py history <YYYY-MM-DD> | --month YYYY-MM [--json]", file=sys.stderr)
        return 4
    path = SCHEDULE_DIR / f"{args.date}.json"
    if not path.exists():
        print(f"history: 没有 {args.date} 的快照", file=sys.stderr)
        return 4
    s = json.loads(path.read_text(encoding="utf-8"))
    res = s.get("results") or {}
    if args.json:
        print(json.dumps({"date": s["date"], "results": res}, ensure_ascii=False, indent=2))
        return 0
    if not res:
        print(f"history: {args.date} 还没有归档的执行结果"
              f"（快照只有计划——结果在次日归档时回填）")
        return 0
    print(f"{args.date} 的执行结果（{len(res)} 项）：")
    for wid, r in res.items():
        print(f"    {r.get('label') or wid:<14} {outcome_text(r)}")
    return 0


# ---------------------------------------------------------------- 状态与指标


def cmd_status(args) -> int:
    routine = load_routine()
    state = load_state()
    if not state:
        print("尚无计划。先跑：python rtc.py plan")
        return 0
    rows = []
    for w in state["windows"]:
        rows.append(
            {
                "id": w["id"],
                "label": w["label"],
                "window": f"{w['start'][11:16]}-{w['end'][11:16]}",
                "state": w["state"],
                "fireCount": w["fireCount"],
                "expectFires": w["expectFires"],
                "ackedAt": w.get("ackedAt"),
            }
        )
    if args.json:
        print(json.dumps({"planDate": state["planDate"], "rev": state["rev"], "windows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"{state['planDate']}  {state['planKind']}  rev={state['rev']}")
    hb = read_heartbeat()
    if hb is None:
        print("  调度器心跳：无记录")
    else:
        gap = (now_tz(routine["tzOffset"]) - hb).total_seconds() / 60
        print(f"  调度器心跳：{gap:.0f} 分钟前")
    for r in rows:
        print(f"  {r['label']:<8} {r['window']}  {r['state']:<8} {r['fireCount']}/{r['expectFires']}")
    return 0


def cmd_metrics(args) -> int:
    state = load_state()
    if not state:
        print("metrics: 没有状态文件", file=sys.stderr)
        return 3
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS daily (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             expect INTEGER NOT NULL, fired INTEGER NOT NULL,
             acked_at TEXT, missed TEXT,
             PRIMARY KEY (date, window))"""
    )
    # schedule 表：每日快照的 SQL 索引。快照文件是权威存档，这张表只为查询方便。
    con.execute(
        """CREATE TABLE IF NOT EXISTS schedule (
             date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
             start TEXT, end TEXT, expect INTEGER NOT NULL,
             one_off INTEGER NOT NULL DEFAULT 0,
             valid_from TEXT, valid_until TEXT,
             PRIMARY KEY (date, window))"""
    )
    if args.action == "sync":
        for w in state["windows"]:
            con.execute(
                """INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(date, window) DO UPDATE SET
                     label=excluded.label, expect=excluded.expect, fired=excluded.fired,
                     acked_at=excluded.acked_at, missed=excluded.missed""",
                (
                    state["planDate"],
                    w["id"],
                    w["label"],
                    w["expectFires"],
                    w["fireCount"],
                    w.get("ackedAt"),
                    w.get("missedReason"),
                ),
            )
        for h in state.get("history", []):
            con.execute(
                """INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(date, window) DO UPDATE SET
                     label=excluded.label, expect=excluded.expect,
                     fired=excluded.fired, acked_at=excluded.acked_at, missed=excluded.missed""",
                (
                    h["date"],
                    h["id"],
                    h.get("label") or h["id"],
                    int(h.get("expect", 0)),
                    h.get("fires", 0),
                    h.get("ackedAt"),
                    h.get("missedReason"),
                ),
            )
        # schedule 表：从每日快照目录补齐。快照文件是权威存档，这里只做索引。
        snap_count = 0
        if SCHEDULE_DIR.exists():
            for f in sorted(SCHEDULE_DIR.glob("*.json")):
                try:
                    s = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for w in s.get("windows", []):
                    con.execute(
                        """INSERT INTO schedule (date, window, label, start, end, expect,
                                                one_off, valid_from, valid_until)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(date, window) DO UPDATE SET
                             label=excluded.label, start=excluded.start, end=excluded.end,
                             expect=excluded.expect, one_off=excluded.one_off,
                             valid_from=excluded.valid_from, valid_until=excluded.valid_until""",
                        (s.get("date"), w["id"], w.get("label"), w.get("start"), w.get("end"),
                         int(w.get("expectFires", 0)), 1 if w.get("oneOff") else 0,
                         w.get("validFrom"), w.get("validUntil")),
                    )
                    snap_count += 1
                # 「有结果但已不在计划里」的条目（如当天被推迟走的窗口）也要进索引，
                # 否则查历史时那一项凭空消失。
                planned = {w["id"] for w in s.get("windows", [])}
                for wid, r in (s.get("results") or {}).items():
                    if wid in planned:
                        continue
                    con.execute(
                        """INSERT INTO schedule (date, window, label, start, end, expect,
                                                one_off, valid_from, valid_until)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(date, window) DO UPDATE SET label=excluded.label""",
                        (s.get("date"), wid, r.get("label") or wid, None, None,
                         int(r.get("expect") or 0), 0, None, None),
                    )
                    snap_count += 1
        con.commit()
        print(f"metrics: 已同步（schedule 索引 {snap_count} 行）", file=sys.stderr)
        con.close()
        return 0

    if args.action == "schedule":
        if args.date:
            rows = con.execute(
                """SELECT date, window, label, start, end, expect, one_off,
                          valid_from, valid_until
                   FROM schedule WHERE date=?""", (args.date,)).fetchall()
            if not rows:
                print(f"metrics: schedule 表里没有 {args.date}", file=sys.stderr)
                con.close()
                return 4
            for r in rows:
                # start/end 可能为 NULL：「有结果但已不在计划里」的条目（当天被推迟走的）
                span = f"{r[3][11:16]}–{r[4][11:16]}" if r[3] and r[4] else "（已移出计划）"
                print(f"{r[0]}  {r[1]:<12} {r[2]:<12} {span}"
                      f"  ×{r[5]}" + ("  [一次性]" if r[6] else "")
                      + (f"  {r[7]}~{r[8]}" if r[7] or r[8] else ""))
        else:
            rows = con.execute(
                "SELECT date, COUNT(*) FROM schedule GROUP BY date ORDER BY date").fetchall()
            print(f"schedule 表覆盖 {len(rows)} 天"
                  + (f"：{rows[0][0]} ~ {rows[-1][0]}" if rows else ""))
        con.close()
        return 0

    cur = con.execute(
        """SELECT COUNT(*) rows,
                  SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) acked,
                  SUM(CASE WHEN missed IS NOT NULL THEN 1 ELSE 0 END) missed,
                  SUM(fired) fires
             FROM daily"""
    )
    rows, acked, missed, fires = cur.fetchone()
    resolved = con.execute(
        "SELECT COUNT(*) FROM daily WHERE acked_at IS NOT NULL OR missed IS NOT NULL"
    ).fetchone()[0]
    print(f"总行数 {rows}  已结算 {resolved}")
    if resolved:
        print(f"确认率（按已结算口径）{acked}/{resolved} = {acked / resolved * 100:.1f}%")
    print(f"确认 {acked}  漏响 {missed}  响铃尝试合计 {fires}")
    span = con.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM schedule").fetchone()
    if span and span[0]:
        print(f"日程快照索引：{span[0]} ~ {span[1]}（{span[2]} 天）")
    con.close()
    return 0


# ---------------------------------------------------------------- 入口


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
    ssch.set_defaults(func=cmd_schedule)

    shis = sub.add_parser("history")
    shis.add_argument("date", nargs="?", help="YYYY-MM-DD")
    shis.add_argument("--month", help="YYYY-MM")
    shis.add_argument("--json", action="store_true")
    shis.set_defaults(func=cmd_history)

    ss = sub.add_parser("status")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_status)

    sm = sub.add_parser("metrics")
    sm.add_argument("action", choices=["sync", "report", "schedule"])
    sm.add_argument("--days", type=int, default=30)
    sm.add_argument("--date", help="YYYY-MM-DD（用于 metrics schedule）")
    sm.set_defaults(func=cmd_metrics)

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


if __name__ == "__main__":
    raise SystemExit(main())
