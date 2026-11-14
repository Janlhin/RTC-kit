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
            "id": "travel", "label": "旅游", "start": "09:00", "end": "09:15",
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
        self.assertIn("旅游", out, "一次性提醒必须产出投递文案")
        w = self.window("travel")
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
        self.assertIn("旅游", out, "错过 start 那一分钟不应导致漏送")

    def test_gone_next_day(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        rc, out, _ = self.cli("plan", now="2026-11-11T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertNotIn("旅游", out, "有效期过后不应再有提醒文案")
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

    def test_query_without_snapshot_falls_back_to_preview(self):
        """没有快照的日期按配置推算，而不是报「没有」。"""
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        rc, out, _ = self.cli("schedule", "2026-12-20")
        self.assertEqual(rc, 0, "没有快照时应推算，而不是报错")
        self.assertIn("按配置推算", out)

    def test_query_disabled_day_has_no_preview(self):
        """被显式禁用的日期类型推算不出安排 → 退出码 4。"""
        self.write_routine(base_routine(weekendAlarms=False))
        rc, _, err = self.cli("schedule", "2026-11-14")   # 周六
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_snapshots_survive_many_days(self):
        days = ["2026-12-14", "2026-12-15", "2026-12-16", "2027-01-11"]
        for d in days:
            self.cli("plan", now=f"{d}T06:00:00+08:00")
        for d in days:
            self.assertTrue(self.snapshot(d).exists(), f"{d} 的快照必须永久保留")

    def test_future_date_is_previewed_from_config(self):
        """未来日期没有快照时，按配置推算——能看见自己预约了什么。"""
        r = base_routine()
        r["alarmWindows"].append({
            "id": "travel", "label": "旅游", "start": "09:00", "end": "23:45",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "validFrom": "2028-01-01", "validUntil": "2028-01-01"})
        self.write_routine(r)
        rc, out, _ = self.cli("schedule", "2028-01-01")
        self.assertEqual(rc, 0, "未来日期应能推算出安排")
        self.assertIn("旅游", out)
        self.assertIn("按配置推算", out, "推算结果必须标明来源，不与快照混淆")
        rc, out, _ = self.cli("schedule", "2027-06-15")
        self.assertEqual(rc, 0)
        self.assertNotIn("旅游", out, "不在有效期内，不该出现该窗口")

    def test_wide_window_oneoff_still_delivers_late_in_day(self):
        """宽容错窗口时，当天晚些开机也能收到。"""
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "travel", "label": "旅游", "start": "09:00", "end": "23:45",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "validFrom": "2026-12-15", "validUntil": "2026-12-15"}]
        self.write_routine(r)
        self.cli("plan", now="2026-12-15T09:30:00+08:00")
        rc, out, _ = self.cli("due", now="2026-12-15T09:31:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("旅游", out, "宽容错窗口时，晚开机也应当天收到")

    def test_metrics_indexes_schedule_table(self):
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        rc, _, err = self.cli("metrics", "sync")
        self.assertEqual(rc, 0)
        rc, out, _ = self.cli("metrics", "schedule", "--date", "2026-12-15")
        self.assertEqual(rc, 0)
        self.assertIn("起床", out)


class TestCountdown(RtcCase):
    """倒计时提醒（dueDate）：剩余天数由代码算，不由模型判断"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "zzz", "label": "限时活动", "start": "20:00", "end": "20:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "dueDate": "2026-05-10",
            "validFrom": "2026-05-08", "validUntil": "2026-05-10"}]
        self.write_routine(r)

    def test_counts_down_in_text(self):
        self.cli("plan", now="2026-05-08T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-08T20:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("限时活动", out)
        self.assertIn("距截止还有 2 天", out)

    def test_tomorrow_and_last_day(self):
        self.cli("plan", now="2026-05-09T06:00:00+08:00")
        _, out, _ = self.cli("due", now="2026-05-09T20:00:00+08:00")
        self.assertIn("明天截止", out)
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        _, out, _ = self.cli("due", now="2026-05-10T20:00:00+08:00")
        self.assertIn("最后一天", out)

    def test_fires_once_per_day(self):
        self.cli("plan", now="2026-05-08T06:00:00+08:00")
        self.cli("due", now="2026-05-08T20:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-08T20:05:00+08:00")
        self.assertEqual(out, "", "倒计时窗口每天只提醒一次")

    def test_reminds_again_next_day_after_ack(self):
        """确认过这次提醒 ≠ 事情做完了：次日仍要提醒。"""
        self.cli("plan", now="2026-05-08T06:00:00+08:00")
        self.cli("due", now="2026-05-08T20:00:00+08:00")
        self.cli("ack", "--window", "zzz", "--text", "好了", now="2026-05-08T20:06:00+08:00")
        self.cli("plan", now="2026-05-09T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-09T20:00:00+08:00")
        self.assertIn("限时活动", out, "倒计时任务次日仍应提醒")

    def test_stops_after_valid_until(self):
        self.cli("plan", now="2026-05-11T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [], "有效期过后不再生成窗口")
        rc, out, _ = self.cli("due", now="2026-05-11T20:00:00+08:00")
        self.assertEqual(out, "")

    def test_snapshot_records_due_date(self):
        self.cli("plan", now="2026-05-08T06:00:00+08:00")
        snap = json.loads((self.ws / "state" / "schedule" / "2026-05-08.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(snap["windows"][0]["dueDate"], "2026-05-10")


class TestChecklist(RtcCase):
    """带清单的提醒：逐项打勾"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "pack", "label": "清点并带走", "start": "20:00", "end": "20:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "checklist": ["电饭煲", "电脑", "被子枕头", "牙膏"]}]
        self.write_routine(r)

    def test_due_lists_all_items(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-12T20:00:00+08:00")
        self.assertIn("清点并带走", out)
        self.assertIn("还剩 4 项", out)
        for label in ["电饭煲", "电脑", "被子枕头", "牙膏"]:
            self.assertIn(label, out, "提醒里必须列出全部清单项")

    def test_check_by_label_or_index(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        rc, out, err = self.cli("check", "--window", "pack", "--item", "电饭煲",
                                 now="2026-05-12T20:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "check 的 stdout 必须为空——它由用户交互触发，不该被投递")
        rc, _, _ = self.cli("check", "--window", "pack", "--item", "2",
                             now="2026-05-12T20:06:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state()["windows"][0]["checked"], ["1", "2"])

    def test_checked_survives_next_day(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        self.cli("due", now="2026-05-12T20:00:00+08:00")
        self.cli("check", "--window", "pack", "--item", "电饭煲", now="2026-05-12T20:05:00+08:00")
        self.cli("plan", now="2026-05-13T06:00:00+08:00")
        w = self.state()["windows"][0]
        self.assertEqual(w["checked"], ["1"], "清单不因日切重置")
        rc, out, _ = self.cli("due", now="2026-05-13T20:00:00+08:00")
        self.assertIn("还剩 3 项", out)
        self.assertIn("☑ 电饭煲", out, "已勾的项要显示 ☑")

    def test_all_checked_completes_window(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        for i in range(1, 5):
            self.cli("check", "--window", "pack", "--item", str(i),
                     now="2026-05-12T20:05:00+08:00")
        w = self.state()["windows"][0]
        self.assertEqual(w["state"], "acked", "全部打完即视为完成")
        self.assertEqual(len(w["checked"]), 4)

    def test_completed_list_stops_reminding(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        for i in range(1, 5):
            self.cli("check", "--window", "pack", "--item", str(i),
                     now="2026-05-12T20:05:00+08:00")
        self.cli("plan", now="2026-05-13T06:00:00+08:00")
        self.assertEqual(self.state()["windows"][0]["state"], "acked")
        rc, out, _ = self.cli("due", now="2026-05-13T20:00:00+08:00")
        self.assertEqual(out, "", "清单打完就不再提醒")

    def test_unknown_item(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        rc, _, err = self.cli("check", "--window", "pack", "--item", "不存在的东西",
                               now="2026-05-12T20:05:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_undo(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        self.cli("check", "--window", "pack", "--item", "电饭煲", now="2026-05-12T20:05:00+08:00")
        rc, _, _ = self.cli("check", "--window", "pack", "--item", "电饭煲", "--undo",
                             now="2026-05-12T20:06:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state()["windows"][0]["checked"], [])


class TestPostpone(RtcCase):
    """推迟：今天不响，明天同一时间自动重新创建并提醒"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "thermos", "label": "洗保温杯", "start": "20:00", "end": "20:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "validFrom": "2026-05-10", "validUntil": "2026-05-10"}]
        self.write_routine(r)

    def test_today_is_skipped_and_tomorrow_fires_at_same_time(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, out, err = self.cli("postpone", "--window", "thermos",
                                 now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "postpone 由用户触发，stdout 不该被投递出去")

        rc, out, _ = self.cli("due", now="2026-05-10T20:00:00+08:00")
        self.assertEqual(out, "", "推迟后今天不该再响")

        self.cli("plan", now="2026-05-11T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-11T20:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("洗保温杯", out, "明天同一时间应自动创建并提醒")
        self.assertIn("20:00", out, "时刻不变")

    def test_state_records_postponed_reason(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "thermos", now="2026-05-10T08:00:00+08:00")
        w = self.window("thermos")
        self.assertEqual(w["state"], "skipped")
        self.assertEqual(w["missedReason"], "postponed")

    def test_history_keeps_postponed_reason(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "thermos", now="2026-05-10T08:00:00+08:00")
        self.cli("plan", now="2026-05-11T06:00:00+08:00")
        hist = self.state().get("history", [])
        entry = next((h for h in hist if h["id"] == "thermos"), None)
        self.assertIsNotNone(entry, "推迟的窗口也要进归档")
        self.assertEqual(entry["state"], "skipped")
        self.assertEqual(entry["missedReason"], "postponed",
                         "归档要保留推迟原因，不能丢")

    def test_does_not_repeat_next_day(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "thermos", now="2026-05-10T08:00:00+08:00")
        self.cli("plan", now="2026-05-11T06:00:00+08:00")
        self.cli("due", now="2026-05-11T20:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-11T20:05:00+08:00")
        self.assertEqual(out, "", "一次性提醒推迟后仍只响一次")

    def test_disappears_after_target_day(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "thermos", now="2026-05-10T08:00:00+08:00")
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [], "目标日过后不再生成窗口")

    def test_days_moves_further(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, _, err = self.cli("postpone", "--window", "thermos", "--days", "3",
                               now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 0)
        routine = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        w = next(x for x in routine["alarmWindows"] if x["id"] == "thermos")
        self.assertEqual(w["validFrom"], "2026-05-13")
        self.assertEqual(w["validUntil"], "2026-05-13")

    def test_unknown_window(self):
        rc, _, err = self.cli("postpone", "--window", "nope",
                               now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_target_not_later_than_today(self):
        for target in ("2026-05-09", "2026-05-10"):
            rc, _, err = self.cli("postpone", "--window", "thermos", "--to", target,
                                   now="2026-05-10T08:00:00+08:00")
            self.assertEqual(rc, 3, target)
            self.assertIn("不晚于今天", err)

    def test_bad_date_format(self):
        rc, _, err = self.cli("postpone", "--window", "thermos", "--to", "05-11",
                               now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 3)
        self.assertIn("YYYY-MM-DD", err)

    def test_target_conflicting_with_date_types(self):
        """推迟到该窗口不生效的日期类型 → 拒绝，而不是明天静默不响。"""
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "thermos", "label": "洗保温杯", "start": "20:00", "end": "20:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "dateTypes": ["workday"],
            "validFrom": "2026-05-10", "validUntil": "2026-05-10"}]
        self.write_routine(r)
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        # 2026-05-16 是周六，dateTypes=["workday"] 不生效
        rc, _, err = self.cli("postpone", "--window", "thermos", "--to", "2026-05-16",
                               now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 3)
        self.assertIn("dateTypes", err)


class TestPostponePeriodic(RtcCase):
    """周期窗口的推迟：跳过某一天，之后照常"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "wake", "label": "起床", "start": "07:00", "end": "07:25",
            "intervalMin": 5, "expectFires": 6, "ackHints": ["起了"]}]
        self.write_routine(r)

    def test_skips_today_only(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, _, err = self.cli("postpone", "--window", "wake",
                               now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("跳过", err)

        rc, out, _ = self.cli("due", now="2026-05-10T07:00:00+08:00")
        self.assertEqual(out, "", "今天应被跳过")

        self.cli("plan", now="2026-05-11T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-11T07:00:00+08:00")
        self.assertIn("起床", out, "明天应照常响")

    def test_skip_dates_recorded(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "wake", now="2026-05-10T08:00:00+08:00")
        routine = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        w = next(x for x in routine["alarmWindows"] if x["id"] == "wake")
        self.assertEqual(w.get("skipDates"), ["2026-05-10"])

    def test_skipped_day_does_not_trigger_self_check(self):
        """被推迟的窗口不算「整段没响」，不该触发次日告警。"""
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "wake", now="2026-05-10T08:00:00+08:00")
        rc, out, _ = self.cli("plan", now="2026-05-11T06:00:00+08:00")
        self.assertNotIn("一次都没响过", out, "推迟不等于漏响，不该告警")


class TestPostponeCountdown(RtcCase):
    """倒计时窗口的推迟：截止日平移，天数由代码重算"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [{
            "id": "zzz", "label": "限时活动", "start": "20:00", "end": "20:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "dueDate": "2026-05-12",
            "validFrom": "2026-05-10", "validUntil": "2026-05-12"}]
        self.write_routine(r)

    def test_deadline_moves_and_countdown_recomputes(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, _, err = self.cli("postpone", "--window", "zzz", "--to", "2026-05-13",
                               now="2026-05-10T21:00:00+08:00")
        self.assertEqual(rc, 0)

        for day, expect in (("2026-05-11", "距截止还有 2 天"),
                            ("2026-05-12", "明天截止"),
                            ("2026-05-13", "最后一天")):
            self.cli("plan", now=f"{day}T06:00:00+08:00")
            _, out, _ = self.cli("due", now=f"{day}T20:00:00+08:00")
            self.assertIn(expect, out, day)

    def test_stops_after_new_deadline(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "zzz", "--to", "2026-05-13",
                 now="2026-05-10T21:00:00+08:00")
        self.cli("plan", now="2026-05-14T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-14T20:00:00+08:00")
        self.assertEqual(out, "")


class TestCancelCompleteDisable(RtcCase):
    """取消 / 完成 / 永不提醒"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {"id": "meeting", "label": "临时会议", "start": "11:00", "end": "11:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "tea", "label": "喝茶", "start": "15:00", "end": "15:25",
             "intervalMin": 5, "expectFires": 6, "ackHints": ["好了"]},
        ]
        self.write_routine(r)

    def routine(self) -> dict:
        return json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))

    def window_def(self, wid):
        return next(w for w in self.routine()["alarmWindows"] if w["id"] == wid)

    # ---------- cancel ----------

    def test_cancel_one_off_stops_it_forever(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("cancel", "--window", "meeting",
                                 now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "cancel 由用户触发，stdout 不该被投递出去")

        rc, out, _ = self.cli("due", now="2026-11-13T11:00:00+08:00")
        self.assertEqual(out, "", "取消后今天不该响")

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn("meeting", [w["id"] for w in self.state().get("windows", [])],
                         "一次性窗口取消后不该再生成")

    def test_cancel_marks_state_cancelled(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("cancel", "--window", "meeting", now="2026-11-13T08:00:00+08:00")
        w = self.window("meeting")
        self.assertEqual(w["state"], "cancelled")
        self.assertEqual(w["missedReason"], "cancelled")
        self.assertIsNotNone(w.get("cancelledAt"))

    def test_cancel_keeps_reason_in_history(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("cancel", "--window", "meeting", now="2026-11-13T08:00:00+08:00")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        hist = self.state().get("history", [])
        entry = next((h for h in hist if h["id"] == "meeting"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["state"], "cancelled")
        self.assertEqual(entry["missedReason"], "cancelled",
                         "取消的原因要进归档，不能丢")

    def test_cancel_recurring_only_today(self):
        """周期窗口没有「这件事」可取消，只取消今天这一次。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "tea", now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("明天照常", err, "周期窗口的取消语义要说清")

        rc, out, _ = self.cli("due", now="2026-11-13T15:00:00+08:00")
        self.assertEqual(out, "", "今天不该响")

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-14T15:00:00+08:00")
        self.assertIn("喝茶", out, "明天照常响")

    def test_cancel_future_date(self):
        """取消还没到的一次性安排。"""
        r = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        r["alarmWindows"].append({
            "id": "future", "label": "季度汇报", "start": "09:00", "end": "09:15",
            "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
            "oneOff": True, "validFrom": "2027-03-01", "validUntil": "2027-03-01"})
        self.write_routine(r)
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "future", "--date", "2027-03-01",
                               now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertTrue(self.window_def("future").get("disabled"),
                        "取消未来那次 = 那个窗口不再生成")

    def test_cancel_date_window_does_not_exist(self):
        """那天根本没有这个窗口 → 退 4 并说明，不静默无效。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "meeting", "--date", "2026-11-20",
                               now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("不存在", err)
        self.assertFalse(self.window_def("meeting").get("disabled"),
                         "取消失败就不该改动配置")

    def test_cancel_unknown_window(self):
        rc, _, err = self.cli("cancel", "--window", "nope", now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_cancel_twice(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("cancel", "--window", "meeting", now="2026-11-13T08:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "meeting", now="2026-11-13T08:05:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("已经", err)

    # ---------- complete ----------

    def test_complete_acknowledges_and_disables(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("complete", "--window", "tea", "--text", "喝完了",
                                 now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        w = self.window("tea")
        self.assertEqual(w["state"], "acked")
        self.assertEqual(w["ackText"], "喝完了")
        self.assertTrue(self.window_def("tea").get("disabled"),
                        "完成 = 以后不再提醒")

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn("tea", [w["id"] for w in self.state().get("windows", [])])

    def test_complete_vs_ack_difference(self):
        """complete 与 ack 的区别：ack 只停今天，complete 连以后也停。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("ack", "--window", "tea", "--text", "好了", now="2026-11-13T08:00:00+08:00")
        self.assertFalse(self.window_def("tea").get("disabled"),
                         "ack 不该停用窗口")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertIn("tea", [w["id"] for w in self.state().get("windows", [])],
                      "ack 之后明天照常响")

    # ---------- disable / enable ----------

    def test_disable_stops_forever(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("disable", "--window", "tea",
                                 now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertTrue(self.window_def("tea").get("disabled"))

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn("tea", [w["id"] for w in self.state().get("windows", [])])
        rc, out, _ = self.cli("due", now="2026-11-14T15:00:00+08:00")
        self.assertEqual(out, "")

    def test_disable_marks_today_occurrence_cancelled(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("disable", "--window", "tea", now="2026-11-13T08:00:00+08:00")
        w = self.window("tea")
        self.assertEqual(w["state"], "cancelled", "停用时今天这次也不该再响")
        rc, out, _ = self.cli("due", now="2026-11-13T15:00:00+08:00")
        self.assertEqual(out, "")

    def test_enable_restores(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("disable", "--window", "tea", now="2026-11-13T08:00:00+08:00")
        rc, _, err = self.cli("enable", "--window", "tea")
        self.assertEqual(rc, 0)
        self.assertFalse(self.window_def("tea").get("disabled"))
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertIn("tea", [w["id"] for w in self.state().get("windows", [])])

    def test_enable_already_enabled(self):
        rc, _, err = self.cli("enable", "--window", "tea")
        self.assertEqual(rc, 4)
        self.assertIn("本来就是", err)

    def test_all_disabled_is_legal_empty_day(self):
        """全部窗口被停用 = 合法空日，不是配置错误。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("disable", "--window", "tea", now="2026-11-13T08:00:00+08:00")
        self.cli("disable", "--window", "meeting", now="2026-11-13T08:00:00+08:00")
        rc, out, err = self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertEqual(rc, 0, "全部停用不该报配置错误")
        self.assertEqual(self.state().get("windows"), [])

    def test_disabled_window_not_counted_in_plan(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("disable", "--window", "tea", now="2026-11-13T08:00:00+08:00")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        ids = [w["id"] for w in self.state().get("windows", [])]
        self.assertNotIn("tea", ids)


class TestSevenOpsSameDay(RtcCase):
    """七种操作挤在同一天：创建一次性 / 取消 / 推迟 / 倒计时 / 预约 / 完成 / 永不提醒"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {"id": "travel", "label": "旅游", "start": "09:00", "end": "09:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "cancel-me", "label": "临时会议", "start": "11:00", "end": "11:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "postpone-me", "label": "取件", "start": "14:00", "end": "14:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "zzz", "label": "限时活动", "start": "20:00", "end": "20:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "dueDate": "2026-11-15",
             "validFrom": "2026-11-13", "validUntil": "2026-11-15"},
            {"id": "future", "label": "季度汇报", "start": "09:00", "end": "09:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2027-03-01", "validUntil": "2027-03-01"},
            {"id": "wake", "label": "起床", "start": "07:00", "end": "07:25",
             "intervalMin": 5, "expectFires": 6, "ackHints": ["起了"]},
            {"id": "tea", "label": "喝茶", "start": "15:00", "end": "15:25",
             "intervalMin": 5, "expectFires": 6, "ackHints": ["好了"]},
        ]
        self.write_routine(r)

    def test_all_seven_ops_in_one_day(self):
        T = "2026-11-13"
        self.cli("plan", now=f"{T}T06:00:00+08:00")

        # ⑥ 完成
        self.cli("complete", "--window", "wake", "--text", "起了", now=f"{T}T07:05:00+08:00")
        # ② 取消
        self.cli("cancel", "--window", "cancel-me", now=f"{T}T08:10:00+08:00")
        # ③ 推迟
        self.cli("postpone", "--window", "postpone-me", "--to", "2026-11-14",
                 now=f"{T}T08:20:00+08:00")
        # ⑦ 永不提醒
        self.cli("disable", "--window", "tea", now=f"{T}T08:50:00+08:00")
        # ⑤ 取消未来的预约
        self.cli("cancel", "--window", "future", "--date", "2027-03-01",
                 now=f"{T}T08:55:00+08:00")

        # 当天：只有旅游（09:00）和倒计时（20:00）该响
        expected = {
            "07:00": "",        # wake 已完成
            "09:00": "旅游",     # travel 一次性
            "11:00": "",       # cancel-me 已取消
            "14:00": "",       # postpone-me 已推迟
            "15:00": "",       # tea 已停用
            "20:00": "限时活动",  # zzz 倒计时
        }
        for t, want in expected.items():
            rc, out, _ = self.cli("due", now=f"{T}T{t}:00+08:00")
            if want:
                self.assertIn(want, out, t)
            else:
                self.assertEqual(out, "", f"{t} 应静默")

        # 次日：只有取件（推迟过来的）与倒计时
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        ids = [w["id"] for w in self.state().get("windows", [])]
        self.assertEqual(sorted(ids), ["postpone-me", "zzz"])
        rc, out, _ = self.cli("due", now="2026-11-14T14:00:00+08:00")
        self.assertIn("取件", out, "推迟的那次在明天同一时间响")
        rc, out, _ = self.cli("due", now="2026-11-14T20:00:00+08:00")
        self.assertIn("明天截止", out)

        # 倒计时走到最后一天
        self.cli("plan", now="2026-11-15T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-15T20:00:00+08:00")
        self.assertIn("最后一天", out)
        # 之后彻底消失
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [])

    def test_seven_ops_keep_state_contract_valid(self):
        """七操作之后，状态文件仍要符合契约（additionalProperties: false）。"""
        import jsonschema
        schema = json.loads(
            (REPO / "contract" / "alarm-state.schema.json").read_text(encoding="utf-8"))
        T = "2026-11-13"
        self.cli("plan", now=f"{T}T06:00:00+08:00")
        self.cli("complete", "--window", "wake", now=f"{T}T07:05:00+08:00")
        self.cli("cancel", "--window", "cancel-me", now=f"{T}T08:10:00+08:00")
        self.cli("postpone", "--window", "postpone-me", "--to", "2026-11-14",
                 now=f"{T}T08:20:00+08:00")
        self.cli("disable", "--window", "tea", now=f"{T}T08:50:00+08:00")
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(self.state()))
        self.assertEqual(errs, [], f"状态文件违反契约：{errs[:1]}")

    def test_cancelled_does_not_trigger_self_check_alarm(self):
        """被取消的窗口不算「整段没响」；确实没响过的窗口仍要报。"""
        T = "2026-11-13"
        self.cli("plan", now=f"{T}T06:00:00+08:00")
        self.cli("cancel", "--window", "cancel-me", now=f"{T}T08:10:00+08:00")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        rc, out, _ = self.cli("plan", "--force", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn("cancel-me", out,
                         "取消 ≠ 漏响，不该触发「整段没响」告警")
        self.assertIn("travel", out,
                      "确实整段没响过的窗口仍要报出来——否则真漏了就没人知道")


class TestExecutionArchive(RtcCase):
    """推迟 / 取消 / 未完成 的记录留档——一年后查得到"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {"id": "postponed", "label": "取件", "start": "14:00", "end": "14:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "cancelled", "label": "临时会议", "start": "11:00", "end": "11:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "unfinished", "label": "没做完的事", "start": "16:00", "end": "16:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
            {"id": "ok", "label": "正常响的", "start": "09:00", "end": "09:15",
             "intervalMin": 5, "expectFires": 1, "ackHints": ["好了"],
             "oneOff": True, "validFrom": "2026-11-13", "validUntil": "2026-11-13"},
        ]
        self.write_routine(r)

    def snapshot(self, day: str) -> dict:
        p = self.ws / "state" / "schedule" / f"{day}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def make_day(self):
        """跑出四种结局：推迟 / 取消 / 未完成 / 正常。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("postpone", "--window", "postponed", "--to", "2026-11-14",
                 now="2026-11-13T08:00:00+08:00")
        self.cli("cancel", "--window", "cancelled", now="2026-11-13T08:10:00+08:00")
        self.cli("due", now="2026-11-13T09:00:00+08:00")
        self.cli("due", now="2026-11-13T20:00:00+08:00")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")

    def test_snapshot_records_execution_results(self):
        """快照要有 results 层：那天实际怎么了。"""
        self.make_day()
        snap = self.snapshot("2026-11-13")
        res = snap.get("results")
        self.assertIsNotNone(res, "快照必须记录执行结果")
        self.assertEqual(sorted(res.keys()), ["cancelled", "ok", "postponed", "unfinished"])
        self.assertEqual(res["postponed"]["state"], "skipped")
        self.assertEqual(res["postponed"]["missedReason"], "postponed")
        self.assertEqual(res["cancelled"]["state"], "cancelled")
        self.assertEqual(res["cancelled"]["missedReason"], "cancelled")
        self.assertEqual(res["unfinished"]["state"], "missed")
        self.assertEqual(res["unfinished"]["missedReason"], "window_expired")
        self.assertEqual(res["ok"]["state"], "delivered")
        self.assertEqual(res["ok"]["fires"], 1)

    def test_snapshot_result_has_label(self):
        self.make_day()
        res = self.snapshot("2026-11-13")["results"]
        self.assertEqual(res["postponed"]["label"], "取件")
        self.assertEqual(res["cancelled"]["label"], "临时会议")

    def test_history_command_lists_outcomes(self):
        self.make_day()
        rc, out, _ = self.cli("history", "2026-11-13")
        self.assertEqual(rc, 0)
        self.assertIn("推迟到别处", out)
        self.assertIn("被取消", out)
        self.assertIn("窗口过期", out)
        self.assertIn("已送达", out)

    def test_history_month_summary(self):
        self.make_day()
        rc, out, _ = self.cli("history", "--month", "2026-11")
        self.assertEqual(rc, 0)
        self.assertIn("2026-11-13", out)

    def test_schedule_shows_results_inline(self):
        """schedule 查那天，窗口行后面要带结果。"""
        self.make_day()
        rc, out, _ = self.cli("schedule", "2026-11-13")
        self.assertEqual(rc, 0)
        self.assertIn("已取消", out)
        self.assertIn("漏响", out)
        self.assertIn("推迟到别处", out, "被推迟走的窗口也要出现在结果里")

    def test_history_for_day_without_results(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("history", "2026-11-13")
        self.assertEqual(rc, 0)
        self.assertIn("还没有归档", out + err)

    def test_history_missing_snapshot(self):
        rc, _, err = self.cli("history", "2026-11-30")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_results_survive_beyond_history_window(self):
        """快照是永久的，不受 14 天截断影响。"""
        self.make_day()
        snap = self.snapshot("2026-11-13")
        self.assertIn("results", snap)
        # state 的 history 只有 14 天，但快照一直在
        self.assertTrue((self.ws / "state" / "schedule" / "2026-11-13.json").exists())

    def test_metrics_label_and_reason_are_real(self):
        """metrics.daily 的 label 要是真实名字，missed 要是真实原因。"""
        self.make_day()
        self.cli("metrics", "sync")
        import sqlite3
        con = sqlite3.connect(self.ws / "state" / "metrics.db")
        rows = dict(con.execute(
            "SELECT window, label FROM daily WHERE date='2026-11-13'").fetchall())
        reasons = dict(con.execute(
            "SELECT window, missed FROM daily WHERE date='2026-11-13'").fetchall())
        con.close()
        self.assertEqual(rows.get("cancelled"), "临时会议", "label 不该是 id")
        self.assertEqual(rows.get("unfinished"), "没做完的事")
        self.assertEqual(reasons.get("postponed"), "postponed", "原因要真实")
        self.assertEqual(reasons.get("cancelled"), "cancelled")
        self.assertEqual(reasons.get("unfinished"), "window_expired")

    def test_metrics_schedule_indexes_postponed_entry(self):
        """被推迟走的窗口也要进索引，否则查历史时凭空消失。"""
        self.make_day()
        self.cli("metrics", "sync")
        rc, out, _ = self.cli("metrics", "schedule", "--date", "2026-11-13")
        self.assertEqual(rc, 0)
        self.assertIn("postponed", out)
        self.assertIn("已移出计划", out)

    def test_results_backfilled_on_late_plan(self):
        """机器关机跨过日切，次日才开机——补跑 plan 也要把结果回填上。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("cancel", "--window", "cancelled", now="2026-11-13T08:10:00+08:00")
        # 直接跳到 11-15，跨了两天
        self.cli("plan", now="2026-11-15T06:00:00+08:00")
        snap = self.snapshot("2026-11-13")
        res = snap.get("results") or {}
        self.assertIn("cancelled", res, "跨多天开机也要回填前一天的快照")
        self.assertEqual(res["cancelled"]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
