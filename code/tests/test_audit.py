#!/usr/bin/env python3
"""审计溯源回归测试：events 表的 actor/source 跨代兼容。

运行：
    python code/tests/test_audit.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_core as core  # noqa: E402
import rtc_history as hist  # noqa: E402


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-audit-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        self.old_ws = getattr(core, "WORKSPACE", None)
        core.WORKSPACE = self.ws

    def tearDown(self):
        if self.old_ws is not None:
            core.WORKSPACE = self.old_ws
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_legacy_call_defaults_to_system_cli(self):
        """现有调用不传 actor/source，仍可写、默认 system/cli。"""
        hist.log_event("ack", "wake", note="老调用")
        rows = hist.events("wake")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "system")
        self.assertEqual(rows[0]["source"], "cli")

    def test_new_call_records_actor_source(self):
        """新调用能记下谁改的、从哪来。"""
        hist.log_event("postpone", "wake", actor="model", source="wechat",
                       from_date="2026-11-18", to_date="2026-11-20",
                       note="用户说推迟两天")
        row = hist.events("wake")[0]
        self.assertEqual(row["actor"], "model")
        self.assertEqual(row["source"], "wechat")
        self.assertEqual(row["from_date"], "2026-11-18")
        self.assertEqual(row["to_date"], "2026-11-20")

    def test_old_db_migrates_without_data_loss(self):
        """老 db（无 actor/source 列）打开后自动补列，旧数据不丢。"""
        db = self.ws / "state" / "history.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, date TEXT,
            window_id TEXT NOT NULL, item_id TEXT, kind TEXT NOT NULL,
            from_date TEXT, to_date TEXT, note TEXT)""")
        con.execute("INSERT INTO events (ts, window_id, kind, note) VALUES (?,?,?,?)",
                    ("2026-01-01T00:00:00+00:00", "old", "ack", "迁移前的数据"))
        con.commit()
        con.close()
        # 触发建表/迁移
        hist.log_event("ack", "new")
        rows = hist.events()
        self.assertEqual(len(rows), 2)
        old = [r for r in rows if r["window_id"] == "old"][0]
        self.assertEqual(old["note"], "迁移前的数据")
        self.assertEqual(old["actor"], "system")  # 默认值

    def test_history_answers_who_changed(self):
        """能回答"谁改的"：同一个窗口的变更序列带不同 actor。"""
        hist.log_event("create", "w1", actor="user", source="cli")
        hist.log_event("edit", "w1", actor="model", source="wechat",
                       from_date="2026-11-20", to_date="2026-11-25")
        hist.log_event("ack", "w1", actor="user", source="cron")
        rows = hist.events("w1")
        self.assertEqual([r["kind"] for r in rows], ["create", "edit", "ack"])
        self.assertEqual([r["actor"] for r in rows], ["user", "model", "user"])
        self.assertEqual([r["source"] for r in rows], ["cli", "wechat", "cron"])


if __name__ == "__main__":
    unittest.main()
