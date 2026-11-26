#!/usr/bin/env python3
"""issue #75：前一晚预排次日账本，跨日第一轮激活。"""

from __future__ import annotations

import json
import unittest

from test_rtc import RtcCase, base_routine


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


if __name__ == "__main__":
    unittest.main()
