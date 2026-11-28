#!/usr/bin/env python3
"""L2 P1：Event 模型 / 影子账本 / 旧窗口投影 / 迁移骨架 / golden 逐字节比对。

覆盖：

- ``contract/event.schema.json`` 合法样例通过、非法 owner/uid/status 被拒。
- 旧窗口 → Event 投影：once/recurring、RRULE 生成、deadline、status 映射、别名。
- 迁移骨架四条路径：dry-run 不写盘、真跑先备份、重跑幂等、回滚可恢复。
- golden：迁移前后五条旧命令（``status --json`` / ``history <date>`` /
  ``schedule <date>`` / ``due --now`` / ``report``）stdout+stderr+退出码 SHA256 逐字节一致。
- 静态守卫：tick 热路径模块不得 import ``rtc_events`` / ``rtc_migrate``。

运行：
    python -m unittest code.tests.test_events_p1 -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

import rtc_events as events  # noqa: E402
import rtc_migrate as migrate  # noqa: E402

RTC = REPO / "code" / "rtc.py"
REPORT = REPO / "code" / "rtc_report.py"
MIGRATE = REPO / "code" / "rtc_migrate.py"
EVENT_SCHEMA = REPO / "contract" / "event.schema.json"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

FIXTURE_DAY = "2026-11-30"
FIXTURE_NOW = f"{FIXTURE_DAY}T06:00:00+08:00"
DUE_NOW = f"{FIXTURE_DAY}T06:30:00+08:00"

try:
    import jsonschema  # noqa: F401

    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture_routine() -> dict:
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


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
    return p.returncode, p.stdout, p.stderr


class _WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-p1-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_routine(self, routine: dict):
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def plan(self):
        rc, out, err = _run(
            [sys.executable, str(RTC), "--workspace", str(self.ws), "plan", "--now", FIXTURE_NOW]
        )
        self.assertEqual(rc, 0, err or out)

    def cli(self, *args: str) -> tuple[int, str, str]:
        return _run([sys.executable, str(RTC), "--workspace", str(self.ws), *args])

    def migrate_cli(self, *args: str) -> tuple[int, str, str]:
        return _run([sys.executable, str(MIGRATE), "--workspace", str(self.ws), *args])


class TestEventSchema(_WorkspaceCase):
    def _envelope(self) -> dict:
        routine = _fixture_routine()
        env = events.empty_envelope()
        for w in routine["alarmWindows"]:
            ev = events.map_window(w, as_of=date.fromisoformat(FIXTURE_DAY))
            events.upsert_event(env, ev)
        return env

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_projected_envelope_validates(self):
        import jsonschema

        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
        )
        errs = list(validator.iter_errors(self._envelope()))
        self.assertEqual(errs, [], f"投影样例不该被契约拒绝：{errs[:1]}")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_rejects_bad_owner(self):
        import jsonschema

        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
        env = self._envelope()
        env["events"][0]["owner"] = "principal"
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(env))
        self.assertTrue(errs, "owner 非 self/other:<name> 必须被拒")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_rejects_bad_uid(self):
        import jsonschema

        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
        env = self._envelope()
        env["events"][0]["uid"] = "plain-old-id"
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(env))
        self.assertTrue(errs, "非 RFC 5545 风格 uid 必须被拒")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_roundtrip_fields_validate(self):
        import jsonschema

        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
        env = self._envelope()
        ev = env["events"][0]
        ev.update(
            {
                "summary": "起床",
                "categories": ["routine", "morning"],
                "tzid": "Asia/Shanghai",
                "dtstamp": "2026-11-30T06:00:00+08:00",
                "unknown": {"X-RTC-ORIGINAL-SEQLABEL": "保留原样"},
                "rev": 3,
                "crossYear": False,
            }
        )
        validator = jsonschema.Draft202012Validator(schema)
        errs = list(validator.iter_errors(env))
        self.assertEqual(errs, [], f"往返字段不该被契约拒绝：{errs[:1]}")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_rejects_negative_rev(self):
        import jsonschema

        schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
        env = self._envelope()
        env["events"][0]["rev"] = -1
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(env))
        self.assertTrue(errs, "rev 为负必须被拒")


class TestWindowProjection(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 11, 30)

    def test_oneoff_maps_to_once_without_rrule(self):
        w = {
            "id": "travel",
            "start": "09:00",
            "end": "09:15",
            "oneOff": True,
            "validFrom": "2026-11-10",
            "validUntil": "2026-11-10",
        }
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["kind"], "once")
        self.assertIsNone(ev["rrule"])
        self.assertEqual(ev["owner"], "self")
        self.assertEqual(ev["aliases"], ["travel"])
        self.assertEqual(ev["source"]["routineId"], "travel")

    def test_rrule_oneoff_directly_returns_none(self):
        self.assertIsNone(
            events.rrule_for_window({"id": "x", "oneOff": True, "start": "09:00", "end": "09:15"})
        )

    def test_rrule_carries_until(self):
        w = {
            "id": "wake",
            "start": "07:00",
            "end": "07:25",
            "dateTypes": ["workday"],
            "validUntil": "2027-01-31",
        }
        self.assertEqual(
            events.rrule_for_window(w),
            "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20270131T235959+0800",
        )

    def test_until_is_date_time_and_keeps_last_day_evening(self):
        w = {
            "id": "wake",
            "start": "07:00",
            "end": "07:25",
            "dateTypes": ["workday"],
            "validUntil": "2026-11-30",
        }
        rrule = events.rrule_for_window(w, tz_offset="+08:00")
        self.assertIn("UNTIL=20261130T235959+0800", rrule)

        until = next(p.split("=", 1)[1] for p in rrule.split(";") if p.startswith("UNTIL="))
        until_dt = datetime.strptime(until, "%Y%m%dT%H%M%S%z")
        shanghai = timezone(timedelta(hours=8))
        evening_of_last_day = datetime(2026, 11, 30, 20, 0, tzinfo=shanghai)
        evening_of_next_day = datetime(2026, 12, 1, 20, 0, tzinfo=shanghai)
        # 截止日当天 20:00 实例仍落在窗口内；次日 20:00 实例已超出 UNTIL。
        self.assertLessEqual(evening_of_last_day, until_dt)
        self.assertGreater(evening_of_next_day, until_dt)

    def test_rrule_all_week_falls_back_to_daily_and_dedupes(self):
        w = {"id": "drink", "start": "15:00", "end": "15:10", "dateTypes": ["workday", "weekend"]}
        self.assertEqual(events.rrule_for_window(w), "FREQ=DAILY")

    def test_ledger_appointment_type_is_once(self):
        w = {"id": "meet", "start": "14:00", "end": "15:00", "dateTypes": ["workday"]}
        ledger = {"id": "meet", "type": "appointment", "state": "pending"}
        ev = events.map_window(w, ledger, as_of=self.as_of)
        self.assertEqual(ev["kind"], "once")

    def test_template_cancelled_status(self):
        w = {"id": "old", "start": "07:00", "end": "07:25", "canceled": True}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["status"], "cancelled")

    def test_invalid_offset_falls_back_to_eight(self):
        iso = events.compose_local_iso("2026-11-30", "09:00", "not-an-offset")
        self.assertTrue(iso.endswith("+08:00"))

    def test_workday_recurring_rrule(self):
        w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["kind"], "recurring")
        self.assertEqual(ev["rrule"], "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")

    def test_weekend_recurring_rrule(self):
        w = {"id": "yoga", "start": "08:00", "end": "08:30", "dateTypes": ["weekend"]}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["rrule"], "FREQ=WEEKLY;BYDAY=SA,SU")

    def test_no_date_types_is_daily(self):
        w = {"id": "drink", "start": "15:00", "end": "15:10"}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["rrule"], "FREQ=DAILY")

    def test_due_date_becomes_deadline(self):
        w = {"id": "report-ddl", "start": "18:00", "end": "18:15", "dueDate": "2026-12-05"}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["deadline"], "2026-12-05")

    def test_ledger_state_maps_to_status(self):
        w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        cases = {
            "pending": "scheduled",
            "ringing": "ringing",
            "acked": "done",
            "delivered": "done",
            "cancelled": "cancelled",
            "missed": "missed",
            "skipped": "scheduled",
        }
        for ledger_state, expected in cases.items():
            with self.subTest(state=ledger_state):
                ledger = {"id": "wake", "state": ledger_state, "start": "2026-11-30T07:00:00+08:00"}
                ev = events.map_window(w, ledger, as_of=self.as_of)
                self.assertEqual(ev["status"], expected)

    def test_ledger_instance_times_are_preserved(self):
        w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ledger = {
            "id": "wake",
            "ocid": "oc-wake-1",
            "state": "pending",
            "start": "2026-11-30T07:00:00+08:00",
            "end": "2026-11-30T07:25:00+08:00",
        }
        ev = events.map_window(w, ledger, as_of=self.as_of)
        self.assertEqual(ev["start"], ledger["start"])
        self.assertEqual(ev["end"], ledger["end"])
        self.assertEqual(ev["aliases"], ["wake", "oc-wake-1"])

    def test_existing_uid_is_kept(self):
        w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ev = events.map_window(w, as_of=self.as_of, existing_uid="stable-uid@rtc-kit")
        self.assertEqual(ev["uid"], "stable-uid@rtc-kit")

    def test_store_round_trip_and_alias_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            env = events.load_events(ws)
            w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
            ev = events.map_window(w, as_of=self.as_of)
            events.upsert_event(env, ev)
            events.save_events(ws, env)

            reloaded = events.load_events(ws)
            self.assertEqual(len(events.all_events(reloaded)), 1)
            self.assertEqual(events.find_uid(reloaded, "wake"), ev["uid"])
            self.assertEqual(events.get_event(reloaded, ev["uid"])["uid"], ev["uid"])

    def test_projection_emits_roundtrip_fields(self):
        w = {"id": "wake", "label": "起床", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["summary"], "起床")
        self.assertIsNone(ev["categories"])
        self.assertIsNone(ev["tzid"])
        self.assertIsNone(ev["unknown"])
        self.assertIsNone(ev["crossYear"])
        self.assertEqual(ev["rev"], 0)
        self.assertTrue(ev["dtstamp"])

    def test_summary_falls_back_to_id_without_label(self):
        w = {"id": "drink", "start": "15:00", "end": "15:10"}
        ev = events.map_window(w, as_of=self.as_of)
        self.assertEqual(ev["summary"], "drink")

    def test_rev_increments_on_reupsert_and_survives_round_trip(self):
        env = events.empty_envelope()
        w = {"id": "wake", "label": "起床", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ev = events.map_window(w, as_of=self.as_of)
        events.upsert_event(env, ev)
        self.assertEqual(events.get_event(env, ev["uid"])["rev"], 0)

        # 同一 uid 再次写入 = 一次修订，rev +1。
        events.upsert_event(env, events.get_event(env, ev["uid"]))
        self.assertEqual(events.get_event(env, ev["uid"])["rev"], 1)
        events.upsert_event(env, events.get_event(env, ev["uid"]))
        self.assertEqual(events.get_event(env, ev["uid"])["rev"], 2)

        # JSON 往返后 rev 守恒，不被重新投影清零。
        reloaded = json.loads(json.dumps(env, ensure_ascii=False))
        self.assertEqual(events.get_event(reloaded, ev["uid"])["rev"], 2)

    def test_corrupt_shadow_file_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "state").mkdir()
            (ws / "state" / "events.json").write_text("{ not json", encoding="utf-8")
            env = events.load_events(ws)
            self.assertEqual(env["events"], [])
            self.assertEqual(env["aliases"], {})

    def test_non_dict_shadow_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "state").mkdir()
            (ws / "state" / "events.json").write_text("[1, 2, 3]", encoding="utf-8")
            env = events.load_events(ws)
            self.assertEqual(env["events"], [])

    def test_get_event_miss_returns_none(self):
        env = events.empty_envelope()
        self.assertIsNone(events.get_event(env, "nobody@rtc-kit"))

    def test_upsert_replaces_same_uid(self):
        env = events.empty_envelope()
        w = {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]}
        ev = events.map_window(w, as_of=self.as_of)
        events.upsert_event(env, ev)
        ev2 = dict(ev)
        ev2["status"] = "done"
        events.upsert_event(env, ev2)
        self.assertEqual(len(events.all_events(env)), 1)
        self.assertEqual(events.get_event(env, ev["uid"])["status"], "done")

    def test_save_failure_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            env = events.empty_envelope()
            with (
                mock.patch("rtc_events.os.replace", side_effect=OSError("disk full")),
                self.assertRaises(OSError),
            ):
                events.save_events(ws, env)
            leftovers = [p.name for p in (ws / "state").iterdir() if p.name.startswith(".events.")]
            self.assertEqual(leftovers, [])


class TestMigrationSkeleton(_WorkspaceCase):
    def _backup_dir(self) -> Path:
        bd = self.ws / "state" / "migrate-backup"
        bd.mkdir(parents=True, exist_ok=True)
        return bd

    def test_dry_run_writes_nothing(self):
        self.write_routine(_fixture_routine())
        rc, out, err = self.migrate_cli("--as-of", FIXTURE_DAY)
        self.assertEqual(rc, 0, err)
        plan = json.loads(out)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(len(plan["planned"]), 3)
        self.assertFalse((self.ws / "state" / "events.json").exists())

    def test_apply_backs_up_and_projects(self):
        self.write_routine(_fixture_routine())
        bd = self._backup_dir()
        rc, out, err = self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertFalse(result["dry_run"])
        env = events.load_events(self.ws)
        self.assertEqual(len(events.all_events(env)), 3)
        aliases = {ev["aliases"][0]: ev["uid"] for ev in events.all_events(env)}
        self.assertEqual(set(aliases), {"wake", "travel", "report-ddl"})
        backups = list(bd.iterdir())
        self.assertEqual(len(backups), 1)
        manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["files"]["events.json"], "首跑前没有影子账本，备份应记 null")

    def test_idempotent_reapply_keeps_uids(self):
        self.write_routine(_fixture_routine())
        bd = self._backup_dir()
        self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        first = {ev["aliases"][0]: ev["uid"] for ev in events.all_events(events.load_events(self.ws))}

        rc, out, err = self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        self.assertEqual(rc, 0, err)
        second_plan = json.loads(out)
        self.assertEqual(len(second_plan["skipped_existing"]), 3)
        self.assertEqual(second_plan["planned"], [])
        second = {ev["aliases"][0]: ev["uid"] for ev in events.all_events(events.load_events(self.ws))}
        self.assertEqual(first, second, "重跑不得换 uid、不得产生第二份")

    def test_rollback_removes_shadow_when_none_existed(self):
        self.write_routine(_fixture_routine())
        bd = self._backup_dir()
        self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        self.assertTrue((self.ws / "state" / "events.json").exists())
        backup = sorted(bd.iterdir())[0]

        rc, out, err = self.migrate_cli("--rollback", str(backup))
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.ws / "state" / "events.json").exists())

    def test_rollback_restores_preexisting_shadow(self):
        self.write_routine(_fixture_routine())
        shadow = self.ws / "state" / "events.json"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text('{"version": 1, "events": [], "aliases": {}}\n', encoding="utf-8")
        marker = shadow.read_text(encoding="utf-8")

        bd = self._backup_dir()
        self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        self.assertNotEqual(shadow.read_text(encoding="utf-8"), marker)
        backup = sorted(bd.iterdir())[0]

        rc, _, err = self.migrate_cli("--rollback", str(backup))
        self.assertEqual(rc, 0, err)
        self.assertEqual(shadow.read_text(encoding="utf-8"), marker)

    def test_old_files_untouched_after_apply(self):
        self.write_routine(_fixture_routine())
        self.plan()
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.ws / "state").iterdir()
            if p.is_file()
        }
        routine_before = (self.ws / "rules" / "routines.json").read_bytes()

        bd = self._backup_dir()
        rc, _, err = self.migrate_cli("--backup-dir", str(bd), "--apply", "--as-of", FIXTURE_DAY)
        self.assertEqual(rc, 0, err)

        self.assertEqual((self.ws / "rules" / "routines.json").read_bytes(), routine_before)
        after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.ws / "state").iterdir()
            if p.is_file() and p.name != "events.json"
        }
        for name, digest in before.items():
            if name == "events.json":
                continue
            self.assertEqual(after.get(name), digest, f"旧账本文件 {name} 被迁移改动了")

    def test_unmatched_ledger_ids_listed(self):
        self.write_routine(_fixture_routine())
        state = self.ws / "state" / "alarms.json"
        state.write_text(
            json.dumps(
                {"planDate": FIXTURE_DAY, "rev": 1, "windows": [{"id": "ghost", "state": "pending"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plan = migrate.plan_migration(self.ws, as_of=date.fromisoformat(FIXTURE_DAY))
        self.assertIn("ghost", plan["unmatched_ledger"])

    def test_malformed_windows_are_skipped(self):
        routine = _fixture_routine()
        routine["alarmWindows"].append("not-a-window")
        routine["alarmWindows"].append({"label": "no-id"})
        self.write_routine(routine)
        plan = migrate.plan_migration(self.ws, as_of=date.fromisoformat(FIXTURE_DAY))
        self.assertEqual(len(plan["planned"]), 3)

    def test_ledger_entry_without_id_is_ignored(self):
        self.write_routine(_fixture_routine())
        state = self.ws / "state" / "alarms.json"
        state.write_text(
            json.dumps({"planDate": FIXTURE_DAY, "rev": 1, "windows": ["junk", {"state": "pending"}]}),
            encoding="utf-8",
        )
        plan = migrate.plan_migration(self.ws, as_of=date.fromisoformat(FIXTURE_DAY))
        self.assertEqual(len(plan["planned"]), 3)

    def test_sha256_missing_file_is_none(self):
        self.assertIsNone(migrate._sha256(self.ws / "nope.json"))

    def test_rollback_already_absent_is_noop(self):
        bd = self._backup_dir()
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "manifest.json").write_text(
            json.dumps({"files": {"events.json": None}, "restored": False}), encoding="utf-8"
        )
        rc, out, err = self.migrate_cli("--rollback", str(bd))
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.ws / "state" / "events.json").exists())

    def test_rollback_missing_manifest_fails_cleanly(self):
        rc, _, err = self.migrate_cli("--rollback", str(self.ws / "state" / "nope"))
        self.assertEqual(rc, 3)
        self.assertIn("manifest.json", err)


def _capture_five(ws: Path) -> dict[str, dict[str, str]]:
    commands: dict[str, list[str]] = {
        "status": [sys.executable, str(RTC), "--workspace", str(ws), "status", "--json"],
        "history": [sys.executable, str(RTC), "--workspace", str(ws), "history", FIXTURE_DAY],
        "schedule": [sys.executable, str(RTC), "--workspace", str(ws), "schedule", FIXTURE_DAY],
        "due": [sys.executable, str(RTC), "--workspace", str(ws), "due", "--now", DUE_NOW],
        "report": [sys.executable, str(REPORT), "--workspace", str(ws), "--date", FIXTURE_DAY],
    }
    snap: dict[str, dict[str, str]] = {}
    for name, cmd in commands.items():
        rc, out, err = _run(cmd)
        snap[name] = {
            "rc": _sha(str(rc)),
            "stdout": _sha(out),
            "stderr": _sha(err),
            "rc_value": str(rc),
        }
    return snap


class TestGoldenRegression(_WorkspaceCase):
    def test_five_commands_byte_identical_after_migration(self):
        self.write_routine(_fixture_routine())
        self.plan()

        before = _capture_five(self.ws)

        # 复制一份工作区做迁移：隔离 due 自身的 incidental 写入，只比迁移带来的差异。
        ws2 = Path(tempfile.mkdtemp(prefix="rtc-p1-golden-"))
        shutil.copytree(self.ws, ws2, dirs_exist_ok=True)
        try:
            bd = ws2 / "state" / "migrate-backup"
            rc, out, err = _run(
                [
                    sys.executable,
                    str(MIGRATE),
                    "--workspace",
                    str(ws2),
                    "--backup-dir",
                    str(bd),
                    "--apply",
                    "--as-of",
                    FIXTURE_DAY,
                ]
            )
            self.assertEqual(rc, 0, err or out)

            after = _capture_five(ws2)

            for name in before:
                with self.subTest(command=name):
                    self.assertEqual(before[name]["rc_value"], after[name]["rc_value"], "退出码变了")
                    self.assertEqual(before[name]["stdout"], after[name]["stdout"], "stdout 不一致")
                    self.assertEqual(before[name]["stderr"], after[name]["stderr"], "stderr 不一致")

            # 旧真相源文件逐字节未动。
            for rel in ("rules/routines.json", "state/alarms.json"):
                self.assertEqual(
                    (self.ws / rel).read_bytes(),
                    (ws2 / rel).read_bytes(),
                    f"{rel} 迁移后被改动",
                )
        finally:
            shutil.rmtree(ws2, ignore_errors=True)


class TestHotPathImportGuard(unittest.TestCase):
    TICK_MODULES = [
        "rtc.py",
        "rtc_core.py",
        "rtc_plan.py",
        "rtc_due.py",
        "rtc_once.py",
        "rtc_brief.py",
        "rtc_report.py",
        "rtc_recovery.py",
        "rtc_metrics.py",
        "rtc_history.py",
        "rtc_archive.py",
        "rtc_ics.py",
    ]

    def test_tick_modules_do_not_import_events_stack(self):
        code_dir = REPO / "code"
        for name in self.TICK_MODULES:
            src = (code_dir / name).read_text(encoding="utf-8")
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn("rtc_events", stripped, f"{name} 热路径不得 import rtc_events")
                self.assertNotIn("rtc_migrate", stripped, f"{name} 热路径不得 import rtc_migrate")


if __name__ == "__main__":
    unittest.main()
