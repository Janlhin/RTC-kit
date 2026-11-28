#!/usr/bin/env python3
"""L2 P2：旧数据 → .ics 迁移工具回归。

dry-run 默认 / 写前备份 / 幂等 / 可回滚；旧 routines.json / alarms.json 留在
原地作只读历史，迁移不删不改。零外部依赖，临时工作区。

运行：
    python -m unittest code.tests.test_migrate_ics -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
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

RTC = REPO / "code" / "rtc.py"
REPORT = REPO / "code" / "rtc_report.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

FIXTURE_DAY = "2026-11-30"
FIXTURE_NOW = f"{FIXTURE_DAY}T06:00:00+08:00"
DUE_NOW = f"{FIXTURE_DAY}T06:30:00+08:00"
MAIN_REF = "origin/main"
EVENT_SCHEMA = REPO / "contract" / "event.schema.json"

try:
    import jsonschema  # noqa: F401

    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


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

    def test_shadow_view_rebuilt_from_ics_matches_source(self):
        # C4：--to-ics 完成后从 .ics 重建 events.json 派生视图；适配器读到的核心字段与真相一致。
        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)

        source = {e["uid"]: e for e in ics.load_all_events(self.ws)}
        envelope = events.load_events(self.ws)
        shadow = {e["uid"]: e for e in events.all_events(envelope)}

        self.assertEqual(set(shadow), set(source), "影子视图事件集合必须与 .ics 一致")
        compare = ("kind", "start", "end", "rrule", "owner", "deadline", "status", "aliases")
        for uid, src in source.items():
            with self.subTest(uid=uid):
                ev = shadow[uid]
                for key in compare:
                    self.assertEqual(ev.get(key), src.get(key), f"{uid} 字段 {key} 不一致")
                # 扩展字段留在 .ics 真相层，不进契约形状的派生视图。
                self.assertNotIn("summary", ev)
                self.assertNotIn("tzid", ev)
                self.assertNotIn("unknown", ev)
                self.assertIn("reminderPolicy", ev)

        if HAVE_JSONSCHEMA:
            import jsonschema

            schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
            validator = jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
            )
            validator.validate(envelope)

    def test_shadow_view_is_idempotent_on_reapply(self):
        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        first = {e["uid"]: e for e in events.all_events(events.load_events(self.ws))}

        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        second = {e["uid"]: e for e in events.all_events(events.load_events(self.ws))}

        self.assertEqual(set(first), set(second))
        for uid, ev1 in first.items():
            ev2 = second[uid]
            for key in ("kind", "start", "end", "rrule", "owner", "deadline", "status", "aliases"):
                self.assertEqual(ev1.get(key), ev2.get(key), f"{uid} {key} 重跑漂移")

    def test_rollback_ics_restores_or_removes_shadow(self):
        # 迁移前无影子账本；apply 重建后回滚，影子产物应一并删除。
        self.assertFalse(events.events_path(self.ws).exists())
        migrate.apply_migration_to_ics(self.ws, self.backup, as_of=self.as_of)
        self.assertTrue(events.events_path(self.ws).exists())

        stamp_dir = next(self.backup.glob("*-to-ics"))
        migrate.rollback_ics(self.ws, stamp_dir)
        self.assertFalse(events.events_path(self.ws).exists())

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


def _golden_routine() -> dict:
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
                "dateTypes": ["workday"],
            },
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": FIXTURE_DAY,
                "validUntil": FIXTURE_DAY,
            },
            {
                "id": "report-ddl",
                "label": "交报告",
                "start": "18:00",
                "end": "18:15",
                "intervalMin": 5,
                "expectFires": 1,
                "dueDate": "2026-12-05",
            },
        ],
        "rollcalls": [],
        "phases": [],
    }


def _extract_main_code() -> Path | None:
    """把 ``origin/main`` 的 ``code/`` 解到临时目录；ref 不可达（CI 浅克隆）时返回 None。"""
    probe = subprocess.run(["git", "rev-parse", "--verify", MAIN_REF], cwd=REPO, capture_output=True)
    if probe.returncode != 0:
        return None
    archive = subprocess.run(
        ["git", "archive", MAIN_REF, "code"], cwd=REPO, capture_output=True, check=True
    ).stdout
    tmp = Path(tempfile.mkdtemp(prefix="rtc-main-code-"))
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        tf.extractall(tmp)
    return tmp / "code"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
    return p.returncode, p.stdout, p.stderr


def _five(rtc: Path, report: Path, ws: Path) -> dict[str, dict[str, str]]:
    """拍五条旧命令的退出码 / stdout / stderr 的 SHA256 快照。"""
    cmds: dict[str, list[str]] = {
        "status": [sys.executable, str(rtc), "--workspace", str(ws), "status", "--json"],
        "history": [sys.executable, str(rtc), "--workspace", str(ws), "history", FIXTURE_DAY],
        "schedule": [sys.executable, str(rtc), "--workspace", str(ws), "schedule", FIXTURE_DAY],
        "due": [sys.executable, str(rtc), "--workspace", str(ws), "due", "--now", DUE_NOW],
        "report": [sys.executable, str(report), "--workspace", str(ws), "--date", FIXTURE_DAY],
    }
    snap: dict[str, dict[str, str]] = {}
    for name, cmd in cmds.items():
        rc, out, err = _run(cmd)
        snap[name] = {
            "rc": hashlib.sha256(str(rc).encode("utf-8")).hexdigest(),
            "stdout": hashlib.sha256(out.encode("utf-8")).hexdigest(),
            "stderr": hashlib.sha256(err.encode("utf-8")).hexdigest(),
        }
    return snap


class TestEmptyWorkspaceGolden(unittest.TestCase):
    """D1：无 ``events.json`` / 无 ``calendar/`` 的工作区上，五条旧命令与 ``origin/main`` 基线逐字节一致。"""

    def setUp(self):
        self.ws_cur = Path(tempfile.mkdtemp(prefix="rtc-p2-golden-cur-"))
        (self.ws_cur / "rules").mkdir()
        (self.ws_cur / "state").mkdir()
        (self.ws_cur / "rules" / "routines.json").write_text(
            json.dumps(_golden_routine(), ensure_ascii=False), encoding="utf-8"
        )
        rc, out, err = _run(
            [sys.executable, str(RTC), "--workspace", str(self.ws_cur), "plan", "--now", FIXTURE_NOW]
        )
        self.assertEqual(rc, 0, err or out)

        self.ws_main = Path(tempfile.mkdtemp(prefix="rtc-p2-golden-main-"))
        shutil.copytree(self.ws_cur, self.ws_main, dirs_exist_ok=True)
        self.main_code = _extract_main_code()

    def tearDown(self):
        shutil.rmtree(self.ws_cur, ignore_errors=True)
        shutil.rmtree(self.ws_main, ignore_errors=True)
        if self.main_code is not None:
            shutil.rmtree(self.main_code.parent, ignore_errors=True)

    def _assert_no_l2_products(self, ws: Path) -> None:
        self.assertFalse(events.events_path(ws).exists(), "空工作区不应生成影子账本")
        self.assertFalse(ics.calendar_dir(ws).exists(), "空工作区不应生成 calendar/")

    def test_five_commands_match_main_baseline(self):
        if self.main_code is None:
            self.skipTest("origin/main ref 不可达（CI 浅克隆），跳过 main 基线对照")
        main_rtc = self.main_code / "rtc.py"
        main_report = self.main_code / "rtc_report.py"
        self.assertTrue(main_rtc.exists())
        self.assertTrue(main_report.exists())

        snap_main = _five(main_rtc, main_report, self.ws_main)
        snap_cur = _five(RTC, REPORT, self.ws_cur)

        for cmd in snap_main:
            for field in ("rc", "stdout", "stderr"):
                self.assertEqual(
                    snap_cur[cmd][field], snap_main[cmd][field], f"{cmd} {field} 与 main 基线不一致"
                )

        self._assert_no_l2_products(self.ws_cur)
        self._assert_no_l2_products(self.ws_main)

    def test_five_commands_stable_across_reruns(self):
        # main ref 不可达时的退化保护：当前 code 两遍自比快照稳定，且不偷写 L2 产物。
        snap1 = _five(RTC, REPORT, self.ws_cur)
        ws2 = Path(tempfile.mkdtemp(prefix="rtc-p2-golden-rerun-"))
        shutil.copytree(self.ws_cur, ws2, dirs_exist_ok=True)
        try:
            snap2 = _five(RTC, REPORT, ws2)
            for cmd in snap1:
                for field in ("rc", "stdout", "stderr"):
                    self.assertEqual(snap1[cmd][field], snap2[cmd][field], f"{cmd} {field} 重跑漂移")
            self._assert_no_l2_products(ws2)
        finally:
            shutil.rmtree(ws2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
