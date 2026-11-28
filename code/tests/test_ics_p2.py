#!/usr/bin/env python3
"""L2 P2：.ics 源真相层回归——序列化/解析往返、折行转义、分年文件、跨年事件。

零外部依赖，临时工作区。运行：
    python -m unittest code.tests.test_ics_p2 -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_ics as ics  # noqa: E402


def make_event(**over: object) -> dict:
    ev: dict = {
        "uid": "11111111-2222-4333-8888-999999999999@rtc-kit",
        "kind": "once",
        "start": "2026-11-18T07:00:00+08:00",
        "end": "2026-11-18T07:25:00+08:00",
        "rrule": None,
        "owner": "self",
        "deadline": None,
        "status": "scheduled",
        "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
        "aliases": ["wake"],
        "summary": "起床",
        "categories": ["routine"],
        "tzid": None,
        "dtstamp": "20261118T000000Z",
    }
    ev.update(over)
    return ev


def _parse_one(text: str) -> dict:
    events = ics.parse_events(text)
    assert len(events) == 1, f"期望 1 条事件，实际 {len(events)}"
    return events[0]


class RoundtripCase(unittest.TestCase):
    def test_roundtrip_all_fields(self):
        ev = make_event(
            kind="recurring",
            rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR",
            owner="other:alice",
            deadline="2026-12-01",
            status="done",
            aliases=["standup", "ocid-1"],
            categories=["work", "standup"],
            tzid="Asia/Shanghai",
        )
        text = ics.render_calendar([ev], year=2026)
        back = _parse_one(text)

        self.assertEqual(back["uid"], ev["uid"])
        self.assertEqual(back["kind"], ev["kind"])
        self.assertEqual(back["rrule"], ev["rrule"])
        self.assertEqual(back["owner"], ev["owner"])
        self.assertEqual(back["deadline"], ev["deadline"])
        self.assertEqual(back["status"], ev["status"])
        self.assertEqual(back["aliases"], ev["aliases"])
        self.assertEqual(back["categories"], ev["categories"])
        self.assertEqual(back["tzid"], ev["tzid"])
        self.assertEqual(back["summary"], ev["summary"])
        self.assertEqual(datetime.fromisoformat(back["start"]), datetime.fromisoformat(ev["start"]))
        self.assertEqual(datetime.fromisoformat(back["end"]), datetime.fromisoformat(ev["end"]))
        self.assertEqual(back["reminderPolicy"], ev["reminderPolicy"])

    def test_status_mapping_to_ics_and_back(self):
        for status, ics_status in [
            ("done", "COMPLETED"),
            ("cancelled", "CANCELLED"),
            ("scheduled", "CONFIRMED"),
            ("ringing", "CONFIRMED"),
            ("missed", "CONFIRMED"),
        ]:
            text = ics.render_calendar([make_event(status=status)], year=2026)
            self.assertIn(f"STATUS:{ics_status}", text, status)
            back = _parse_one(text)
            self.assertEqual(back["status"], status, "X-RTC-STATUS 必须保住原枚举")

    def test_unknown_properties_preserved(self):
        ev = make_event(unknown=["X-RTC-EXTRA:keep-me", "X-OTHER:42"])
        text = ics.render_calendar([ev], year=2026)
        back = _parse_one(text)
        self.assertIn("X-RTC-EXTRA:keep-me", back["unknown"])
        self.assertIn("X-OTHER:42", back["unknown"])


class EscapingFoldingCase(unittest.TestCase):
    def test_special_chars_escaped_and_restored(self):
        ev = make_event(summary="开会, 讨论; 项目\n第二行内容")
        text = ics.render_calendar([ev], year=2026)
        self.assertIn(r"SUMMARY:开会\, 讨论\; 项目\n第二行内容", text)
        back = _parse_one(text)
        self.assertEqual(back["summary"], "开会, 讨论; 项目\n第二行内容")

    def test_long_summary_folded_at_75_octets(self):
        # 中文 3 字节/字；28 个字远超 75 octet，必然触发折行。
        ev = make_event(summary="日程" * 28)
        text = ics.render_calendar([ev], year=2026)
        lines = text.split("\r\n")
        folded = [ln for ln in lines if ln[:1] in (" ", "\t")]
        self.assertTrue(folded, "长 SUMMARY 必须被折行，续行以空格开头")
        back = _parse_one(text)
        self.assertEqual(back["summary"], "日程" * 28)

    def test_crlf_no_bom_and_vcalendar_header(self):
        text = ics.render_calendar([make_event()], year=2026)
        self.assertNotEqual(text.encode("utf-8")[:3], b"\xef\xbb\xbf", "不允许 UTF-8 BOM")
        self.assertIn("\r\n", text, "行尾必须是 CRLF")
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", text)
        self.assertIn(f"PRODID:{ics.PRODID}", text)
        self.assertIn("CALSCALE:GREGORIAN", text)
        self.assertTrue(text.rstrip("\r\n").endswith("END:VCALENDAR"))


class YearSplitCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-ics-year-"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_events_split_by_start_year(self):
        ev_2026 = make_event(uid="aaaaaaaa-1111-4aaa-8aaa-000000000001@rtc-kit")
        ev_2027 = make_event(
            uid="aaaaaaaa-1111-4aaa-8aaa-000000000002@rtc-kit",
            start="2027-03-01T09:00:00+08:00",
            end="2027-03-01T09:30:00+08:00",
        )
        written = ics.write_events(self.ws, [ev_2026, ev_2027])
        self.assertTrue((self.ws / "calendar" / "2026.ics").exists())
        self.assertTrue((self.ws / "calendar" / "2027.ics").exists())

        y2026 = {e["uid"] for e in ics.read_file(self.ws / "calendar" / "2026.ics")}
        y2027 = {e["uid"] for e in ics.read_file(self.ws / "calendar" / "2027.ics")}
        self.assertEqual(y2026, {ev_2026["uid"]})
        self.assertEqual(y2027, {ev_2027["uid"]})
        self.assertEqual({p.name for p in written}, {"2026.ics", "2027.ics"})

    def test_cross_year_event_has_segment_in_both_files(self):
        ev = make_event(
            uid="cccccccc-1111-4ccc-8ccc-000000000003@rtc-kit",
            start="2026-12-31T23:00:00+08:00",
            end="2027-01-01T00:30:00+08:00",
        )
        ics.write_events(self.ws, [ev])

        main_year = ics.read_file(self.ws / "calendar" / "2026.ics")
        cross_year = ics.read_file(self.ws / "calendar" / "2027.ics")
        self.assertEqual({e["uid"] for e in main_year}, {ev["uid"]})
        self.assertEqual({e["uid"] for e in cross_year}, {ev["uid"]})
        self.assertEqual(cross_year[0]["crossYear"], "2026")

        # 直扫合并去重：跨年事件只算一条，主记录字段完整。
        all_events = ics.load_all_events(self.ws)
        self.assertEqual(len(all_events), 1)
        self.assertEqual(all_events[0]["uid"], ev["uid"])
        self.assertIsNone(all_events[0].get("crossYear"))

    def test_missing_dir_loads_empty(self):
        self.assertEqual(ics.load_all_events(self.ws), [])


if __name__ == "__main__":
    unittest.main()
