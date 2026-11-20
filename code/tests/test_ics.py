#!/usr/bin/env python3
"""归档层的回归测试：.ics 标准导出。

零外部依赖，临时工作区。运行：
    python -m unittest code.tests.test_ics -v
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_ics  # noqa: E402


class IcsExportCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-ics-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_state(self, windows):
        (self.ws / "state" / "alarms.json").write_text(
            json.dumps({"version": 1, "planDate": "2026-11-18", "windows": windows}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_empty_state_still_valid_calendar(self):
        out = rtc_ics.export_calendar(self.ws)
        self.assertTrue(out.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(out.rstrip().endswith("END:VCALENDAR"))
        self.assertIn("VERSION:2.0", out)

    def test_window_becomes_vevent(self):
        self.write_state(
            [
                {
                    "id": "wake",
                    "label": "起床",
                    "start": "2026-11-18T07:00:00+08:00",
                    "end": "2026-11-18T07:25:00+08:00",
                    "state": "acked",
                    "fireCount": 3,
                }
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("BEGIN:VEVENT", out)
        self.assertIn("SUMMARY:起床", out)
        self.assertIn("DTSTART:20261118T070000", out)
        self.assertIn("DTEND:20261118T072500", out)
        # COMPLETED 是 VTODO 的状态值，VEVENT 只能用 CONFIRMED；原状态留在自定义字段
        self.assertIn("STATUS:CONFIRMED", out)
        self.assertIn("X-RTC-STATE:acked", out)
        self.assertNotIn("STATUS:COMPLETED", out)

    def test_event_status_never_uses_todo_values(self):
        """VEVENT 的 STATUS 只能是 TENTATIVE / CONFIRMED / CANCELLED。"""
        self.write_state(
            [
                {"id": e, "start": f"2026-11-18T0{i}:00:00+08:00", "state": s, "fireCount": 0}
                for i, (e, s) in enumerate(
                    [
                        ("a", "pending"),
                        ("b", "ringing"),
                        ("c", "acked"),
                        ("d", "done"),
                        ("e", "missed"),
                        ("f", "skipped"),
                        ("g", "cancelled"),
                    ]
                )
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        statuses = set(re.findall(r"^STATUS:(\w+)", out, flags=re.MULTILINE))
        self.assertTrue(statuses <= {"TENTATIVE", "CONFIRMED", "CANCELLED"}, statuses)

    def test_special_chars_escaped(self):
        self.write_state(
            [
                {
                    "id": "w1",
                    "label": "开会, 讨论; 项目",
                    "start": "2026-11-18T09:00:00+08:00",
                    "state": "ringing",
                    "fireCount": 1,
                }
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn(r"SUMMARY:开会\, 讨论\; 项目", out)

    def test_cancelled_and_missed(self):
        self.write_state(
            [
                {
                    "id": "a",
                    "label": "取消的",
                    "start": "2026-11-18T10:00:00+08:00",
                    "state": "cancelled",
                    "fireCount": 0,
                },
                {
                    "id": "b",
                    "label": "漏响的",
                    "start": "2026-11-18T11:00:00+08:00",
                    "state": "missed",
                    "fireCount": 0,
                    "missedReason": "window_expired",
                },
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("STATUS:CANCELLED", out)
        # DESCRIPTION 可能被 RFC5545 折行，去掉续行的空格和换行再查
        unfolded = out.replace("\r\n ", "").replace("\r\n", "")
        self.assertIn("window_expired", unfolded)

    def test_uid_stable_across_exports(self):
        self.write_state(
            [
                {
                    "id": "wake",
                    "label": "起床",
                    "start": "2026-11-18T07:00:00+08:00",
                    "state": "ringing",
                    "fireCount": 1,
                }
            ]
        )
        a = rtc_ics.export_calendar(self.ws)
        b = rtc_ics.export_calendar(self.ws)
        uid_a = re.search(r"UID:([^\r\n]+)", a).group(1)
        uid_b = re.search(r"UID:([^\r\n]+)", b).group(1)
        self.assertEqual(uid_a, uid_b, "同一事件两次导出 UID 必须稳定（日历去重靠它）")

    def test_uid_uses_ocid_and_survives_reschedule(self):
        window = {
            "id": "wake",
            "ocid": "wake@2026-11-18",
            "seq": 0,
            "label": "起床",
            "start": "2026-11-18T07:00:00+08:00",
            "state": "pending",
            "fireCount": 0,
        }
        self.write_state([window])
        before = rtc_ics.export_calendar(self.ws)
        uid_before = re.search(r"UID:([^\r\n]+)", before).group(1)
        self.assertEqual(uid_before, "wake@2026-11-18@rtc-kit")
        self.assertIn("SEQUENCE:0", before)

        window.update(seq=1, start="2026-11-18T07:10:00+08:00")
        self.write_state([window])
        after = rtc_ics.export_calendar(self.ws)
        uid_after = re.search(r"UID:([^\r\n]+)", after).group(1)
        self.assertEqual(uid_after, uid_before, "改期后 UID 不许变，否则日历当成两条")
        self.assertIn("SEQUENCE:1", after, "改了时间要让日历知道是新版本")

    def test_legacy_window_without_ocid_keeps_old_uid(self):
        self.write_state(
            [
                {
                    "id": "wake",
                    "label": "起床",
                    "start": "2026-11-18T07:00:00+08:00",
                    "state": "pending",
                    "fireCount": 0,
                }
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("UID:wake-20261118T070000@rtc-kit", out, "老账本没有 ocid，退回旧算法")
        self.assertIn("SEQUENCE:0", out)


if __name__ == "__main__":
    unittest.main()
