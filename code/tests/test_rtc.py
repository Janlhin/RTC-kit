#!/usr/bin/env python3
"""RTC-kit 回归测试：进程级 E2E。

每个用例在临时工作区里用真实 CLI 调用 code/rtc.py，断言退出码、stdout
（投递内容）与状态文件。零外部依赖——只用标准库。

运行：
    python -m unittest discover -s code/tests -v
    python code/tests/test_rtc.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RTC = REPO / "code" / "rtc.py"
SCHEMA = REPO / "contract" / "routine.schema.json"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

try:
    import jsonschema  # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


def base_routine(**over) -> dict:
    r = {
        "version": 1, "timezone": "Asia/Shanghai", "tzOffset": "+08:00",
        "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
        "alarmWindows": [
            {"id": "wake", "label": "起床", "start": "07:00", "end": "07:25",
             "intervalMin": 5, "expectFires": 6, "ackHints": ["起了"]},
        ],
        "rollcalls": [], "phases": [],
    }
    r.update(over)
    return r


class RtcCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-test-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_routine(self, routine: dict):
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8")

    def cli(self, *args, now=None):
        cmd = [sys.executable, str(RTC), "--workspace", str(self.ws)] + list(args)
        if now:
            cmd += ["--now", now]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
        return p.returncode, p.stdout, p.stderr

    def state(self) -> dict:
        p = self.ws / "state" / "alarms.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def window(self, wid: str) -> dict:
        for w in self.state().get("windows", []):
            if w["id"] == wid:
                return w
        return {}


class TestOneOff(RtcCase):
    """一次性提醒（oneOff）"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "trip", "label": "深圳行程", "start": "09:00", "end": "09:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "validFrom": "2026-11-10", "validUntil": "2026-11-10",
        }]
        self.write_routine(r)

    def test_fires_once_then_delivered(self):
        rc, out, _ = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "plan 正常时必须静默")

        rc, out, _ = self.cli("due", now="2026-11-10T09:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("深圳行程", out, "一次性提醒必须产出投递文案")
        w = self.window("trip")
        self.assertEqual(w["state"], "delivered", "送达即完成，不需要等确认")
        self.assertEqual(w["fireCount"], 1)
        self.assertEqual(w["expectFires"], 1)

    def test_does_not_fire_twice(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-10T09:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "一次性提醒不能响第二次")

    def test_tolerates_missing_start_minute(self):
        """cron 恰好错过 start 那一分钟时，在 end 之前仍应送达。"""
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-10T09:03:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("深圳行程", out, "错过 start 那一分钟不应导致漏送")

    def test_gone_next_day(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        rc, out, _ = self.cli("plan", now="2026-11-11T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertNotIn("深圳行程", out, "有效期过后不应再有提醒文案")
        self.assertEqual(self.state().get("windows"), [], "有效期过后不再生成窗口")
        self.assertEqual(self.state().get("planDate"), "2026-11-11", "空日也要落盘 state")

    def test_ack_on_delivered_has_no_match(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        rc, _, _ = self.cli("ack", "--window", "trip", "--text", "好了",
                             now="2026-11-10T09:05:00+08:00")
        self.assertEqual(rc, 4, "已送达的一次性窗口没有可确认的匹配")


class TestValidity(RtcCase):
    """有效期（validFrom / validUntil）"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "season", "label": "版本限时任务", "start": "10:00", "end": "10:25",
            "intervalMin": 5, "expectFires": 6, "ackHints": ["好了"],
            "validFrom": "2026-11-10", "validUntil": "2026-11-12",
        }]
        self.write_routine(r)

    def test_generated_inside_range(self):
        for d in ("2026-11-10", "2026-11-11", "2026-11-12"):
            rc, _, _ = self.cli("plan", "--force", now=f"{d}T06:00:00+08:00")
            self.assertEqual(rc, 0, d)
            self.assertEqual(len(self.state()["windows"]), 1, d)

    def test_quiet_before_start(self):
        rc, out, err = self.cli("plan", now="2026-11-09T06:00:00+08:00")
        self.assertEqual(rc, 0, "有效期内没有窗口 = 合法空日，不该报配置错误")
        self.assertEqual(out, "")
        self.assertEqual(self.state().get("windows"), [])

    def test_quiet_after_end(self):
        rc, out, _ = self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.assertEqual(rc, 0, "过期后 = 合法空日")
        self.assertEqual(out, "")
        self.assertEqual(self.state().get("windows"), [])

    def test_empty_day_still_touches_heartbeat(self):
        self.cli("plan", now="2026-11-09T06:00:00+08:00")
        self.cli("due", now="2026-11-09T09:00:00+08:00")
        hb = self.ws / "state" / "heartbeat.json"
        self.assertTrue(hb.exists(), "空日也必须写心跳，否则次日自检会误报调度器停过")


class TestEmptyDaySemantics(RtcCase):
    """合法空日 vs 意外空配置"""

    def test_explicitly_disabled_day_is_quiet(self):
        r = base_routine(weekendAlarms=False)
        self.write_routine(r)
        rc, out, err = self.cli("plan", now="2026-11-14T06:00:00+08:00")  # 周六
        self.assertEqual(rc, 0, "显式禁用的日期类型 = 合法空日")
        self.assertEqual(out, "")
        self.assertEqual(self.state().get("planKind"), "weekend")
        self.assertEqual(self.state().get("windows"), [])

    def test_holiday_disabled_is_quiet(self):
        r = base_routine(holidayAlarms=False)
        r["dateTypes"]["holidayOverrides"] = [{"date": "2026-11-10", "type": "holiday"}]
        self.write_routine(r)
        rc, out, _ = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state().get("planKind"), "holiday")
        self.assertEqual(self.state().get("windows"), [])

    def test_unexpected_empty_is_configuration_error(self):
        r = base_routine()
        r["alarmWindows"][0]["dateTypes"] = ["weekend"]
        self.write_routine(r)
        rc, out, err = self.cli("plan", now="2026-11-10T06:00:00+08:00")  # 工作日
        self.assertEqual(rc, 3, "有窗口在有效期内却被 dateTypes 全排除 = 配置错误")
        self.assertEqual(out, "")
        self.assertIn("dateTypes", err)


class TestSchema(unittest.TestCase):
    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_oneoff_requires_expect_fires_one(self):
        import jsonschema
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        bad = base_routine()
        bad["alarmWindows"][0].update({"oneOff": True, "expectFires": 6})
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(bad))
        self.assertTrue(errs, "oneOff=true 但 expectFires≠1 必须被契约拒绝")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_new_fields_accepted(self):
        import jsonschema
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        good = base_routine()
        good["alarmWindows"][0].update({
            "oneOff": True, "expectFires": 1,
            "validFrom": "2026-11-10", "validUntil": "2026-11-10"})
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(good))
        self.assertEqual(errs, [], f"合法配置不该被拒：{errs[:1]}")


class TestMainlineRegression(RtcCase):
    """既有主线行为不得回退"""

    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())

    def test_plan_due_ack_cycle(self):
        rc, out, err = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("4 次", err) if "expectFires" in err else None

        rc, out, _ = self.cli("due", now="2026-11-10T07:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("起床", out)

        rc, out, err = self.cli("due", now="2026-11-10T07:02:00+08:00")
        self.assertEqual(out, "", "未到间隔必须静默")

        rc, out, _ = self.cli("due", now="2026-11-10T07:05:00+08:00")
        self.assertIn("第 2 次", out)

        rc, _, _ = self.cli("ack", "--window", "wake", "--text", "起了",
                             now="2026-11-10T07:06:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.window("wake")["state"], "acked")

        rc, out, _ = self.cli("due", now="2026-11-10T07:10:00+08:00")
        self.assertEqual(out, "", "已确认后不得再响")

    def test_plan_is_idempotent_same_day(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, err = self.cli("plan", now="2026-11-10T06:30:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("跳过", err)

    def test_missed_after_window(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        self.assertEqual(self.window("wake")["state"], "missed")

    def test_rollover_archives_history(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:00:00+08:00")
        self.cli("ack", "--window", "wake", "--text", "起了", now="2026-11-10T07:06:00+08:00")
        self.cli("plan", now="2026-11-11T06:00:00+08:00")
        hist = self.state().get("history", [])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["id"], "wake")
        self.assertEqual(hist[0]["state"], "acked")
        self.assertIsNotNone(hist[0]["ackedAt"], "归档要保留 ackedAt")


class TestScheduleArchive(RtcCase):
    """每日日程快照与历史查询"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["rollcalls"] = [{"hour": 20, "minute": 0,
                           "kindByDateType": {"workday": "summary", "holiday": "summary",
                                              "weekend": "summary"}}]
        r["weeklyChores"] = [{"dayOfWeek": 6, "label": "洗衣服", "attachToRollcallHour": 14}]
        self.write_routine(r)

    def snapshot(self, day: str) -> Path:
        return self.ws / "state" / "schedule" / f"{day}.json"

    def test_snapshot_written_into_workspace(self):
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        sp = self.snapshot("2026-12-15")
        self.assertTrue(sp.exists(), "plan 必须写每日快照")
        self.assertTrue(sp.is_relative_to(self.ws),
                        "快照必须落在 --workspace 指定的目录里，不能写到进程 cwd 下")
        snap = json.loads(sp.read_text(encoding="utf-8"))
        self.assertEqual(snap["date"], "2026-12-15")
        self.assertEqual(snap["planKind"], "workday")
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["windows"][0]["label"], "起床")
        self.assertEqual(snap["windows"][0]["expectFires"], 6)
        self.assertEqual(len(snap["rollcalls"]), 1)
        self.assertEqual(snap["rollcalls"][0]["kind"], "summary")

    def test_snapshot_records_weekly_chore(self):
        self.cli("plan", now="2026-12-19T06:00:00+08:00")   # 周六
        snap = json.loads(self.snapshot("2026-12-19").read_text(encoding="utf-8"))
        self.assertEqual(len(snap["weeklyChores"]), 1)
        self.assertEqual(snap["weeklyChores"][0]["label"], "洗衣服")

    def test_snapshot_records_one_off_and_validity(self):
        r = base_routine()
        r["alarmWindows"][0].update({"oneOff": True, "expectFires": 1,
                                      "validFrom": "2026-12-15", "validUntil": "2026-12-15"})
        self.write_routine(r)
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        w = json.loads(self.snapshot("2026-12-15").read_text(encoding="utf-8"))["windows"][0]
        self.assertTrue(w["oneOff"])
        self.assertEqual(w["validFrom"], "2026-12-15")
        self.assertEqual(w["validUntil"], "2026-12-15")

    def test_query_day_and_month(self):
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        self.cli("plan", now="2026-12-16T06:00:00+08:00")
        rc, out, _ = self.cli("schedule", "2026-12-15")
        self.assertEqual(rc, 0)
        self.assertIn("2026-12-15", out)
        self.assertIn("起床", out)
        rc, out, _ = self.cli("schedule", "--month", "2026-12")
        self.assertEqual(rc, 0)
        self.assertIn("2026-12-15", out)
        self.assertIn("2026-12-16", out)

    def test_query_missing_day(self):
        rc, _, err = self.cli("schedule", "2026-11-30")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_snapshots_survive_many_days(self):
        days = ["2026-12-14", "2026-12-15", "2026-12-16", "2027-01-11"]
        for d in days:
            self.cli("plan", now=f"{d}T06:00:00+08:00")
        for d in days:
            self.assertTrue(self.snapshot(d).exists(), f"{d} 的快照必须永久保留")

    def test_metrics_indexes_schedule_table(self):
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        rc, _, err = self.cli("metrics", "sync")
        self.assertEqual(rc, 0)
        rc, out, _ = self.cli("metrics", "schedule", "--date", "2026-12-15")
        self.assertEqual(rc, 0)
        self.assertIn("起床", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
