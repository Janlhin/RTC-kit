#!/usr/bin/env python3
"""RTC-kit 回归测试：进程级 E2E。

每个用例在临时工作区里用真实 CLI 调用 code/rtc.py，断言退出码、stdout
（投递内容）与状态文件。零外部依赖——只用标准库。

运行：
    python -m unittest discover -s code/tests -v
    python code/tests/test_rtc.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc  # noqa: E402  (回滚竞态测试直接调 cmd_postpone)

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
        ],
        "rollcalls": [],
        "phases": [],
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
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )

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
        r["alarmWindows"] = [
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-10",
                "validUntil": "2026-11-10",
            }
        ]
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

    def test_oneoff_message_has_no_ack_hint(self):
        """一次性提醒送达即完成，还教「回复就不响了」等于告诉用户一件不存在的事。"""
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-10T09:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.window("travel")["state"], "delivered")
        self.assertNotIn("就不响了", out, f"一次性提醒不该带确认提示：{out!r}")

    def test_repeating_message_hints_only_first_and_last(self):
        """会反复响的窗口：只在第 1 次和最后一次提怎么停，中途别每句都念。"""
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "meds",
                "label": "吃药",
                "start": "09:00",
                "end": "09:20",
                "intervalMin": 5,
                "expectFires": 5,
                "ackHints": ["吃了"],
            }
        ]
        self.write_routine(r)
        self.cli("plan", now="2026-11-10T06:00:00+08:00")

        outs = []
        for hhmm in ("09:00", "09:05", "09:10", "09:15", "09:20"):
            rc, out, _ = self.cli("due", now=f"2026-11-10T{hhmm}:00+08:00")
            self.assertEqual(rc, 0)
            outs.append(out)

        self.assertIn("就不响了", outs[0], "第一次该教怎么停")
        self.assertIn("就不响了", outs[-1], "最后一次该再提一遍")
        for mid in outs[1:-1]:
            if mid.strip():
                self.assertNotIn("就不响了", mid, f"中途不该再念：{mid!r}")
        for i, out in enumerate(outs[1:], start=2):
            if out.strip():
                self.assertIn(f"第 {i} 次", out, f"第 {i} 次没标次数：{out!r}")

    def test_force_after_delivery_keeps_delivered(self):
        """实机事故：16:35 那条已投递（日志有 delivered to weixin），
        之后任何一次 plan --force 重建时窗口已过期 → 被写成 skipped 0/1，
        「一年后查」看到的是「没响过」。

        delivered 是终态，重建不许覆盖它。
        """
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T09:00:00+08:00")
        self.assertEqual(self.window("travel")["state"], "delivered")

        rc, _, _ = self.cli("plan", "--force", now="2026-11-10T21:00:00+08:00")
        self.assertEqual(rc, 0)
        w = self.window("travel")
        self.assertEqual(w["state"], "delivered", "已送达的记录被重建冲成 skipped")
        self.assertEqual(w["fireCount"], 1, "fireCount 被清零")
        self.assertIsNone(w.get("missedReason"), "已送达的窗口不该带 missedReason")

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
        rc, _, _ = self.cli("ack", "--window", "trip", "--text", "好了", now="2026-11-10T09:05:00+08:00")
        self.assertEqual(rc, 4, "已送达的一次性窗口没有可确认的匹配")


class TestValidity(RtcCase):
    """有效期（validFrom / validUntil）"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "season",
                "label": "版本限时任务",
                "start": "10:00",
                "end": "10:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
                "validFrom": "2026-11-10",
                "validUntil": "2026-11-12",
            }
        ]
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
        good["alarmWindows"][0].update(
            {"oneOff": True, "expectFires": 1, "validFrom": "2026-11-10", "validUntil": "2026-11-10"}
        )
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

        rc, _, _ = self.cli("ack", "--window", "wake", "--text", "起了", now="2026-11-10T07:06:00+08:00")
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
        r["rollcalls"] = [
            {
                "hour": 20,
                "minute": 0,
                "kindByDateType": {"workday": "summary", "holiday": "summary", "weekend": "summary"},
            }
        ]
        r["weeklyChores"] = [{"dayOfWeek": 6, "label": "洗衣服", "attachToRollcallHour": 14}]
        self.write_routine(r)

    def snapshot(self, day: str) -> Path:
        return self.ws / "state" / "schedule" / f"{day}.json"

    def test_snapshot_written_into_workspace(self):
        self.cli("plan", now="2026-12-15T06:00:00+08:00")
        sp = self.snapshot("2026-12-15")
        self.assertTrue(sp.exists(), "plan 必须写每日快照")
        self.assertTrue(
            sp.is_relative_to(self.ws), "快照必须落在 --workspace 指定的目录里，不能写到进程 cwd 下"
        )
        snap = json.loads(sp.read_text(encoding="utf-8"))
        self.assertEqual(snap["date"], "2026-12-15")
        self.assertEqual(snap["planKind"], "workday")
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["windows"][0]["label"], "起床")
        self.assertEqual(snap["windows"][0]["expectFires"], 6)
        self.assertEqual(len(snap["rollcalls"]), 1)
        self.assertEqual(snap["rollcalls"][0]["kind"], "summary")

    def test_snapshot_records_weekly_chore(self):
        self.cli("plan", now="2026-12-19T06:00:00+08:00")  # 周六
        snap = json.loads(self.snapshot("2026-12-19").read_text(encoding="utf-8"))
        self.assertEqual(len(snap["weeklyChores"]), 1)
        self.assertEqual(snap["weeklyChores"][0]["label"], "洗衣服")

    def test_snapshot_records_one_off_and_validity(self):
        r = base_routine()
        r["alarmWindows"][0].update(
            {"oneOff": True, "expectFires": 1, "validFrom": "2026-12-15", "validUntil": "2026-12-15"}
        )
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
        rc, _, err = self.cli("schedule", "2026-11-14")  # 周六
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
        r["alarmWindows"].append(
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "23:45",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2028-01-01",
                "validUntil": "2028-01-01",
            }
        )
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
        r["alarmWindows"] = [
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "23:45",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-12-15",
                "validUntil": "2026-12-15",
            }
        ]
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
        r["alarmWindows"] = [
            {
                "id": "zzz",
                "label": "限时活动",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "dueDate": "2026-05-10",
                "validFrom": "2026-05-08",
                "validUntil": "2026-05-10",
            }
        ]
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
        snap = json.loads((self.ws / "state" / "schedule" / "2026-05-08.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["windows"][0]["dueDate"], "2026-05-10")


class TestChecklist(RtcCase):
    """带清单的提醒：逐项打勾"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "pack",
                "label": "清点并带走",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "checklist": ["电饭煲", "电脑", "被子枕头", "牙膏"],
            }
        ]
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
        rc, out, err = self.cli(
            "check", "--window", "pack", "--item", "电饭煲", now="2026-05-12T20:05:00+08:00"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "check 的 stdout 必须为空——它由用户交互触发，不该被投递")
        rc, _, _ = self.cli("check", "--window", "pack", "--item", "2", now="2026-05-12T20:06:00+08:00")
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
            self.cli("check", "--window", "pack", "--item", str(i), now="2026-05-12T20:05:00+08:00")
        w = self.state()["windows"][0]
        self.assertEqual(w["state"], "acked", "全部打完即视为完成")
        self.assertEqual(len(w["checked"]), 4)

    def test_completed_list_stops_reminding(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        for i in range(1, 5):
            self.cli("check", "--window", "pack", "--item", str(i), now="2026-05-12T20:05:00+08:00")
        self.cli("plan", now="2026-05-13T06:00:00+08:00")
        self.assertEqual(self.state()["windows"][0]["state"], "acked")
        rc, out, _ = self.cli("due", now="2026-05-13T20:00:00+08:00")
        self.assertEqual(out, "", "清单打完就不再提醒")

    def test_unknown_item(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        rc, _, err = self.cli(
            "check", "--window", "pack", "--item", "不存在的东西", now="2026-05-12T20:05:00+08:00"
        )
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_undo(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        self.cli("check", "--window", "pack", "--item", "电饭煲", now="2026-05-12T20:05:00+08:00")
        rc, _, _ = self.cli(
            "check", "--window", "pack", "--item", "电饭煲", "--undo", now="2026-05-12T20:06:00+08:00"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.state()["windows"][0]["checked"], [])


class TestPostpone(RtcCase):
    """推迟：今天不响，明天同一时间自动重新创建并提醒"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "thermos",
                "label": "洗保温杯",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-05-10",
                "validUntil": "2026-05-10",
            }
        ]
        self.write_routine(r)

    def test_today_is_skipped_and_tomorrow_fires_at_same_time(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, out, err = self.cli("postpone", "--window", "thermos", now="2026-05-10T08:00:00+08:00")
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
        self.assertEqual(entry["missedReason"], "postponed", "归档要保留推迟原因，不能丢")

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
        rc, _, err = self.cli(
            "postpone", "--window", "thermos", "--days", "3", now="2026-05-10T08:00:00+08:00"
        )
        self.assertEqual(rc, 0)
        routine = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        w = next(x for x in routine["alarmWindows"] if x["id"] == "thermos")
        self.assertEqual(w["validFrom"], "2026-05-13")
        self.assertEqual(w["validUntil"], "2026-05-13")

    def test_unknown_window(self):
        rc, _, err = self.cli("postpone", "--window", "nope", now="2026-05-10T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("没有", err)

    def test_target_not_later_than_today(self):
        for target in ("2026-05-09", "2026-05-10"):
            rc, _, err = self.cli(
                "postpone", "--window", "thermos", "--to", target, now="2026-05-10T08:00:00+08:00"
            )
            self.assertEqual(rc, 3, target)
            self.assertIn("不晚于今天", err)

    def test_bad_date_format(self):
        rc, _, err = self.cli(
            "postpone", "--window", "thermos", "--to", "05-11", now="2026-05-10T08:00:00+08:00"
        )
        self.assertEqual(rc, 3)
        self.assertIn("YYYY-MM-DD", err)

    def test_target_conflicting_with_date_types(self):
        """推迟到该窗口不生效的日期类型 → 拒绝，而不是明天静默不响。"""
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "thermos",
                "label": "洗保温杯",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "dateTypes": ["workday"],
                "validFrom": "2026-05-10",
                "validUntil": "2026-05-10",
            }
        ]
        self.write_routine(r)
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        # 2026-05-16 是周六，dateTypes=["workday"] 不生效
        rc, _, err = self.cli(
            "postpone", "--window", "thermos", "--to", "2026-05-16", now="2026-05-10T08:00:00+08:00"
        )
        self.assertEqual(rc, 3)
        self.assertIn("dateTypes", err)


class TestPostponePeriodic(RtcCase):
    """周期窗口的推迟：跳过某一天，之后照常"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "wake",
                "label": "起床",
                "start": "07:00",
                "end": "07:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["起了"],
            }
        ]
        self.write_routine(r)

    def test_skips_today_only(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, _, err = self.cli("postpone", "--window", "wake", now="2026-05-10T08:00:00+08:00")
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
        r["alarmWindows"] = [
            {
                "id": "zzz",
                "label": "限时活动",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "dueDate": "2026-05-12",
                "validFrom": "2026-05-10",
                "validUntil": "2026-05-12",
            }
        ]
        self.write_routine(r)

    def test_deadline_moves_and_countdown_recomputes(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, _, err = self.cli(
            "postpone", "--window", "zzz", "--to", "2026-05-13", now="2026-05-10T21:00:00+08:00"
        )
        self.assertEqual(rc, 0)

        for day, expect in (
            ("2026-05-11", "距截止还有 2 天"),
            ("2026-05-12", "明天截止"),
            ("2026-05-13", "最后一天"),
        ):
            self.cli("plan", now=f"{day}T06:00:00+08:00")
            _, out, _ = self.cli("due", now=f"{day}T20:00:00+08:00")
            self.assertIn(expect, out, day)

    def test_stops_after_new_deadline(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        self.cli("postpone", "--window", "zzz", "--to", "2026-05-13", now="2026-05-10T21:00:00+08:00")
        self.cli("plan", now="2026-05-14T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-05-14T20:00:00+08:00")
        self.assertEqual(out, "")


class TestCancelCompleteDisable(RtcCase):
    """取消 / 完成 / 永不提醒"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "meeting",
                "label": "临时会议",
                "start": "11:00",
                "end": "11:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "tea",
                "label": "喝茶",
                "start": "15:00",
                "end": "15:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            },
        ]
        self.write_routine(r)

    def routine(self) -> dict:
        return json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))

    def window_def(self, wid):
        return next(w for w in self.routine()["alarmWindows"] if w["id"] == wid)

    # ---------- cancel ----------

    def test_cancel_one_off_stops_it_forever(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("cancel", "--window", "meeting", now="2026-11-13T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "cancel 由用户触发，stdout 不该被投递出去")

        rc, out, _ = self.cli("due", now="2026-11-13T11:00:00+08:00")
        self.assertEqual(out, "", "取消后今天不该响")

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn(
            "meeting", [w["id"] for w in self.state().get("windows", [])], "一次性窗口取消后不该再生成"
        )

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
        self.assertEqual(entry["missedReason"], "cancelled", "取消的原因要进归档，不能丢")

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
        r["alarmWindows"].append(
            {
                "id": "future",
                "label": "季度汇报",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2027-03-01",
                "validUntil": "2027-03-01",
            }
        )
        self.write_routine(r)
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, _, err = self.cli(
            "cancel", "--window", "future", "--date", "2027-03-01", now="2026-11-13T08:00:00+08:00"
        )
        self.assertEqual(rc, 0)
        self.assertTrue(self.window_def("future").get("disabled"), "取消未来那次 = 那个窗口不再生成")

    def test_cancel_date_window_does_not_exist(self):
        """那天根本没有这个窗口 → 退 4 并说明，不静默无效。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, _, err = self.cli(
            "cancel", "--window", "meeting", "--date", "2026-11-20", now="2026-11-13T08:00:00+08:00"
        )
        self.assertEqual(rc, 4)
        self.assertIn("不存在", err)
        self.assertFalse(self.window_def("meeting").get("disabled"), "取消失败就不该改动配置")

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
        rc, out, err = self.cli(
            "complete", "--window", "tea", "--text", "喝完了", now="2026-11-13T08:00:00+08:00"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        w = self.window("tea")
        self.assertEqual(w["state"], "acked")
        self.assertEqual(w["ackText"], "喝完了")
        self.assertTrue(self.window_def("tea").get("disabled"), "完成 = 以后不再提醒")

        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertNotIn("tea", [w["id"] for w in self.state().get("windows", [])])

    def test_complete_vs_ack_difference(self):
        """complete 与 ack 的区别：ack 只停今天，complete 连以后也停。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("ack", "--window", "tea", "--text", "好了", now="2026-11-13T08:00:00+08:00")
        self.assertFalse(self.window_def("tea").get("disabled"), "ack 不该停用窗口")
        self.cli("plan", now="2026-11-14T06:00:00+08:00")
        self.assertIn("tea", [w["id"] for w in self.state().get("windows", [])], "ack 之后明天照常响")

    # ---------- disable / enable ----------

    def test_disable_stops_forever(self):
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        rc, out, err = self.cli("disable", "--window", "tea", now="2026-11-13T08:00:00+08:00")
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
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "cancel-me",
                "label": "临时会议",
                "start": "11:00",
                "end": "11:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "postpone-me",
                "label": "取件",
                "start": "14:00",
                "end": "14:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "zzz",
                "label": "限时活动",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "dueDate": "2026-11-15",
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-15",
            },
            {
                "id": "future",
                "label": "季度汇报",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2027-03-01",
                "validUntil": "2027-03-01",
            },
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
                "id": "tea",
                "label": "喝茶",
                "start": "15:00",
                "end": "15:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            },
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
        self.cli("postpone", "--window", "postpone-me", "--to", "2026-11-14", now=f"{T}T08:20:00+08:00")
        # ⑦ 永不提醒
        self.cli("disable", "--window", "tea", now=f"{T}T08:50:00+08:00")
        # ⑤ 取消未来的预约
        self.cli("cancel", "--window", "future", "--date", "2027-03-01", now=f"{T}T08:55:00+08:00")

        # 当天：只有旅游（09:00）和倒计时（20:00）该响
        expected = {
            "07:00": "",  # wake 已完成
            "09:00": "旅游",  # travel 一次性
            "11:00": "",  # cancel-me 已取消
            "14:00": "",  # postpone-me 已推迟
            "15:00": "",  # tea 已停用
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

        schema = json.loads((REPO / "contract" / "alarm-state.schema.json").read_text(encoding="utf-8"))
        T = "2026-11-13"
        self.cli("plan", now=f"{T}T06:00:00+08:00")
        self.cli("complete", "--window", "wake", now=f"{T}T07:05:00+08:00")
        self.cli("cancel", "--window", "cancel-me", now=f"{T}T08:10:00+08:00")
        self.cli("postpone", "--window", "postpone-me", "--to", "2026-11-14", now=f"{T}T08:20:00+08:00")
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
        self.assertNotIn("cancel-me", out, "取消 ≠ 漏响，不该触发「整段没响」告警")
        self.assertIn("travel", out, "确实整段没响过的窗口仍要报出来——否则真漏了就没人知道")


class TestExecutionArchive(RtcCase):
    """推迟 / 取消 / 未完成 的记录留档——一年后查得到"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "postponed",
                "label": "取件",
                "start": "14:00",
                "end": "14:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "cancelled",
                "label": "临时会议",
                "start": "11:00",
                "end": "11:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "unfinished",
                "label": "没做完的事",
                "start": "16:00",
                "end": "16:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
            {
                "id": "ok",
                "label": "正常响的",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-13",
                "validUntil": "2026-11-13",
            },
        ]
        self.write_routine(r)

    def snapshot(self, day: str) -> dict:
        p = self.ws / "state" / "schedule" / f"{day}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def make_day(self):
        """跑出四种结局：推迟 / 取消 / 未完成 / 正常。"""
        self.cli("plan", now="2026-11-13T06:00:00+08:00")
        self.cli("postpone", "--window", "postponed", "--to", "2026-11-14", now="2026-11-13T08:00:00+08:00")
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
        rows = dict(con.execute("SELECT window, label FROM daily WHERE date='2026-11-13'").fetchall())
        reasons = dict(con.execute("SELECT window, missed FROM daily WHERE date='2026-11-13'").fetchall())
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


class TestPostponeRollbackPreservesConcurrentEdit(RtcCase):
    """回归：推迟失败回滚时，不得覆盖并发改过的另一个窗口（审计发现 ①）。

    真实竞态：推迟先写时刻表（第 1 步成功），随后另一个进程改了别的窗口；
    再写状态时冲突，触发回滚。回滚若用「当前磁盘摘要」当守卫，会把别人的
    改动一起抹掉；但用「推迟前摘要」也不行——推迟自己已经写过文件，
    那个摘要必然不匹配，回滚永远失败却谎报成功。正确做法是用
    「本次操作第 1 步写入后的版本摘要」做 CAS，冲突即如实报部分应用。
    """

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
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
                "id": "report",
                "label": "助理汇报",
                "start": "14:00",
                "end": "14:30",
                "intervalMin": 5,
                "expectFires": 7,
                "ackHints": ["好了"],
            },
        ]
        self.write_routine(r)
        rtc.set_workspace(self.ws)

    def test_rollback_keeps_concurrent_edit(self):
        # 先把今天排出来，让 wake 处于 pending（推迟才会走「写状态」那一步）
        self.cli("plan", now="2026-05-10T06:00:00+08:00")

        calls = {"n": 0}
        orig = rtc.atomic_write

        def fake(path, data, expected_rev=None, expected_digest=None):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第 1 步：时刻表写入成功；随后模拟「另一个进程」改了 report
                res = orig(path, data, expected_rev, expected_digest)
                rb = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
                for x in rb["alarmWindows"]:
                    if x["id"] == "report":
                        x["start"] = "15:00"
                rtc.ROUTINE.write_text(json.dumps(rb, ensure_ascii=False, indent=2), encoding="utf-8")
                return res
            if path == rtc.STATE:
                # 第 2 步：强制状态写入冲突 -> 触发回滚分支
                raise rtc.Conflict("injected: state changed concurrently")
            # 第 3 步（回滚写时刻表）：交给真实代码，由 fix 决定成败
            return orig(path, data, expected_rev, expected_digest)

        rtc.atomic_write = fake
        self.addCleanup(lambda: setattr(rtc, "atomic_write", orig))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rtc.cmd_postpone(
                argparse.Namespace(
                    window="wake", to=None, days=1, deadline=False, now="2026-05-10T08:00:00+08:00"
                )
            )
        self.assertEqual(rc, 3, "并发编辑使回滚 CAS 失败，应报『部分应用』(rc=3)")
        self.assertIn("部分应用", err.getvalue(), "诊断不得谎报『已回滚』")

        rb = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        report = next(x for x in rb["alarmWindows"] if x["id"] == "report")
        self.assertEqual(report["start"], "15:00", "回滚不得覆盖并发改过的『助理汇报』（15:00）")
        wake = next(x for x in rb["alarmWindows"] if x["id"] == "wake")
        self.assertEqual(
            wake.get("skipDates"), ["2026-05-10"], "部分应用：推迟确实留在时刻表里，必须如实告知"
        )

    def test_rollback_restores_without_concurrent_edit(self):
        """审计发现 ②：没有并发编辑时，回滚必须真的把推迟撤销。

        旧实现用推迟前 digest 做回滚守卫，而推迟本身已改过文件，
        摘要必然不匹配 -> 回滚被拒却被吞掉 -> 报『已回滚』而推迟残留。
        """
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        state_before = json.loads(rtc.STATE.read_text(encoding="utf-8"))

        calls = {"n": 0}
        orig = rtc.atomic_write

        def fake(path, data, expected_rev=None, expected_digest=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return orig(path, data, expected_rev, expected_digest)
            if path == rtc.STATE:
                raise rtc.Conflict("injected: state changed concurrently")
            return orig(path, data, expected_rev, expected_digest)

        rtc.atomic_write = fake
        self.addCleanup(lambda: setattr(rtc, "atomic_write", orig))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rtc.cmd_postpone(
                argparse.Namespace(
                    window="wake", to=None, days=1, deadline=False, now="2026-05-10T08:00:00+08:00"
                )
            )
        self.assertEqual(rc, 2, "状态冲突且无并发编辑时应成功回滚")
        self.assertIn("已回滚", err.getvalue())

        rb = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        wake = next(x for x in rb["alarmWindows"] if x["id"] == "wake")
        self.assertNotIn("skipDates", wake, "推迟必须被撤销，不能残留")
        self.assertEqual(
            json.loads(rtc.STATE.read_text(encoding="utf-8")), state_before, "今日状态不应被改动"
        )

    def test_cas_rejects_edit_between_check_and_replace(self):
        """审计要求：临门再验必须抓出 check 与 replace 之间的外部改写。

        旧测试直接传错 digest，只命中第一道守卫（读取后即比对），没触及
        「写临时文件 -> 原子改名」之间的窗口。这里让 file_digest 在第二次
        调用（临门再验）时真去改一次磁盘文件，模拟一次并发编辑，断言
        atomic_write 抛 Conflict、且磁盘文件保留外部编辑而非被覆盖。
        """
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        real_digest = rtc.file_digest
        orig_digest = real_digest(rtc.ROUTINE)
        calls = {"n": 0}

        def fake_digest(p):
            calls["n"] += 1
            if calls["n"] == 2:
                # 临门再验前，模拟另一进程改了时刻表（真实的磁盘写入）
                rb = json.loads(p.read_text(encoding="utf-8"))
                for x in rb["alarmWindows"]:
                    if x["id"] == "report":
                        x["start"] = "16:00"
                p.write_text(json.dumps(rb, ensure_ascii=False, indent=2), encoding="utf-8")
            return real_digest(p)

        rtc.file_digest = fake_digest
        self.addCleanup(lambda: setattr(rtc, "file_digest", real_digest))

        data = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        with self.assertRaises(rtc.Conflict):
            rtc.atomic_write(rtc.ROUTINE, data, expected_rev=None, expected_digest=orig_digest)

        # 磁盘文件必须保留外部编辑，而非被 data 覆盖
        rb = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        report = next(x for x in rb["alarmWindows"] if x["id"] == "report")
        self.assertEqual(report["start"], "16:00", "临门再验必须拒绝把并发编辑覆盖掉")


class TestLockOwnership(unittest.TestCase):
    """审计发现 ①：stale-lock 接管既不许两个写入者同时进入临界区，
    也不许一方删掉另一方的锁。"""

    def _lock_for(self, name):
        d = tempfile.mkdtemp(prefix="rtc-lock-")
        p = Path(d) / name
        return p, p.with_name(p.name + ".lock")

    def test_fresh_acquire_writes_token(self):
        p, lock = self._lock_for("a.json")
        self.assertTrue(rtc._try_acquire_lock(lock, "A", lock.with_name(lock.name + ".A")))
        self.assertEqual(lock.read_bytes().decode(), "A")

    def test_live_lock_not_taken_over(self):
        p, lock = self._lock_for("a.json")
        rtc._try_acquire_lock(lock, "A", lock.with_name(lock.name + ".A"))
        self.assertEqual(lock.read_bytes().decode(), "A")
        # 锁新鲜 → 第二个写入者必须拿不到
        self.assertFalse(rtc._try_acquire_lock(lock, "B", lock.with_name(lock.name + ".B")))
        self.assertEqual(lock.read_bytes().decode(), "A")

    def test_stale_lock_takeover_yields_single_owner(self):
        p, lock = self._lock_for("a.json")
        lock.write_bytes(b"ghost")
        import time as _t

        old = _t.time() - (rtc._LOCK_STALE_S + 5)
        os.utime(str(lock), (old, old))
        # A 接管陈旧锁
        self.assertTrue(rtc._try_acquire_lock(lock, "A", lock.with_name(lock.name + ".A")))
        self.assertEqual(lock.read_bytes().decode(), "A")
        # 接管后锁变新鲜，B 不得再拿到（不会出现「两者都拿到」）
        self.assertFalse(rtc._try_acquire_lock(lock, "B", lock.with_name(lock.name + ".B")))
        self.assertEqual(lock.read_bytes().decode(), "A")

    def test_owner_does_not_delete_other_owners_lock(self):
        p, lock = self._lock_for("a.json")
        with rtc._exclusive_file_lock(p):
            owner = lock.read_bytes().decode()
            self.assertTrue(owner, "进入临界区后锁必须含本持有者令牌")
            # 模拟 A 临界区过长、锁被 B 接管
            lock.write_bytes(b"B")
        # A 退出时不得删掉 B 的锁
        self.assertTrue(lock.exists(), "A 退出不得删除 B 接管的锁")
        self.assertEqual(lock.read_bytes().decode(), "B")

    def test_concurrent_writers_no_lost_update(self):
        import threading

        counter_path = Path(tempfile.mkdtemp(prefix="rtc-cnt-")) / "counter.json"
        counter_path.write_text(json.dumps({"n": 0}))
        n_threads, per = 8, 20
        conflicts = {"n": 0}

        def worker():
            done = 0
            while done < per:
                try:
                    with rtc._exclusive_file_lock(counter_path):
                        d = json.loads(counter_path.read_text())
                        d["n"] += 1
                        counter_path.write_text(json.dumps(d))
                    done += 1
                except rtc.Conflict:
                    # 极少发生（锁等待超时）；重试以保证最终计数正确，
                    # 同时不掩盖「两个写入者同时进入临界区」这类真正的丢失更新。
                    conflicts["n"] += 1

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = json.loads(counter_path.read_text())["n"]
        self.assertEqual(final, n_threads * per, f"并发写入丢失更新：期望 {n_threads * per}，实际 {final}")
        # 冲突重试只是环境兜底：正常情况下几乎不应触发，确保我们测的是锁本身
        self.assertLessEqual(conflicts["n"], n_threads * per, "冲突次数异常偏高，锁可能未正常串行化")


class TestDueConflictSilent(RtcCase):
    """审计发现 ②：due 写入冲突必须保持静默（空 stdout、退出 0），
    诊断走 stderr，绝不重复投递文案（issue #4 §11）。"""

    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())
        rtc.set_workspace(self.ws)

    def test_due_conflict_is_silent(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        orig = rtc.atomic_write

        def fake(path, data, expected_rev=None, expected_digest=None):
            # 只在「本次 fire 写入」上注入冲突：有窗口处于 ringing/delivered 即视为 fire
            if path == rtc.STATE and any(
                w.get("state") in ("ringing", "delivered") for w in data.get("windows", [])
            ):
                raise rtc.Conflict("injected: due state conflict")
            return orig(path, data, expected_rev, expected_digest)

        rtc.atomic_write = fake
        self.addCleanup(lambda: setattr(rtc, "atomic_write", orig))

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rtc.cmd_due(argparse.Namespace(now="2026-05-10T07:05:00+08:00"))
        self.assertEqual(rc, 0, "due 冲突应退出 0")
        self.assertEqual(out.getvalue(), "", "due 冲突必须空 stdout（不投递）")
        self.assertIn("写入冲突", err.getvalue(), "诊断必须留在 stderr")

        # 状态未被本次写入破坏：fireCount 仍为 0（下一次 due 会重试并成功）
        st = json.loads(rtc.STATE.read_text(encoding="utf-8"))
        wake = next(w for w in st["windows"] if w["id"] == "wake")
        self.assertEqual(wake["fireCount"], 0, "冲突那次不得记录 fire")


class TestSetTime(RtcCase):
    """set-time：模型改窗口时刻走命令，而不是手改 JSON"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "wake",
                "label": "起床",
                "start": "08:10",
                "end": "08:40",
                "intervalMin": 5,
                "expectFires": 7,
                "ackHints": ["起了"],
            },
            {
                "id": "meds",
                "label": "吃药",
                "start": "09:00",
                "end": "09:20",
                "intervalMin": 5,
                "expectFires": 5,
                "ackHints": ["好了"],
            },
        ]
        self.write_routine(r)

    def _routine(self):
        return json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))

    def test_set_time_changes_start_and_keeps_length(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, out, err = self.cli("set-time", "--window", "wake", "--start", "07:30", "--end", "08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "set-time 由用户触发，stdout 不该被投递出去")
        w = next(x for x in self._routine()["alarmWindows"] if x["id"] == "wake")
        self.assertEqual(w["start"], "07:30")
        self.assertEqual(w["end"], "08:00")
        self.assertEqual(w["expectFires"], 7, "长度不变，expectFires 仍应为 7")

    def test_set_time_recomputes_expectFires_when_length_changes(self):
        self.cli("plan", now="2026-05-10T06:00:00+08:00")
        rc, out, err = self.cli("set-time", "--window", "meds", "--start", "09:00", "--end", "09:30")
        self.assertEqual(rc, 0)
        w = next(x for x in self._routine()["alarmWindows"] if x["id"] == "meds")
        # 09:30-09:00=30 分钟 -> (30/5)+1 = 7
        self.assertEqual(w["expectFires"], 7)

    def test_set_time_rejects_end_not_later_than_start(self):
        rc, out, err = self.cli("set-time", "--window", "wake", "--start", "09:00", "--end", "08:00")
        self.assertEqual(rc, 3, "end 不晚于 start 应拒绝")
        self.assertIn("end", err)

    def test_set_time_rejects_missing_window(self):
        rc, out, err = self.cli("set-time", "--window", "nope", "--start", "07:30")
        self.assertEqual(rc, 4, "不存在的窗口应返回 4")

    def test_set_time_rejects_bad_format(self):
        rc, out, err = self.cli("set-time", "--window", "wake", "--start", "730")
        self.assertEqual(rc, 3, "非法 HH:MM 应返回 3")


class TestAckGrace(RtcCase):
    """ack 宽限：当天刚过期的 missed 窗口也应能被确认（模型晚一点回『好了』也有效）。"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "noon",
                "label": "午间",
                "start": "12:00",
                "end": "12:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            }
        ]
        self.write_routine(r)

    def test_ack_missed_same_day_auto_pick(self):
        # 1) 生成当天计划
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        # 2) 窗口过期 -> due 把它标为 missed
        self.cli("due", now="2026-11-10T12:30:00+08:00")
        self.assertEqual(self.window("noon")["state"], "missed")
        # 3) 晚于宽限(12:40)但同日内，省略 --window 自动挑到它
        rc, _, _ = self.cli("ack", "--text", "好了", now="2026-11-10T13:00:00+08:00")
        self.assertEqual(rc, 0, "当天 missed 窗口应在宽限外仍可被 ack")
        self.assertEqual(self.window("noon")["state"], "acked")

    def test_ack_missed_prev_day_rejected(self):
        # 计划生成在前一天
        self.cli("plan", now="2026-11-09T06:00:00+08:00")
        self.cli("due", now="2026-11-09T12:30:00+08:00")
        # 第二天再回『好了』不应 ack 到昨天的 missed
        rc, _, _ = self.cli("ack", "--text", "好了", now="2026-11-10T09:00:00+08:00")
        self.assertEqual(rc, 4, "跨日的 missed 窗口不应被误 ack")

    def test_ack_active_window_auto_pick_still_works(self):
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T12:05:00+08:00")
        rc, _, _ = self.cli("ack", "--text", "好了", now="2026-11-10T12:06:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.window("noon")["state"], "acked")

    def test_ack_prefers_active_over_missed(self):
        # 当天更早的窗口已 missed，但 later 正在响；回复应绑到正在响的
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "early",
                "label": "早间",
                "start": "08:00",
                "end": "08:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            },
            {
                "id": "later",
                "label": "晚间",
                "start": "18:00",
                "end": "18:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            },
        ]
        self.write_routine(r)
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        # early 过期 -> missed；later 当前在响
        self.cli("due", now="2026-11-10T08:30:00+08:00")
        self.cli("due", now="2026-11-10T18:05:00+08:00")
        self.assertEqual(self.window("early")["state"], "missed")
        self.assertEqual(self.window("later")["state"], "ringing")
        # 省略 --window，应优先确认正在响的 later，而非更早的 missed early
        rc, _, _ = self.cli("ack", "--text", "好了", now="2026-11-10T18:06:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.window("later")["state"], "acked")
        self.assertEqual(self.window("early")["state"], "missed", "更早的 missed 不应被误绑")


class TestStateCorruption(RtcCase):
    """状态文件坏掉时必须优雅退出：一行诊断、退出码 3、原文件不被覆盖。

    回归自实机自检（F4）：以前 `alarms.json` 写成 `{oops` 之后，
    `status` 与 `due` 都直接抛 Traceback 退 1——而 `rtc-due` 每 5 分钟
    跑一次，等于每 5 分钟给宿主留一条看不懂的失败记录。
    """

    def test_corrupt_state_status_is_readable_error(self):
        self.write_routine(base_routine())
        (self.ws / "state" / "alarms.json").write_text("{oops", encoding="utf-8")
        rc, out, err = self.cli("status", "--json")
        self.assertEqual(rc, 3, f"应退 3（校验失败），实际 {rc}")
        self.assertNotIn("Traceback", err, "不该抛 Traceback")
        self.assertIn("不是合法 JSON", err)
        self.assertIn("alarms.json", err)

    def test_corrupt_state_due_keeps_silent_and_file_intact(self):
        self.write_routine(base_routine())
        p = self.ws / "state" / "alarms.json"
        p.write_text("{oops", encoding="utf-8")
        rc, out, err = self.cli("due", now="2026-11-10T07:00:00+08:00")
        self.assertEqual(rc, 3)
        self.assertEqual(out, "", "坏文件时该静默，不得输出半截提醒")
        self.assertNotIn("Traceback", err)
        self.assertEqual(p.read_text(encoding="utf-8"), "{oops", "坏文件不得被覆盖")

    def test_corrupt_routine_is_readable_error(self):
        (self.ws / "rules" / "routines.json").write_text("[]", encoding="utf-8")
        rc, _, err = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 3)
        self.assertNotIn("Traceback", err, "时刻表结构不对时也不该甩 Traceback")


class TestPlanForceKeepsRinging(RtcCase):
    """同日重建（plan --force）不得把正在响的窗口重置。

    回归自实机自检（A3b）：rtc_once.py 每加一条提醒都跑 `plan --force`，
    若此时正好有窗口在响，fireCount 会被清零、状态回 pending，
    下一 tick 再响一轮——响的次数上限就形同虚设。
    """

    def test_force_preserves_ringing_window(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:00:00+08:00")
        w = self.window("wake")
        self.assertEqual(w["fireCount"], 1)
        self.assertEqual(w["state"], "ringing")

        rc, _, _ = self.cli("plan", "--force", now="2026-11-10T07:01:00+08:00")
        self.assertEqual(rc, 0)
        w = self.window("wake")
        self.assertEqual(w["state"], "ringing", "正在响的窗口被打回 pending")
        self.assertEqual(w["fireCount"], 1, "fireCount 被清零")
        self.assertIn("lastFiredAt", w, "丢失 lastFiredAt → 间隔判断失效")

    def test_force_then_due_continues_cadence(self):
        """重建后再响一次：计数应连续（2），而不是从 1 重来。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:00:00+08:00")
        self.cli("plan", "--force", now="2026-11-10T07:01:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-10T07:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("第 2 次", out, f"重建后计数没接上：{out!r}")
        self.assertEqual(self.window("wake")["fireCount"], 2)


class TestPruneExpiredOneOff(RtcCase):
    """过期的一次性提醒条目要被 plan 清掉，周期窗口一个都不能动。

    回归自实机自检：routines.json 里还留着几天前的一次性窗口，只增不删。
    """

    def test_expired_oneoff_is_pruned(self):
        r = base_routine()
        r["alarmWindows"].append(
            {
                "id": "once-old-1400-20261114",
                "label": "旧提醒",
                "start": "14:00",
                "end": "14:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "oneOff": True,
                "validFrom": "2026-11-14",
                "validUntil": "2026-11-14",
            }
        )
        r["alarmWindows"].append(
            {
                "id": "once-today-1500",
                "label": "今天的",
                "start": "15:00",
                "end": "15:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "oneOff": True,
                "validFrom": "2026-11-18",
                "validUntil": "2026-11-18",
            }
        )
        self.write_routine(r)
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("已清理过期的一次性提醒", err)

        saved = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        ids = [w["id"] for w in saved["alarmWindows"]]
        self.assertNotIn("once-old-1400-20261114", ids, "过期的 oneOff 没被清掉")
        self.assertIn("once-today-1500", ids, "今天的 oneOff 被误删")
        self.assertIn("wake", ids, "周期窗口被误删")

    def test_future_oneoff_untouched(self):
        r = base_routine()
        r["alarmWindows"].append(
            {
                "id": "once-future",
                "label": "以后的",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "oneOff": True,
                "validFrom": "2026-12-01",
                "validUntil": "2026-12-01",
            }
        )
        self.write_routine(r)
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        saved = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        self.assertIn("once-future", [w["id"] for w in saved["alarmWindows"]])


class TestBrief(RtcCase):
    """行程简报：模型不跑命令也能知道今天排了什么。

    回归自实机问题：模型只在用户开口时被调用，它看不到今天的时间表，
    于是用户每次都得重讲一遍「我现在要干嘛」。解法是把简报写进 AGENTS.md。
    """

    def agents_md(self) -> Path:
        return self.ws / "AGENTS.md"

    def write_agents(self, extra: str = "用户原文，不能被动。"):
        self.agents_md().write_text(
            "# 入口\n\n" + extra + "\n\n"
            "<!-- RTC:BRIEF:BEGIN 自动生成，勿手改本区块；改时刻表请用 rtc.py -->\n"
            "旧内容\n"
            "<!-- RTC:BRIEF:END -->\n\n## 红线\n- 不建 cron\n",
            encoding="utf-8",
        )

    def test_brief_lists_next_window(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, _ = self.cli("brief", now="2026-11-10T06:30:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("今日行程", out)
        self.assertIn("接下来：07:00 起床", out, out)
        self.assertIn("30 分钟", out, "应给出到点还有多久")

    def test_plan_syncs_agents_block(self):
        self.write_routine(base_routine())
        self.write_agents()
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        text = self.agents_md().read_text(encoding="utf-8")
        self.assertIn("今日行程", text, "简报没写进 AGENTS.md")
        self.assertIn("用户原文，不能被动。", text, "区块外的原文被改动了")
        self.assertIn("## 红线", text, "区块后的内容丢了")
        self.assertNotIn("旧内容", text, "旧区块没被替换")

    def test_no_block_leaves_file_untouched(self):
        self.write_routine(base_routine())
        p = self.agents_md()
        p.write_text("# 入口\n没有标记\n", encoding="utf-8")
        rc, _, err = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 0, "没有标记不该让 plan 失败")
        self.assertEqual(p.read_text(encoding="utf-8"), "# 入口\n没有标记\n", "无标记时不该改文件")

    def test_ack_refreshes_block(self):
        self.write_routine(base_routine())
        self.write_agents()
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:05:00+08:00")
        self.cli("ack", "--window", "wake", "--text", "起了", now="2026-11-10T07:06:00+08:00")
        text = self.agents_md().read_text(encoding="utf-8")
        self.assertIn("起床 已确认", text, "确认后简报没刷新")


class TestHandEditedRoutine(RtcCase):
    """文字约束拦不住手写；摘要守卫要能当场抓住并留原件。

    实机上发生过：模型照着技能里的 JSON 模板，把宿主目录那份 routines.json
    从 4 个窗口改写成 1 个自己编的一次性提醒——没人发现，也没人被告知。
    """

    def test_external_edit_is_reported_and_backed_up(self):
        self.write_routine(base_routine())
        rc, out, err = self.cli("plan", now="2026-11-16T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("被 RTC 之外", out, "第一次 plan 只是建印章，不该报警")
        # 窗口数从印章里取，别写死——不同基线的时刻表窗口数并不一样
        sealed = json.loads((self.ws / "state" / "routine-seal.json").read_text(encoding="utf-8"))
        was = sealed["windows"]

        hand = {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "tzOffset": "+08:00",
            "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6]},
            "alarmWindows": [
                {
                    "id": "go-out-1535",
                    "label": "今天 15:35 出门",
                    "start": "15:35",
                    "end": "15:40",
                    "intervalMin": 5,
                    "expectFires": 1,
                    "ackHints": ["知道了"],
                    "oneOff": True,
                    "validFrom": "2026-11-16",
                    "validUntil": "2026-11-16",
                },
            ],
        }
        self.write_routine(hand)
        rc, out, err = self.cli("plan", "--force", now="2026-11-16T08:01:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("被 RTC 之外的东西改过", out)
        self.assertIn(f"上次程序写出 {was} 个窗口", out)
        self.assertIn("现在是 1", out)

        backups = sorted((self.ws / "state" / "backups").glob("routines.handedit-*.json"))
        self.assertEqual(len(backups), 1, "手改的原件必须留一份")
        self.assertIn("go-out-1535", backups[0].read_text(encoding="utf-8"))

    def test_program_writes_stay_quiet(self):
        self.write_routine(base_routine())
        for i in range(3):
            rc, out, err = self.cli("plan", "--force", now=f"2026-11-16T08:0{i}:00+08:00")
            self.assertEqual(rc, 0, err)
            self.assertNotIn("被 RTC 之外", out, f"第 {i + 1} 次程序写入被误报成手改")

    def test_minimal_handwritten_file_gets_a_clean_error(self):
        """模型实机写的那份只有 version + alarmWindows：要报错，不要抛栈。"""
        self.write_routine({"version": 1, "alarmWindows": []})
        rc, out, err = self.cli("plan", now="2026-11-16T08:00:00+08:00")
        self.assertEqual(rc, 3, f"形状不对应当是退出码 3，不是栈：{err[-300:]}")
        self.assertIn("dateTypes", err)
        self.assertNotIn("Traceback", err)

    def test_truncated_routine_self_heals_from_last_good(self):
        """实机事故：文件被写空 → 当天所有提醒全哑。程序手里有好文件就该自己长回来。"""
        self.write_routine(base_routine())
        rc, out, err = self.cli("plan", now="2026-11-16T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        last = self.ws / "state" / "backups" / "routines.program-last.json"
        self.assertTrue(last.exists(), "程序写完必须留一份底稿")

        (self.ws / "rules" / "routines.json").write_text("", encoding="utf-8")
        rc, out, err = self.cli("plan", "--force", now="2026-11-16T08:05:00+08:00")
        self.assertEqual(rc, 0, f"文件坏了不该让整天停摆：{err[-300:]}")
        self.assertIn("自动恢复", out)
        restored = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        self.assertGreater(len(restored["alarmWindows"]), 0)
        self.assertTrue(
            list((self.ws / "state" / "backups").glob("routines.corrupt-*.json")),
            "坏件也要留一份，便于追是谁写坏的",
        )


class TestBackup(RtcCase):
    """每日快照是永久档案，只存在一块盘上；盘坏了就什么都没了。"""

    def _zip_names(self, target: Path) -> list[str]:

        return sorted(p.name for p in Path(target).glob("RTC-*.zip"))

    def test_backup_writes_zip_with_archive_members(self):
        import zipfile

        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        target = Path(tempfile.mkdtemp(prefix="rtc-bk-"))
        try:
            rc, out, err = self.cli("backup", "--to", str(target))
            self.assertEqual(rc, 0, err)
            zips = self._zip_names(target)
            self.assertEqual(len(zips), 1, f"应当只写一份：{zips}")
            with zipfile.ZipFile(target / zips[0]) as z:
                names = z.namelist()
                self.assertIn("rules/routines.json", names)
                self.assertIn("state/alarms.json", names)
                self.assertTrue(any(n.startswith("state/schedule/") for n in names), "每日快照必须进备份")

            # 同一天再跑一次：不重写（否则每天都在覆盖，滚动保留就失效了）
            before = (target / zips[0]).stat().st_mtime_ns
            rc2, _, _ = self.cli("backup", "--to", str(target))
            self.assertEqual(rc2, 0)
            self.assertEqual((target / zips[0]).stat().st_mtime_ns, before, "同日重复备份不该重写")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_backup_ignores_handmade_copies_in_state_backups(self):
        """实机事故：模型/人 `cp` 出来的 routines.<时间>.json 被当成档案收走，
        归档里于是出现两份 routines（一份 10 窗口一份 7 窗口），没人分得清哪份是真的。
        只认程序自己写的那三类。"""
        import zipfile

        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        bdir = self.ws / "state" / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        self.assertTrue((bdir / "routines.program-last.json").is_file(), "程序底稿没生成")
        # 模拟模型手抄 + 程序的手改证据
        (bdir / "routines.20261116_180845.json").write_text("{}", encoding="utf-8")
        (bdir / "routines.handedit-20261116-161521.json").write_text("{}", encoding="utf-8")
        (bdir / "notes.json").write_text("{}", encoding="utf-8")

        target = Path(tempfile.mkdtemp(prefix="rtc-bk-"))
        try:
            rc, out, err = self.cli("backup", "--to", str(target))
            self.assertEqual(rc, 0, err)
            with zipfile.ZipFile(target / self._zip_names(target)[0]) as z:
                names = z.namelist()
            self.assertIn("state/backups/routines.program-last.json", names, "程序底稿必须进归档")
            self.assertIn("state/backups/routines.handedit-20261116-161521.json", names, "手改证据必须进归档")
            self.assertNotIn("state/backups/routines.20261116_180845.json", names, "手抄副本不该进归档")
            self.assertNotIn("state/backups/notes.json", names, "无关 json 不该进归档")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_backup_keeps_only_latest_n(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        target = Path(tempfile.mkdtemp(prefix="rtc-bk-"))
        try:
            for day in ("20200101", "20200102", "20200103"):
                (target / f"RTC-{day}.zip").write_bytes(b"old")
            rc, out, err = self.cli("backup", "--to", str(target), "--keep", "2")
            self.assertEqual(rc, 0, err)
            self.assertLessEqual(len(self._zip_names(target)), 2, "超出 keep 的旧档案要被删掉")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_backup_failure_is_reported_not_swallowed(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        # 目标是一个「文件」而不是目录 → 建不出来，必须给退出码 3 而不是假装成功
        bad = self.ws / "state" / "not-a-dir"
        bad.write_text("x", encoding="utf-8")
        rc, out, err = self.cli("backup", "--to", str(bad))
        self.assertEqual(rc, 3, f"没有一份写成功就不能返回 0：{out} {err}")
        self.assertIn("失败", err)

    def test_no_config_means_no_backup_and_no_disk_scanning(self):
        """没有 rules/backup.json 时绝不自己扫盘：
        扫盘会在别人机器上往不认识的盘写字，测试也会污染真实磁盘。"""
        self.write_routine(base_routine())
        self.assertFalse((self.ws / "rules" / "backup.json").exists())
        rc, out, err = self.cli("plan", now="2026-11-16T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "", "没有备份配置时 plan 不该多说什么")
        self.assertFalse((self.ws / "state" / ".backup-last.json").exists(), "没配置就不该留下备份标记")

    def test_plan_runs_backup_once_per_day(self):
        """cron 每天只调 plan，所以备份必须挂在 plan 上，且一天只跑一次。"""
        target = Path(tempfile.mkdtemp(prefix="rtc-bk-"))
        try:
            (self.ws / "rules" / "backup.json").write_text(
                json.dumps({"targets": [str(target)], "keep": 30}), encoding="utf-8"
            )
            self.write_routine(base_routine())
            self.cli("plan", now="2026-11-16T08:00:00+08:00")
            self.assertEqual(len(self._zip_names(target)), 1, "plan 应当顺手备份一次")
            first = self._zip_names(target)[0]
            stamp = (target / first).stat().st_mtime_ns
            self.cli("plan", "--force", now="2026-11-16T12:00:00+08:00")
            self.assertEqual((target / first).stat().st_mtime_ns, stamp, "同一天不该备份第二次")
        finally:
            shutil.rmtree(target, ignore_errors=True)


class TestMetricsAutoSync(RtcCase):
    """metrics.db 以前是个摆设：命令在，但没有任何东西去调它。
    结果「一年后想统计」时库里一行都没有。现在 plan 每天顺手同步一次。"""

    def test_plan_builds_and_dumps_metrics(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")

        db = self.ws / "state" / "metrics.db"
        self.assertTrue(db.is_file(), "plan 没生成 metrics.db")
        import sqlite3

        con = sqlite3.connect(db)
        try:
            daily = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
            schedule = con.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        finally:
            con.close()
        self.assertGreater(daily, 0, "daily 表是空的")
        self.assertGreater(schedule, 0, "schedule 表是空的")

        dump = self.ws / "state" / "metrics.sql"
        self.assertTrue(dump.is_file(), "没有文本导出，git 里没法看出每天多了什么")
        text = dump.read_text(encoding="utf-8")
        self.assertIn("INSERT INTO", text, "导出里没有数据行")
        self.assertIn("daily", text)

    def test_dump_is_stable_when_nothing_changed(self):
        """没有新数据时导出内容要一致，否则 git 里天天一堆噪声提交。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        first = (self.ws / "state" / "metrics.sql").read_text(encoding="utf-8")
        self.cli("plan", "--force", now="2026-11-16T08:01:00+08:00")
        second = (self.ws / "state" / "metrics.sql").read_text(encoding="utf-8")
        self.assertEqual(first, second, "数据没变，导出却变了")


class TestGitArchive(RtcCase):
    """压缩包只防「盘坏」，不防「机器没了」：档案要能进私人 git 仓库。"""

    def _bare(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="rtc-bare-")) / "data.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(d)], check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, d.parent, True)
        return d

    def _config(self, bare: Path) -> Path:
        vault = self.ws.parent / "vault"
        (self.ws / "rules" / "backup.json").write_text(
            json.dumps(
                {
                    "targets": [],
                    "keep": 30,
                    "git": {"dir": str(vault), "remotes": [str(bare)], "branch": "main"},
                }
            ),
            encoding="utf-8",
        )
        return vault

    def test_commits_and_pushes_today_archive(self):
        bare = self._bare()
        self._config(bare)
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")

        rc, out, err = self.cli("gitarchive")
        self.assertEqual(rc, 0, f"{out} {err}")

        log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertIn("archive", log.stdout, "远端没有收到提交")
        tree = subprocess.run(
            ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertIn("rules/routines.json", tree.stdout)
        self.assertIn("state/alarms.json", tree.stdout)

        # 再跑一次：没有变化就不该再产生提交，也不该报错
        rc2, out2, _ = self.cli("gitarchive")
        self.assertEqual(rc2, 0)
        log2 = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            len(log2.stdout.strip().splitlines()),
            len(log.stdout.strip().splitlines()),
            "内容没变却又提交了一次",
        )

    def test_plan_drives_git_archive_daily(self):
        bare = self._bare()
        self._config(bare)
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")

        log = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertIn("archive", log.stdout, "plan 没把档案推进 git")

    def test_unpushed_remote_is_reported(self):
        """远端写错不能假装成功——否则用户以为有备份，其实一个字节都没出去。"""
        self._config(Path(tempfile.mkdtemp(prefix="rtc-nope-")) / "no-such-repo.git")
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T08:00:00+08:00")
        rc, out, err = self.cli("gitarchive")
        self.assertNotEqual(rc, 0, "推不上去还返回 0，等于假成功")
        self.assertIn("失败", err)


class TestOnceScriptSeals(RtcCase):
    """rtc_once.py 是我们自己的写入口，不能让自己的文件被报成「有人手改」。"""

    def test_once_seals_so_plan_does_not_cry_hand_edit(self):
        import hashlib

        self.write_routine(base_routine())
        once = Path(__file__).resolve().parents[1] / "rtc_once.py"
        proc = subprocess.run(
            [sys.executable, str(once), "15:00", "一次性-测试", "--workspace", str(self.ws)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

        routines = self.ws / "rules" / "routines.json"
        seal = json.loads((self.ws / "state" / "routine-seal.json").read_text(encoding="utf-8"))
        self.assertEqual(
            seal["digest"],
            hashlib.sha256(routines.read_bytes()).hexdigest(),
            "rtc_once 写完没盖章 → 下次 plan 会误报手改",
        )
        self.assertTrue((self.ws / "state" / "backups" / "routines.program-last.json").is_file())

        rc, out, err = self.cli("plan", now="2026-11-16T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("手改", out + err, "自己写的文件被当成人手改")


if __name__ == "__main__":
    unittest.main(verbosity=2)
