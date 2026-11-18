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
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
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
        # 专门挑周日：周一是「周报日」，plan 会额外打一段周报，静默断言就测不到空日本身了
        rc, out, err = self.cli("plan", now="2026-11-08T06:00:00+08:00")
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
        errs = list(
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
            ).iter_errors(bad)
        )
        self.assertTrue(errs, "oneOff=true 但 expectFires≠1 必须被契约拒绝")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_new_fields_accepted(self):
        import jsonschema

        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        good = base_routine()
        good["alarmWindows"][0].update(
            {"oneOff": True, "expectFires": 1, "validFrom": "2026-11-10", "validUntil": "2026-11-10"}
        )
        errs = list(
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
            ).iter_errors(good)
        )
        self.assertEqual(errs, [], f"合法配置不该被拒：{errs[:1]}")


class TestContractExamples(unittest.TestCase):
    """契约示例必须被「同一把尺子」判定（issue #3：统一校验口径）。

    examples/ 里是**模板**，填占位符之前不能通过校验；否则「通过校验」就没有意义。
    这里用 jsonschema Draft 2020-12 + format 断言，等价于 CI 与 README 里写的
    ajv-cli --spec=draft2020 -c ajv-formats。
    """

    CASES = {
        "alarms.example.json": "alarm-state.schema.json",
        "routines.example.json": "routine.schema.json",
        "tasks.example.json": "task.schema.json",
    }

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_examples_are_rejected_before_placeholders_are_filled(self):
        import jsonschema

        for example, schema_name in self.CASES.items():
            with self.subTest(example=example):
                schema = json.loads((REPO / "contract" / schema_name).read_text(encoding="utf-8"))
                data = json.loads((REPO / "contract" / "examples" / example).read_text(encoding="utf-8"))
                validator = jsonschema.Draft202012Validator(
                    schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
                )
                errs = list(validator.iter_errors(data))
                self.assertTrue(errs, f"{example} 含占位符，必须被 {schema_name} 拒绝")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "本解释器没有 jsonschema")
    def test_real_routine_from_cli_is_accepted(self):
        """代码生成的配置必须通过契约——两支尺子的另一端。"""
        import jsonschema

        schema = json.loads((REPO / "contract" / "routine.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
        )
        self.assertEqual(list(validator.iter_errors(base_routine())), [])


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
        self.assertEqual(
            self.state()["windows"][0]["checked"],
            sorted([rtc._item_id("电饭煲"), rtc._item_id("电脑")]),
            "勾选存稳定 id，不存序号",
        )

    def test_checked_survives_next_day(self):
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        self.cli("due", now="2026-05-12T20:00:00+08:00")
        self.cli("check", "--window", "pack", "--item", "电饭煲", now="2026-05-12T20:05:00+08:00")
        self.cli("plan", now="2026-05-13T06:00:00+08:00")
        w = self.state()["windows"][0]
        self.assertEqual(w["checked"], [rtc._item_id("电饭煲")], "清单不因日切重置")
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

    def test_undo_after_all_checked_restores_reminder(self):
        """撤销打勾不能留下「窗口已确认、清单没勾完」的矛盾状态（审计 #8）。"""
        self.cli("plan", now="2026-05-12T06:00:00+08:00")
        for i in range(1, 5):
            self.cli("check", "--window", "pack", "--item", str(i), now="2026-05-12T20:05:00+08:00")
        self.assertEqual(self.state()["windows"][0]["state"], "acked")
        rc, _, _ = self.cli(
            "check", "--window", "pack", "--item", "4", "--undo", now="2026-05-12T20:06:00+08:00"
        )
        self.assertEqual(rc, 0)
        w = self.state()["windows"][0]
        self.assertEqual(w["state"], "pending", "撤销后窗口要退回未完成")
        self.assertIsNone(w["ackedAt"])
        rc, out, _ = self.cli("due", now="2026-05-12T20:10:00+08:00")
        self.assertIn("还剩 1 项", out, "没勾完的项应当继续提醒")


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
        errs = list(
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
            ).iter_errors(self.state())
        )
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
    """锁协议：持久 sidecar + 操作系统持锁。

    审计发现 ① 的原始形态是「按 mtime 判陈旧、把锁抢过来接管」，重叠期会丢写入。
    本类锁定替换后的协议，逐条对应审计的六项针对性检查：

    - sidecar 常驻，进出临界区都不删（不再有「陈旧」这个概念）
    - mtime 再旧也不接管，第二个写入者必须等第一个真的退出
    - 跨进程互斥成立（不是同进程内的线程假象）
    - 持有者被强杀后立刻可获取，不需要等任何超时
    - 多写入者并发不丢更新
    - POSIX：sidecar 被删除/替换后能被 inode 守卫自检出来
    """

    def _lock_for(self, name):
        d = tempfile.mkdtemp(prefix="rtc-lock-")
        p = Path(d) / name
        return p, p.with_name(p.name + ".lock")

    def _spawn_holder(self, target, hold_s):
        """起一个子进程拿锁并保持 hold_s 秒，读到 HELD 才返回，保证锁确实已在别人手里。"""
        d = tempfile.mkdtemp(prefix="rtc-child-")
        script = Path(d) / "hold.py"
        script.write_text(
            "import pathlib, sys, time\n"
            f"sys.path.insert(0, {str(RTC.parent)!r})\n"
            "import rtc\n"
            "p = pathlib.Path(sys.argv[1])\n"
            "with rtc._exclusive_file_lock(p):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n",
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(script), str(target), str(hold_s)],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=ENV,
        )

        def cleanup():
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), "HELD", "子进程未能拿到锁")
        return child

    def test_sidecar_persists_after_release(self):
        """sidecar 常驻：进出临界区后它仍然在盘上，可重复获取。"""
        p, lock = self._lock_for("a.json")
        with rtc._exclusive_file_lock(p):
            self.assertTrue(lock.exists(), "进入临界区时 sidecar 必须存在")
        self.assertTrue(lock.exists(), "释放时不得删除 sidecar（删了就等于换协议）")
        with rtc._exclusive_file_lock(p):
            pass
        self.assertTrue(lock.exists())

    def test_no_stale_threshold_constant(self):
        """「按年龄判陈旧」的实现痕迹必须消失，否则两种协议会再次混在一起。"""
        self.assertFalse(hasattr(rtc, "_LOCK_STALE_S"), "陈旧阈值概念应已移除")
        self.assertFalse(hasattr(rtc, "_try_acquire_lock"), "按 mtime 接管的入口应已移除")

    def test_no_age_based_takeover(self):
        """sidecar 的 mtime 是一小时前，也不许接管：必须等持有者真的退出。"""
        import threading
        import time

        p, lock = self._lock_for("a.json")
        lock.write_bytes(b"")
        old = time.time() - 3600
        os.utime(str(lock), (old, old))
        entered = []

        def holder():
            with rtc._exclusive_file_lock(p):
                entered.append(time.monotonic())
                time.sleep(0.4)

        th = threading.Thread(target=holder)
        th.start()
        while not entered:
            time.sleep(0.01)
        t0 = time.monotonic()
        with rtc._exclusive_file_lock(p):
            waited = time.monotonic() - t0
        th.join()
        self.assertGreaterEqual(waited, 0.3, f"陈旧 sidecar 被提前接管了（只等了 {waited:.2f}s）")

    def test_exclusive_across_processes(self):
        """跨进程互斥：子进程持锁 0.5s，父进程必须等它退出才进得去。"""
        import time

        p, lock = self._lock_for("a.json")
        child = self._spawn_holder(p, 0.5)
        t0 = time.monotonic()
        with rtc._exclusive_file_lock(p):
            waited = time.monotonic() - t0
        child.wait(timeout=10)
        self.assertGreaterEqual(waited, 0.3, f"两个进程同时进入临界区（只等了 {waited:.2f}s）")
        self.assertTrue(lock.exists(), "子进程退出也不得删除 sidecar")

    def test_lock_released_when_holder_dies(self):
        """持有者被强杀 → 锁立刻释放，不依赖任何超时。"""
        import time

        p, lock = self._lock_for("a.json")
        child = self._spawn_holder(p, 60)
        child.kill()
        child.wait(timeout=10)
        t0 = time.monotonic()
        with rtc._exclusive_file_lock(p):
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0, f"持有者死亡后仍等了 {elapsed:.2f}s，说明还在按超时判死")

    def test_windows_sidecar_not_unlinkable_while_held(self):
        """Windows 的锁是强制锁：持锁期间别人删不掉 sidecar，旧写者只会空等。"""
        if os.name != "nt":
            self.skipTest("POSIX 上 sidecar 可被删除，由 inode 守卫兜底")
        p, lock = self._lock_for("a.json")
        with rtc._exclusive_file_lock(p), self.assertRaises(PermissionError):
            os.unlink(str(lock))

    def test_posix_inode_guard_detects_replaced_sidecar(self):
        """POSIX：flock 锁的是 inode，sidecar 被替换后必须能自检出来。"""
        if os.name == "nt":
            self.skipTest("Windows 持锁期间文件删不掉，此洞只在 POSIX 存在")
        p, lock = self._lock_for("a.json")
        lock.write_bytes(b"")
        fd = os.open(str(lock), os.O_RDWR)
        try:
            self.assertTrue(rtc._sidecar_is_our_inode(lock, fd))
            os.unlink(str(lock))  # 删掉旧 inode（fd 仍开着）
            lock.write_bytes(b"new")  # 同名重建，inode 已变
            self.assertFalse(
                rtc._sidecar_is_our_inode(lock, fd), "sidecar 被替换后必须判定为「不是我的 inode」"
            )
        finally:
            os.close(fd)

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

    def test_brief_lists_unsettled(self):
        """响过但没确认的必须点名，别让模型以为「没事了」。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:05:00+08:00")
        self.cli("due", now="2026-11-10T07:35:00+08:00")  # 过了 end，标 missed
        rc, out, _ = self.cli("brief", now="2026-11-10T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("欠着的：", out, out)
        self.assertIn("起床", out, out)
        self.assertIn("已过期", out, out)

    def test_brief_daily_accounting(self):
        """一行账：计划几条、响几次、确认几条、漏几条。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.cli("due", now="2026-11-10T07:05:00+08:00")
        self.cli("ack", "--window", "wake", "--text", "起了", now="2026-11-10T07:06:00+08:00")
        rc, out, _ = self.cli("brief", now="2026-11-10T08:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("今天的账：计划 1 条 · 响 1 次 · 确认 1 条 · 漏 0 条", out, out)
        self.assertNotIn("欠着的：", out, "已确认不该再列进欠着的")

    def test_brief_warns_one_off_only_timetable(self):
        """表里只剩一次性窗口时，简报第一行就得提醒：明天不会响。"""
        r = base_routine(
            alarmWindows=[
                {
                    "id": "once-2000",
                    "label": "回家",
                    "start": "20:00",
                    "end": "20:04",
                    "intervalMin": 5,
                    "expectFires": 1,
                    "ackHints": ["知道了"],
                    "oneOff": True,
                    "validFrom": "2026-11-10",
                    "validUntil": "2026-11-10",
                }
            ]
        )
        self.write_routine(r)
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, _ = self.cli("brief", now="2026-11-10T07:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("时刻表里只有一次性提醒", out, out)

    def test_plan_warns_when_only_one_offs(self):
        """表里只剩一次性提醒时，plan 的 stderr 要说一句，免得「计划成功」被当成「明天还会响」。"""
        self.write_routine(
            base_routine(
                alarmWindows=[
                    {
                        "id": "once-1300",
                        "label": "出门",
                        "start": "13:00",
                        "end": "13:04",
                        "intervalMin": 5,
                        "expectFires": 1,
                        "ackHints": ["知道了"],
                        "oneOff": True,
                        "validFrom": "2026-11-10",
                        "validUntil": "2026-11-10",
                    }
                ]
            )
        )
        rc, _, err = self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("时刻表里只有一次性提醒", err, err)

    def test_status_reports_timetable(self):
        """status 要能一眼看出「今天几个日常窗口」，JSON 与文字都要有。"""
        self.write_routine(
            base_routine(
                alarmWindows=[
                    {
                        "id": "once-1300",
                        "label": "出门",
                        "start": "13:00",
                        "end": "13:04",
                        "intervalMin": 5,
                        "expectFires": 1,
                        "ackHints": ["知道了"],
                        "oneOff": True,
                        "validFrom": "2026-11-10",
                        "validUntil": "2026-11-10",
                    }
                ]
            )
        )
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, err = self.cli("status", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["timetable"], {"total": 1, "recurring": 0, "oneOff": 1}, out)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("没有日常窗口", out, out)

    def _write_backup_mark(self, **fields):
        mark = self.ws / "state" / ".backup-last.json"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")

    def test_status_says_never_backed_up(self):
        """没备份过也要明说，不能留白让用户猜。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("尚未备份", out, out)
        rc, out, err = self.cli("status", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(json.loads(out)["backup"], out)

    def test_status_shows_last_backup(self):
        """备份成功一行读完：什么时候、几处、git 推没推、有没有错。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self._write_backup_mark(
            date="2026-11-10", at="2026-11-10T17:51:38+08:00", written=2, git=True, errors=[]
        )
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("本地 2 处 · git 已推送 · 最近无错误", out, out)
        self.assertIn("11-10 17:51", out, out)
        rc, out, err = self.cli("status", "--json")
        data = json.loads(out)["backup"]
        self.assertEqual(data["written"], 2, out)
        self.assertTrue(data["git"], out)

    def test_status_shows_backup_errors(self):
        """部分失败要写清哪一处、为什么，别只说「备份」两个字。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        self._write_backup_mark(
            date="2026-11-10",
            at="2026-11-10T17:51:38+08:00",
            written=1,
            git=False,
            errors=["E: 盘不可写"],
        )
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("本地 1 处 · git 未推送 · 错误 1：E: 盘不可写", out, out)

    def test_manual_backup_records_itself(self):
        """手动 backup 也要记账：15:09 跑完，status 不能还报凌晨那次。

        实机上就是这么被坑的：跑了一次 `rtc.py backup`，zip 写好了，
        status 那行却还停在 00:00，看着像没备份。
        """
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-10T06:00:00+08:00")
        out_dir = self.ws / "backup-target"
        rc, out, err = self.cli("backup", "--to", str(out_dir))
        self.assertEqual(rc, 0, err)
        self.assertTrue(list(out_dir.glob("RTC-*.zip")), err)
        mark = json.loads((self.ws / "state" / ".backup-last.json").read_text(encoding="utf-8"))
        self.assertEqual(mark["written"], 1, mark)
        self.assertIsNone(mark["git"], mark)
        self.assertEqual(date.today().isoformat(), mark["date"], mark)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("本地 1 处 · git 未跑", out, out)


class TestSimulate(RtcCase):
    """预演：只读地回答「那天会怎样」。

    硬约束只有一条：预演和真跑必须是同一套判定。所以断言方式不是「看起来对」，
    而是把 plan 真跑出来的窗口逐条拿来对比 id / 起止 / expectFires。
    """

    def test_matches_real_plan(self):
        self.write_routine(
            base_routine(
                alarmWindows=[
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
                        "id": "meds-am",
                        "label": "吃药（早）",
                        "start": "08:00",
                        "end": "08:04",
                        "intervalMin": 5,
                        "expectFires": 1,
                        "ackHints": ["吃了"],
                    },
                ]
            )
        )
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("simulate", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        real = {w["id"]: w for w in self.state()["windows"]}
        self.assertEqual(set(real), {"wake", "meds-am"}, out)
        self.assertIn("起床", out)
        self.assertIn("6 次", out)
        self.assertIn("预计响 7 次", out)
        rc, out, err = self.cli("simulate", "--date", "2026-11-17", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["date"], "2026-11-17")
        self.assertEqual(data["kind"], "workday")
        self.assertEqual(data["totalFires"], 7)
        for w in data["windows"]:
            self.assertEqual(w["start"], real[w["id"]]["start"], w)
            self.assertEqual(w["end"], real[w["id"]]["end"], w)
            self.assertEqual(w["expectFires"], real[w["id"]]["expectFires"], w)

    def test_one_off_only_on_its_day(self):
        self.write_routine(
            base_routine(
                alarmWindows=[
                    {
                        "id": "once-1525",
                        "label": "出门",
                        "start": "15:25",
                        "end": "15:29",
                        "intervalMin": 5,
                        "expectFires": 1,
                        "ackHints": ["知道了"],
                        "oneOff": True,
                        "validFrom": "2026-11-17",
                        "validUntil": "2026-11-17",
                    }
                ]
            )
        )
        rc, out, err = self.cli("simulate", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("出门", out)
        rc, out, err = self.cli("simulate", "--date", "2026-11-18")
        self.assertEqual(rc, 0, err)
        self.assertIn("不会响", out)
        self.assertIn("不在有效期", out)

    def test_disabled_window_is_listed_as_skipped(self):
        self.write_routine(
            base_routine(
                alarmWindows=[
                    {
                        "id": "meds-pm",
                        "label": "吃药（晚）",
                        "start": "19:00",
                        "end": "19:04",
                        "intervalMin": 5,
                        "expectFires": 1,
                        "ackHints": ["吃了"],
                        "disabled": True,
                    }
                ]
            )
        )
        rc, out, err = self.cli("simulate", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("已停用", out)

    def test_bad_date_exits_3_without_traceback(self):
        self.write_routine(base_routine())
        rc, out, err = self.cli("simulate", "--date", "2026-13-45")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("日期看不懂", err)
        self.assertNotIn("Traceback", out + err)

    def test_writes_nothing(self):
        """只读是硬约束：跑完 state/ 下不能有任何文件被碰过。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        before = {p.name: p.stat().st_mtime_ns for p in (self.ws / "state").iterdir()}
        rc, out, err = self.cli("simulate", "--date", "2026-11-18")
        self.assertEqual(rc, 0, err)
        after = {p.name: p.stat().st_mtime_ns for p in (self.ws / "state").iterdir()}
        self.assertEqual(before, after, "--simulate 动了 state/")

    def test_works_without_any_state(self):
        """还没跑过 plan 的新工作区也能预演——不该顺手创建 state 目录。"""
        self.write_routine(base_routine())
        state_dir = self.ws / "state"
        shutil.rmtree(state_dir)
        rc, out, err = self.cli("simulate", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertFalse(state_dir.exists(), "simulate 建了 state 目录")


class TestWeeklyReport(RtcCase):
    """周报：周一早上用 metrics 现成数据回答「上周靠谱吗」。

    这里直接往 metrics.db 里灌固定的一周数据——周报读的是库，不是现场状态，
    所以用真实表结构造数据比连跑七天 plan 更接近它实际面对的输入。
    """

    def seed(self, days: list[str], per_day: list[str], acked_every: int = 0, missed_every: int = 0):
        db = self.ws / "state" / "metrics.db"
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE IF NOT EXISTS daily (
                 date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
                 expect INTEGER NOT NULL, fired INTEGER NOT NULL,
                 acked_at TEXT, missed TEXT, PRIMARY KEY (date, window))"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS schedule (
                 date TEXT NOT NULL, window TEXT NOT NULL, label TEXT,
                 start TEXT, end TEXT, expect INTEGER NOT NULL,
                 one_off INTEGER NOT NULL DEFAULT 0,
                 valid_from TEXT, valid_until TEXT, PRIMARY KEY (date, window))"""
        )
        n = 0
        for day in days:
            for wid, label in per_day:
                acked = f"{day}T08:00:00+08:00" if acked_every and n % acked_every == 0 else None
                missed = "user_ack_timeout" if missed_every and n % missed_every == 0 else None
                con.execute(
                    "INSERT INTO daily VALUES (?,?,?,?,?,?,?)",
                    (day, wid, label, 6, 6, acked, missed),
                )
                con.execute(
                    "INSERT INTO schedule (date, window, label, start, end, expect, one_off)"
                    " VALUES (?,?,?,?,?,?,0)",
                    (day, wid, label, f"{day}T07:00:00+08:00", f"{day}T07:25:00+08:00", 6),
                )
                n += 1
        con.commit()
        con.close()

    def test_monday_plan_prints_weekly(self):
        self.write_routine(base_routine())
        labels = [
            ("wake", "起床"),
            ("meds-am", "吃药（早）"),
            ("meds-pm", "吃药（晚）"),
            ("brief-morning", "早报"),
            ("brief-follow", "跟进"),
            ("brief-night", "晚总结"),
        ]
        self.seed(
            [f"2026-11-{d:02d}" for d in range(9, 16)],
            labels,
            acked_every=0,
            missed_every=9,
        )
        # 再单独给 9 条确认，凑出 9/42 的确认率
        db = self.ws / "state" / "metrics.db"
        con = sqlite3.connect(db)
        for i in range(9):
            day = f"2026-11-{9 + i % 7:02d}"
            wid = labels[i % 3][0]
            con.execute(
                "UPDATE daily SET acked_at=? WHERE date=? AND window=?",
                (f"{day}T08:00:00+08:00", day, wid),
            )
        con.commit()
        con.close()
        rc, out, err = self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("📊 上周（11-09 ~ 11-15）", out)
        self.assertIn("提醒 42 次 · 确认 9 次（21%）· 漏响", out)
        self.assertIn("最常漏：", out)

    def test_only_monday(self):
        self.write_routine(base_routine())
        labels = [("wake", "起床")]
        self.seed(["2026-11-09", "2026-11-10", "2026-11-11"], labels)
        rc, out, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("📊", out)
        self.assertNotIn("上周", out)

    def test_weekly_once_a_week(self):
        self.write_routine(base_routine())
        labels = [("wake", "起床")]
        self.seed([f"2026-11-{d:02d}" for d in range(9, 16)], labels)
        rc, out, err = self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("📊", out)
        rc, out2, err = self.cli("plan", now="2026-11-16T07:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("📊", out2, out2)

    def test_too_few_records(self):
        self.write_routine(base_routine())
        self.seed(["2026-11-09", "2026-11-10"], [("wake", "起床")])
        rc, out, err = self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("上周记录太少", out)
        self.assertNotIn("确认率", out)
        self.assertNotIn("0%", out)

    def test_no_records_does_not_crash(self):
        """新工作区：plan 会先建出空的 metrics.db，周报只能走「没数据」分支。"""
        self.write_routine(base_routine())
        rc, out, err = self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("上周记录太少", out)
        self.assertIn("先看这周", out)
        self.assertNotIn("Traceback", out + err)


class TestPause(RtcCase):
    """休假模式：一句话停、一句话恢复，恢复时说清「跳过多少」。"""

    def test_pause_empties_today_and_stays_quiet(self):
        self.write_routine(base_routine())
        # 先让心跳存在，否则 plan 会额外打一条「调度器可能没跑过」的提醒，
        # 那行是真心跳告警，不是暂停造成的噪音
        (self.ws / "state" / "heartbeat.json").write_text(
            json.dumps({"at": "2026-11-17T08:55:00+08:00"}), encoding="utf-8"
        )
        rc, out, err = self.cli("pause", "--days", "3", now="2026-11-17T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["windows"], [], "暂停当天就必须收掉窗口")
        rc, out, err = self.cli("plan", "--force", now="2026-11-17T09:00:00+08:00")
        self.assertEqual(rc, 0, "暂停期 plan 不能因为「没有窗口」报错")
        self.assertEqual(out.strip(), "", "暂停期 plan 一个字都不该输出")
        self.assertEqual(self.state()["windows"], [])

    def test_due_silent_while_paused(self):
        self.write_routine(base_routine())
        self.cli("pause", "--days", "3", now="2026-11-17T08:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-18T07:05:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "", out)

    def test_auto_resume_on_until_day(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("pause", "--days", "2", now="2026-11-17T08:00:00+08:00")
        rc, out, err = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.state()["windows"]), 1, "until 当天应自动恢复")
        self.assertIn("暂停了 2 天", out, out)
        self.assertFalse((self.ws / "state" / "paused.json").exists())

    def test_resume_restores_windows_and_reports(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("pause", "--days", "2", now="2026-11-17T08:00:00+08:00")
        rc, out, err = self.cli("resume", now="2026-11-18T20:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("暂停了 2 天", out, out)
        self.assertIn("跳过 1 次提醒", out, out)
        self.assertFalse((self.ws / "state" / "paused.json").exists())
        rc, out, err = self.cli("plan", "--force", now="2026-11-18T21:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.state()["windows"]), 1)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("已暂停", out)

    def test_seal_untouched(self):
        """暂停是临时静音，不是手改时刻表——印章摘要必须不变。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        seal = self.ws / "state" / "routine-seal.json"
        before = seal.read_text(encoding="utf-8")
        self.cli("pause", "--days", "2", now="2026-11-17T08:00:00+08:00")
        self.cli("resume", now="2026-11-17T09:00:00+08:00")
        self.assertEqual(before, seal.read_text(encoding="utf-8"), "暂停动了印章")

    def test_corrupt_marker_treated_as_no_pause(self):
        self.write_routine(base_routine())
        (self.ws / "state" / "paused.json").write_text("{ 不是 json", encoding="utf-8")
        rc, out, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.state()["windows"]), 1, "标记坏了也要照常响")
        self.assertIn("当作没暂停", err)
        self.assertNotIn("Traceback", err)

    def test_status_reports_pause(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("pause", "--days", "3", now="2026-11-17T08:00:00+08:00")
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("已暂停至 2026-11-20", out)
        rc, out, err = self.cli("status", "--json")
        data = json.loads(out)
        self.assertEqual(data["paused"]["until"], "2026-11-20")


class TestTasksSnapshot(RtcCase):
    """tasks.md 的底稿：唯一没人守护的活文件，写坏了就真丢。"""

    def add_task(self, text: str):
        p = self.ws / "tasks.md"
        with p.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def snapshots(self):
        return sorted((self.ws / "state" / "backups").glob("tasks.*.md"))

    def test_three_edits_three_snapshots(self):
        self.add_task("- [ ] 第一条")
        for i in range(3):
            rc, out, err = self.cli("touch-tasks")
            self.assertEqual(rc, 0, err)
            self.assertIn("已留底稿", out)
            if i < 2:
                self.add_task(f"- [ ] 第{i + 2}条")
        self.assertEqual(len(self.snapshots()), 3, [p.name for p in self.snapshots()])

    def test_same_content_writes_nothing(self):
        self.add_task("- [ ] 只有这一条")
        self.cli("touch-tasks")
        before = len(self.snapshots())
        rc, out, err = self.cli("touch-tasks")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.snapshots()), before, "内容没变还写新底稿")
        self.assertIn("一样", out)

    def test_restore_after_truncation(self):
        self.write_routine(base_routine())
        self.add_task("- [ ] 交周报（截止 11-20）")
        self.add_task("- [ ] 买药")
        self.cli("touch-tasks")
        (self.ws / "tasks.md").write_text("", encoding="utf-8")
        latest = self.snapshots()[-1]
        (self.ws / "tasks.md").write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertIn("交周报", (self.ws / "tasks.md").read_text(encoding="utf-8"))
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)

    def test_keep_limit_prunes_oldest(self):
        (self.ws / "rules" / "backup.json").write_text(
            json.dumps({"targets": [], "keep": 2}), encoding="utf-8"
        )
        self.add_task("- [ ] a")
        self.cli("touch-tasks")
        for t in ("b", "c"):
            self.add_task(f"- [ ] {t}")
            self.cli("touch-tasks")
            import time as _t

            _t.sleep(1.1)  # 时间戳按秒，隔开一秒才能产生不同文件名
        self.assertEqual(len(self.snapshots()), 2, [p.name for p in self.snapshots()])
        self.assertIn("c", self.snapshots()[-1].read_text(encoding="utf-8"))

    def test_snapshots_go_into_zip(self):
        target = Path(tempfile.mkdtemp(prefix="rtc-taskzip-"))
        try:
            self.write_routine(base_routine())
            (self.ws / "rules" / "backup.json").write_text(
                json.dumps({"targets": [str(target)], "keep": 5}), encoding="utf-8"
            )
            self.add_task("- [ ] 会被打包")
            self.cli("touch-tasks")
            rc, out, err = self.cli("backup")
            self.assertEqual(rc, 0, err)
            zips = list(target.glob("RTC-*.zip"))
            self.assertTrue(zips, err)
            import zipfile

            with zipfile.ZipFile(zips[0]) as z:
                names = z.namelist()
            self.assertTrue(any(n.startswith("state/backups/tasks.") for n in names), names)
        finally:
            shutil.rmtree(target, ignore_errors=True)


class TestDeliveryReceipt(RtcCase):
    """投递回执：脚本看不见「你收到没有」，只能靠这一行账。"""

    def record(self, *args):
        return self.cli("delivery-record", *args)

    def test_records_and_summarises_a_day(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.record(
            "--window",
            "wake",
            "--target",
            "weixin",
            "--result",
            "ok",
            "--at",
            "2026-11-17T07:00:00+08:00",
        )
        self.assertEqual(rc, 0, err)
        rc, out, err = self.record(
            "--window",
            "wake",
            "--target",
            "weixin",
            "--result",
            "fail",
            "--error",
            "iLink session not ready ret=-2",
            "--at",
            "2026-11-17T07:05:00+08:00",
        )
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("delivery", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("投递 2 条：成功 1 · 失败 1", out)
        self.assertIn("iLink session not ready", out)
        rc, out, err = self.cli("delivery", "--date", "2026-11-17", "--json")
        data = json.loads(out)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["failures"][0]["window"], "wake")

    def test_no_log_says_so_instead_of_zero(self):
        rc, out, err = self.cli("delivery")
        self.assertEqual(rc, 4, "没回执要明确退出码，不能假装 0 条都成功")
        self.assertIn("还没有投递回执", err)

    def test_status_shows_failure(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.record(
            "--target", "weixin", "--result", "fail", "--error", "boom", "--at", "2026-11-17T09:00:00+08:00"
        )
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("投递  1 条 · 成功 0 · 失败 1", out)
        self.assertIn("boom", out)

    def test_bad_result_rejected(self):
        rc, out, err = self.cli("delivery-record", "--target", "weixin", "--result", "maybe")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("ok / fail", err)

    def test_broken_line_does_not_break_the_rest(self):
        path = self.ws / "state" / "delivery.log"
        path.write_text(
            '{"at":"2026-11-17T07:00:00+08:00","window":"wake","target":"x","result":"ok"}\n'
            "不是 json 的一行\n"
            '{"at":"2026-11-17T08:00:00+08:00","window":"wake","target":"x","result":"fail"}\n',
            encoding="utf-8",
        )
        rc, out, err = self.cli("delivery", "--date", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("投递 2 条", out)


class TestDigestWindow(RtcCase):
    """摘要窗口：文案由代码在到点那一刻用本地数据算，模型零参与。"""

    def digest_routine(self):
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "wake",
                "label": "起床",
                "start": "07:00",
                "end": "07:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["起了"],
            },
            {
                "id": "morning",
                "label": "早报",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
            },
            {
                "id": "noon",
                "label": "午间提醒",
                "start": "12:00",
                "end": "12:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
            },
            {
                "id": "follow",
                "label": "助理跟进",
                "start": "15:00",
                "end": "15:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "follow",
            },
            {
                "id": "evening",
                "label": "助理总结",
                "start": "19:00",
                "end": "19:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "summary",
            },
        ]
        return r

    def seed_day(self):
        """一天 5 个窗口：3 确认 / 1 漏响 / 1 未到（未到的是正在响的这一条）。

        15:00 的跟进在这里就响一次并确认——同一天再跑一次 due 不会再命中。
        """
        self.write_routine(self.digest_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T07:00:00+08:00")
        self.cli("ack", "--window", "wake", now="2026-11-17T07:01:00+08:00")
        self.cli("due", now="2026-11-17T09:00:00+08:00")
        self.cli("ack", "--window", "morning", now="2026-11-17T09:01:00+08:00")
        # 12:10 才跑：午间那条已过期未确认 → 判 missed
        self.cli("due", now="2026-11-17T12:10:00+08:00")
        follow = self.cli("due", now="2026-11-17T15:00:00+08:00")
        self.cli("ack", "--window", "follow", now="2026-11-17T15:01:00+08:00")
        return follow

    def test_follow_format_first_line(self):
        rc, out, err = self.seed_day()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "🔔 助理跟进 — 2026-11-17 15:00")
        self.assertIn("📋 今日待办：", out)
        self.assertIn("💬 进展：", out)

    def test_summary_selfcheck_numbers(self):
        self.seed_day()
        rc, out, err = self.cli("due", now="2026-11-17T19:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "📋 助理总结 — 2026-11-17")
        self.assertIn("今日闹钟：5 个窗口，响 4 次，确认 3 / 漏 1 / 未到 1", out)
        self.assertIn("📅 明日预告：", out)
        self.assertIn("12:00 午间提醒", out)  # 漏的那条要出现在「未完成」
        self.assertIn("（没确认）", out, "内部原因码要翻成人话，别把 window_expired 丢给用户")

    def test_label_with_its_own_icon_is_not_doubled(self):
        r = self.digest_routine()
        r["alarmWindows"][4]["label"] = "📋晚总结"  # 实机里的写法
        self.write_routine(r)
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T19:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "📋 晚总结 — 2026-11-17")

    def test_tasks_today_feeds_the_digest(self):
        (self.ws / "tasks.md").write_text(
            "# 任务\n\n## Today\n\n- [ ] 交周报\n- [x] 买咖啡\n\n## 待安排（没日期）\n- [ ] 换灯泡\n",
            encoding="utf-8",
        )
        rc, out, err = self.seed_day()
        self.assertEqual(rc, 0, err)
        self.assertIn("- 交周报", out)
        self.assertNotIn("换灯泡", out, "只列 Today，不该把待安排混进来")

    def test_digest_failure_degrades_to_plain_reminder(self):
        (self.ws / "tasks.md").mkdir()  # 读不动 → 摘要算不出来
        self.write_routine(self.digest_routine())
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T19:00:00+08:00")
        self.assertEqual(rc, 0, "算不出摘要也必须照常响")
        self.assertIn("助理总结 —— 19:00", out)
        self.assertIn("digest: 摘要生成失败", err)

    def test_same_data_same_text(self):
        self.seed_day()
        first = self.cli("due", now="2026-11-17T19:00:00+08:00")[1]
        self.setUp()
        self.seed_day()
        second = self.cli("due", now="2026-11-17T19:00:00+08:00")[1]
        self.assertEqual(first, second, "同一份数据必须算出同一段文字")


class TestHistoryDB(RtcCase):
    """#9 Phase 1：state/history.db 是执行历史的唯一真相源。

    快照（state/schedule/*.json）继续生成，但它只是一份人读的副本；
    删掉整个目录，历史照样查得到——这条是 issue #9 §4 的验收条款 1。
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
                "id": "pack",
                "label": "清点",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "checklist": ["电饭煲", "电脑"],
            },
        ]
        self.write_routine(r)

    def q(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(self.ws / "state" / "history.db")
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_plan_creates_windows_daily_and_checklist(self):
        rc, _, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rows = self.q("SELECT window_id FROM windows ORDER BY window_id")
        self.assertEqual([r[0] for r in rows], ["pack", "wake"])
        daily = self.q("SELECT date, window_id, expect, state FROM daily ORDER BY window_id")
        self.assertEqual([(d, w) for d, w, *_ in daily], [("2026-11-17", "pack"), ("2026-11-17", "wake")])
        items = self.q("SELECT item_label, ordinal, checked_at FROM checklist ORDER BY ordinal")
        self.assertEqual([(label, ordinal) for label, ordinal, _ in items], [("电饭煲", 1), ("电脑", 2)])

    def test_history_still_answers_after_schedule_dir_is_deleted(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T07:00:00+08:00")
        rc, _, err = self.cli("ack", now="2026-11-17T07:01:00+08:00")
        self.assertEqual(rc, 0, err)
        shutil.rmtree(self.ws / "state" / "schedule", ignore_errors=True)
        rc, out, err = self.cli("history", "2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("已确认", out, out)

    def test_ack_writes_daily_state_and_event(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T07:00:00+08:00")
        self.cli("ack", "--text", "起了", now="2026-11-17T07:01:00+08:00")
        state = self.q("SELECT state, fired, acked_at FROM daily WHERE window_id='wake'")[0]
        self.assertEqual(state[0], "acked")
        self.assertEqual(state[1], 1)
        self.assertIsNotNone(state[2])
        kinds = [r[0] for r in self.q("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["plan", "ack"])

    def test_check_writes_checklist_state_and_event(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, _, err = self.cli("check", "--window", "pack", "--item", "电饭煲")
        self.assertEqual(rc, 0, err)
        checked = self.q("SELECT checked_at FROM checklist WHERE window_id='pack' AND item_label='电饭煲'")[
            0
        ][0]
        self.assertEqual(checked, "2026-11-17")
        ev = self.q("SELECT window_id, item_id, kind FROM events WHERE kind='check'")[0]
        self.assertEqual(ev[0], "pack")
        self.assertEqual(ev[2], "check")

    def test_postpone_writes_event(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, _, err = self.cli("postpone", "--window", "wake", "--days", "1", now="2026-11-17T07:10:00+08:00")
        self.assertEqual(rc, 0, err)
        ev = self.q("SELECT window_id, kind FROM events WHERE kind='postpone'")
        self.assertEqual(ev[0], ("wake", "postpone"))

    def test_cancel_and_disable_write_events(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("cancel", "--window", "wake", now="2026-11-17T07:10:00+08:00")
        kinds = [r[0] for r in self.q("SELECT kind FROM events ORDER BY id")]
        self.assertIn("cancel", kinds)

    def test_stats_and_range(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T07:00:00+08:00")
        self.cli("ack", now="2026-11-17T07:01:00+08:00")
        rc, out, err = self.cli("stats", "--json")
        self.assertEqual(rc, 0, err)
        rows = json.loads(out)
        self.assertEqual({r["grp"] for r in rows}, {"wake", "pack"})
        self.assertTrue(any(r["acked"] == 1 for r in rows), out)
        rc, out, err = self.cli("history", "--range", "2026-11-17..2026-11-17")
        self.assertEqual(rc, 0, err)
        self.assertIn("2026-11-17", out)

    def test_backfill_pulls_old_snapshots_into_db(self):
        snap = self.ws / "state" / "schedule"
        snap.mkdir(parents=True)
        (snap / "2026-11-16.json").write_text(
            json.dumps(
                {
                    "date": "2026-11-16",
                    "planKind": "workday",
                    "results": {
                        "wake": {"state": "acked", "fires": 2, "ackedAt": "2026-11-16T07:01:00+08:00"}
                    },
                    "windows": [
                        {
                            "id": "wake",
                            "label": "起床",
                            "start": "2026-11-16T07:00:00+08:00",
                            "end": "2026-11-16T07:25:00+08:00",
                            "intervalMin": 5,
                            "expectFires": 6,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rc, _, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        days = [r[0] for r in self.q("SELECT DISTINCT date FROM daily ORDER BY date")]
        self.assertIn("2026-11-16", days)
        rc, out, _ = self.cli("history", "2026-11-16")
        self.assertEqual(rc, 0, out)
        self.assertIn("已确认", out)


class TestStableIds(RtcCase):
    """#12 Phase 2：清单项有跨日不变的稳定 id，序号只用于显示。"""

    def pack_routine(self, items) -> dict:
        r = base_routine()
        r["id"] = "main"
        r["alarmWindows"] = [
            {
                "id": "pack",
                "label": "清点并带走",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "checklist": items,
            }
        ]
        return r

    def ids(self) -> list[str]:
        return [it["id"] for it in self.state()["windows"][0]["checklist"]]

    def test_items_get_stable_ids(self):
        self.write_routine(self.pack_routine(["电饭煲", "电脑"]))
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        day1 = self.ids()
        self.assertEqual(day1, [rtc._item_id("电饭煲"), rtc._item_id("电脑")])
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(self.ids(), day1, "同一份清单第二天必须还是同一批 id")

    def test_explicit_id_survives_relabel(self):
        self.write_routine(self.pack_routine([{"id": "rice-cooker", "label": "电饭煲"}]))
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(self.ids(), ["rice-cooker"])
        self.write_routine(self.pack_routine([{"id": "rice-cooker", "label": "电饭锅（新的）"}]))
        self.cli("plan", "--force", now="2026-11-17T06:05:00+08:00")
        self.assertEqual(self.ids(), ["rice-cooker"], "改了文案，id 不能变")

    def test_check_by_index_maps_to_stable_id(self):
        self.write_routine(self.pack_routine(["电饭煲", "电脑", "被子枕头"]))
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, _, err = self.cli("check", "--window", "pack", "--item", "2", now="2026-11-17T20:05:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["windows"][0]["checked"], [rtc._item_id("电脑")])

    def test_removed_item_is_orphaned_not_silently_dropped(self):
        self.write_routine(self.pack_routine(["电饭煲", "电脑"]))
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("check", "--window", "pack", "--item", "电饭煲", now="2026-11-17T20:05:00+08:00")
        self.write_routine(self.pack_routine(["电脑"]))
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["windows"][0]["checked"], [])
        con = sqlite3.connect(self.ws / "state" / "history.db")
        try:
            rows = con.execute("SELECT kind, item_id FROM events WHERE kind='orphaned'").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("orphaned", rtc._item_id("电饭煲"))])

    def test_routine_id_is_recorded(self):
        self.write_routine(self.pack_routine(["电饭煲"]))
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        con = sqlite3.connect(self.ws / "state" / "history.db")
        try:
            rows = con.execute("SELECT DISTINCT routine_id FROM windows").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("main",)])


class TestScheduleTypes(RtcCase):
    """#9 §10：类型显式化 + 倒计时「最后一天 → 次日复盘 → 永停」。"""

    def countdown(self, **over) -> dict:
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "event",
                "label": "限时活动",
                "start": "20:00",
                "end": "20:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "validFrom": "2026-01-01",
                "dueDate": "2026-01-15",
                **over,
            }
        ]
        return r

    def test_three_types_are_explicit(self):
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
                "id": "once",
                "label": "一次性",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-17",
                "validUntil": "2026-11-17",
            },
            {
                "id": "appt",
                "label": "预约",
                "start": "10:00",
                "end": "10:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": "2026-11-17",
                "validUntil": "2026-11-17",
                "dueDate": "2026-11-17",
            },
        ]
        self.write_routine(r)
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        types = {w["id"]: w["type"] for w in self.state()["windows"]}
        self.assertEqual(types, {"wake": "fixed", "once": "oneoff", "appt": "appointment"})

    def test_last_day_then_review_then_gone(self):
        self.write_routine(self.countdown())
        self.cli("plan", now="2026-01-15T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-01-15T20:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("最后一天", out)
        self.cli("plan", now="2026-01-16T06:00:00+08:00")
        w = self.state()["windows"][0]
        self.assertTrue(w["review"], "截止日次日是复盘日")
        self.assertIn("复盘", w["label"])
        rc, out, _ = self.cli("due", now="2026-01-16T20:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("复盘", out)
        self.cli("plan", now="2026-01-17T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [], "复盘日之后永不重生")

    def test_cancelled_countdown_has_no_review(self):
        self.write_routine(self.countdown())
        self.cli("plan", now="2026-01-14T06:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "event", now="2026-01-14T08:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.cli("plan", now="2026-01-16T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [], "取消了就整段停，复盘日也不响")


class TestOneOffNoRegen(RtcCase):
    """#12 Phase 4：一次性只活在指定那一天，跨天不重生（I1 / I3）。"""

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
                "validFrom": "2026-11-17",
                "validUntil": "2026-11-17",
            }
        ]
        self.write_routine(r)

    def test_fires_on_its_day(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-17T20:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("洗保温杯", out)

    def test_unacked_does_not_regenerate_next_day(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T20:00:00+08:00")  # 响了，但没确认
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [], "没确认也不许次日重生")
        rc, out, _ = self.cli("due", now="2026-11-18T20:00:00+08:00")
        self.assertEqual(out, "")
        routine = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        self.assertNotIn("thermos", [w["id"] for w in routine["alarmWindows"]], "过期的一次性条目要清理掉")

    def test_acked_also_gone_next_day(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T20:00:00+08:00")
        self.cli("ack", "--window", "thermos", "--text", "好了", now="2026-11-17T20:05:00+08:00")
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(self.state().get("windows"), [])

    def test_history_keeps_the_record(self):
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli("due", now="2026-11-17T20:00:00+08:00")
        self.cli("ack", "--window", "thermos", "--text", "好了", now="2026-11-17T20:05:00+08:00")
        rc, out, _ = self.cli("history", "--range", "2026-11-17..2026-11-17")
        self.assertEqual(rc, 0, "响过的事要查得到，不随窗口清理消失")
        self.assertIn("2026-11-17 1 项", out)
        _, js, _ = self.cli("history", "--range", "2026-11-17..2026-11-17", "--json")
        self.assertIn("洗保温杯", js)


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
        rc, out, err = self.cli("plan", now="2026-11-17T08:00:00+08:00")
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


class TestHeartbeatOk(RtcCase):
    """心跳拆分（issue #25）：attemptAt 证明调过，lastOkAt 证明判定完整走完。"""

    def hb(self) -> dict:
        p = self.ws / "state" / "heartbeat.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def test_successful_due_writes_both_timestamps(self):
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        rc, _, err = self.cli("due", now="2026-11-16T07:02:00+08:00")
        self.assertEqual(rc, 0, err)
        hb = self.hb()
        self.assertIn("attemptAt", hb)
        self.assertEqual(hb["lastOkAt"], "2026-11-16T07:02:00+08:00")
        self.assertEqual(hb["at"], hb["lastOkAt"], "旧字段 at 应与 lastOkAt 一致，兼容旧读者")

    def test_silent_due_still_marks_ok(self):
        """判为静默（无命中窗口）也算判定完整走完，必须推进 lastOkAt。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        rc, out, _ = self.cli("due", now="2026-11-16T09:00:00+08:00")  # wake 窗口早已过
        self.assertEqual(rc, 0)
        hb = self.hb()
        self.assertEqual(hb["lastOkAt"], "2026-11-16T09:00:00+08:00")

    def test_conflict_due_still_marks_ok(self):
        """写入冲突时 due 静默退 0，也是完整判定，lastOkAt 应推进。"""
        import rtc as rtcmod

        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        real = rtcmod.atomic_write

        def boom(*a, **k):
            raise rtcmod.Conflict("test conflict")

        rtcmod.atomic_write = boom
        try:
            # 进程级调用替换不到，改用同进程直接调 cmd_due
            rtcmod.set_workspace(self.ws)
            rc = rtcmod.cmd_due(argparse.Namespace(now="2026-11-16T07:02:00+08:00"))
        finally:
            rtcmod.atomic_write = real
        self.assertEqual(rc, 0)
        self.assertEqual(self.hb()["lastOkAt"], "2026-11-16T07:02:00+08:00")

    def test_broken_state_marks_only_attempt(self):
        """状态文件损坏 → due 退 3，只留 attemptAt，lastOkAt 不推进。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.cli("due", now="2026-11-16T07:02:00+08:00")  # 先留一个成功的 lastOkAt
        good = self.hb()["lastOkAt"]

        (self.ws / "state" / "alarms.json").write_text("{坏掉的 JSON", encoding="utf-8")
        rc, _, err = self.cli("due", now="2026-11-16T07:07:00+08:00")
        self.assertEqual(rc, 3, err)
        hb = self.hb()
        self.assertEqual(hb["attemptAt"], "2026-11-16T07:07:00+08:00", "失败也要留下被调用的痕迹")
        self.assertEqual(hb["lastOkAt"], good, "失败路径绝不推进 lastOkAt")

    def test_self_check_warns_when_attempt_but_never_ok(self):
        """只有 attempt、从无 lastOkAt：自检要报「没有成功判定」。"""
        self.write_routine(base_routine())
        hbpath = self.ws / "state" / "heartbeat.json"
        hbpath.write_text(
            json.dumps({"attemptAt": "2026-11-16T07:02:00+08:00", "lastOkAt": None, "at": None}),
            encoding="utf-8",
        )
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        rc, out, _ = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertIn("没有成功判定心跳", out)

    def test_self_check_silence_on_healthy_day(self):
        """正常成功判定后，次日自检不该报心跳异常。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        self.cli("due", now="2026-11-16T07:02:00+08:00")
        rc, out, _ = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertNotIn("心跳", out)
        self.assertNotIn("判定完整走完", out)

    def test_legacy_heartbeat_at_field(self):
        """旧格式只有 at 的心跳文件仍能被 self_check 识别为一次成功判定。"""
        self.write_routine(base_routine())
        (self.ws / "state" / "heartbeat.json").write_text(
            json.dumps({"at": "2026-11-16T08:55:00+08:00"}), encoding="utf-8"
        )
        rc, out, _ = self.cli("plan", now="2026-11-16T09:00:00+08:00")
        self.assertNotIn("心跳", out, "旧格式 at 应被当作成功判定")


class TestMorningReport(RtcCase):
    """单窗口模式（format=report）：due 直接出稿，模型文件优先，读不到就静态兜底。"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "brief-morning",
                "label": "☀️早报",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "report",
            }
        ]
        self.write_routine(r)
        self.reports = self.ws / "state" / "reports"
        self.reports.mkdir(parents=True, exist_ok=True)

    def write_report(self, day: str, text: str):
        (self.reports / f"{day}.md").write_text(text, encoding="utf-8")

    def test_report_window_uses_model_file(self):
        """有 reports 文件时，9:00 窗口原文输出模型写的内容。"""
        body = (
            "📊 昨日汇报 — 2026-11-16\n"
            "✅ 已完成：\n- 跑了 3 公里\n"
            "❌ 未完成：\n- 无\n"
            "📝 备注：\n- 下午去医院\n"
            "💬 遗留建议：\n- 无"
        )
        self.write_report("2026-11-17", body)
        rc, out, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("due", now="2026-11-17T09:01:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("跑了 3 公里", out)
        self.assertIn("下午去医院", out)

    def test_report_window_falls_back_to_static(self):
        """没有 reports 文件时，9:00 窗口必须发静态汇报，不能空着不响。"""
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T09:01:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("📊 昨日汇报 — 2026-11-16", out)
        self.assertIn("✅ 已完成", out)
        self.assertIn("📝 备注", out)

    def test_empty_report_file_falls_back(self):
        """模型写了空文件 = 没写成功，仍要静态兜底。"""
        self.write_report("2026-11-17", "   \n  ")
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T09:01:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("📊 昨日汇报 — 2026-11-16", out)

    def test_invalid_utf8_report_does_not_block_other_windows(self):
        """乱码文件只让这一窗退回静态，不能让整天的提醒全挂（审计 2026-11）。"""
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
                "id": "brief-morning",
                "label": "☀️早报",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "report",
            },
            {
                "id": "tea",
                "label": "泡茶",
                "start": "20:00",
                "end": "20:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
            },
        ]
        self.write_routine(r)
        (self.reports / "2026-11-17.md").write_bytes(b"\xff\xfe\x00bad")
        rc, out, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("due", now="2026-11-17T09:01:00+08:00")
        self.assertEqual(rc, 0, f"乱码不该让 due 退出非 0：{err}")
        self.assertIn("📊 昨日汇报 — 2026-11-16", out, "读不动就发静态，不能失声")
        rc, out, err = self.cli("due", now="2026-11-17T20:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("泡茶", out, "其它窗口照常响")


class TestMorningFallback(RtcCase):
    """双窗口模式（format=report-fallback）：只有「送达回执」能压住兜底。"""

    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "brief-morning-fallback",
                "label": "早报兜底",
                "start": "09:10",
                "end": "09:14",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "report-fallback",
            }
        ]
        self.write_routine(r)
        self.reports = self.ws / "state" / "reports"
        self.reports.mkdir(parents=True, exist_ok=True)

    def write_report(self, day: str, text: str):
        (self.reports / f"{day}.md").write_text(text, encoding="utf-8")

    def test_fallback_silent_only_with_delivery_receipt(self):
        """回执 result=ok 才是「已送达」；文件存在不算。"""
        self.write_report("2026-11-17", "📊 昨日汇报 — 2026-11-16\n模型写的")
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli(
            "delivery-record",
            "--window",
            "report",
            "--target",
            "hermes",
            "--result",
            "ok",
            "--at",
            "2026-11-17T09:05:00+08:00",
        )
        rc, out, err = self.cli("due", now="2026-11-17T09:11:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "", f"有回执就不该再发：{out}")
        self.assertEqual(self.window("brief-morning-fallback")["state"], "skipped")
        self.assertEqual(
            self.window("brief-morning-fallback")["missedReason"],
            "covered_by_receipt",
            "压住兜底要留下原因：是「已送达所以没发」，不是「漏响」",
        )

    def test_fallback_resends_report_file_without_receipt(self):
        """写好了但没送达（无回执）→ 兜底要把文件内容补发出去，而不是静默判 delivered。"""
        self.write_report("2026-11-17", "📊 昨日汇报 — 2026-11-16\n模型写的")
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T09:11:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("模型写的", out, "没回执就必须补发，内容用模型写的正文")
        self.assertFalse((self.ws / "state" / "delivery.log").exists())

    def test_fallback_static_when_nothing_written(self):
        """模型没写 → 静态汇报，保证有声音。"""
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        rc, out, err = self.cli("due", now="2026-11-17T09:11:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("📊 昨日汇报 — 2026-11-16", out)
        self.assertEqual(self.window("brief-morning-fallback")["fireCount"], 1)

    def test_fallback_expired_with_receipt_is_skipped_not_missed(self):
        """过了点的兜底窗口：有回执 = 无需再响（skipped），不是漏响（missed）。"""
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.cli(
            "delivery-record",
            "--window",
            "report",
            "--target",
            "hermes",
            "--result",
            "ok",
            "--at",
            "2026-11-17T09:05:00+08:00",
        )
        rc, out, err = self.cli("due", now="2026-11-17T12:00:00+08:00")
        self.assertEqual(rc, 0, err)
        w = self.window("brief-morning-fallback")
        self.assertEqual(w["state"], "skipped")
        self.assertEqual(w["missedReason"], "covered_by_receipt")
        self.assertEqual(out.strip(), "")

    def test_fallback_not_counted_in_tally(self):
        """兜底窗口是技术守卫，不该出现在「今日闹钟 N 个窗口」里。"""
        self.cli("plan", now="2026-11-17T06:00:00+08:00")
        from rtc_brief import _window_tally

        tally = _window_tally(self.state())
        self.assertEqual(tally["windows"], 0)

    def test_both_report_formats_are_rejected(self):
        """两种输出方式同时启用会让同一份汇报发两次——plan 直接拒绝。"""
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "brief-morning",
                "label": "早报",
                "start": "09:00",
                "end": "09:04",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "report",
            },
            {
                "id": "brief-morning-fallback",
                "label": "早报兜底",
                "start": "09:10",
                "end": "09:14",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["知道了"],
                "format": "report-fallback",
            },
        ]
        self.write_routine(r)
        rc, out, err = self.cli("plan", now="2026-11-17T06:00:00+08:00")
        self.assertEqual(rc, 3, "同时配置两种早报输出方式必须被拒")
        self.assertIn("不能同时配置", err)
        self.assertEqual(out, "")


class TestReportMaterial(RtcCase):
    """早报素材脚本（rtc_report.py）：给模型喂料，只读不写。"""

    def run_material(self, day="2026-11-17"):
        script = Path(__file__).resolve().parents[1] / "rtc_report.py"
        p = subprocess.run(
            [sys.executable, str(script), "--workspace", str(self.ws), "--date", day],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, p.stdout, p.stderr

    def test_material_contains_three_sources(self):
        self.write_routine(base_routine())
        (self.ws / "tasks.md").write_text(
            "# tasks.md\n\n## Today\n\n### 我\n\n- [ ] 写周报\n- [x] 已完成的事\n",
            encoding="utf-8",
        )
        notes = self.ws / "state" / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "2026-11-16.md").write_text("- 下午去医院了\n", encoding="utf-8")
        self.cli("plan", now="2026-11-16T06:00:00+08:00")

        rc, out, err = self.run_material()
        self.assertEqual(rc, 0, err)
        self.assertIn("写周报", out, "今日待办要进素材")
        self.assertIn("下午去医院了", out, "用户口述要进素材")
        self.assertIn("state/reports/2026-11-17.md", out, "要告诉模型写到哪")

    def test_material_missing_files_is_not_error(self):
        """没有 tasks.md / notes 也不能报错——素材可以缺，早报不能因此不发。"""
        self.write_routine(base_routine())
        rc, out, err = self.run_material()
        self.assertEqual(rc, 0, err)
        self.assertIn("早报素材", out)
        self.assertNotIn("Traceback", out + err)

    def test_material_never_writes(self):
        """素材脚本只读：跑完不能在 state/ 里留下 reports/notes。"""
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-16T06:00:00+08:00")
        before = {p.name for p in (self.ws / "state").iterdir()}
        self.run_material()
        after = {p.name for p in (self.ws / "state").iterdir()}
        self.assertEqual(before, after, f"素材脚本不该写文件，却多了：{after - before}")

    def test_material_does_not_repair_broken_routine(self):
        """坏时刻表只报错，不顺手恢复：修复是 plan / 用户命令的职责（审计 2026-11）。"""
        self.write_routine(base_routine())
        bdir = self.ws / "state" / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "routines.program-last.json").write_text(
            json.dumps(base_routine(), ensure_ascii=False), encoding="utf-8"
        )
        routine = self.ws / "rules" / "routines.json"
        bad = "{ 这不是 JSON"
        routine.write_text(bad, encoding="utf-8")

        rc, out, err = self.run_material()
        self.assertEqual(rc, 3, "坏时刻表要报错退出")
        self.assertIn("校验失败", err)
        self.assertEqual(routine.read_text(encoding="utf-8"), bad, "素材脚本绝不能改写时刻表")
        self.assertEqual(
            [p.name for p in bdir.iterdir() if p.name.startswith("routines.corrupt-")],
            [],
            "素材脚本不该留下「我把坏文件备份了」的痕迹",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
