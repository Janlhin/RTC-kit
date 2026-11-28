#!/usr/bin/env python3
"""L2 P2：旧数据 → .ics 迁移工具回归。

dry-run 默认 / 写前备份 / 幂等 / 可回滚；旧 routines.json / alarms.json 留在
原地作只读历史，迁移不删不改。零外部依赖，临时工作区。

运行：
    python -m unittest code.tests.test_migrate_ics -v
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_calindex as calindex  # noqa: E402
import rtc_events as events  # noqa: E402
import rtc_ics as ics  # noqa: E402
import rtc_migrate as migrate  # noqa: E402


def _routine() -> dict:
    return {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "tzOffset": "+08:00",
        "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
        "alarmWindows": [
            {
                "id": "wake",
                "label": "起床",
                "start": "07:00",
                "end": "07:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["起了"],
            },
            {
                "id": "dentist",
                "label": "看牙",
                "start": "15:00",
                "end": "15:30",
                "intervalMin": 30,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-20",
                "validUntil": "2026-11-20",
            },
        ],
        "rollcalls": [],
        "phases": [],
    }


def _state() -> dict:
    return {
        "version": 1,
        "rev": 1,
        "timezone": "Asia/Shanghai",
        "planDate": "2026-11-18",
        "planKind": "workday",
        "windows": [],
    }


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MigrateIcsCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-migrate-ics-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        self.routine_p = self.ws / "rules" / "routines.json"
        self.state_p = self.ws / "state" / "alarms.json"
        self.routine_p.write_text(json.dumps(_routine(), ensure_ascii=False), encoding="utf-8")
        self.state_p.write_text(json.dumps(_state(), ensure_ascii=False), encoding="utf-8")
        self.backup = self.ws / "state" / "migrate-backup"
        self.as_of = date(2026, 11, 18)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_dry_run_writes_nothing(self):
        plan = migrate.plan_migration_to_ics(self.ws, as_of=self.as_of)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["target"], "ics")
        self.assertEqual(len(plan["planned"]), 2)
        self.assertFalse(ics.calendar_dir(self.ws).exists())
        self.assertFalse(calindex.index_path(self.ws).exists())

    def test_apply_writes_ics_and_index_keeps_old_files(self):
        before_routine = _sha(self.routine_p)
        before_state = _sha(self.state_p)

        result = migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["indexed"], 2)

        cal_dir = ics.calendar_dir(self.ws)
        self.assertTrue(cal_dir.exists())
        self.assertTrue(list(cal_dir.glob("*.ics")))
        self.assertTrue(calindex.index_path(self.ws).exists())

        source = ics.load_all_events(self.ws)
        self.assertEqual(len(source), 2)
        self.assertEqual(calindex.diff_with_source(self.ws), [])

        # 旧文件留在原地，字节不变。
        self.assertEqual(_sha(self.routine_p), before_routine)
        self.assertEqual(_sha(self.state_p), before_state)

    def test_second_apply_is_idempotent(self):
        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        first_uids = {e["uid"] for e in ics.load_all_events(self.ws)}
        first_files = {p.name: p.read_bytes() for p in ics.calendar_dir(self.ws).glob("*.ics")}

        second = migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        second_uids = {e["uid"] for e in ics.load_all_events(self.ws)}
        self.assertEqual(first_uids, second_uids, "重跑不产生第二份、不换 uid")
        self.assertEqual(second["indexed"], 2)
        for name, body in first_files.items():
            self.assertEqual((ics.calendar_dir(self.ws) / name).read_bytes(), body, name)

    def test_rollback_removes_products_when_nothing_existed_before(self):
        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        self.assertTrue(ics.calendar_dir(self.ws).exists())
        stamp_dir = next(self.backup.glob("*-to-ics"))

        migrate.rollback_ics(self.ws, stamp_dir)
        self.assertFalse(ics.calendar_dir(self.ws).exists())
        self.assertFalse(calindex.index_path(self.ws).exists())

    def test_rollback_restores_preexisting_calendar(self):
        # 迁移前已有一份旧 .ics；apply 追加新事件后回滚，旧 .ics 必须还原。
        old_ev = {
            "uid": "dddddddd-1111-4ddd-8ddd-000000000009@rtc-kit",
            "kind": "once",
            "start": "2026-01-05T08:00:00+08:00",
            "end": "2026-01-05T08:15:00+08:00",
            "rrule": None,
            "owner": "self",
            "deadline": None,
            "status": "scheduled",
            "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
            "aliases": ["old-one"],
            "summary": "旧日程",
            "categories": [],
            "tzid": None,
            "dtstamp": "20260105T000000Z",
        }
        ics.write_events(self.ws, [old_ev])
        old_body = (ics.calendar_dir(self.ws) / "2026.ics").read_bytes()

        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        self.assertEqual(len(ics.load_all_events(self.ws)), 3)

        stamp_dir = next(self.backup.glob("*-to-ics"))
        migrate.rollback_ics(self.ws, stamp_dir)
        restored = (ics.calendar_dir(self.ws) / "2026.ics").read_bytes()
        self.assertEqual(restored, old_body)
        self.assertEqual([e["uid"] for e in ics.load_all_events(self.ws)], [old_ev["uid"]])

    def test_p1_shadow_apply_and_rollback_still_work(self):
        # P1 影子账本路径（events.json）不被 P2 迁移破坏；顺带覆盖其公开 API。
        plan = migrate.plan_migration(self.ws, as_of=self.as_of)
        self.assertEqual(len(plan["planned"]), 2)
        result = migrate.apply_migration(self.ws, self.backup, as_of=self.as_of)
        self.assertFalse(result["dry_run"])
        envelope = events.load_events(self.ws)
        self.assertEqual(len(envelope["events"]), 2)

        stamp_dir = next(self.backup.glob("*-migrate"))
        migrate.rollback(self.ws, stamp_dir)
        self.assertFalse(events.events_path(self.ws).exists())


if __name__ == "__main__":
    unittest.main()
