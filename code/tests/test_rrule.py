#!/usr/bin/env python3
"""RRULE 解析与 occurrence 展开引擎的回归测试。

零外部依赖，只用标准库 datetime / zoneinfo。运行：
    python -m unittest code.tests.test_rrule -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

import rtc_rrule  # noqa: E402
from rtc_rrule import ExpansionLimit, RruleError, expand_occurrences, parse_rrule  # noqa: E402

CST = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")


def _dt(y: int, m: int, d: int, h: int = 9, mi: int = 0, tz=CST) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


def _dates(out: list[datetime]) -> list[str]:
    return [o.strftime("%Y-%m-%d %H:%M") for o in out]


class TestParse(unittest.TestCase):
    def test_minimal_daily(self):
        rule = parse_rrule("FREQ=DAILY")
        self.assertEqual(rule.freq, "DAILY")
        self.assertEqual(rule.interval, 1)
        self.assertIsNone(rule.count)
        self.assertIsNone(rule.until)

    def test_full_fields(self):
        rule = parse_rrule("FREQ=WEEKLY;INTERVAL=2;COUNT=10;BYDAY=MO,WE;BYMONTH=3,9;WKST=SU")
        self.assertEqual(rule.freq, "WEEKLY")
        self.assertEqual(rule.interval, 2)
        self.assertEqual(rule.count, 10)
        self.assertEqual([b.weekday for b in rule.byday], [0, 2])
        self.assertEqual(rule.bymonth, (3, 9))
        self.assertEqual(rule.wkst, 6)

    def test_until_aware_utc_and_local(self):
        z = parse_rrule("FREQ=DAILY;UNTIL=20261231T235959Z")
        self.assertIsNotNone(z.until)
        self.assertEqual(z.until.tzinfo, UTC)  # type: ignore[union-attr]
        local = parse_rrule("FREQ=DAILY;UNTIL=20261231T235959")
        self.assertIsNotNone(local.until)
        self.assertIsNone(local.until.tzinfo)  # type: ignore[union-attr]

    def test_byday_ordinal_prefix(self):
        rule = parse_rrule("FREQ=MONTHLY;BYDAY=1MO,-1FR")
        self.assertEqual(rule.byday[0].ordinal, 1)
        self.assertEqual(rule.byday[0].weekday, 0)
        self.assertEqual(rule.byday[1].ordinal, -1)
        self.assertEqual(rule.byday[1].weekday, 4)

    def test_unknown_freq_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=HOURLY")

    def test_unknown_key_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;FOO=1")

    def test_bad_byday_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=WEEKLY;BYDAY=XU")

    def test_count_and_until_mutually_exclusive(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;COUNT=3;UNTIL=20261231T000000")

    def test_interval_must_be_positive(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;INTERVAL=0")

    def test_count_must_be_positive(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;COUNT=0")

    def test_bad_bymonthday_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=MONTHLY;BYMONTHDAY=32")

    def test_bad_bymonth_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=YEARLY;BYMONTH=13")

    def test_ordinal_byday_in_weekly_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=WEEKLY;BYDAY=2MO")

    def test_duplicate_key_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;INTERVAL=2;INTERVAL=3")

    def test_empty_string_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("")

    def test_missing_freq_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("BYDAY=MO")

    def test_non_integer_interval_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;INTERVAL=abc")

    def test_bad_until_format_rejected(self):
        with self.assertRaises(RruleError):
            parse_rrule("FREQ=DAILY;UNTIL=not-a-time")


class TestExpandDaily(unittest.TestCase):
    def test_daily_within_window_keeps_tz_and_order(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=DAILY"), _dt(2026, 11, 28), _dt(2026, 12, 2))
        self.assertEqual(
            _dates(out), ["2026-11-28 09:00", "2026-11-29 09:00", "2026-11-30 09:00", "2026-12-01 09:00"]
        )
        self.assertTrue(all(o.tzinfo is CST for o in out))
        self.assertEqual(out, sorted(out))

    def test_window_from_inclusive_to_exclusive(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=DAILY"), _dt(2026, 11, 29), _dt(2026, 11, 30, 9, 0))
        self.assertEqual(_dates(out), ["2026-11-29 09:00"])

    def test_daily_byday_filter(self):
        ds = _dt(2026, 11, 27, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=DAILY;BYDAY=SA,SU"), _dt(2026, 11, 27), _dt(2026, 12, 6)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-11-28 09:00",
                "2026-11-29 09:00",
                "2026-12-05 09:00",
            ],
        )


class TestExpandWeekly(unittest.TestCase):
    def test_byday_mo_we_fr(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;BYDAY=MO,WE,FR"), _dt(2026, 11, 28), _dt(2026, 12, 7)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-11-30 09:00",
                "2026-12-02 09:00",
                "2026-12-04 09:00",
            ],
        )

    def test_interval_two_biweekly_spacing(self):
        ds = _dt(2026, 11, 27, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"), _dt(2026, 11, 27), _dt(2027, 2, 1)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-11-27 09:00",
                "2026-12-11 09:00",
                "2026-12-25 09:00",
                "2027-01-08 09:00",
                "2027-01-22 09:00",
            ],
        )

    def test_wkst_parsed_and_expansion_stable(self):
        ds = _dt(2026, 11, 29, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"), _dt(2026, 11, 29), _dt(2027, 2, 1)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-11-30 09:00",
                "2026-12-14 09:00",
                "2026-12-28 09:00",
                "2027-01-11 09:00",
                "2027-01-25 09:00",
            ],
        )


class TestExpandMonthly(unittest.TestCase):
    def test_bymonthday_minus_one_last_day(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=MONTHLY;BYMONTHDAY=-1"), _dt(2026, 11, 28), _dt(2027, 5, 1)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-11-30 09:00",
                "2026-12-31 09:00",
                "2027-01-31 09:00",
                "2027-02-28 09:00",
                "2027-03-31 09:00",
                "2027-04-30 09:00",
            ],
        )

    def test_bymonthday_31_skipped_in_short_months(self):
        ds = _dt(2026, 12, 31, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=MONTHLY;BYMONTHDAY=31"), _dt(2026, 12, 31), _dt(2027, 6, 1)
        )
        self.assertEqual(
            _dates(out),
            [
                "2026-12-31 09:00",
                "2027-01-31 09:00",
                "2027-03-31 09:00",
                "2027-05-31 09:00",
            ],
        )

    def test_byday_first_monday(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=MONTHLY;BYDAY=1MO"), _dt(2026, 11, 28), _dt(2027, 3, 1)
        )
        self.assertEqual(_dates(out), ["2026-12-07 09:00", "2027-01-04 09:00", "2027-02-01 09:00"])

    def test_byday_last_friday(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=MONTHLY;BYDAY=-1FR"), _dt(2026, 11, 28), _dt(2027, 2, 1)
        )
        self.assertEqual(_dates(out), ["2026-12-25 09:00", "2027-01-29 09:00"])

    def test_plain_byday_all_matching_weekdays(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=MONTHLY;BYDAY=MO"), _dt(2026, 11, 28), _dt(2027, 1, 1))
        self.assertEqual(
            _dates(out),
            [
                "2026-11-30 09:00",
                "2026-12-07 09:00",
                "2026-12-14 09:00",
                "2026-12-21 09:00",
                "2026-12-28 09:00",
            ],
        )


class TestExpandYearly(unittest.TestCase):
    def test_leap_day_skipped_in_common_years_not_rolled_to_march(self):
        ds = _dt(2024, 2, 29, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=YEARLY"), _dt(2024, 1, 1), _dt(2033, 1, 1))
        self.assertEqual([o.strftime("%Y-%m-%d") for o in out], ["2024-02-29", "2028-02-29", "2032-02-29"])

    def test_bymonth_june_december(self):
        ds = _dt(2026, 6, 15, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=YEARLY;BYMONTH=6,12"), _dt(2026, 6, 1), _dt(2028, 1, 1)
        )
        self.assertEqual(
            [o.strftime("%Y-%m-%d") for o in out],
            [
                "2026-06-15",
                "2026-12-15",
                "2027-06-15",
                "2027-12-15",
            ],
        )

    def test_invalid_month_day_skipped(self):
        ds = _dt(2024, 2, 29, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=YEARLY;BYMONTHDAY=29"), _dt(2025, 1, 1), _dt(2025, 12, 31)
        )
        self.assertEqual(out, [])


class TestBudget(unittest.TestCase):
    def test_count_is_global_budget_not_per_window(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=DAILY;COUNT=5"), _dt(2026, 11, 30), _dt(2027, 1, 1))
        # 全局第 3/4/5 条落在窗口内；窗口不会把 COUNT 重新数成 5 条
        self.assertEqual(_dates(out), ["2026-11-30 09:00", "2026-12-01 09:00", "2026-12-02 09:00"])

    def test_count_three_returns_three(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(ds, parse_rrule("FREQ=DAILY;COUNT=3"), _dt(2026, 11, 28), _dt(2027, 1, 1))
        self.assertEqual(len(out), 3)

    def test_until_boundary_inclusive(self):
        ds = _dt(2026, 11, 28, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=DAILY;UNTIL=20261130T090000"), _dt(2026, 11, 28), _dt(2026, 12, 3)
        )
        self.assertEqual(_dates(out), ["2026-11-28 09:00", "2026-11-29 09:00", "2026-11-30 09:00"])

    def test_until_utc_converted_to_dtstart_zone(self):
        ds = _dt(2026, 11, 25, 9, 0, tz=NY)
        out = expand_occurrences(
            ds,
            parse_rrule("FREQ=DAILY;UNTIL=20261127T140000Z"),
            _dt(2026, 11, 25, tz=NY),
            _dt(2026, 11, 29, tz=NY),
        )
        self.assertEqual(
            [o.strftime("%Y-%m-%d %z") for o in out],
            [
                "2026-11-25 -0500",
                "2026-11-26 -0500",
                "2026-11-27 -0500",
            ],
        )


class TestCrossYear(unittest.TestCase):
    def test_weekly_across_new_year_no_dup_no_gap(self):
        ds = _dt(2026, 12, 21, 9, 0)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;BYDAY=MO,WE,FR"), _dt(2026, 12, 28), _dt(2027, 1, 5)
        )
        self.assertEqual(
            [o.strftime("%Y-%m-%d %a") for o in out],
            [
                "2026-12-28 Mon",
                "2026-12-30 Wed",
                "2027-01-01 Fri",
                "2027-01-04 Mon",
            ],
        )


class TestDST(unittest.TestCase):
    def test_spring_forward_offset_changes_no_skip(self):
        ds = _dt(2026, 2, 23, 9, 0, tz=NY)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;BYDAY=MO"), _dt(2026, 2, 23, tz=NY), _dt(2026, 3, 17, tz=NY)
        )
        stamps = [(o.strftime("%Y-%m-%d"), o.strftime("%z")) for o in out]
        self.assertEqual(
            stamps,
            [
                ("2026-02-23", "-0500"),
                ("2026-03-02", "-0500"),
                ("2026-03-09", "-0400"),
                ("2026-03-16", "-0400"),
            ],
        )
        self.assertTrue(all(o.tzinfo is NY for o in out))

    def test_fall_back_offset_changes_no_duplicate(self):
        ds = _dt(2026, 10, 19, 9, 0, tz=NY)
        out = expand_occurrences(
            ds, parse_rrule("FREQ=WEEKLY;BYDAY=MO"), _dt(2026, 10, 19, tz=NY), _dt(2026, 11, 10, tz=NY)
        )
        stamps = [(o.strftime("%Y-%m-%d"), o.strftime("%z")) for o in out]
        self.assertEqual(
            stamps,
            [
                ("2026-10-19", "-0400"),
                ("2026-10-26", "-0400"),
                ("2026-11-02", "-0500"),
                ("2026-11-09", "-0500"),
            ],
        )
        self.assertEqual(len(out), len({o.isoformat() for o in out}))


class TestLimits(unittest.TestCase):
    def test_scan_year_cap_raises_expansion_limit(self):
        ds = _dt(2026, 1, 15, 9, 0)
        rule = parse_rrule("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30")
        with patch.object(rtc_rrule, "MAX_SCAN_YEARS", 1), self.assertRaises(ExpansionLimit):
            expand_occurrences(ds, rule, _dt(2026, 1, 1), _dt(2030, 1, 1))

    def test_occurrence_count_cap_raises_expansion_limit(self):
        ds = _dt(2026, 1, 1, 9, 0)
        rule = parse_rrule("FREQ=DAILY")
        with patch.object(rtc_rrule, "MAX_OCCURRENCES", 50), self.assertRaises(ExpansionLimit):
            expand_occurrences(ds, rule, _dt(2026, 1, 1), _dt(2030, 1, 1))


class TestExpandErrors(unittest.TestCase):
    def test_non_datetime_dtstart_rejected(self):
        with self.assertRaises(RruleError):
            expand_occurrences("2026-11-28", parse_rrule("FREQ=DAILY"), None, None)  # type: ignore[arg-type]

    def test_mixed_timezone_rejected(self):
        ds = datetime(2026, 11, 28, 9, 0)
        with self.assertRaises(RruleError):
            expand_occurrences(ds, parse_rrule("FREQ=DAILY"), _dt(2026, 11, 28), None)

    def test_naive_dtstart_with_aware_until_rejected(self):
        ds = datetime(2026, 11, 28, 9, 0)
        with self.assertRaises(RruleError):
            expand_occurrences(ds, parse_rrule("FREQ=DAILY;UNTIL=20261130T090000Z"), None, None)


if __name__ == "__main__":
    unittest.main()
