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
import rtc_events as events  # noqa: E402

__all__ = ["plan_migration", "apply_migration", "rollback", "main"]

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rtc_migrate", description="L2 P1 迁移骨架：旧窗口投影到事件影子账本")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--backup-dir", help="真跑前备份目录；缺省 <workspace>/state/migrate-backup")
    ap.add_argument("--apply", action="store_true", help="真跑；默认 dry-run 只打印计划")
    ap.add_argument("--rollback", metavar="BACKUP_DIR", help="按备份目录恢复影子账本")
    ap.add_argument("--as-of", help="模板级投影参考日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace)
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        if args.rollback:
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
