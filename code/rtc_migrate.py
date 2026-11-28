#!/usr/bin/env python3
"""rtc_migrate.py — L2 P1 迁移骨架：旧窗口 → 事件影子账本。

定位：**只做投影落盘，不改旧真相源**。``rules/routines.json`` 与
``state/alarms.json`` 全程只读；产出物是派生影子副本 ``state/events.json``
（口径见 ``contract/event.schema.json`` 头注，P2 起 ``.ics`` 为准）。

四条硬性质：

- **默认 dry-run**：不带 ``--apply`` 只打印将做什么，不写任何文件。
- **写前备份**：真跑前把现有影子账本（及旧文件快照）拷到备份目录，manifest 留哈希。
- **幂等**：旧 id / uid 已存在的事件原样跳过，重跑不产生第二份、不换 uid。
- **可回滚**：``--rollback <备份目录>`` 按 manifest 恢复影子账本；迁移前没有影子
  账本的，回滚时删除本次产物。

本脚本是离线迁移工具，**不在** ``plan`` / ``due`` 热路径上；纯标准库。

用法：

    python rtc_migrate.py --workspace <dir>                      # dry-run
    python rtc_migrate.py --workspace <dir> --backup-dir <dir> --apply
    python rtc_migrate.py --workspace <dir> --rollback <备份目录>
    python rtc_migrate.py --workspace <dir> --as-of 2026-11-28  # 模板投影参考日
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc_calindex as calindex  # noqa: E402
import rtc_events as events  # noqa: E402
import rtc_ics as ics  # noqa: E402

__all__ = [
    "plan_migration",
    "apply_migration",
    "rollback",
    "plan_migration_to_ics",
    "apply_migration_to_ics",
    "rollback_ics",
    "rebuild_shadow_from_ics",
    "main",
]

ROUTINE_REL = Path("rules") / "routines.json"
STATE_REL = Path("state") / "alarms.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_index(state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for w in (state or {}).get("windows") or []:
        if isinstance(w, dict) and w.get("id"):
            out[str(w["id"])] = w
    return out


def plan_migration(workspace: Path, *, as_of: date | None = None) -> dict[str, Any]:
    """投影计划：只读旧文件，算出将新增 / 已存在跳过 / 账本孤儿清单，不写盘。"""
    ref_day = as_of or datetime.now().date()
    routine = _read_json(workspace / ROUTINE_REL)
    state = _read_json(workspace / STATE_REL)
    envelope = events.load_events(workspace)
    ledger = _ledger_index(state)
    tz_offset = str((routine or {}).get("tzOffset") or events.DEFAULT_OFFSET)
    ledger_date = str(state.get("planDate")) if state and state.get("planDate") else None

    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    routine_ids: set[str] = set()

    for w in (routine or {}).get("alarmWindows") or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        old_id = str(w["id"])
        routine_ids.add(old_id)
        existing_uid = events.find_uid(envelope, old_id)
        if existing_uid and events.get_event(envelope, existing_uid):
            existing = events.get_event(envelope, existing_uid)
            skipped.append({"alias": old_id, "uid": existing_uid, "kind": (existing or {}).get("kind")})
            continue
        ledger_w = ledger.get(old_id)
        ev = events.map_window(
            w,
            ledger_w,
            as_of=ref_day,
            tz_offset=tz_offset,
            ledger_date=ledger_date,
            existing_uid=existing_uid,
        )
        planned.append({"alias": old_id, "uid": ev["uid"], "kind": ev["kind"], "rrule": ev["rrule"]})

    unmatched = sorted(set(ledger) - routine_ids)
    return {
        "dry_run": True,
        "as_of": ref_day.isoformat(),
        "planned": planned,
        "skipped_existing": skipped,
        "unmatched_ledger": unmatched,
    }


def _backup(workspace: Path, backup_dir: Path) -> Path:
    """真跑前备份：影子账本原样拷走，旧文件留快照与哈希；manifest 供回滚对账。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"{stamp}-migrate"
    dest.mkdir()

    events_file = events.events_path(workspace)
    manifest: dict[str, Any] = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "workspace": str(workspace),
        "files": {},
        "restored": False,
    }
    if events_file.exists():
        shutil.copyfile(events_file, dest / "events.json.bak")
        manifest["files"]["events.json"] = _sha256(events_file)
    else:
        manifest["files"]["events.json"] = None

    for rel, name in ((ROUTINE_REL, "routines.json.snapshot"), (STATE_REL, "alarms.json.snapshot")):
        p = workspace / rel
        if p.exists():
            shutil.copyfile(p, dest / name)
            manifest["files"][rel.as_posix()] = _sha256(p)

    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return dest


def apply_migration(workspace: Path, backup_dir: Path, *, as_of: date | None = None) -> dict[str, Any]:
    """真跑：先备份，再按计划投影；已存在别名/uid 的事件跳过，不重生。"""
    plan = plan_migration(workspace, as_of=as_of)
    backup = _backup(workspace, backup_dir)

    routine = _read_json(workspace / ROUTINE_REL)
    state = _read_json(workspace / STATE_REL)
    envelope = events.load_events(workspace)
    ledger = _ledger_index(state)
    tz_offset = str((routine or {}).get("tzOffset") or events.DEFAULT_OFFSET)
    ledger_date = str(state.get("planDate")) if state and state.get("planDate") else None

    for w in (routine or {}).get("alarmWindows") or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        old_id = str(w["id"])
        existing_uid = events.find_uid(envelope, old_id)
        if existing_uid and events.get_event(envelope, existing_uid):
            continue
        ev = events.map_window(
            w,
            ledger.get(old_id),
            as_of=date.fromisoformat(plan["as_of"]),
            tz_offset=tz_offset,
            ledger_date=ledger_date,
            existing_uid=existing_uid,
        )
        events.upsert_event(envelope, ev)

    events.save_events(workspace, envelope)
    plan["dry_run"] = False
    plan["backup_dir"] = str(backup)
    return plan


def rollback(workspace: Path, backup_dir: Path) -> dict[str, Any]:
    """按备份目录恢复影子账本；迁移前没有影子账本的，删除本次产物。

    旧 ``routines.json`` / ``alarms.json`` 本工具从未改写，回滚不触碰它们。
    """
    manifest_p = backup_dir / "manifest.json"
    if not manifest_p.exists():
        raise FileNotFoundError(f"备份目录缺少 manifest.json：{backup_dir}")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))

    events_file = events.events_path(workspace)
    bak = backup_dir / "events.json.bak"
    if bak.exists():
        events_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bak, events_file)
        restored = True
    elif events_file.exists():
        events_file.unlink()
        restored = False
    else:
        restored = False

    manifest["restored"] = True
    manifest_p.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {"restored": True, "events_existed": restored, "backup_dir": str(backup_dir)}


# ---------------------------------------------------------------------------
# P2：旧数据 → .ics 源真相（.ics + SQLite 索引；旧文件留在原地作只读历史）
# ---------------------------------------------------------------------------


def _existing_alias_index(workspace: Path) -> dict[str, str]:
    """现有 ``.ics`` 事件的 旧id → uid 别名映射；幂等重跑命中后保留原 uid。"""
    index: dict[str, str] = {}
    for ev in ics.load_all_events(workspace):
        uid = str(ev["uid"])
        for alias in ev.get("aliases") or []:
            index[str(alias)] = uid
    return index


def _project_to_ics(workspace: Path, as_of: date | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把旧 routine 窗口 + 当天账本投影成 Event 列表（不写盘）。

    补齐 ``.ics`` 需要的 ``summary``（旧窗口 label）与 ``tzid``（IANA 时区名）。
    已在现有 ``.ics`` 里按别名命中的事件跳过，不重生、不换 uid。
    """
    routine = _read_json(workspace / ROUTINE_REL)
    state = _read_json(workspace / STATE_REL)
    ledger = _ledger_index(state)
    tz_offset = str((routine or {}).get("tzOffset") or events.DEFAULT_OFFSET)
    tzid = str((routine or {}).get("timezone") or "").strip() or None
    ledger_date = str(state.get("planDate")) if state and state.get("planDate") else None
    ref_day = as_of or datetime.now().date()
    existing = _existing_alias_index(workspace)

    projected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    routine_ids: set[str] = set()
    for w in (routine or {}).get("alarmWindows") or []:
        if not isinstance(w, dict) or not w.get("id"):
            continue
        old_id = str(w["id"])
        routine_ids.add(old_id)
        existing_uid = existing.get(old_id)
        if existing_uid:
            skipped.append({"alias": old_id, "uid": existing_uid})
            continue
        ev = events.map_window(
            w,
            ledger.get(old_id),
            as_of=ref_day,
            tz_offset=tz_offset,
            ledger_date=ledger_date,
            existing_uid=existing_uid,
        )
        ev["summary"] = str(w.get("label") or ev["uid"])
        if tzid:
            ev["tzid"] = tzid
        projected.append(ev)

    unmatched = sorted(set(ledger) - routine_ids)
    return projected, {"skipped_existing": skipped, "unmatched_ledger": unmatched}


def plan_migration_to_ics(workspace: Path, *, as_of: date | None = None) -> dict[str, Any]:
    """dry-run：只读旧文件，算出将写入 ``.ics`` 的事件清单，不写任何文件。"""
    ref_day = as_of or datetime.now().date()
    projected, meta = _project_to_ics(workspace, ref_day)
    planned = [
        {
            "alias": ev["aliases"][0] if ev.get("aliases") else None,
            "uid": ev["uid"],
            "kind": ev["kind"],
            "rrule": ev["rrule"],
            "year": datetime.fromisoformat(str(ev["start"])).year,
        }
        for ev in projected
    ]
    return {"dry_run": True, "target": "ics", "as_of": ref_day.isoformat(), "planned": planned, **meta}


def _backup_ics(workspace: Path, backup_dir: Path) -> Path:
    """真跑前备份：calendar/ 目录、索引 db、影子账本原样拷走，旧文件留快照与哈希。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"{stamp}-to-ics"
    dest.mkdir()

    manifest: dict[str, Any] = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "workspace": str(workspace),
        "files": {},
        "dirs": {},
        "restored": False,
    }

    cal = ics.calendar_dir(workspace)
    if cal.exists():
        shutil.copytree(cal, dest / "calendar")
        manifest["dirs"]["calendar"] = True
    else:
        manifest["dirs"]["calendar"] = False

    idx = calindex.index_path(workspace)
    if idx.exists():
        shutil.copyfile(idx, dest / "calendar-index.db.bak")
        manifest["files"]["calendar-index.db"] = _sha256(idx)
    else:
        manifest["files"]["calendar-index.db"] = None

    events_file = events.events_path(workspace)
    if events_file.exists():
        shutil.copyfile(events_file, dest / "events.json.bak")
        manifest["files"]["events.json"] = _sha256(events_file)
    else:
        manifest["files"]["events.json"] = None

    for rel, name in ((ROUTINE_REL, "routines.json.snapshot"), (STATE_REL, "alarms.json.snapshot")):
        p = workspace / rel
        if p.exists():
            shutil.copyfile(p, dest / name)
            manifest["files"][rel.as_posix()] = _sha256(p)

    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return dest


# 影子账本（events.json）契约允许的字段；.ics 专有扩展（summary / categories /
# tzid / unknown / crossYear / rev / dtstamp）留在真相层，不进派生视图。
_SHADOW_FIELDS = (
    "uid",
    "kind",
    "start",
    "end",
    "rrule",
    "owner",
    "deadline",
    "status",
    "reminderPolicy",
    "aliases",
)

_DEFAULT_POLICY = {"preRemind": None, "ddlLadder": None, "snooze": None}
_VALID_STATUS = {"scheduled", "ringing", "done", "cancelled", "missed"}
_VALID_KIND = {"once", "recurring"}


def _shadow_event_from_ics(ev: dict[str, Any]) -> dict[str, Any]:
    """把一条 ``.ics`` 事件裁剪成影子账本契约形状；扩展字段不进派生视图。"""
    aliases = [str(a) for a in (ev.get("aliases") or [])]
    policy_in = ev.get("reminderPolicy") or {}
    policy = {**_DEFAULT_POLICY, **{k: v for k, v in policy_in.items() if k in _DEFAULT_POLICY}}
    status = ev.get("status") or "scheduled"
    if status not in _VALID_STATUS:
        status = "scheduled"
    kind = ev.get("kind") if ev.get("kind") in _VALID_KIND else "once"
    stamp = datetime.now().astimezone().isoformat()
    event: dict[str, Any] = {
        "uid": str(ev["uid"]),
        "kind": kind,
        "start": str(ev["start"]),
        "end": str(ev.get("end") or ev["start"]),
        "rrule": ev.get("rrule"),
        "owner": ev.get("owner") or "self",
        "deadline": ev.get("deadline"),
        "status": status,
        "reminderPolicy": policy,
        "aliases": aliases,
        "createdAt": stamp,
        "updatedAt": stamp,
        "source": {"routineId": aliases[0] if aliases else "", "ledgerDate": None},
    }
    return event


def rebuild_shadow_from_ics(workspace: Path) -> int:
    """从 ``.ics`` 真相重建 ``events.json`` 派生影子视图；幂等，事件集合与 `.ics` 逐条一致。

    派生视图只背契约字段；P3 适配器读影子账本时看到的核心判定字段与 `.ics` 对齐，
    不读到迁移前的陈旧快照。
    """
    envelope = events.empty_envelope()
    for ev in ics.load_all_events(workspace):
        events.upsert_event(envelope, _shadow_event_from_ics(ev))
    events.save_events(workspace, envelope)
    return len(events.all_events(envelope))


def apply_migration_to_ics(workspace: Path, backup_dir: Path, *, as_of: date | None = None) -> dict[str, Any]:
    """真跑：先备份，再投影写 ``.ics``，重建索引，最后从 ``.ics`` 重建影子视图；旧文件不删不改。"""
    plan = plan_migration_to_ics(workspace, as_of=as_of)
    backup = _backup_ics(workspace, backup_dir)

    projected, _meta = _project_to_ics(workspace, as_of or datetime.now().date())
    by_uid: dict[str, dict[str, Any]] = {str(ev["uid"]): ev for ev in ics.load_all_events(workspace)}
    for ev in projected:
        by_uid[str(ev["uid"])] = ev

    written = ics.write_events(workspace, list(by_uid.values()))
    indexed = calindex.rebuild(workspace)
    shadow = rebuild_shadow_from_ics(workspace)

    plan["dry_run"] = False
    plan["backup_dir"] = str(backup)
    plan["written_files"] = [str(p) for p in written]
    plan["indexed"] = indexed
    plan["shadow_events"] = shadow
    return plan


def rollback_ics(workspace: Path, backup_dir: Path) -> dict[str, Any]:
    """按备份目录恢复 ``calendar/`` 与索引；迁移前没有的，删除本次产物。

    旧 ``routines.json`` / ``alarms.json`` 本工具从未改写，回滚不触碰它们。
    """
    manifest_p = backup_dir / "manifest.json"
    if not manifest_p.exists():
        raise FileNotFoundError(f"备份目录缺少 manifest.json：{backup_dir}")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))

    cal = ics.calendar_dir(workspace)
    cal_bak = backup_dir / "calendar"
    if cal_bak.exists():
        if cal.exists():
            shutil.rmtree(cal)
        shutil.copytree(cal_bak, cal)
        calendar_existed = True
    elif cal.exists():
        shutil.rmtree(cal)
        calendar_existed = False
    else:
        calendar_existed = False

    idx = calindex.index_path(workspace)
    idx_bak = backup_dir / "calendar-index.db.bak"
    if idx_bak.exists():
        idx.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(idx_bak, idx)
    elif idx.exists():
        idx.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(f"{idx}{suffix}")
            if side.exists():
                side.unlink()

    events_file = events.events_path(workspace)
    ev_bak = backup_dir / "events.json.bak"
    if ev_bak.exists():
        events_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ev_bak, events_file)
    elif events_file.exists():
        events_file.unlink()

    manifest["restored"] = True
    manifest_p.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return {"restored": True, "calendar_existed": calendar_existed, "backup_dir": str(backup_dir)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rtc_migrate", description="L2 P1 迁移骨架：旧窗口投影到事件影子账本")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--backup-dir", help="真跑前备份目录；缺省 <workspace>/state/migrate-backup")
    ap.add_argument("--apply", action="store_true", help="真跑；默认 dry-run 只打印计划")
    ap.add_argument("--rollback", metavar="BACKUP_DIR", help="按备份目录恢复影子账本 / .ics")
    ap.add_argument(
        "--to-ics",
        action="store_true",
        help="P2：迁移到 .ics 源真相 + 索引（默认仍投影 events.json 影子账本）",
    )
    ap.add_argument("--as-of", help="模板级投影参考日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace)
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        if args.to_ics:
            backup_dir = Path(args.backup_dir) if args.backup_dir else workspace / "state" / "migrate-backup"
            if args.rollback:
                result = rollback_ics(workspace, Path(args.rollback))
            elif args.apply:
                result = apply_migration_to_ics(workspace, backup_dir, as_of=as_of)
            else:
                result = plan_migration_to_ics(workspace, as_of=as_of)
        elif args.rollback:
            result = rollback(workspace, Path(args.rollback))
        else:
            backup_dir = Path(args.backup_dir) if args.backup_dir else workspace / "state" / "migrate-backup"
            if args.apply:
                result = apply_migration(workspace, backup_dir, as_of=as_of)
            else:
                result = plan_migration(workspace, as_of=as_of)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"rtc_migrate: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
