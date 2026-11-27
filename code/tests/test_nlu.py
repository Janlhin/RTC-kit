#!/usr/bin/env python3
"""NLU L0/L1 确定性中文时间解析回归测试。

覆盖：五句必测短语、同族表达、周锚点歧义、月末 / 跨年、DST 边界、不支持表达、
确定性（同输入同 now 两次解析一致），以及 tick 路径零依赖的静态 + 运行时双重断言。

零外部依赖（tzdata 仅测试层补 Windows 时区库）。运行：
    python -m unittest code.tests.test_nlu -v
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

import rtc_nlu  # noqa: E402

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")

# 固定「现在」：2026-11-28 周六 10:15（+08:00）。测试不许读系统时间。
NOW = datetime(2026, 11, 28, 10, 15, tzinfo=SH)


def _parse(phrase: str, now: datetime = NOW, tz: tzinfo | None = SH) -> rtc_nlu.ParsedTime:
    return rtc_nlu.parse_phrase(phrase, now, tz)


class RequiredPhrasesCase(unittest.TestCase):
    """需求表点名的五句必测短语，结果钉死。"""

    def test_tomorrow_afternoon_three(self):
        r = _parse("明天下午三点")
        self.assertEqual(r.date_iso, "2026-11-29")
        self.assertEqual(r.hhmm, "15:00")
        self.assertTrue(r.crosses_midnight)

    def test_next_wednesday(self):
        # 2026-11-28 周六；下一个自然周的周三 = 2026-12-02。
        r = _parse("下周三")
        self.assertEqual(r.date_iso, "2026-12-02")
        self.assertEqual(r.hhmm, "09:00")
        self.assertTrue(r.crosses_midnight)

    def test_three_days_later(self):
        r = _parse("3天后")
        self.assertEqual(r.date_iso, "2026-12-01")
        self.assertEqual(r.hhmm, "10:15")
        self.assertTrue(r.crosses_midnight)

    def test_half_hour_later(self):
        r = _parse("半小时后")
        self.assertEqual(r.date_iso, "2026-11-28")
        self.assertEqual(r.hhmm, "10:45")
        self.assertFalse(r.crosses_midnight)

    def test_first_of_next_month(self):
        r = _parse("下个月1号")
        self.assertEqual(r.date_iso, "2026-12-01")
        self.assertEqual(r.hhmm, "09:00")
        self.assertTrue(r.crosses_midnight)


class FamilyExpressionsCase(unittest.TestCase):
    """同族表达：日锚点 / 时段 / 时刻 / 周锚点 / 相对时长 / 月锚点。"""

    def test_day_offset_words(self):
        cases = {
            "今天": "2026-11-28",
            "明天": "2026-11-29",
            "后天": "2026-11-30",
            "大后天": "2026-12-01",
            "昨天": "2026-11-27",
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(_parse(phrase).date_iso, expected)

    def test_period_shifts(self):
        self.assertEqual(_parse("凌晨1点").hhmm, "01:00")
        self.assertEqual(_parse("早上7点").hhmm, "07:00")
        self.assertEqual(_parse("上午11点").hhmm, "11:00")
        self.assertEqual(_parse("中午12点").hhmm, "12:00")
        self.assertEqual(_parse("下午3点").hhmm, "15:00")
        self.assertEqual(_parse("晚上8点半").hhmm, "20:30")

    def test_clock_minute_forms(self):
        self.assertEqual(_parse("下午三点半").hhmm, "15:30")
        self.assertEqual(_parse("下午两点十分").hhmm, "14:10")
        self.assertEqual(_parse("7:05").hhmm, "07:05")

    def test_weekday_forms(self):
        # 裸周 X：本周内未到取本周，已过顺到下周；今天正好是该周几就取今天。
        self.assertEqual(_parse("周一").date_iso, "2026-11-30")
        self.assertEqual(_parse("本周六").date_iso, "2026-11-28")
        self.assertEqual(_parse("下周一").date_iso, "2026-11-30")

    def test_relative_durations(self):
        self.assertEqual(_parse("2小时后").hhmm, "12:15")
        self.assertEqual(_parse("20分钟后").hhmm, "10:35")
        self.assertEqual(_parse("一个半小时后").hhmm, "11:45")

    def test_this_month_day(self):
        self.assertEqual(_parse("这个月20号").date_iso, "2026-11-20")

    def test_chinese_tens_and_am_noon(self):
        # 中文「十」字拆分（十点半 / 十一点）与上午 12 点归 0 点两条分支。
        self.assertEqual(_parse("晚上十点半").hhmm, "22:30")
        self.assertEqual(_parse("晚上十一点").hhmm, "23:00")
        self.assertEqual(_parse("上午12点").hhmm, "00:00")


class WeekdayAmbiguityCase(unittest.TestCase):
    """今天就是周三时，「下周三」与「周三」的口径必须钉死。"""

    def setUp(self):
        # 2026-11-25 周三 10:00 +08:00。
        self.wed = datetime(2026, 11, 25, 10, 0, tzinfo=SH)

    def test_next_week_wednesday_is_next_natural_week(self):
        # 「下周三」= 下一个自然周的周三，不是「7 天后碰到的周三」，更不是今天。
        r = rtc_nlu.parse_phrase("下周三", self.wed, SH)
        self.assertEqual(r.date_iso, "2026-12-02")

    def test_bare_wednesday_is_today(self):
        r = rtc_nlu.parse_phrase("周三", self.wed, SH)
        self.assertEqual(r.date_iso, "2026-11-25")

    def test_this_week_wednesday_is_today(self):
        r = rtc_nlu.parse_phrase("本周三", self.wed, SH)
        self.assertEqual(r.date_iso, "2026-11-25")


class MonthEdgeCase(unittest.TestCase):
    """月末 / 跨年：「下个月1号」不能被月末天数夹住。"""

    def test_month_end_january_to_february_first(self):
        jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=SH)
        r = rtc_nlu.parse_phrase("下个月1号", jan31, SH)
        self.assertEqual(r.date_iso, "2026-02-01")

    def test_december_rolls_into_next_year(self):
        dec31 = datetime(2026, 12, 31, 9, 0, tzinfo=SH)
        r = rtc_nlu.parse_phrase("下个月1号", dec31, SH)
        self.assertEqual(r.date_iso, "2027-01-01")


class DstBoundaryCase(unittest.TestCase):
    """DST 边界：相对时长走瞬时运算，日锚点按本地墙钟，全程 IANA 时区。"""

    def test_spring_forward_half_hour(self):
        # 2026-03-08 01:30 EST（-05:00）+ 30 分钟 = 07:00Z（03:00 EDT）。
        spr = datetime(2026, 3, 8, 1, 30, tzinfo=NY)
        r = rtc_nlu.parse_phrase("半小时后", spr, NY)
        self.assertEqual(r.moment.astimezone(UTC).isoformat(), "2026-03-08T07:00:00+00:00")

    def test_fall_back_half_hour(self):
        # 2026-11-01 01:30 EDT（-04:00，05:30Z）+ 30 分钟 = 06:00Z（01:00 EST）。
        # 时长走 UTC 往返相加，不在 DST 边界上按墙钟重算偏移。
        fall = datetime(2026, 11, 1, 1, 30, tzinfo=NY)
        r = rtc_nlu.parse_phrase("半小时后", fall, NY)
        self.assertEqual(r.moment.astimezone(UTC).isoformat(), "2026-11-01T06:00:00+00:00")

    def test_date_anchor_across_spring_forward(self):
        # 2026-03-07 纽约；「明天上午9点」= 2026-03-08 09:00 EDT = 13:00Z。
        before = datetime(2026, 3, 7, 20, 0, tzinfo=NY)
        r = rtc_nlu.parse_phrase("明天上午9点", before, NY)
        self.assertEqual(r.date_iso, "2026-03-08")
        self.assertEqual(r.moment.astimezone(UTC).isoformat(), "2026-03-08T13:00:00+00:00")


class UnsupportedExpressionCase(unittest.TestCase):
    """契约表达不了的周期 / 集合表达：明确拒绝，不偷偷降级。"""

    def _assert_unsupported(self, phrase: str) -> None:
        with self.assertRaises(rtc_nlu.UnsupportedExpression) as ctx:
            _parse(phrase)
        self.assertTrue(ctx.exception.reason.startswith("现有契约不支持："))

    def test_yearly(self):
        self._assert_unsupported("每年11月5日交房租")

    def test_weekly_set(self):
        self._assert_unsupported("每周一三五锻炼")

    def test_monthly_last_workday(self):
        self._assert_unsupported("每月最后一个工作日复盘")

    def test_daily(self):
        self._assert_unsupported("每天早上8点吃药")


class RejectedPhraseCase(unittest.TestCase):
    """落在封闭词表之外、或日期 / 时刻越界：抛 NluError，不许静默兜底。"""

    def test_garbage(self):
        with self.assertRaises(rtc_nlu.NluError):
            _parse("嘀嘀嗒嗒")

    def test_invalid_month_day(self):
        # 11 月没有 31 号。
        with self.assertRaises(rtc_nlu.NluError):
            _parse("这个月31号")

    def test_naive_now_rejected(self):
        with self.assertRaises(rtc_nlu.NluError):
            rtc_nlu.parse_phrase("明天下午三点", datetime(2026, 11, 28, 10, 15))

    def test_out_of_range_clock(self):
        with self.assertRaises(rtc_nlu.NluError):
            _parse("下午25点")


class DeterminismCase(unittest.TestCase):
    """确定性证据：同输入 + 同 now 两次解析逐字段一致；模块源码无系统时间兜底。"""

    def test_repeat_parse_identical(self):
        first = _parse("下周三下午3点")
        second = _parse("下周三下午3点")
        self.assertEqual(first, second)
        self.assertEqual(first.moment, second.moment)

    def test_module_source_has_no_system_time_fallback(self):
        # AST 层扫描：解析器代码里不许出现 now() / today() / utcnow() 这类系统时间调用
        # （docstring 里提到词表不算）。
        tree = ast.parse((REPO / "code" / "rtc_nlu.py").read_text(encoding="utf-8"))
        forbidden = {"now", "today", "utcnow"}
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        for attr in calls:
            self.assertNotIn(attr, forbidden, f"解析器不得读系统时间：{attr}()")


# ----------------------------------------------------------------------------
# tick 路径零依赖：静态扫描 + 运行时 sys.modules 双重断言
# ----------------------------------------------------------------------------

CODE_DIR = REPO / "code"
# 唯一允许 import rtc_nlu 的交互入口；判定链路模块一律不准碰它。
ALLOWED_IMPORTERS = {"rtc_once.py"}
# rtc_plan / rtc_due 是模块级即跑 main 的 cron 薄壳，运行时探针不加载它们；
# 其依赖纪律由上方静态 AST 扫描覆盖。
CORE_MODULES = ["rtc_core", "rtc_archive", "rtc_brief", "rtc_history", "rtc_metrics", "rtc_recovery"]


def _imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TickPathIsolationStaticCase(unittest.TestCase):
    """静态 AST：除交互入口外，任何 code 模块都不许 import rtc_nlu。"""

    def test_no_tick_module_imports_nlu(self):
        offenders: list[str] = []
        for path in sorted(CODE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = _imported_modules(tree)
            references_nlu = any(name == "rtc_nlu" or name.endswith(".rtc_nlu") for name in imported)
            if references_nlu and path.name not in ALLOWED_IMPORTERS:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "tick / 判定链路不得依赖 rtc_nlu")

    def test_nlu_module_imports_no_core(self):
        # 反向也成立：解析器不许反向依赖核心模块，保持前端翻译层可独立测试。
        tree = ast.parse((CODE_DIR / "rtc_nlu.py").read_text(encoding="utf-8"))
        imported = _imported_modules(tree)
        self.assertFalse(any("rtc_core" in name or "rtc_once" in name for name in imported))


class TickPathIsolationRuntimeCase(unittest.TestCase):
    """运行时：全新子进程加载判定链路，sys.modules 里不得出现 rtc_nlu。"""

    def test_tick_modules_do_not_load_nlu(self):
        imports = ", ".join(CORE_MODULES)
        probe = (
            f"import sys;sys.path.insert(0, r'{CODE_DIR}');import {imports};print('rtc_nlu' in sys.modules);"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        self.assertEqual(proc.stdout.strip(), "False", "tick 路径运行时不得加载 rtc_nlu")


class OnceScriptNluCase(unittest.TestCase):
    """rtc_once.py 自然语言入口端到端：临时工作条子进程实跑。

    入口层的「现在」由脚本自身注入；这里只钉结构与退出码，解析确定性由
    ``DeterminismCase`` 用注入 now 的单测保证。
    """

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-once-nlu-"))
        (self.ws / "rules").mkdir(parents=True)
        (self.ws / "state").mkdir(parents=True)
        routine = {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "tzOffset": "+08:00",
            "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
            "alarmWindows": [
                {
                    "id": "wake",
                    "label": "起床",
                    "start": "07:00",
                    "end": "07:25",
                    "intervalMin": 5,
                    "expectFires": 6,
                    "ackHints": ["起了"],
                }
            ],
            "rollcalls": [],
            "phases": [],
        }
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.once = CODE_DIR / "rtc_once.py"

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.once), *args, "--workspace", str(self.ws)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _windows(self) -> list[dict[str, object]]:
        data = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        return list(data["alarmWindows"])

    def _registered_once_windows(self, proc: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
        """断言入口成功落盘，并返回 once 窗口；失败时带子进程全部输出与落盘原文。"""
        raw = (self.ws / "rules" / "routines.json").read_text(encoding="utf-8")
        diag = f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}\nroutines.json={raw}"
        self.assertEqual(proc.returncode, 0, diag)
        return [w for w in self._windows() if str(w.get("id", "")).startswith("once-")]

    def test_chinese_phrase_registers_oneoff(self):
        proc = self._run("明天下午三点", "看牙")
        wins = self._registered_once_windows(proc)
        self.assertEqual(len(wins), 1)
        win = wins[0]
        self.assertEqual(win["start"], "15:00")
        self.assertTrue(win["oneOff"])
        self.assertEqual(win["intervalMin"], 5)
        self.assertEqual(win["expectFires"], 1)
        self.assertEqual(win["validFrom"], win["validUntil"])
        # 日期格式合法即可；具体日历日由注入 now 的单测钉死，避免运行机时区干扰。
        self.assertRegex(str(win["validFrom"]), r"^\d{4}-\d{2}-\d{2}$")

    def test_recurring_expression_rejected_existing_exit_code(self):
        proc = self._run("每周一三五锻炼", "健身")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("现有契约不支持", proc.stderr)
        self.assertEqual([w for w in self._windows() if str(w.get("id", "")).startswith("once-")], [])

    def test_unparseable_phrase_rejected(self):
        proc = self._run("嘀嘀嗒嗒", "测试")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("无法识别", proc.stderr)

    def test_phrase_and_date_flag_conflict(self):
        proc = self._run("明天下午三点", "看牙", "--date", "2026-11-15")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("不能同时指定", proc.stderr)

    def test_legacy_hhmm_path_unchanged(self):
        proc = self._run("15:00", "一次性-测试")
        wins = self._registered_once_windows(proc)
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["start"], "15:00")


if __name__ == "__main__":
    unittest.main()
