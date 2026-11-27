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

    def write_state(self, windows, timezone=None):
        body: dict[str, object] = {"version": 1, "planDate": "2026-11-18", "windows": windows}
        if timezone is not None:
            body["timezone"] = timezone
        (self.ws / "state" / "alarms.json").write_text(
            json.dumps(body, ensure_ascii=False),
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
        # 旧账本没有时区标识 → 换算成 UTC 瞬时，Z 后缀，不输出浮动时间
        self.assertIn("DTSTART:20261117T230000Z", out)
        self.assertIn("DTEND:20261117T232500Z", out)
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

    def test_no_floating_time_any_event(self):
        """#91：每条 DTSTART/DTEND 必须带时区（Z 或 TZID），不许是浮动时间。"""
        self.write_state(
            [
                {
                    "id": "a",
                    "start": "2026-11-18T09:00:00+08:00",
                    "end": "2026-11-18T09:30:00+08:00",
                    "state": "pending",
                    "fireCount": 0,
                },
                {
                    "id": "b",
                    "start": "2026-11-18T10:00:00+08:00",
                    "end": "2026-11-18T10:20:00+08:00",
                    "state": "ringing",
                    "fireCount": 1,
                },
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        for line in out.splitlines():
            if line.startswith(("DTSTART:", "DTEND:")) or line.startswith(("DTSTART;", "DTEND;")):
                self.assertRegex(
                    line, r"^(DTSTART|DTEND)(;TZID=[^:]+)?:\d{8}T\d{6}Z$", f"浮动时间或格式不对：{line}"
                )
                self.assertTrue(line.endswith("Z") or ";TZID=" in line, f"没有时区标识：{line}")

    def test_named_zone_emits_tzid_and_vtimezone(self):
        """带 IANA 时区的状态 → TZID + 对应 VTIMEZONE 块。"""
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
            ],
            timezone="Asia/Shanghai",
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("DTSTART;TZID=Asia/Shanghai:20261118T070000", out)
        self.assertIn("DTEND;TZID=Asia/Shanghai:20261118T072500", out)
        self.assertIn("BEGIN:VTIMEZONE", out)
        self.assertIn("TZID:Asia/Shanghai", out)
        self.assertIn("END:VTIMEZONE", out)

    def test_ny_dst_zone_keeps_tzid_and_emits_standard_and_daylight(self):
        """America/New_York 有夏令时：VTIMEZONE 同时给 STANDARD 与 DAYLIGHT 观测。"""
        self.write_state(
            [
                {
                    "id": "standup",
                    "label": "站会",
                    "start": "2026-03-08T09:00:00-04:00",
                    "end": "2026-03-08T09:30:00-04:00",
                    "state": "pending",
                    "fireCount": 0,
                }
            ],
            timezone="America/New_York",
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("DTSTART;TZID=America/New_York:20260308T090000", out)
        self.assertIn("TZID:America/New_York", out)
        self.assertIn("BEGIN:STANDARD", out)
        self.assertIn("BEGIN:DAYLIGHT", out)

    def test_utc_event_uses_z_suffix(self):
        """偏移 0 的瞬时直接用 Z 后缀，不挂 TZID。"""
        self.write_state(
            [
                {
                    "id": "sync",
                    "label": "同步",
                    "start": "2026-11-18T07:00:00+00:00",
                    "end": "2026-11-18T07:15:00+00:00",
                    "state": "pending",
                    "fireCount": 0,
                }
            ],
            timezone="UTC",
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("DTSTART:20261118T070000Z", out)
        self.assertIn("DTEND:20261118T071500Z", out)

    def test_naive_local_time_is_pinned_to_utc_not_floating(self):
        """裸本地时间（防御性兜底）先按 UTC 钉住，绝不落成浮动时间。"""
        self.write_state(
            [
                {
                    "id": "legacy",
                    "label": "老格式",
                    "start": "2026-11-18T07:00:00",
                    "end": "2026-11-18T07:15:00",
                    "state": "pending",
                    "fireCount": 0,
                }
            ]
        )
        out = rtc_ics.export_calendar(self.ws)
        self.assertIn("DTSTART:20261118T070000Z", out)
        self.assertNotRegex(out, r"^DTSTART:\d{8}T\d{6}$", "裸本地时间不该漏出去")

    def test_window_without_start_is_skipped(self):
        """缺 start 的脏窗口跳过，不产出 VEVENT。"""
        self.write_state([{"id": "broken", "state": "pending", "fireCount": 0}])
        out = rtc_ics.export_calendar(self.ws)
        self.assertNotIn("BEGIN:VEVENT", out)


if __name__ == "__main__":
    unittest.main()
