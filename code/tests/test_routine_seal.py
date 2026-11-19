import argparse
import json
import unittest
from unittest.mock import patch

from test_rtc import RtcCase, base_routine, rtc


class _SealCase(RtcCase):
    """推迟/改时刻都算「程序写出的版本」，都必须盖章。issue #87 第 1 条。"""

    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)

    def routine_file(self):
        return self.ws / "rules" / "routines.json"

    def seal(self) -> dict:
        p = self.ws / "state" / "routine-seal.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def program_last(self) -> dict:
        p = self.ws / "state" / "backups" / "routines.program-last.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def assert_sealed(self):
        self.assertEqual(
            self.seal()["digest"],
            rtc.file_digest(self.routine_file()),
            "印章摘要必须等于当前 routines.json 的摘要，否则下次 plan 必报手改",
        )
        self.assertEqual(self.program_last(), json.loads(self.routine_file().read_text(encoding="utf-8")))

    def assert_next_plan_quiet(self):
        rc, _, err = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("被 RTC 之外的东西改过", err, "程序自己写的改动不能报成手改")


class TestPostponeSeal(_SealCase):
    """issue #87 第 1 条：推迟成功却没盖章——次日 plan 把程序自己当成「手改」。"""

    def test_postpone_reseals_so_next_plan_is_quiet(self):
        """推迟成功 = 程序写出的版本，次日的 plan 不许把它报成外部编辑。"""
        rc, out, err = self.cli(
            "postpone", "--window", "wake", "--to", "2026-11-21", now="2026-11-18T06:00:00+08:00"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assert_sealed()
        self.assertEqual(
            self.program_last()["alarmWindows"][0]["skipDates"],
            ["2026-11-18", "2026-11-19", "2026-11-20"],
            "底稿副本必须带上这次推迟——坏文件恢复时不该把推迟丢掉",
        )

        rc, _, err = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("被 RTC 之外的东西改过", err, "程序自己写的改动不能报成手改")

    def test_rollback_reseals_too(self):
        """回滚后的内容也得盖章：不然「回滚成功」反而留下一条假手改。"""
        rtc.set_workspace(self.ws)
        orig = rtc.atomic_write

        def conflict(path, *a, **kw):
            if path == rtc.STATE:
                raise rtc.Conflict("injected: state changed concurrently")
            return orig(path, *a, **kw)

        with patch.object(rtc, "atomic_write", conflict):
            rc = rtc.cmd_postpone(
                argparse.Namespace(
                    window="wake",
                    to="2026-11-21",
                    days=None,
                    deadline=False,
                    now="2026-11-18T06:00:00+08:00",
                )
            )
        self.assertEqual(rc, 2, "状态冲突且回滚成功，应是 2")
        self.assert_sealed()
        self.assertNotIn("skipDates", self.program_last()["alarmWindows"][0], "回滚要回到原样")

        rc, _, err = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("被 RTC 之外的东西改过", err)


class TestSetTimeSeal(_SealCase):
    """同一个坑的另一处：set-time 也是直接 atomic_write，写完同样不盖章。"""

    def test_set_time_reseals_so_next_plan_is_quiet(self):
        rc, out, err = self.cli("set-time", "--window", "wake", "--start", "07:10")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(self.seal()["windows"], 1, "盖章里的窗口数也要跟着重算")
        self.assert_sealed()
        self.assert_next_plan_quiet()


if __name__ == "__main__":
    unittest.main(verbosity=2)
