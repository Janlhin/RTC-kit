#!/usr/bin/env python3
"""rtc_once.py（一次性提醒入口）的边界与分派用例。

覆盖：plan/due 互斥（只登记 + 跑 plan --force，绝不跑 due）、参数与
文件缺失的拒绝分支、重复窗口、坏账本、plan 失败回滚、既有运行时字段合并。
用子进程跑真实 CLI；只有「plan 失败回滚」一条在进程内替身 subprocess。

运行：
    python -m unittest code.tests.test_once_entry -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import date
from unittest.mock import patch

import rtc_once as once
from test_rtc import ENV, REPO, RtcCase, base_routine

ONCE = REPO / "code" / "rtc_once.py"


class TestOnceEntry(RtcCase):
    """rtc_once.py 写入口：登记一条 oneOff，随后只跑 plan，不碰 due。"""

    def run_once(self, *argv: str):
        cmd = [sys.executable, str(ONCE), *argv, "--workspace", str(self.ws)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=ENV,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def routine_windows(self) -> list[dict]:
        routines = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        return routines["alarmWindows"]

    def test_registers_oneoff_and_never_runs_due(self):
        """成功路径：写 oneOff 窗口、跑 plan --force；不触发 due，响铃计数不涨。"""
        self.write_routine(base_routine())
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        before = self.window("wake")

        today = date.today().isoformat()
        wid = f"once-1500-{date.today().strftime('%Y%m%d')}"
        rc, out, err = self.run_once("15:00", "寄材料", "--ack", "知道了")
        self.assertEqual(rc, 0, err)
        self.assertIn("已登记", out)
        self.assertIn(wid, out)
        self.assertIn("不会手动触发 due", out, "入口必须声明不跑 due")

        window = next(w for w in self.routine_windows() if w["id"] == wid)
        self.assertTrue(window["oneOff"])
        self.assertEqual(window["expectFires"], 1)
        self.assertEqual(window["ackHints"], ["知道了"])
        self.assertEqual((window["validFrom"], window["validUntil"]), (today, today))

        after = self.window("wake")
        self.assertEqual(after.get("fireCount"), before.get("fireCount"), "跑了 due 才会涨计数")
        self.assertEqual(after.get("state"), before.get("state"), "入口不许推进响铃状态")

    def test_bad_time_rejected(self):
        """时刻不是 24 小时制 HH:MM：拒绝，退出码 2，时刻表不动。"""
        self.write_routine(base_routine())
        raw = (self.ws / "rules" / "routines.json").read_bytes()
        for bad in ("25:00", "11:61", "abc"):
            with self.subTest(bad=bad):
                rc, _, err = self.run_once(bad, "测试")
                self.assertEqual(rc, 2)
                self.assertIn("24 小时制", err)
                self.assertEqual((self.ws / "rules" / "routines.json").read_bytes(), raw)

    def test_missing_routine_rejected(self):
        """时刻表文件不存在：拒绝，退出码 2。"""
        rc, _, err = self.run_once("15:00", "测试")
        self.assertEqual(rc, 2)
        self.assertIn("找不到时刻表", err)

    def test_window_min_out_of_range_rejected(self):
        """--window-min 必须在 1..30 之间。"""
        self.write_routine(base_routine())
        for bad in ("0", "31"):
            with self.subTest(bad=bad):
                rc, _, err = self.run_once("15:00", "测试", "--window-min", bad)
                self.assertEqual(rc, 2)
                self.assertIn("window-min", err)

    def test_duplicate_window_rejected(self):
        """同一时刻同一日期的窗口已存在：拒绝，不重复追加。"""
        routine = base_routine()
        routine["alarmWindows"].append(
            {
                "id": "once-1500-20261118",
                "label": "旧提醒",
                "start": "15:00",
                "end": "15:04",
                "intervalMin": 5,
                "expectFires": 1,
                "oneOff": True,
                "ackHints": ["知道了"],
                "validFrom": "2026-11-18",
                "validUntil": "2026-11-18",
            }
        )
        self.write_routine(routine)
        rc, _, err = self.run_once("15:00", "重复", "--date", "2026-11-18")
        self.assertEqual(rc, 2)
        self.assertIn("已存在", err)
        self.assertEqual(len(self.routine_windows()), 2, "重复窗口被追加了")

    def test_malformed_ledger_is_read_as_empty_then_plan_rejects(self):
        """坏账本：入口读旧状态失败按空处理；子进程 plan 不认坏账本，回滚时刻表。"""
        self.write_routine(base_routine())
        raw = (self.ws / "rules" / "routines.json").read_bytes()
        (self.ws / "state" / "alarms.json").write_text("{不是 JSON", encoding="utf-8")
        rc, _, err = self.run_once("15:00", "坏账本", "--date", "2026-11-18")
        self.assertEqual(rc, 2)
        self.assertIn("回滚", err)
        self.assertEqual((self.ws / "rules" / "routines.json").read_bytes(), raw, "坏账本场景回滚不彻底")

    def test_plan_failure_rolls_back_routine(self):
        """plan --force 失败：回滚时刻表，退出码 2，不留脏配置。"""
        self.write_routine(base_routine())
        raw = (self.ws / "rules" / "routines.json").read_bytes()
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="plan boom")
        argv = [str(ONCE), "15:00", "测试", "--workspace", str(self.ws)]
        with patch.object(once.subprocess, "run", return_value=fake), patch.object(sys, "argv", argv):
            rc = once.main()
        self.assertEqual(rc, 2)
        self.assertEqual((self.ws / "rules" / "routines.json").read_bytes(), raw, "plan 失败必须回滚")

    def test_rebuild_keeps_existing_runtime_fields(self):
        """plan --force 重建账本：旧窗口的计数 / 状态 / 确认时间要合并回来，不许复活成 pending。"""
        self.write_routine(base_routine())
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)

        state = self.state()
        for w in state["windows"]:
            w["fireCount"] = 2
            w["state"] = "ringing"
            w["ackedAt"] = "2026-11-18T07:01:00+08:00"
        (self.ws / "state" / "alarms.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        rc, _, err = self.run_once("15:00", "合并测试", "--date", "2026-11-18")
        self.assertEqual(rc, 0, err)
        wake = self.window("wake")
        self.assertEqual(wake.get("fireCount"), 2, "重建把运行时计数清了")
        self.assertEqual(wake.get("state"), "ringing", "正在响的窗口被打回 pending")
        self.assertEqual(wake.get("ackedAt"), "2026-11-18T07:01:00+08:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
