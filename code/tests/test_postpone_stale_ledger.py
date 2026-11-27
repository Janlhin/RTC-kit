"""issue #87 第 2 条：postpone 不得改写前一天的 occurrence / 快照。

方案 A（见 PR 正文「待裁决」留档）：账本缺失 / 为空 / 属于更早有效日期时，
只改时刻表（schedule-only），旧账本与前一天快照逐字节不动；非空账本缺
planDate / 非法 planDate / 未来 planDate 在时刻表写入前拒绝。

运行：
    python -m unittest discover -s code/tests -p "test_postpone_stale_ledger.py"
"""

import json

from test_rtc import RtcCase, base_routine, rtc


def _two_window_routine() -> dict:
    r = base_routine()
    r["alarmWindows"] = [
        {
            "id": "audit",
            "label": "审计",
            "start": "09:00",
            "end": "09:25",
            "intervalMin": 5,
            "expectFires": 6,
            "ackHints": ["好"],
        },
        {
            "id": "peer",
            "label": "对练",
            "start": "15:00",
            "end": "15:25",
            "intervalMin": 5,
            "expectFires": 6,
            "ackHints": ["好"],
        },
    ]
    return r


class TestStaleLedgerUntouched(RtcCase):
    """账本还是昨天的旧账时，推迟只能改时刻表，不能回头改昨天。"""

    def setUp(self):
        super().setUp()
        self.write_routine(_two_window_routine())
        rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rtc.set_workspace(self.ws)

    def test_stale_ledger_keeps_yesterday_state_and_snapshot_byte_identical(self):
        """旧账的 occurrence 与当天快照必须逐字节不变（issue #87 第 2 条）。"""
        state_before = rtc.STATE.read_bytes()
        snap_before = (rtc.SCHEDULE_DIR / "2026-11-18.json").read_bytes()

        rc, out, err = self.cli(
            "postpone",
            "--window",
            "audit",
            "--to",
            "2026-11-21",
            now="2026-11-19T06:00:00+08:00",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "postpone 由用户触发，stdout 必须为空")
        self.assertIn("旧账", err)
        self.assertIn("未改动当日条目", err)

        self.assertEqual(
            rtc.STATE.read_bytes(),
            state_before,
            "前一天账本逐字节不变：旧 occurrence 不能被改成 skipped",
        )
        self.assertEqual(
            (rtc.SCHEDULE_DIR / "2026-11-18.json").read_bytes(),
            snap_before,
            "前一天快照逐字节不变：不能回头重写昨天的安排",
        )
        audit = self.window("audit")
        self.assertEqual(audit["state"], "pending", "旧账里的 audit 仍是待响")
        self.assertFalse(
            (rtc.SCHEDULE_DIR / "2026-11-19.json").exists(),
            "schedule-only 推迟不得凭空生成今天的快照",
        )

        # 时刻表本身仍按方案 A 完成平移：今天（11-19）起进入 skipDates。
        routine = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        audit_def = next(w for w in routine["alarmWindows"] if w["id"] == "audit")
        self.assertIn("2026-11-19", audit_def["skipDates"])
        self.assertIn("2026-11-20", audit_def["skipDates"])

    def test_missing_ledger_stays_schedule_only(self):
        """没有账本时沿用既有 schedule-only 语义：不造账本 / 快照 / 历史事件。"""
        rtc.STATE.unlink(missing_ok=True)
        rc, out, err = self.cli(
            "postpone",
            "--window",
            "audit",
            "--to",
            "2026-11-21",
            now="2026-11-19T06:00:00+08:00",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertFalse(rtc.STATE.exists(), "缺账本时不得补造一条今日账本")
        self.assertFalse((rtc.SCHEDULE_DIR / "2026-11-19.json").exists())
        self.assertNotIn("partially applied", err)

    def test_invalid_or_future_plandate_rejects_before_routine_write(self):
        """非空账本缺 / 非法 / 未来 planDate：时刻表写入前就拒绝（rc=3）。"""
        cases = [
            {"rev": 1, "windows": []},  # 缺 planDate
            {"rev": 1, "windows": [], "planDate": "not-a-date"},
            {"rev": 1, "windows": [], "planDate": "2026-11-25"},  # 相对今天是未来
        ]
        for state in cases:
            with self.subTest(state=state):
                rtc.STATE.write_text(json.dumps(state), encoding="utf-8")
                before = rtc.ROUTINE.read_bytes()
                rc, out, _ = self.cli(
                    "postpone",
                    "--window",
                    "audit",
                    "--to",
                    "2026-11-21",
                    now="2026-11-19T06:00:00+08:00",
                )
                self.assertEqual(rc, 3)
                self.assertEqual(out, "")
                self.assertEqual(rtc.ROUTINE.read_bytes(), before, "校验失败不得写时刻表")

    def test_current_day_skips_target_and_leaves_peers_untouched(self):
        """对照口径：今天的账本照常跳过目标窗口，无关窗口保持原样。"""
        rc, out, err = self.cli(
            "postpone",
            "--window",
            "audit",
            "--to",
            "2026-11-21",
            now="2026-11-18T06:00:00+08:00",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        audit = self.window("audit")
        self.assertEqual(audit["state"], "skipped")
        self.assertEqual(audit["missedReason"], "postponed")
        self.assertEqual(self.window("peer")["state"], "pending")
