#!/usr/bin/env python3
"""L2 P2：日历索引回归——可删可重建，查询结果与直扫 .ics 逐条一致。

零外部依赖，临时工作区。运行：
    python -m unittest code.tests.test_calindex -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_calindex as calindex  # noqa: E402
import rtc_ics as ics  # noqa: E402


def _ev(uid: str, start: str, end: str, **over: object) -> dict:
    ev: dict = {
        "uid": uid,
        "kind": "once",
        "start": start,
        "end": end,
        "rrule": None,
        "owner": "self",
        "deadline": None,
        "status": "scheduled",
        "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
        "aliases": [uid.split("@")[0]],
        "summary": uid.split("@")[0],
        "categories": [],
        "tzid": None,
        "dtstamp": "20261118T000000Z",
    }
    ev.update(over)
    return ev


class CalindexCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-calindex-"))
        self.events = [
            _ev(
                "aaaaaaaa-1111-4aaa-8aaa-000000000001@rtc-kit",
                "2026-11-18T07:00:00+08:00",
                "2026-11-18T07:25:00+08:00",
            ),
            _ev(
                "aaaaaaaa-1111-4aaa-8aaa-000000000002@rtc-kit",
                "2027-03-01T09:00:00+08:00",
                "2027-03-01T09:30:00+08:00",
                kind="recurring",
                rrule="FREQ=WEEKLY;BYDAY=MO",
            ),
            _ev(
                "aaaaaaaa-1111-4aaa-8aaa-000000000003@rtc-kit",
                "2026-12-31T23:00:00+08:00",
                "2027-01-01T00:30:00+08:00",
            ),
        ]
        ics.write_events(self.ws, self.events)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_rebuild_from_scratch_matches_source(self):
        # 索引文件不存在 → rebuild 从零扫描 .ics 灌入。
        self.assertFalse(calindex.index_path(self.ws).exists())
        count = calindex.rebuild(self.ws)
        self.assertEqual(count, len(self.events))

        source = {e["uid"]: e for e in ics.load_all_events(self.ws)}
        indexed = {e["uid"]: e for e in calindex.all_events(self.ws)}
        self.assertEqual(set(indexed), set(source))
        self.assertEqual(calindex.diff_with_source(self.ws), [])

    def test_delete_index_then_rebuild_reproduces_query(self):
        calindex.rebuild(self.ws)
        # 删掉整个索引库（含 WAL/SHM），模拟缓存丢失。
        for side in calindex.index_path(self.ws).parent.glob("calendar-index.db*"):
            side.unlink()
        self.assertFalse(calindex.index_path(self.ws).exists())

        calindex.rebuild(self.ws)
        source = ics.load_all_events(self.ws)
        indexed = calindex.all_events(self.ws)
        self.assertEqual(len(indexed), len(source))
        for s in source:
            i = calindex.get_event(self.ws, s["uid"])
            self.assertIsNotNone(i)
            for key in ("start", "end", "rrule", "owner", "status", "summary", "aliases"):
                self.assertEqual(i[key], s[key], s["uid"])

    def test_events_for_year_and_alias_lookup(self):
        calindex.rebuild(self.ws)
        y2026 = {e["uid"] for e in calindex.events_for_year(self.ws, 2026)}
        self.assertEqual(len(y2026), 2)
        alias = self.events[0]["aliases"][0]
        self.assertEqual(calindex.find_uid(self.ws, alias), self.events[0]["uid"])

    def test_stale_indexed_record_reported_with_source_wins(self):
        calindex.rebuild(self.ws)
        # 手改 .ics：删掉第一条事件（直接重写 2026 文件，只留第三条跨年事件）。
        ics.write_events(self.ws, [self.events[2]])
        # 不 rebuild → 索引陈旧：索引多出的两条被点名，以 .ics 为准。
        problems = calindex.diff_with_source(self.ws)
        stale = [p for p in problems if "陈旧记录" in p]
        self.assertEqual({p.split("：")[-1] for p in stale}, {self.events[0]["uid"], self.events[1]["uid"]})
        self.assertFalse(any("需 rebuild" in p for p in problems), problems)

    def test_missing_index_reads_empty(self):
        self.assertEqual(calindex.all_events(self.ws), [])
        self.assertIsNone(calindex.get_event(self.ws, "nope@rtc-kit"))


if __name__ == "__main__":
    unittest.main()
