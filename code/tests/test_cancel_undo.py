#!/usr/bin/env python3
"""FR-3.4 误取消可撤销：`cancel --undo` 回归测试。

覆盖：
  - 取消 → 撤销后窗口逐字段回到取消前状态（state / fireCount / ackedAt / start / end …）
  - 撤销后再次 due 能按恢复后的状态正常响
  - 昨天及更早的取消拒绝撤销（当天有效）
  - 并发写冲突走既有 rev 乐观锁语义，不静默覆盖别人的写入
  - 没有痕迹（从未取消 / 永久停用）明确失败，不改账本、不凭空复活
  - 审计流里 cancel 与 cancel-undo 各一条，old/new 可逐字段对照

运行：
    python -m unittest discover -s code/tests -p "test_cancel_undo.py"
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc  # noqa: E402  (并发冲突用例在进程内直接调 cmd_cancel)
import rtc_audit as audit  # noqa: E402

RTC = REPO / "code" / "rtc.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

DAY = "2026-11-13"  # 周五（workday）
NEXT_DAY = "2026-11-14"  # 周六


def _routine() -> dict:
    return {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "tzOffset": "+08:00",
        "dateTypes": {"workday": [1, 2, 3, 4, 5], "weekend": [0, 6], "holidayOverrides": []},
        "alarmWindows": [
            {
                "id": "tea",
                "label": "喝茶",
                "start": "15:00",
                "end": "15:25",
                "intervalMin": 5,
                "expectFires": 6,
                "ackHints": ["好了"],
            },
            {
                "id": "report",
                "label": "助理汇报",
                "start": "14:00",
                "end": "14:30",
                "intervalMin": 5,
                "expectFires": 7,
                "ackHints": ["好了"],
            },
        ],
        "rollcalls": [],
        "phases": [],
    }


class TestCancelUndo(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-undo-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(_routine(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def cli(self, *args: str, now: str | None = None) -> tuple[int, str, str]:
        cmd = [sys.executable, str(RTC), "--workspace", str(self.ws), *args]
        if now:
            cmd += ["--now", now]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
        return p.returncode, p.stdout, p.stderr

    def state(self) -> dict:
        p = self.ws / "state" / "alarms.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def window(self, wid: str) -> dict:
        return next((w for w in self.state().get("windows", []) if w["id"] == wid), {})

    def events(self) -> list[dict]:
        return audit.read_events(self.ws / "state" / "audit.jsonl")

    def _plan_fire_cancel(self, cancel_at: str = f"{DAY}T15:05:00+08:00"):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("due", now=f"{DAY}T15:00:00+08:00")
        pre = deepcopy(self.window("tea"))
        rc, _, err = self.cli("cancel", "--window", "tea", now=cancel_at)
        self.assertEqual(rc, 0, err)
        return pre

    # ---------- ① 往返逐字段一致 ----------

    def test_undo_restores_window_field_by_field(self):
        pre = self._plan_fire_cancel()
        report_pre = deepcopy(self.window("report"))
        routine_pre = (self.ws / "rules" / "routines.json").read_text(encoding="utf-8")

        rc, out, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:06:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "", "交互命令 stdout 必须为空")

        self.assertEqual(self.window("tea"), pre, "撤销后必须逐字段回到取消前状态")
        self.assertEqual(self.window("report"), report_pre, "只动被取消的那次，另一个窗口不受影响")
        self.assertEqual(
            (self.ws / "rules" / "routines.json").read_text(encoding="utf-8"),
            routine_pre,
            "周期窗口的撤销不改时刻表、不影响其它日期",
        )

    # ---------- ② 撤销后再次 due 正常响 ----------

    def test_undo_then_due_fires_again(self):
        self._plan_fire_cancel()
        rc, _, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:06:00+08:00")
        self.assertEqual(rc, 0, err)

        rc, out, err = self.cli("due", now=f"{DAY}T15:10:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertIn("喝茶", out, "恢复后到点必须正常投递")
        self.assertEqual(self.window("tea")["fireCount"], 2, "恢复的 fireCount 接着累计")

    # ---------- ③ 跨日拒绝 ----------

    def test_undo_rejects_yesterday_trace(self):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        rc, _, err = self.cli("cancel", "--window", "tea", now=f"{DAY}T08:00:00+08:00")
        self.assertEqual(rc, 0, err)

        # 次日重新出账；旧痕迹留在审计流里，但已跨日。
        self.cli("plan", now=f"{NEXT_DAY}T06:00:00+08:00")
        rc, out, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{NEXT_DAY}T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertEqual(out, "")
        self.assertIn("昨天", err)
        self.assertEqual(self.window("tea")["state"], "pending", "跨日不得把旧痕迹恢复到今天账本")
        self.assertFalse(any(e["action"] == "cancel-undo" for e in self.events()), "跨日拒绝不得留撤销审计")

    def test_undo_rejects_stale_ledger(self):
        """账本属于昨天、今天来撤销：先拦账本日期，不读旧痕迹恢复。"""
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("cancel", "--window", "tea", now=f"{DAY}T08:00:00+08:00")

        rc, _, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{NEXT_DAY}T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("不是今天", err)
        self.assertFalse(any(e["action"] == "cancel-undo" for e in self.events()))

    # ---------- ④ 并发冲突：走既有 rev 语义，不静默丢写 ----------

    def test_undo_conflict_keeps_concurrent_write(self):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("due", now=f"{DAY}T15:00:00+08:00")
        self.cli("cancel", "--window", "tea", now=f"{DAY}T15:05:00+08:00")
        rtc.set_workspace(self.ws)

        orig = rtc.atomic_write

        def fake(path, data, expected_rev=None, expected_digest=None):
            if path == rtc.STATE:
                raise rtc.Conflict("injected: state changed concurrently")
            return orig(path, data, expected_rev, expected_digest)

        rtc.atomic_write = fake
        self.addCleanup(lambda: setattr(rtc, "atomic_write", orig))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rtc.cmd_cancel(
                argparse.Namespace(window="tea", undo=True, date=None, now=f"{DAY}T15:06:00+08:00")
            )
        self.assertEqual(rc, 2, "rev 冲突按既有口径退出 2")
        self.assertIn("状态冲突", err.getvalue())

        hit = self.window("tea")
        self.assertEqual(hit["state"], "cancelled", "冲突时不得覆盖账本，取消状态保留")
        self.assertFalse(
            any(e["action"] == "cancel-undo" for e in self.events()),
            "冲突失败不得写撤销审计",
        )

    # ---------- ⑤ 无痕迹失败：不改账本、不凭空复活 ----------

    def test_undo_without_trace_fails_cleanly(self):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        before = self.state()

        rc, out, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T08:00:00+08:00")
        self.assertEqual(rc, 4)
        self.assertEqual(out, "")
        self.assertIn("没有可撤销的取消记录", err)
        self.assertEqual(self.state(), before, "无痕迹不得改动账本")
        self.assertFalse(any(e["action"] == "cancel-undo" for e in self.events()))

    def test_undo_cannot_resurrect_disabled(self):
        """永久停用（disable）没有 cancel 痕迹：undo 必须失败，不得复活。"""
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("disable", "--window", "tea", now=f"{DAY}T08:00:00+08:00")

        rc, _, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T08:01:00+08:00")
        self.assertEqual(rc, 4)

        routine = json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))
        tea = next(w for w in routine["alarmWindows"] if w["id"] == "tea")
        self.assertTrue(tea["disabled"], "永久停用状态不受 undo 影响")
        self.assertEqual(self.window("tea")["state"], "cancelled")
        self.assertFalse(any(e["action"] == "cancel-undo" for e in self.events()))

    def test_undo_missing_ledger_occurrence_fails(self):
        """痕迹还在，但今天账本里已没有这次实例：拒绝凭空造窗口。"""
        self._plan_fire_cancel()
        state = self.state()
        state["windows"] = [w for w in state["windows"] if w["id"] != "tea"]
        (self.ws / "state" / "alarms.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        rc, _, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:06:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("不能凭空恢复", err)
        self.assertNotIn("tea", [w["id"] for w in self.state().get("windows", [])])

    def test_undo_audit_unreadable_fails_cleanly(self):
        """审计流读不出来（坏行 / 损坏）时按无痕迹安全失败，不冒泡 Traceback。"""
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("cancel", "--window", "tea", now=f"{DAY}T08:00:00+08:00")
        rtc.set_workspace(self.ws)

        orig = audit.read_events

        def boom(*_args: object, **_kwargs: object) -> list[dict]:
            raise ValueError("torn line")

        audit.read_events = boom
        self.addCleanup(lambda: setattr(audit, "read_events", orig))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rtc.cmd_cancel(
                argparse.Namespace(window="tea", undo=True, date=None, now=f"{DAY}T08:01:00+08:00")
            )
        self.assertEqual(rc, 4)
        self.assertIn("读不出来", err.getvalue())
        self.assertEqual(self.window("tea")["state"], "cancelled", "失败不得改动账本")

    def test_second_undo_rejected(self):
        self._plan_fire_cancel()
        rc, _, _ = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:06:00+08:00")
        self.assertEqual(rc, 0)
        rc, _, err = self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:07:00+08:00")
        self.assertEqual(rc, 4)
        self.assertIn("不是取消状态", err)

    def test_undo_rejects_date_combo(self):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        rc, _, err = self.cli(
            "cancel",
            "--window",
            "tea",
            "--undo",
            "--date",
            NEXT_DAY,
            now=f"{DAY}T08:00:00+08:00",
        )
        self.assertEqual(rc, 3)
        self.assertIn("--date", err)

    # ---------- ⑥ 审计流：cancel / cancel-undo 各一条，old/new 可对照 ----------

    def test_cancel_and_undo_audit_events_comparable(self):
        self.cli("plan", now=f"{DAY}T06:00:00+08:00")
        self.cli("due", now=f"{DAY}T15:00:00+08:00")
        pre = deepcopy(self.window("tea"))
        self.cli("cancel", "--window", "tea", now=f"{DAY}T15:05:00+08:00")
        self.cli("cancel", "--window", "tea", "--undo", now=f"{DAY}T15:06:00+08:00")

        events = self.events()
        cancel_events = [e for e in events if e["action"] == "cancel" and e["source"] == "cancel"]
        undo_events = [e for e in events if e["action"] == "cancel-undo"]
        self.assertEqual(len(cancel_events), 1)
        self.assertEqual(len(undo_events), 1)

        canceled, undone = cancel_events[0], undo_events[0]
        self.assertEqual(canceled["old"]["window"], pre, "cancel 的 old 必须是取消前快照")
        self.assertEqual(canceled["new"]["window"]["state"], "cancelled")
        self.assertEqual(undone["old"]["window"]["state"], "cancelled")
        self.assertEqual(undone["new"]["window"], pre, "cancel-undo 的 new 必须回到取消前快照")
        self.assertEqual(undone["reason"], "undo 撤销今天的取消")


if __name__ == "__main__":
    unittest.main()
