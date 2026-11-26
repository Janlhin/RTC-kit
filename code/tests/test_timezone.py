#!/usr/bin/env python3
"""#91 时区模型回归测试：IANA 解析、旧固定偏移回退、DST 偏移、双时区瞬时对齐。

零外部依赖（tzdata 仅测试层用于补 Windows 上的时区库），临时工作区。运行：
    python -m unittest code.tests.test_timezone -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_core as core  # noqa: E402

RTC = REPO / "code" / "rtc.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
UTC = UTC


def _routine(**over: object) -> dict[str, object]:
    r: dict[str, object] = {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "tzOffset": "+08:00",
        "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
        "alarmWindows": [],
        "rollcalls": [],
        "phases": [],
    }
    r.update(over)
    return r


class ResolveTzCase(unittest.TestCase):
    def tearDown(self):
        core.NOTICES.clear()

    def test_named_zone_loads_zoneinfo(self):
        tz = core.resolve_tz(_routine(timezone="America/New_York", tzOffset=None))
        self.assertIsInstance(tz, ZoneInfo)
        self.assertEqual(str(tz), "America/New_York")

    def test_legacy_only_tzoffset_keeps_fixed_offset(self):
        # 旧配置：没有 timezone，只有 tzOffset —— 行为必须与改造前逐字节一致
        tz = core.resolve_tz(_routine(timezone=None, tzOffset="+08:00"))
        self.assertEqual(tz.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=8))

    def test_bad_timezone_name_falls_back_to_tzoffset_and_notices(self):
        tz = core.resolve_tz(_routine(timezone="Mars/Olympus", tzOffset="+08:00"))
        self.assertEqual(tz.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=8))
        self.assertTrue(core.NOTICES, "不可用时必须留一条提示，不能静默吞掉")

    def test_bad_timezone_name_no_offset_falls_back_default(self):
        tz = core.resolve_tz(_routine(timezone="Not/AZone", tzOffset=None))
        self.assertEqual(tz.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=8))
        self.assertTrue(core.NOTICES)

    def test_unavailable_tzdata_fallback_branch(self):
        # 模拟 Windows 缺 tzdata：zoneinfo 全部加载失败 → 回退固定偏移，不崩
        original = core._load_zoneinfo
        core._load_zoneinfo = lambda name: None  # type: ignore[assignment]
        try:
            tz = core.resolve_tz(_routine(timezone="America/New_York", tzOffset="+08:00"))
        finally:
            core._load_zoneinfo = original
        self.assertEqual(tz.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=8))
        self.assertTrue(core.NOTICES)

    def test_missing_both_defaults_to_shanghai_offset(self):
        tz = core.resolve_tz(_routine(timezone=None, tzOffset=None))
        self.assertEqual(tz.utcoffset(datetime(2026, 7, 1, 12)), timedelta(hours=8))


class DstCase(unittest.TestCase):
    def setUp(self):
        self.ny = ZoneInfo("America/New_York")

    def _off(self, utc_iso: str) -> timedelta:
        return datetime.fromisoformat(utc_iso).astimezone(self.ny).utcoffset() or timedelta(0)

    def test_spring_forward_offset_changes_est_to_edt(self):
        # 2026-03-08 02:00 拨快到 03:00：切换前 -05:00（EST），切换后 -04:00（EDT）
        self.assertEqual(self._off("2026-03-07T12:00:00+00:00"), timedelta(hours=-5))
        self.assertEqual(self._off("2026-03-09T12:00:00+00:00"), timedelta(hours=-4))

    def test_fall_back_offset_changes_edt_to_est(self):
        # 2026-11-01 02:00 拨慢到 01:00：切换前 -04:00（EDT），切换后 -05:00（EST）
        self.assertEqual(self._off("2026-10-31T12:00:00+00:00"), timedelta(hours=-4))
        self.assertEqual(self._off("2026-11-02T12:00:00+00:00"), timedelta(hours=-5))

    def test_wall_clock_to_instant_roundtrip(self):
        # 纽约 2026-03-08 09:00（EDT, -04:00）= UTC 13:00
        local = datetime(2026, 3, 8, 9, 0, tzinfo=self.ny)
        self.assertEqual(local.astimezone(UTC).isoformat(), "2026-03-08T13:00:00+00:00")


class TwoTimezoneCase(unittest.TestCase):
    """同一行程跨两个时区：两个瞬时必须能对齐，墙钟各自正确。"""

    def test_ny_to_shanghai_instant_alignment(self):
        ny = ZoneInfo("America/New_York")
        sh = ZoneInfo("Asia/Shanghai")
        # 纽约 2026-11-18 08:00 EST（-05:00）= 上海 2026-11-18 21:00（+08:00）
        ny_local = datetime(2026, 11, 18, 8, 0, tzinfo=ny)
        sh_local = ny_local.astimezone(sh)
        self.assertEqual(sh_local.strftime("%H:%M"), "21:00")
        # 往返瞬时一致，不会因换时区漂移
        self.assertEqual(sh_local.astimezone(ny).astimezone(UTC), ny_local.astimezone(UTC))


class RoutineValidationCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-tz-"))
        (self.ws / "rules").mkdir()
        core.set_workspace(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _write(self, routine: dict[str, object]) -> None:
        core.ROUTINE.write_text(json.dumps(routine, ensure_ascii=False), encoding="utf-8")

    def test_legacy_routine_without_timezone_still_loads(self):
        self._write(_routine(timezone=None))  # 只留 tzOffset
        routine = core._validate_routine()
        self.assertEqual(routine["tzOffset"], "+08:00")

    def test_routine_without_either_tz_identifier_rejected(self):
        self._write(_routine(timezone=None, tzOffset=None))
        with self.assertRaises(core.Validation):
            core._validate_routine()


class ScheduleJsonCase(unittest.TestCase):
    """钉住既有对外口径：`schedule --json` 纯 JSON 到 stdout，退出码语义不变。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-sched-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(_routine(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _cli(self, *args: str) -> tuple[int, str, str]:
        p = subprocess.run(
            [sys.executable, str(RTC), "--workspace", str(self.ws), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ENV,
        )
        return p.returncode, p.stdout, p.stderr

    def test_schedule_json_is_pure_stdout(self):
        rc, _, err = self._cli("plan", "--now", "2026-12-15T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rc, out, err = self._cli("schedule", "2026-12-15", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "", "--json 模式不该往 stderr 灌内容")
        json.loads(out)  # stdout 必须能被解析成纯 JSON


if __name__ == "__main__":
    unittest.main()
