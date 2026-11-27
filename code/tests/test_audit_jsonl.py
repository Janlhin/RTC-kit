#!/usr/bin/env python3
"""audit.jsonl 写入层回归测试：字段、不可变、并发追加、失败不阻断。

回放重建用例见本文件后半段（replay 相关）。

运行：
    python code/tests/test_audit_jsonl.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_audit as audit  # noqa: E402
import rtc_core as core  # noqa: E402

CONCURRENCY_PROCS = 4
CONCURRENCY_PER_PROC = 25


class AuditWriterCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-audit-ws-"))
        (self.ws / "state").mkdir()
        self.old_ws = core.WORKSPACE
        core.WORKSPACE = self.ws
        audit.core.WORKSPACE = self.ws

    def tearDown(self):
        core.WORKSPACE = self.old_ws
        audit.core.WORKSPACE = self.old_ws
        shutil.rmtree(self.ws, ignore_errors=True)

    def read_lines(self) -> list[str]:
        p = audit.audit_path()
        if not p.exists():
            return []
        return p.read_text(encoding="utf-8").splitlines()

    def test_record_writes_one_line_with_required_fields(self):
        audit.record(
            "ack",
            "wake",
            actor=audit.ACTOR_USER,
            source="ack",
            old={"state": "ringing", "fireCount": 2},
            new={"state": "acked", "ackedAt": "2026-11-28T08:11:00+08:00"},
            reason="用户确认",
        )
        events = audit.read_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["action"], "ack")
        self.assertEqual(e["target"], "wake")
        self.assertEqual(e["actor"], "user")
        self.assertEqual(e["source"], "ack")
        self.assertEqual(e["old"]["fireCount"], 2)
        self.assertEqual(e["new"]["state"], "acked")
        self.assertEqual(e["reason"], "用户确认")
        # ts 必须带时区偏移，可被解析回 datetime
        ts = datetime.fromisoformat(e["ts"])
        self.assertIsNotNone(ts.tzinfo)

    def test_old_lines_are_byte_immutable(self):
        audit.record("add", "wake", actor=audit.ACTOR_USER, source="add-window", new={"id": "wake"})
        audit.record(
            "set-time",
            "wake",
            actor=audit.ACTOR_USER,
            source="set-time",
            old={"start": "07:00"},
            new={"start": "07:10"},
        )
        before = self.read_lines()
        prefix_hash = hashlib.sha256("\n".join(before).encode("utf-8")).hexdigest()

        audit.record("ack", "wake", actor=audit.ACTOR_USER, source="ack", new={"state": "acked"})
        after = self.read_lines()
        self.assertEqual(len(after), 3)
        # 前两行逐字节不变：追加只动文件尾部，不重写既有行。
        self.assertEqual(after[:2], before)
        self.assertEqual(hashlib.sha256("\n".join(after[:2]).encode("utf-8")).hexdigest(), prefix_hash)

    def test_unknown_actor_normalized_not_raised(self):
        audit.record("plan", "-", actor="external", source="plan", new={"rev": 1})
        e = audit.read_events()[0]
        self.assertEqual(e["actor"], "program")

    def test_write_failure_does_not_block_main_operation(self):
        # 把 state 路径压成一个普通文件：parent.mkdir 会失败，追加抛 OSError。
        blocker = self.ws / "state"
        shutil.rmtree(blocker)
        blocker.write_text("not-a-directory", encoding="utf-8")
        # record 必须自己接住异常，不向上抛。
        audit.record("plan", "-", actor=audit.ACTOR_PROGRAM, source="plan", new={"rev": 1})
        self.assertFalse(audit.audit_path().exists())

    def test_concurrent_multiprocess_append_no_torn_lines(self):
        env = dict(os.environ, RTC_WORKSPACE=str(self.ws), PYTHONIOENCODING="utf-8")
        procs = []
        code_dir = str(REPO / "code")
        for proc_idx in range(CONCURRENCY_PROCS):
            code = (
                f"import sys; sys.path.insert(0, r'{code_dir}');"
                "import rtc_audit as a;"
                f"[a.record('probe', 'win-{proc_idx}', actor='program', source='concurrency',"
                f" new={{'proc': {proc_idx}, 'i': i}}) for i in range({CONCURRENCY_PER_PROC})]"
            )
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            )
        for p in procs:
            p.wait()
            self.assertEqual(p.returncode, 0, p.stderr or p.stdout)
            p.stdout.close()
            p.stderr.close()

        events = audit.read_events()
        self.assertEqual(len(events), CONCURRENCY_PROCS * CONCURRENCY_PER_PROC)
        # 100% 可解析：read_events 已逐行 json.loads；再断言每条关键字段齐全。
        seen: set[tuple[int, int]] = set()
        for e in events:
            self.assertIn(e["action"], {"probe"})
            self.assertIsInstance(e["new"]["proc"], int)
            self.assertIsInstance(e["new"]["i"], int)
            seen.add((e["new"]["proc"], e["new"]["i"]))
        self.assertEqual(len(seen), CONCURRENCY_PROCS * CONCURRENCY_PER_PROC)

    def test_no_absolute_path_or_token_in_record(self):
        audit.record("add", "wake", actor=audit.ACTOR_USER, source="add-window", new={"id": "wake"})
        raw = audit.audit_path().read_text(encoding="utf-8")
        self.assertNotIn("C:\\", raw)
        self.assertNotIn("/home/", raw)
        self.assertNotIn("token", raw.lower())


RTC = REPO / "code" / "rtc.py"
FIXED_DATE = "2026-12-01"  # 周二：避开周末过滤，也避开周一的周报日


def _base_routine() -> dict:
    return {
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


class AuditReplayCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-audit-replay-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        self.old_ws = core.WORKSPACE
        core.WORKSPACE = self.ws
        audit.core.WORKSPACE = self.ws
        self.routine = _base_routine()
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(self.routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tearDown(self):
        core.WORKSPACE = self.old_ws
        audit.core.WORKSPACE = self.old_ws
        shutil.rmtree(self.ws, ignore_errors=True)

    def cli(self, *args: str, now: str | None = None) -> tuple[int, str, str]:
        env = dict(os.environ, RTC_WORKSPACE=str(self.ws), PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, str(RTC), "--workspace", str(self.ws), *args]
        if now is not None:
            cmd += ["--now", now]
        p = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, p.stdout, p.stderr

    def read_state(self) -> dict:
        p = self.ws / "state" / "alarms.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def read_routine(self) -> dict:
        p = self.ws / "rules" / "routines.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def test_replay_state_matches_real_snapshot_field_by_field(self):
        # checkpoint 1：plan 生成账本
        rc, out, err = self.cli("plan", now=f"{FIXED_DATE}T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        snap_plan = self.read_state()
        events = audit.read_events()
        rebuilt = audit.replay_state(events, at=f"{FIXED_DATE}T06:00:00+08:00")
        self.assertEqual(rebuilt, snap_plan, "plan 后回放必须与真实账本逐字段相等（含 rev）")

        # checkpoint 2：due 命中开火
        rc, out, err = self.cli("due", now=f"{FIXED_DATE}T07:00:00+08:00")
        self.assertEqual(rc, 0, err)
        snap_fire = self.read_state()
        events = audit.read_events()
        rebuilt = audit.replay_state(events, at=f"{FIXED_DATE}T07:00:00+08:00")
        self.assertEqual(rebuilt, snap_fire, "due-fire 后回放必须与真实账本逐字段相等（含 rev）")
        self.assertEqual(rebuilt["rev"], snap_fire["rev"])

        # checkpoint 3：ack 收口
        rc, out, err = self.cli(
            "ack", "--window", "wake", "--text", "起了", now=f"{FIXED_DATE}T07:05:00+08:00"
        )
        self.assertEqual(rc, 0, err)
        snap_ack = self.read_state()
        events = audit.read_events()
        rebuilt = audit.replay_state(events, at=f"{FIXED_DATE}T07:05:00+08:00")
        self.assertEqual(rebuilt, snap_ack, "ack 后回放必须与真实账本逐字段相等（含 rev）")
        self.assertEqual(rebuilt["rev"], snap_ack["rev"])

    def test_replay_cutoff_excludes_later_events(self):
        self.cli("plan", now=f"{FIXED_DATE}T06:00:00+08:00")
        self.cli("due", now=f"{FIXED_DATE}T07:00:00+08:00")
        self.cli("ack", "--window", "wake", "--text", "起了", now=f"{FIXED_DATE}T07:05:00+08:00")
        events = audit.read_events()
        # 只回放 plan 时点：窗口应还是 pending，fireCount 未消耗。
        rebuilt = audit.replay_state(events, at=f"{FIXED_DATE}T06:00:00+08:00")
        win = next(w for w in rebuilt["windows"] if w["id"] == "wake")
        self.assertEqual(win["state"], "pending")
        self.assertEqual(win["fireCount"], 0)

    def test_replay_routine_rebuilds_window_definition(self):
        rc, out, err = self.cli(
            "add-window",
            "--id",
            "milk",
            "--label",
            "牛奶",
            "--start",
            "08:00",
            "--end",
            "08:10",
            "--interval",
            "5",
            "--ack",
            "好",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        routine = self.read_routine()
        events = audit.read_events()
        rebuilt = audit.replay_routine(events, base=self.routine)
        by_id = {w["id"]: w for w in rebuilt["alarmWindows"]}
        self.assertIn("milk", by_id)
        real = next(w for w in routine["alarmWindows"] if w["id"] == "milk")
        self.assertEqual(by_id["milk"], real)


if __name__ == "__main__":
    unittest.main()
