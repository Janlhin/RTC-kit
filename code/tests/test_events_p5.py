#!/usr/bin/env python3
"""L2 P5：事件级 CRUD（create / edit / complete / cancel / undo）+ owner/deadline/reminderPolicy。

覆盖：

- 全链路：create → edit → complete → cancel → undo，字段持久化、审计 old/new、history 留痕。
- 幂等：同 uid 重复 create 不产生第二条，不涨审计 / history。
- 撤销口径：只当天有效；跨日拒绝；无痕迹失败；当前非 cancelled 不复活。
- 并发：摘要守卫命中别人写入 → ``Conflict``，磁盘文件不被覆盖。
- 校验：owner / 时刻顺序 / deadline / 分钟数 / 空标题拒绝（退出 3）；找不到事件退出 4。
- 旧命令零变化：事件命令跑完后，五条旧命令 stdout+stderr+退出码 SHA256 逐字节一致。

运行：
    python code/tests/test_events_p5.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

import rtc_audit as audit  # noqa: E402
import rtc_core as core  # noqa: E402
import rtc_event_cmd as event_cmd  # noqa: E402
import rtc_events as events  # noqa: E402
import rtc_history as hist  # noqa: E402

RTC = REPO / "code" / "rtc.py"
REPORT = REPO / "code" / "rtc_report.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

FIXTURE_DAY = "2026-11-30"
FIXTURE_NOW = f"{FIXTURE_DAY}T06:00:00+08:00"
DUE_NOW = f"{FIXTURE_DAY}T06:30:00+08:00"
CREATE_NOW = "2026-11-28T10:00:00+08:00"
NEXT_DAY_NOW = "2026-11-29T10:06:00+08:00"
START_ISO = "2026-12-01T15:00:00+08:00"
END_ISO = "2026-12-01T15:30:00+08:00"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
    return p.returncode, p.stdout, p.stderr


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
        ],
        "rollcalls": [],
        "phases": [],
    }


class _WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-p5-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        self.old_ws = core.WORKSPACE
        core.WORKSPACE = self.ws

    def tearDown(self):
        core.WORKSPACE = self.old_ws
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_routine(self, routine: dict | None = None):
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine or _fixture_routine(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def cli(self, *args: str) -> tuple[int, str, str]:
        return _run([sys.executable, str(RTC), "--workspace", str(self.ws), *args])

    def load(self) -> dict:
        return events.load_events(self.ws)

    def first_uid(self) -> str:
        envelope = self.load()
        self.assertEqual(len(events.all_events(envelope)), 1)
        return events.all_events(envelope)[0]["uid"]

    def audit_records(self) -> list[dict]:
        return audit.read_events(self.ws / "state" / "audit.jsonl")

    def create_event(self, *extra: str, title: str = "看牙") -> tuple[int, str, str, str]:
        rc, out, err = self.cli(
            "create",
            "--title",
            title,
            "--start",
            "2026-12-01T15:00:00+08:00",
            "--end",
            "2026-12-01T15:30:00+08:00",
            "--now",
            CREATE_NOW,
            *extra,
        )
        uid = self.first_uid() if rc == 0 else ""
        return rc, out, err, uid


class TestEventCreate(_WorkspaceCase):
    def test_create_persists_owner_deadline_and_policy(self):
        rc, _, err, uid = self.create_event(
            "--owner",
            "other:牙医",
            "--deadline",
            "2026-12-01",
            "--pre-remind",
            "30",
            "--ddl-ladder",
            "120",
            "--ddl-ladder",
            "60",
            "--snooze",
            "10",
        )
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["owner"], "other:牙医")
        self.assertEqual(ev["deadline"], "2026-12-01")
        self.assertEqual(ev["kind"], "once")
        self.assertIsNone(ev["rrule"])
        self.assertEqual(ev["reminderPolicy"]["preRemind"], {"minutesBefore": 30})
        self.assertEqual(ev["reminderPolicy"]["ddlLadder"], [60, 120])
        self.assertEqual(ev["reminderPolicy"]["snooze"], {"minutes": 10})
        self.assertEqual(ev["status"], "scheduled")

    def test_recurring_kind_from_rrule(self):
        rc, _, err, uid = self.create_event("--rrule", "FREQ=WEEKLY;BYDAY=MO,WE,FR")
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["kind"], "recurring")
        self.assertEqual(ev["rrule"], "FREQ=WEEKLY;BYDAY=MO,WE,FR")

    def test_idempotent_create_same_uid_keeps_single_record(self):
        uid = events.new_uid()
        rc, _, err, _ = self.create_event("--uid", uid, title="第一次")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.cli(
            "create",
            "--uid",
            uid,
            "--title",
            "第二次",
            "--start",
            "2026-12-02T09:00:00+08:00",
            "--end",
            "2026-12-02T09:30:00+08:00",
            "--now",
            CREATE_NOW,
        )
        self.assertEqual(rc, 0, err)
        envelope = self.load()
        self.assertEqual(len(events.all_events(envelope)), 1)
        ev = events.get_event(envelope, uid)
        self.assertEqual(ev["title"], "第一次", "同 uid 重复 create 不得覆盖原事件")
        creates = [r for r in self.audit_records() if r["action"] == "create"]
        self.assertEqual(len(creates), 1)

    def test_audit_and_history_written_on_create(self):
        rc, _, err, uid = self.create_event()
        self.assertEqual(rc, 0, err)
        records = self.audit_records()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["action"], "create")
        self.assertEqual(rec["target"], uid)
        self.assertEqual(rec["actor"], "user")
        self.assertEqual(rec["source"], "event:create")
        self.assertIsNone(rec["old"])
        self.assertIn("event", rec["new"])
        rows = hist.events(uid)
        self.assertEqual([r["kind"] for r in rows], ["create"])
        self.assertEqual(rows[0]["actor"], "user")
        self.assertEqual(rows[0]["source"], "event-cli")

    def test_validation_rejections_exit_3(self):
        base = ["--start", START_ISO, "--end", END_ISO]
        cases: list[list[str]] = [
            ["--title", "", *base],
            ["--title", "x", "--start", END_ISO, "--end", START_ISO],
            ["--title", "x", *base, "--owner", "principal"],
            ["--title", "x", *base, "--deadline", "12/01/2026"],
            ["--title", "x", *base, "--pre-remind", "0"],
            ["--title", "x", *base, "--snooze", "-5"],
        ]
        for i, extra in enumerate(cases):
            with self.subTest(case=i):
                rc, _, err = self.cli("create", "--now", CREATE_NOW, *extra)
                self.assertEqual(rc, 3, err)
                self.assertEqual(self.load()["events"], [])

    def test_create_bad_iso_rejected(self):
        rc, _, err = self.cli(
            "create",
            "--title",
            "x",
            "--start",
            "not-a-time",
            "--end",
            "2026-12-01T15:30:00+08:00",
            "--now",
            CREATE_NOW,
        )
        self.assertEqual(rc, 3, err)


class TestEventCreateFromPhrase(_WorkspaceCase):
    """P4 接线：``create --when`` 中文短语落账；解析口径复用 rtc_nlu 既有单测。"""

    _WINDOW = timedelta(minutes=4)

    def latest_uid(self) -> str:
        return events.all_events(self.load())[-1]["uid"]

    def create_when(self, phrase: str, *extra: str, title: str = "提醒") -> tuple[int, str, str, str]:
        rc, out, err = self.cli("create", "--title", title, "--when", phrase, "--now", CREATE_NOW, *extra)
        uid = self.latest_uid() if rc == 0 else ""
        return rc, out, err, uid

    def _assert_offset_window(self, ev: dict) -> None:
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"])
        self.assertIsNotNone(start.tzinfo, "start 必须带偏移")
        self.assertIsNotNone(end.tzinfo, "end 必须带偏移")
        self.assertEqual(end - start, self._WINDOW, "NLU 派生窗口默认 4 分钟")

    def test_recurring_phrases_persist_rrule_and_first_slot(self):
        cases: list[tuple[str, str]] = [
            ("每周三晚上八点", "FREQ=WEEKLY;BYDAY=WE"),
            ("每周一三五", "FREQ=WEEKLY;BYDAY=MO,WE,FR"),
            ("每天早上八点", "FREQ=DAILY"),
            ("每年11月5日", "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=5"),
            ("每月1号", "FREQ=MONTHLY;BYMONTHDAY=1"),
            ("每3天", "FREQ=DAILY;INTERVAL=3"),
            ("每2周", "FREQ=WEEKLY;INTERVAL=2"),
            ("每4个月", "FREQ=MONTHLY;INTERVAL=4"),
        ]
        for phrase, rrule in cases:
            with self.subTest(phrase=phrase):
                rc, _, err, uid = self.create_when(phrase)
                self.assertEqual(rc, 0, err)
                ev = events.get_event(self.load(), uid)
                self.assertEqual(ev["kind"], "recurring")
                self.assertEqual(ev["rrule"], rrule)
                self._assert_offset_window(ev)

    def test_recurring_first_slot_pinned_for_two_cases(self):
        rc, _, err, uid = self.create_when("每月1号")
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["start"], "2026-12-01T09:00:00+08:00")

        rc, _, err, uid = self.create_when("每天早上八点", title="喝水")
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["start"], "2026-11-29T08:00:00+08:00")

    def test_once_phrases_persist_null_rrule(self):
        phrases = ["明天早上八点", "明天下午三点", "下周三下午3点", "半小时后"]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                rc, _, err, uid = self.create_when(phrase)
                self.assertEqual(rc, 0, err)
                ev = events.get_event(self.load(), uid)
                self.assertEqual(ev["kind"], "once")
                self.assertIsNone(ev["rrule"])
                self._assert_offset_window(ev)

    def test_when_conflicts_with_structured_args_exit_3(self):
        cases = [
            ["--start", START_ISO],
            ["--end", END_ISO],
            ["--rrule", "FREQ=DAILY"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                rc, _, err = self.cli(
                    "create",
                    "--title",
                    "x",
                    "--when",
                    "明天八点",
                    "--now",
                    CREATE_NOW,
                    *extra,
                )
                self.assertEqual(rc, 3, err)
                self.assertEqual(self.load()["events"], [])

    def test_unsupported_phrases_exit_2_and_leave_ledger_empty(self):
        phrases = ["每月最后一个工作日", "每年第3周的周三", "工作日", "节假日", "嘀嘀嗒嗒"]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                rc, _, err = self.cli("create", "--title", "x", "--when", phrase, "--now", CREATE_NOW)
                self.assertEqual(rc, 2, err)
                self.assertEqual(self.load()["events"], [])


class TestEventEditCompleteCancel(_WorkspaceCase):
    def test_edit_partial_update_persists_old_new_audit(self):
        rc, _, err, uid = self.create_event(title="看牙")
        self.assertEqual(rc, 0, err)
        prior = events.get_event(self.load(), uid)

        rc, _, err = self.cli(
            "edit",
            "--uid",
            uid,
            "--title",
            "洗牙",
            "--owner",
            "other:牙医",
            "--pre-remind",
            "15",
            "--now",
            "2026-11-28T10:05:00+08:00",
        )
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["title"], "洗牙")
        self.assertEqual(ev["owner"], "other:牙医")
        self.assertEqual(ev["reminderPolicy"]["preRemind"], {"minutesBefore": 15})
        # 未改字段保持原样
        self.assertEqual(ev["start"], prior["start"])
        self.assertEqual(ev["deadline"], prior["deadline"])

        edit_records = [r for r in self.audit_records() if r["action"] == "edit"]
        self.assertEqual(len(edit_records), 1)
        self.assertEqual(edit_records[0]["old"]["event"]["title"], "看牙")
        self.assertEqual(edit_records[0]["new"]["event"]["title"], "洗牙")
        self.assertEqual([r["kind"] for r in hist.events(uid) if r["kind"] == "edit"], ["edit"])

    def test_edit_by_alias_after_migration(self):
        # 旧 id 别名命中：手工塞一条带别名的投影事件。
        envelope = self.load()
        projected = events.map_window(
            {"id": "wake", "start": "07:00", "end": "07:25", "dateTypes": ["workday"]},
            as_of=date.fromisoformat(FIXTURE_DAY),
        )
        events.upsert_event(envelope, projected)
        events.save_events(self.ws, envelope)

        rc, _, err = self.cli("edit", "--alias", "wake", "--title", "晨练", "--now", CREATE_NOW)
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), projected["uid"])
        self.assertEqual(ev["title"], "晨练")

    def test_edit_not_found_exit_4(self):
        rc, _, err = self.cli("edit", "--uid", "nobody@rtc-kit", "--title", "x", "--now", CREATE_NOW)
        self.assertEqual(rc, 4, err)

    def test_complete_marks_done(self):
        rc, _, err, uid = self.create_event()
        self.assertEqual(rc, 0, err)
        rc, _, err = self.cli("complete", "--uid", uid, "--now", "2026-11-28T10:10:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(events.get_event(self.load(), uid)["status"], "done")
        records = [r for r in self.audit_records() if r["action"] == "complete"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["old"]["event"]["status"], "scheduled")
        self.assertEqual(records[0]["new"]["event"]["status"], "done")

    def test_complete_already_done_is_noop(self):
        rc, _, err, uid = self.create_event()
        self.cli("complete", "--uid", uid, "--now", CREATE_NOW)
        rc, _, err = self.cli("complete", "--uid", uid, "--now", CREATE_NOW)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len([r for r in self.audit_records() if r["action"] == "complete"]), 1)

    def test_cancel_and_undo_restores_snapshot(self):
        rc, _, err, uid = self.create_event(title="取快递")
        self.assertEqual(rc, 0, err)
        prior = events.get_event(self.load(), uid)

        rc, _, err = self.cli("cancel", "--uid", uid, "--now", "2026-11-28T10:05:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(events.get_event(self.load(), uid)["status"], "cancelled")

        rc, _, err = self.cli("cancel", "--uid", uid, "--undo", "--now", "2026-11-28T10:06:00+08:00")
        self.assertEqual(rc, 0, err)
        ev = events.get_event(self.load(), uid)
        self.assertEqual(ev["status"], "scheduled")
        self.assertEqual(ev["title"], prior["title"])
        self.assertEqual(ev["start"], prior["start"])
        self.assertEqual(ev["reminderPolicy"], prior["reminderPolicy"])
        undo = [r for r in self.audit_records() if r["action"] == "cancel-undo"]
        self.assertEqual(len(undo), 1)
        self.assertEqual([r["kind"] for r in hist.events(uid) if r["kind"] == "cancel-undo"], ["cancel-undo"])

    def test_cross_day_undo_rejected(self):
        rc, _, err, uid = self.create_event()
        self.cli("cancel", "--uid", uid, "--now", "2026-11-28T10:05:00+08:00")
        rc, _, err = self.cli("cancel", "--uid", uid, "--undo", "--now", NEXT_DAY_NOW)
        self.assertEqual(rc, 4, err)
        self.assertEqual(events.get_event(self.load(), uid)["status"], "cancelled")

    def test_undo_without_trace_fails_cleanly(self):
        rc, _, err, uid = self.create_event()
        rc, _, err = self.cli("cancel", "--uid", uid, "--undo", "--now", CREATE_NOW)
        self.assertEqual(rc, 4, err)
        self.assertEqual(events.get_event(self.load(), uid)["status"], "scheduled")

    def test_cancel_not_found_exit_4(self):
        rc, _, err = self.cli("cancel", "--uid", "nobody@rtc-kit", "--now", CREATE_NOW)
        self.assertEqual(rc, 4, err)

    def test_cancel_terminal_state_rejected(self):
        rc, _, err, uid = self.create_event()
        self.cli("complete", "--uid", uid, "--now", CREATE_NOW)
        rc, _, err = self.cli("cancel", "--uid", uid, "--now", CREATE_NOW)
        self.assertEqual(rc, 4, err)

    def test_concurrent_edit_conflict_does_not_overwrite(self):
        rc, _, err, uid = self.create_event()
        self.assertEqual(rc, 0, err)
        original = events.get_event(self.load(), uid)

        real_digest = core.file_digest
        calls = {"n": 0}

        def fake_digest(p):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_digest(p)
            return "stale-digest-injected"

        core.file_digest = fake_digest
        self.addCleanup(setattr, core, "file_digest", real_digest)

        args = argparse.Namespace(
            uid=uid,
            alias=None,
            title="并发改名",
            start=None,
            end=None,
            rrule=None,
            owner=None,
            deadline=None,
            pre_remind=None,
            ddl_ladder=None,
            snooze=None,
            now=CREATE_NOW,
        )
        with self.assertRaises(core.Conflict):
            event_cmd.cmd_event_edit(args)
        disk = events.get_event(self.load(), uid)
        self.assertEqual(disk["title"], original["title"], "冲突写入不得覆盖磁盘内容")


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
        snap[name] = {"rc": _sha(str(rc)), "stdout": _sha(out), "stderr": _sha(err), "rc_value": str(rc)}
    return snap


class TestGoldenRegression(_WorkspaceCase):
    def test_old_commands_byte_identical_after_event_writes(self):
        self.write_routine()
        _run([sys.executable, str(RTC), "--workspace", str(self.ws), "plan", "--now", FIXTURE_NOW])

        before = _capture_five(self.ws)

        ws2 = Path(tempfile.mkdtemp(prefix="rtc-p5-golden-"))
        shutil.copytree(self.ws, ws2, dirs_exist_ok=True)
        try:
            rc, out, err = _run(
                [
                    sys.executable,
                    str(RTC),
                    "--workspace",
                    str(ws2),
                    "create",
                    "--title",
                    "事件命令",
                    "--start",
                    "2026-12-01T15:00:00+08:00",
                    "--end",
                    "2026-12-01T15:30:00+08:00",
                    "--now",
                    CREATE_NOW,
                ]
            )
            self.assertEqual(rc, 0, err or out)
            uid = events.all_events(events.load_events(ws2))[0]["uid"]
            _run(
                [
                    sys.executable,
                    str(RTC),
                    "--workspace",
                    str(ws2),
                    "edit",
                    "--uid",
                    uid,
                    "--title",
                    "改名",
                    "--now",
                    CREATE_NOW,
                ]
            )
            _run(
                [
                    sys.executable,
                    str(RTC),
                    "--workspace",
                    str(ws2),
                    "cancel",
                    "--uid",
                    uid,
                    "--now",
                    CREATE_NOW,
                ]
            )

            after = _capture_five(ws2)
            for name in before:
                with self.subTest(command=name):
                    self.assertEqual(before[name]["rc_value"], after[name]["rc_value"], "退出码变了")
                    self.assertEqual(before[name]["stdout"], after[name]["stdout"], "stdout 不一致")
                    self.assertEqual(before[name]["stderr"], after[name]["stderr"], "stderr 不一致")
        finally:
            shutil.rmtree(ws2, ignore_errors=True)


class TestHotPathGuard(unittest.TestCase):
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
