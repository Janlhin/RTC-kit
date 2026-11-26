#!/usr/bin/env python3
"""rtc_plan.py（Hermes cron 日切入口）的进程级用例。

入口只做一件事：把环境变量里的工作区与可选 RTC_NOW 拼成 `plan` 命令，
委派给 rtc.main。用子进程真实跑，断言退出码、stdout 与账本字段；
不直接 import rtc_plan——模块顶层就会执行 main。

运行：
    python -m unittest code.tests.test_plan_entry -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import date

from test_rtc import ENV, REPO, RtcCase, base_routine

RTC_PLAN = REPO / "code" / "rtc_plan.py"


class TestPlanEntry(RtcCase):
    """rtc_plan.py 入口：工作区 / RTC_NOW 透传，正常路径静默出账。"""

    def run_entry(self, **extra_env: str):
        env = dict(ENV)
        env["RTC_WORKSPACE"] = str(self.ws)
        env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(RTC_PLAN)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_plain_run_generates_ledger_with_empty_stdout(self):
        """不带 RTC_NOW：用今天出账，成功时 stdout 必须为空（cron 静默约定）。"""
        self.write_routine(base_routine())
        rc, out, err = self.run_entry()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "", "plan 正常时 stdout 必须静默")
        state = self.state()
        self.assertEqual(state["planDate"], date.today().isoformat())
        self.assertEqual(state["generatedBy"], "rtc-plan", "入口必须委派 plan，不能跑去 due")
        self.assertIn("wake", [w["id"] for w in state["windows"]])

    def test_rtc_now_is_forwarded_to_plan(self):
        """RTC_NOW 必须透传成 plan --now，账本日期跟着注入的时间走。"""
        self.write_routine(base_routine())
        rc, out, err = self.run_entry(RTC_NOW="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        state = self.state()
        self.assertEqual(state["planDate"], "2026-11-18")
        self.assertEqual(state["generatedBy"], "rtc-plan")

    def test_missing_routine_exits_nonzero(self):
        """空工作区（时刻表不存在）：plan 校验失败，入口非零退出且不建账本。"""
        rc, _, err = self.run_entry()
        self.assertNotEqual(rc, 0)
        self.assertIn("找不到时刻表", err)
        self.assertFalse((self.ws / "state" / "alarms.json").exists(), "校验失败不该落账本")

    def test_delegates_plan_not_due(self):
        """入口 argv 固定是 plan：账本由 plan 生成，而不是 due 投递后的状态。"""
        self.write_routine(base_routine())
        rc, _, err = self.run_entry(RTC_NOW="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        wake = self.window("wake")
        self.assertEqual(wake.get("state"), "pending", "走 due 才会推进响铃状态")
        self.assertEqual(wake.get("fireCount"), 0, "plan 入口不许把窗口记成已响过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
