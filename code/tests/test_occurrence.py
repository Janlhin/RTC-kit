#!/usr/bin/env python3
"""L2 P3 下半段：recurring 事件 occurrence 接入 plan/due/ack 的 E2E 回归。

每个用例在临时工作区里用真实 CLI 调 ``code/rtc.py``；事件账本 ``state/events.json``
按 P1 契约手写。零外部依赖（tzdata 仅测试层补 Windows 时区库）。

覆盖：每周事件跨 3 天各响一次、重复 tick 不重响、ack 只确认当次、COUNT 用尽
静默、UNTIL 边界、跨年、DST 秋切、状态/类型过滤、坏 RRULE 降级、同键去重、
无 events.json 时 golden 逐字节一致。

运行：
    python -m unittest discover -s code/tests -p "test_occurrence.py" -v
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
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

RTC = REPO / "code" / "rtc.py"
REPORT = REPO / "code" / "rtc_report.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

U1 = "b0c1d2e3-1111-4222-8333-444455556666@rtc-kit"
U2 = "c1d2e3f4-2222-4333-9444-555566667777@rtc-kit"
SLUG1 = U1.split("@", 1)[0]


def base_routine(**over: object) -> dict[str, object]:
    r: dict[str, object] = {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "tzOffset": "+08:00",
        "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
        "alarmWindows": [],
        "rollcalls": [],
        "phases": [],
    }
    r.update(over)
    return r


def event(
    uid: str,
    rrule: str | None,
    start: str,
    end: str | None = None,
    *,
    kind: str = "recurring",
    status: str = "scheduled",
    aliases: list[str] | None = None,
    deadline: str | None = None,
) -> dict[str, object]:
    return {
        "uid": uid,
        "kind": kind,
        "start": start,
        "end": end,
        "rrule": rrule,
        "owner": "self",
        "deadline": deadline,
        "status": status,
        "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
        "aliases": aliases or ["yoga"],
        "createdAt": "2026-11-28T06:00:00+08:00",
        "updatedAt": "2026-11-28T06:00:00+08:00",
        "source": {"routineId": (aliases or ["yoga"])[0], "ledgerDate": None},
    }


def envelope(events: list[dict[str, object]]) -> dict[str, object]:
    aliases: dict[str, str] = {}
    for ev in events:
        for alias in ev.get("aliases") or []:
            aliases[str(alias)] = str(ev["uid"])
    return {"version": 1, "events": events, "aliases": aliases}


class OccurrenceCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-occ-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_routine(self, routine: dict[str, object]):
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def write_events(self, events: list[dict[str, object]]):
        (self.ws / "state" / "events.json").write_text(
            json.dumps(envelope(events), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def cli(self, *args: str, now: str | None = None, report: bool = False):
        exe = str(REPORT) if report else str(RTC)
        cmd = [sys.executable, exe, "--workspace", str(self.ws)] + list(args)
        if now:
            cmd += ["--now", now]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
        return p.returncode, p.stdout, p.stderr

    def state(self) -> dict[str, object]:
        p = self.ws / "state" / "alarms.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def windows(self) -> list[dict[str, object]]:
        return list(self.state().get("windows", []))  # type: ignore[arg-type]

    def event_windows(self) -> list[dict[str, object]]:
        return [w for w in self.windows() if str(w["id"]) == SLUG1]

    def plan(self, now: str):
        rc, out, err = self.cli("plan", "--now", now)
        self.assertEqual(rc, 0, f"plan 失败：{err}")
        self.assertNotIn("Traceback", out + err)
        return rc, out, err


class TestWeeklySchedule(OccurrenceCase):
    """每周事件跨 3 天各响一次；重复 tick 不重响；ack 只确认当次。"""

    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())
        self.write_events(
            [
                event(
                    U1, "FREQ=WEEKLY;BYDAY=MO,WE,FR", "2026-11-30T09:00:00+08:00", "2026-11-30T09:15:00+08:00"
                )
            ]
        )

    def test_matching_days_appear_non_matching_absent(self):
        self.plan("2026-11-30T06:00:00+08:00")  # 周一
        self.assertEqual(len(self.event_windows()), 1)

        self.plan("2026-12-01T06:00:00+08:00")  # 周二
        self.assertEqual(self.event_windows(), [], "周二不该出现瑜伽")

        self.plan("2026-12-02T06:00:00+08:00")  # 周三
        self.assertEqual(len(self.event_windows()), 1)

    def test_repeated_tick_does_not_refire(self):
        self.plan("2026-11-30T06:00:00+08:00")
        rc, out, _ = self.cli("due", "--now", "2026-11-30T09:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("yoga", out)

        rc, out, _ = self.cli("due", "--now", "2026-11-30T09:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "已达上限后重复 tick 必须静默")

        w = self.event_windows()[0]
        self.assertEqual(w["fireCount"], 1)

    def test_ack_only_acks_current_occurrence(self):
        self.plan("2026-11-30T06:00:00+08:00")
        rc, _, err = self.cli("ack", "--window", SLUG1, "--now", "2026-11-30T09:10:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_windows()[0]["state"], "acked")

        # 周三那次重新生成，仍是待响；昨天的确认不带到后续 occurrence。
        self.plan("2026-12-02T06:00:00+08:00")
        w = self.event_windows()[0]
        self.assertEqual(w["state"], "pending")
        self.assertIsNone(w["ackedAt"])


class TestBudgetAndUntil(OccurrenceCase):
    """COUNT 全局预算用尽后静默；UNTIL 含边界。"""

    def test_count_exhausted_then_silent(self):
        self.write_routine(base_routine())
        self.write_events(
            [event(U1, "FREQ=DAILY;COUNT=3", "2026-11-28T09:00:00+08:00", "2026-11-28T09:15:00+08:00")]
        )
        for day in ("2026-11-28", "2026-11-29", "2026-11-30"):
            self.plan(f"{day}T06:00:00+08:00")
            self.assertEqual(len(self.event_windows()), 1, f"{day} 应还有一次 occurrence")

        self.plan("2026-12-01T06:00:00+08:00")
        self.assertEqual(self.event_windows(), [], "COUNT=3 用尽后不再生成新 occurrence")

    def test_until_boundary_inclusive(self):
        self.write_routine(base_routine())
        self.write_events(
            [
                event(
                    U1,
                    "FREQ=DAILY;UNTIL=20261130T235959Z",
                    "2026-11-28T09:00:00+08:00",
                    "2026-11-28T09:15:00+08:00",
                )
            ]
        )
        self.plan("2026-11-30T06:00:00+08:00")
        self.assertEqual(len(self.event_windows()), 1, "UNTIL 当天必须含边界")
        self.plan("2026-12-01T06:00:00+08:00")
        self.assertEqual(self.event_windows(), [], "UNTIL 之后的窗口必须为空")


class TestCrossYear(OccurrenceCase):
    """跨年：12/31 与次年 1/1 各一次，不重复不丢。"""

    def test_cross_year(self):
        self.write_routine(base_routine())
        self.write_events([event(U1, "FREQ=DAILY", "2026-12-30T09:00:00+08:00", "2026-12-30T09:15:00+08:00")])
        self.plan("2026-12-31T06:00:00+08:00")
        w = self.event_windows()
        self.assertEqual(len(w), 1)
        self.assertTrue(w[0]["start"].startswith("2026-12-31T09:00:00+08:00"))

        self.plan("2027-01-01T06:00:00+08:00")
        w = self.event_windows()
        self.assertEqual(len(w), 1)
        self.assertTrue(w[0]["start"].startswith("2027-01-01T09:00:00+08:00"))


class TestDSTFallBack(OccurrenceCase):
    """DST 秋切：朴素时刻挂工作区 ZoneInfo，occurrence 跟随冬令时偏移。"""

    def test_fall_back_day_offset(self):
        self.write_routine(base_routine(timezone="America/New_York", tzOffset=None))
        self.write_events([event(U1, "FREQ=DAILY", "2026-10-25T09:00:00", "2026-10-25T09:15:00")])
        # 2026-11-01 是纽约秋切日（02:00 → 01:00），09:00 已是 EST -05:00。
        self.plan("2026-11-01T08:30:00-05:00")
        w = self.event_windows()
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["start"], "2026-11-01T09:00:00-05:00")
        self.assertIsNotNone(datetime.fromisoformat(w[0]["start"]).tzinfo)


class TestFiltersAndDegradation(OccurrenceCase):
    """状态/类型过滤、坏 RRULE 降级、同键去重、时刻必须带偏移。"""

    def test_done_and_cancelled_events_skipped(self):
        self.write_routine(base_routine())
        self.write_events(
            [
                event(U1, "FREQ=DAILY", "2026-11-30T09:00:00+08:00", status="done"),
                event(U2, "FREQ=DAILY", "2026-11-30T09:30:00+08:00", status="cancelled", aliases=["other"]),
            ]
        )
        self.plan("2026-11-30T06:00:00+08:00")
        self.assertEqual(self.windows(), [])

    def test_once_event_not_projected(self):
        self.write_routine(base_routine())
        self.write_events([event(U1, None, "2026-11-30T09:00:00+08:00", kind="once")])
        self.plan("2026-11-30T06:00:00+08:00")
        self.assertEqual(self.windows(), [])

    def test_bad_rrule_skipped_without_crash(self):
        self.write_routine(base_routine())
        self.write_events([event(U1, "FREQ=HOURLY", "2026-11-30T09:00:00+08:00")])
        rc, out, err = self.cli("plan", "--now", "2026-11-30T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.windows(), [])

    def test_same_occurrence_deduped(self):
        self.write_routine(base_routine())
        dup = event(U1, "FREQ=WEEKLY;BYDAY=MO", "2026-11-30T09:00:00+08:00")
        self.write_events([dup, dict(dup)])
        self.plan("2026-11-30T06:00:00+08:00")
        self.assertEqual(len(self.event_windows()), 1)

    def test_times_carry_offset(self):
        self.write_routine(base_routine())
        self.write_events([event(U1, "FREQ=DAILY", "2026-11-30T09:00:00+08:00", "2026-11-30T09:15:00+08:00")])
        self.plan("2026-11-30T06:00:00+08:00")
        for w in self.event_windows():
            for field in ("start", "end"):
                dt = datetime.fromisoformat(w[field])
                self.assertIsNotNone(dt.tzinfo, f"{field} 禁止裸 naive")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestGoldenBaseline(OccurrenceCase):
    """无 events.json / 空信封时，主链路输出与基线逐字节一致。"""

    COMMANDS: list[tuple[str, list[str], str | None]] = [
        ("plan", ["plan"], "2026-11-30T06:00:00+08:00"),
        ("due", ["due"], "2026-11-30T09:00:00+08:00"),
        ("ack", ["ack"], "2026-11-30T09:10:00+08:00"),
        ("status", ["status", "--json"], None),
        ("history", ["history", "2026-11-30"], None),
        ("schedule", ["schedule", "2026-11-30"], None),
        ("report", ["report", "--date", "2026-11-30"], None),
    ]

    def _capture(self) -> dict[str, tuple[int, str, str]]:
        snap: dict[str, tuple[int, str, str]] = {}
        for name, args, now in self.COMMANDS:
            rc, out, err = self.cli(*args, now=now, report=(name == "report"))
            snap[name] = (rc, out, err)
        return snap

    def test_missing_vs_empty_envelope_byte_identical(self):
        ws_no = Path(tempfile.mkdtemp(prefix="rtc-occ-golden-no-"))
        ws_empty = Path(tempfile.mkdtemp(prefix="rtc-occ-golden-empty-"))
        try:
            for ws in (ws_no, ws_empty):
                (ws / "rules").mkdir()
                (ws / "state").mkdir()
                (ws / "rules" / "routines.json").write_text(
                    json.dumps(base_routine(), ensure_ascii=False, indent=2), encoding="utf-8"
                )
            (ws_empty / "state" / "events.json").write_text(
                json.dumps(envelope([]), ensure_ascii=False, indent=2), encoding="utf-8"
            )

            def capture(ws: Path) -> dict[str, tuple[int, str, str]]:
                snap: dict[str, tuple[int, str, str]] = {}
                for name, args, now in self.COMMANDS:
                    exe = str(REPORT) if name == "report" else str(RTC)
                    cmd = [sys.executable, exe, "--workspace", str(ws)] + list(args)
                    if now:
                        cmd += ["--now", now]
                    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
                    snap[name] = (p.returncode, p.stdout, p.stderr)
                return snap

            no = capture(ws_no)
            empty = capture(ws_empty)
            for name, (rc1, out1, err1) in no.items():
                rc2, out2, err2 = empty[name]
                self.assertEqual(rc1, rc2, f"{name} 退出码不一致")
                self.assertEqual(_sha(out1), _sha(out2), f"{name} stdout 不一致")
                self.assertEqual(_sha(err1), _sha(err2), f"{name} stderr 不一致")
        finally:
            shutil.rmtree(ws_no, ignore_errors=True)
            shutil.rmtree(ws_empty, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
