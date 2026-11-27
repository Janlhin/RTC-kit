#!/usr/bin/env python3
"""issue #75：前一晚预排次日账本，跨日第一轮激活。"""

from __future__ import annotations

import json
import sqlite3
import unittest

from test_rtc import RtcCase, base_routine


def routine_with_tz(timezone: str = "Asia/Shanghai", tz_offset: str = "+08:00", **over) -> dict:
    """可注入时区的时刻表夹具；默认值与 test_rtc.base_routine 完全一致。

    不传时区时既有用例行为零变化——只是把原来写死的 IANA 名与固定偏移
    变成可传参。注意：核心只按固定 ``tzOffset`` 换算墙钟（rtc_core.py:871、
    98-101），IANA 名仅落盘存档；DST 跳变解析当前不支持
    （docs/audit/2026-11-09-response.md:41、134）。
    """
    return base_routine(timezone=timezone, tzOffset=tz_offset, **over)


def _ledger_ids(state: dict) -> list[str]:
    return [w["id"] for w in state.get("windows", [])]


def _next_state(ws) -> dict:
    p = ws / "state" / "alarms.next.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


class TestPrebuildNextDay(RtcCase):
    def setUp(self):
        super().setUp()
        r = base_routine()
        r["alarmWindows"] = [
            {
                "id": "night",
                "label": "夜间提醒",
                "start": "00:30",
                "end": "00:35",
                "intervalMin": 5,
                "expectFires": 2,
                "ackHints": ["好"],
            }
        ]
        self.write_routine(r)

    def next_state(self) -> dict:
        p = self.ws / "state" / "alarms.next.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def test_plan_at_six_prebuilds_tomorrow_window_pending(self):
        rc, _, _ = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0)

        self.assertEqual(self.state()["planDate"], "2026-11-19")
        self.assertEqual(self.window("night")["state"], "skipped")
        self.assertEqual(self.window("night").get("missedReason"), "late_plan")

        nxt = self.next_state()
        self.assertEqual(nxt["planDate"], "2026-11-20")
        self.assertEqual(nxt["windows"][0]["id"], "night")
        self.assertEqual(nxt["windows"][0]["state"], "pending")

    def test_due_first_tick_after_midnight_promotes_prebuilt(self):
        self.cli("plan", now="2026-11-19T06:00:00+08:00")

        rc, out, _ = self.cli("due", now="2026-11-20T00:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.state()["planDate"], "2026-11-20")
        self.assertEqual(self.window("night")["state"], "pending")
        self.assertFalse((self.ws / "state" / "alarms.next.json").exists())

        rc, out, _ = self.cli("due", now="2026-11-20T00:30:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("夜间提醒", out)
        self.assertEqual(self.window("night")["state"], "ringing")

    def test_plan_skip_still_prebuilds_tomorrow(self):
        self.cli("plan", now="2026-11-19T06:00:00+08:00")
        rc, _, _ = self.cli("plan", now="2026-11-19T06:05:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.next_state()["planDate"], "2026-11-20")

    def test_for_date_only_writes_prebuilt_and_refuses_far_future(self):
        self.cli("plan", now="2026-11-19T06:00:00+08:00")
        rc, _, err = self.cli("plan", "--for-date", "2026-11-20", now="2026-11-19T18:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.next_state()["planDate"], "2026-11-20")
        self.assertEqual(self.state()["planDate"], "2026-11-19")

        rc, _, err = self.cli("plan", "--for-date", "2026-11-22", now="2026-11-19T18:00:00+08:00")
        self.assertEqual(rc, 3)
        self.assertIn("只支持预排明天", err)

    def test_stale_prebuilt_is_dropped(self):
        self.cli("plan", now="2026-11-19T06:00:00+08:00")
        rc, _, _ = self.cli("due", now="2026-11-21T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state()["planDate"], "2026-11-21")
        # 旧的 11-20 预排已丢，plan 又为 11-22 预排。
        self.assertEqual(self.next_state()["planDate"], "2026-11-22")


class TestPrebuildDstTransition(RtcCase):
    """跳变日（美东）预排 → 激活 → 到期的账本不变量。

    当前实现按固定 tzOffset 换算，不解析 IANA 跳变；这里只锁「预排窗口不丢/
    不重、到期只触发一次、历史不出现同日同窗两条记录」。缺口时刻（03-08
    02:30 不存在）与歧义时刻（11-01 01:30 出现两次）的时区感知判定属于
    尚未实现的能力，见回报里的待确认项。
    """

    def _write(self, windows: list[dict], tz_offset: str) -> None:
        r = routine_with_tz(timezone="America/New_York", tz_offset=tz_offset)
        r["alarmWindows"] = windows
        self.write_routine(r)

    def _daily_rows(self, day: str) -> list[tuple]:
        con = sqlite3.connect(self.ws / "state" / "history.db")
        try:
            return con.execute(
                "SELECT date, window_id FROM daily WHERE date=? ORDER BY window_id", (day,)
            ).fetchall()
        finally:
            con.close()

    def _dupe_daily(self) -> list[tuple]:
        con = sqlite3.connect(self.ws / "state" / "history.db")
        try:
            return con.execute(
                "SELECT date, window_id, COUNT(*) FROM daily GROUP BY date, window_id HAVING COUNT(*) > 1"
            ).fetchall()
        finally:
            con.close()

    def _run_transition_day(self, *, plan_now: str, promote_now: str, day: str, offset: str) -> None:
        dawn = {
            "id": "dawn",
            "label": "晨间提醒",
            "start": "07:00",
            "end": "07:10",
            "intervalMin": 5,
            "expectFires": 3,
            "ackHints": ["起了"],
        }
        self._write([dawn], offset)

        rc, _, _ = self.cli("plan", now=plan_now)
        self.assertEqual(rc, 0)
        nxt = _next_state(self.ws)
        self.assertEqual(nxt["planDate"], day)
        self.assertEqual(len(nxt["windows"]), 1)
        self.assertEqual(nxt["windows"][0]["id"], "dawn")
        self.assertEqual(nxt["windows"][0]["state"], "pending")
        self.assertEqual(_ledger_ids(nxt), ["dawn"], "预排账本里同一窗口只能出现一次")

        rc, out, _ = self.cli("due", now=promote_now)
        self.assertEqual(rc, 0)
        # 激活轮不投递当天窗口；自检可能提示「昨天窗口零响」，与本链路无关。
        self.assertNotIn("晨间提醒", out, "激活轮不该投递当天窗口")
        state = self.state()
        self.assertEqual(state["planDate"], day)
        self.assertEqual(state["generatedBy"], "rtc-plan:prebuilt")
        self.assertFalse((self.ws / "state" / "alarms.next.json").exists())
        self.assertEqual(_ledger_ids(state), ["dawn"], "激活后账本不丢窗口、也不重复")
        self.assertEqual(self.window("dawn")["state"], "pending")

        # 三轮命中：07:00 / 07:05 / 07:10，之后超出窗口不再触发。
        for tick, fires in (("07:00", 1), ("07:05", 2), ("07:10", 3)):
            rc, out, _ = self.cli("due", now=f"{day}T{tick}:00{offset}")
            self.assertEqual(rc, 0)
            self.assertIn("晨间提醒", out)
            self.assertEqual(self.window("dawn")["fireCount"], fires)
        rc, out, _ = self.cli("due", now=f"{day}T07:15:00{offset}")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "窗口结束后不应再投递")
        self.assertEqual(self.window("dawn")["fireCount"], 3)

        self.assertEqual(self._daily_rows(day), [(day, "dawn")], "历史只记一条当日记录")
        self.assertEqual(self._dupe_daily(), [], "历史不出现同日同窗重复记录")

    def test_spring_forward_2026_03_08(self):
        # 2026-03-08 02:00 跳 EDT（-04:00）；07:00 已在跳变之后。
        self._run_transition_day(
            plan_now="2026-03-07T06:00:00-04:00",
            promote_now="2026-03-08T06:55:00-04:00",
            day="2026-03-08",
            offset="-04:00",
        )

    def test_fall_back_2026_11_01(self):
        # 2026-11-01 02:00 退回 EST（-05:00）；07:00 已在跳变之后。
        self._run_transition_day(
            plan_now="2026-10-31T06:00:00-05:00",
            promote_now="2026-11-01T06:55:00-05:00",
            day="2026-11-01",
            offset="-05:00",
        )


class TestPromoteConsistency(RtcCase):
    """预排之后时刻表被改：激活走旧预排还是新时刻表（issue #75 审计缺口）。"""

    def test_promote_keeps_prebuilt_when_routine_changed(self):
        v1 = routine_with_tz()
        v1["alarmWindows"] = [
            {
                "id": "old",
                "label": "旧时段",
                "start": "07:00",
                "end": "07:05",
                "intervalMin": 5,
                "expectFires": 2,
                "ackHints": ["起了"],
            }
        ]
        self.write_routine(v1)

        rc, _, _ = self.cli("plan", now="2026-11-19T06:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertEqual(_next_state(self.ws)["planDate"], "2026-11-20")

        # 预排之后改时刻表：删掉 old，换成 08:00 的 new。
        v2 = routine_with_tz()
        v2["alarmWindows"] = [
            {
                "id": "new",
                "label": "新时段",
                "start": "08:00",
                "end": "08:05",
                "intervalMin": 5,
                "expectFires": 2,
                "ackHints": ["好了"],
            }
        ]
        self.write_routine(v2)

        rc, out, _ = self.cli("due", now="2026-11-20T00:05:00+08:00")
        self.assertEqual(rc, 0)
        state = self.state()
        self.assertEqual(state["planDate"], "2026-11-20")
        self.assertEqual(state["generatedBy"], "rtc-plan:prebuilt")
        # 激活沿用旧预排账本：old 还在，新时刻表的 new 不会被现场补进账本。
        self.assertEqual(_ledger_ids(state), ["old"])
        self.assertNotIn("new", _ledger_ids(state))
        self.assertEqual(self.window("old")["start"], "2026-11-20T07:00:00+08:00")
        # 对账仍按当前时刻表跑：new 在时刻表有、账本没有，必须告警。
        self.assertIn("new", out)
        _, status, _ = self.cli("status", "--json")
        self.assertIn("new", json.loads(status)["missing_window_ids"])

        # 旧预排窗口到点照常触发。
        rc, out, _ = self.cli("due", now="2026-11-20T07:00:00+08:00")
        self.assertEqual(rc, 0)
        self.assertIn("旧时段", out)
        self.assertEqual(self.window("old")["fireCount"], 1)


if __name__ == "__main__":
    unittest.main()
