#!/usr/bin/env python3
"""L2 P6：事件 / 旧窗口只读查询（list / show / export 占位）。

覆盖：

- 区间三选一：``--date`` / ``--year`` / ``--from``+``--to``；按日、按年、区间。
- 跨年归属：一次性按 start 日期落年；周期按有效期重叠，跨年周期事件两年都列出。
- 筛选：``--q``（大小写不敏感子串，AND）、``--kind once|recurring``、``--owner``。
- 空结果 rc=0；``--json`` 纯 JSON 到 stdout；非法参数 rc=4、stdout 为空。
- ``show``：uid 与旧别名 id 都能解析；未命中 rc=4。
- 旧命令回归：无 ``events.json`` 的工作区上，五条旧命令与 P1 基线逐字节一致。

运行：
    python -m unittest code.tests.test_query_p6 -v
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

import rtc_core as core  # noqa: E402
import rtc_events as events  # noqa: E402
import rtc_history as history  # noqa: E402

RTC = REPO / "code" / "rtc.py"
REPORT = REPO / "code" / "rtc_report.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

FIXTURE_DAY = "2026-11-30"
FIXTURE_NOW = f"{FIXTURE_DAY}T06:00:00+08:00"
DUE_NOW = f"{FIXTURE_DAY}T06:30:00+08:00"
P1_BASE = "30e5ab0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=ENV)
    return p.returncode, p.stdout, p.stderr


def _make_event(
    uid: str,
    kind: str,
    start: str,
    *,
    rrule: str | None = None,
    owner: str = "self",
    aliases: list[str] | None = None,
    deadline: str | None = None,
    status: str = "scheduled",
) -> dict:
    return {
        "uid": uid,
        "kind": kind,
        "start": start,
        "end": start,
        "rrule": rrule,
        "owner": owner,
        "deadline": deadline,
        "status": status,
        "reminderPolicy": {"preRemind": None, "ddlLadder": None, "snooze": None},
        "aliases": aliases or [],
        "createdAt": start,
        "updatedAt": start,
        "source": {"routineId": (aliases or [""])[0], "ledgerDate": None},
    }


def _fixture_routine() -> dict:
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
                "dateTypes": ["workday"],
            },
            {
                "id": "travel",
                "label": "旅游",
                "start": "09:00",
                "end": "09:15",
                "intervalMin": 5,
                "expectFires": 1,
                "ackHints": ["好了"],
                "oneOff": True,
                "validFrom": FIXTURE_DAY,
                "validUntil": FIXTURE_DAY,
            },
        ],
        "rollcalls": [],
        "phases": [],
    }


class _WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-p6-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_routine(self, routine: dict):
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(routine, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def cli(self, *args: str) -> tuple[int, str, str]:
        return _run([sys.executable, str(RTC), "--workspace", str(self.ws), *args])

    def seed_events(self, evs: list[dict]) -> None:
        env = events.empty_envelope()
        for ev in evs:
            events.upsert_event(env, ev)
        events.save_events(self.ws, env)

    def seed_history(self, day: str, windows: list[dict]) -> None:
        core.set_workspace(self.ws)
        history.sync_state({"planDate": day, "windows": windows}, None)

    def seed_fixture(self) -> None:
        self.seed_events(
            [
                _make_event(
                    "once-ny@rtc-kit",
                    "once",
                    "2026-12-31T23:30:00+08:00",
                    aliases=["ny-eve"],
                ),
                _make_event(
                    "daily-open@rtc-kit",
                    "recurring",
                    "2026-12-31T09:00:00+08:00",
                    rrule="FREQ=DAILY",
                    aliases=["open"],
                ),
                _make_event(
                    "until-week@rtc-kit",
                    "recurring",
                    "2026-01-05T09:00:00+08:00",
                    rrule="FREQ=WEEKLY;BYDAY=MO;UNTIL=20260110",
                    aliases=["old-week"],
                ),
                _make_event(
                    "bob@rtc-kit",
                    "once",
                    "2027-03-01T10:00:00+08:00",
                    owner="other:bob",
                    aliases=["bob-task"],
                ),
            ]
        )
        self.seed_history(
            "2026-12-31",
            [
                {
                    "id": "wake",
                    "label": "起床",
                    "start": "07:00",
                    "end": "07:25",
                    "intervalMin": 5,
                    "expectFires": 6,
                    "fireCount": 0,
                    "state": "pending",
                    "type": "fixed",
                }
            ],
        )
        self.seed_history(
            FIXTURE_DAY,
            [
                {
                    "id": "travel",
                    "label": "旅游",
                    "start": "09:00",
                    "end": "09:15",
                    "intervalMin": 5,
                    "expectFires": 1,
                    "fireCount": 1,
                    "ackedAt": f"{FIXTURE_DAY}T09:10:00+08:00",
                    "state": "delivered",
                    "type": "oneoff",
                }
            ],
        )

    def list_json(self, *args: str) -> tuple[int, dict, str]:
        rc, out, err = self.cli("list", *args, "--json")
        if rc == 0:
            return rc, json.loads(out), err
        return rc, {}, err

    @staticmethod
    def event_uids(payload: dict) -> set[str]:
        return {i["uid"] for i in payload.get("items", []) if i.get("source") == "event"}

    @staticmethod
    def window_ids(payload: dict) -> set[str]:
        return {i["windowId"] for i in payload.get("items", []) if i.get("source") == "window"}


class TestListScopes(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.seed_fixture()

    def test_date_scope_matches_event_and_window(self):
        rc, payload, err = self.list_json("--date", "2026-12-31")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"once-ny@rtc-kit", "daily-open@rtc-kit"})
        self.assertEqual(self.window_ids(payload), {"wake"})

    def test_year_2026_includes_active_events(self):
        rc, payload, err = self.list_json("--year", "2026")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.event_uids(payload),
            {"once-ny@rtc-kit", "daily-open@rtc-kit", "until-week@rtc-kit"},
        )
        self.assertNotIn("bob@rtc-kit", self.event_uids(payload))

    def test_year_2027_crosses_year_boundary(self):
        rc, payload, err = self.list_json("--year", "2027")
        self.assertEqual(rc, 0, err)
        # 无 UNTIL 的周期事件跨年两年都在；一次性事件只落 start 年；UNTIL 截止的不进 2027。
        self.assertEqual(self.event_uids(payload), {"daily-open@rtc-kit", "bob@rtc-kit"})

    def test_range_scope(self):
        rc, payload, err = self.list_json("--from", "2026-12-30", "--to", "2027-01-02")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"once-ny@rtc-kit", "daily-open@rtc-kit"})

    def test_empty_result_is_rc0_and_pure_json(self):
        rc, out, err = self.cli("list", "--date", "2026-01-01", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["count"], {"events": 0, "windows": 0})

    def test_text_mode_lists_both_sources(self):
        rc, out, err = self.cli("list", "--date", "2026-12-31")
        self.assertEqual(rc, 0, err)
        self.assertIn("事件", out)
        self.assertIn("旧窗口", out)
        self.assertIn("once-ny@rtc-kit", out)
        self.assertIn("wake", out)


class TestListFilters(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.seed_fixture()

    def test_kind_once(self):
        rc, payload, err = self.list_json("--year", "2026", "--kind", "once")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"once-ny@rtc-kit"})

    def test_kind_recurring(self):
        rc, payload, err = self.list_json("--year", "2026", "--kind", "recurring")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"daily-open@rtc-kit", "until-week@rtc-kit"})

    def test_kind_maps_old_oneoff_to_once(self):
        rc, payload, err = self.list_json("--date", FIXTURE_DAY, "--kind", "once")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.window_ids(payload), {"travel"})

    def test_owner_self(self):
        rc, payload, err = self.list_json("--year", "2027", "--owner", "self")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"daily-open@rtc-kit"})

    def test_owner_other(self):
        rc, payload, err = self.list_json("--year", "2027", "--owner", "other:bob")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"bob@rtc-kit"})

    def test_q_matches_alias_case_insensitive(self):
        rc, payload, err = self.list_json("--year", "2026", "--q", "NY-EVE")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"once-ny@rtc-kit"})

    def test_q_matches_owner(self):
        rc, payload, err = self.list_json("--year", "2027", "--q", "BOB")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.event_uids(payload), {"bob@rtc-kit"})

    def test_q_misses_returns_empty(self):
        rc, payload, err = self.list_json("--year", "2026", "--q", "zzz-no-hit")
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["items"], [])

    def test_q_matches_old_window_label(self):
        rc, payload, err = self.list_json("--date", FIXTURE_DAY, "--q", "旅游")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.window_ids(payload), {"travel"})

    def test_filters_are_and_combined(self):
        rc, payload, err = self.list_json("--year", "2026", "--kind", "once", "--q", "open")
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["items"], [])


class TestShow(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.seed_fixture()

    def test_show_by_uid_json(self):
        rc, out, err = self.cli("show", "bob@rtc-kit", "--json")
        self.assertEqual(rc, 0, err)
        ev = json.loads(out)
        self.assertEqual(ev["owner"], "other:bob")
        self.assertEqual(ev["deadline"], None)
        self.assertIn("reminderPolicy", ev)
        self.assertEqual(ev["aliases"], ["bob-task"])

    def test_show_by_alias_resolves_uid(self):
        rc, out, err = self.cli("show", "ny-eve")
        self.assertEqual(rc, 0, err)
        self.assertIn("uid: once-ny@rtc-kit", out)
        self.assertIn("reminderPolicy:", out)
        self.assertIn("aliases: ny-eve", out)

    def test_show_miss_is_rc4_empty_stdout(self):
        rc, out, err = self.cli("show", "nobody@rtc-kit")
        self.assertEqual(rc, 4)
        self.assertEqual(out, "")
        self.assertIn("找不到事件", err)


class TestInvalidArgs(_WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.seed_fixture()

    def _assert_rc4_empty_stdout(self, *args: str) -> None:
        rc, out, err = self.cli("list", *args)
        self.assertEqual(rc, 4, err)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")

    def test_no_scope(self):
        self._assert_rc4_empty_stdout()

    def test_bad_date(self):
        self._assert_rc4_empty_stdout("--date", "2026-13-99")

    def test_bad_year(self):
        self._assert_rc4_empty_stdout("--year", "26")

    def test_range_missing_to(self):
        self._assert_rc4_empty_stdout("--from", "2026-12-01")

    def test_range_from_after_to(self):
        self._assert_rc4_empty_stdout("--from", "2026-12-31", "--to", "2026-01-01")

    def test_two_modes(self):
        self._assert_rc4_empty_stdout("--year", "2026", "--date", "2026-11-30")

    def test_bad_kind(self):
        self._assert_rc4_empty_stdout("--year", "2026", "--kind", "bogus")

    def test_bad_owner(self):
        self._assert_rc4_empty_stdout("--year", "2026", "--owner", "boss")


class TestExportStub(_WorkspaceCase):
    def test_export_not_wired_yet(self):
        rc, out, err = self.cli("export", "--ics", "out.ics")
        self.assertEqual(rc, 4)
        self.assertEqual(out, "")
        self.assertIn("P2", err)


def _capture_five(ws: Path) -> dict[str, dict[str, str]]:
    commands: dict[str, list[str]] = {
        "status": [sys.executable, str(RTC), "--workspace", str(ws), "status", "--json"],
        "history": [sys.executable, str(RTC), "--workspace", str(ws), "history", FIXTURE_DAY],
        "schedule": [sys.executable, str(RTC), "--workspace", str(ws), "schedule", FIXTURE_DAY],
        "due": [sys.executable, str(RTC), "--workspace", str(ws), "due", "--now", DUE_NOW],
        "report": [sys.executable, str(REPORT), "--workspace", str(ws), "--date", FIXTURE_DAY],
    }
    snap: dict[str, dict[str, str]] = {}
    for name, cmd in commands.items():
        rc, out, err = _run(cmd)
        snap[name] = {"rc": str(rc), "stdout": _sha(out), "stderr": _sha(err)}
    return snap


class TestOldCommandsByteIdentical(unittest.TestCase):
    """无 events.json 的工作区：五条旧命令与 P1 基线（30e5ab0）逐字节一致。"""

    def test_five_commands_byte_identical_against_p1_base(self):
        work = Path(tempfile.mkdtemp(prefix="rtc-p6-base-"))
        base_wt = work / "base"
        ws_mine = work / "mine"
        ws_base = work / "base-ws"
        try:
            add = subprocess.run(
                ["git", "-C", str(REPO), "worktree", "add", "--detach", str(base_wt), P1_BASE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=ENV,
            )
            self.assertEqual(add.returncode, 0, add.stderr)

            for ws in (ws_mine, ws_base):
                (ws / "rules").mkdir(parents=True)
                (ws / "state").mkdir()
                (ws / "rules" / "routines.json").write_text(
                    json.dumps(_fixture_routine(), ensure_ascii=False, indent=2), encoding="utf-8"
                )
                plan_cmd = [
                    sys.executable,
                    str(RTC),
                    "--workspace",
                    str(ws),
                    "plan",
                    "--now",
                    FIXTURE_NOW,
                ]
                rc, _, err = _run(plan_cmd)
                self.assertEqual(rc, 0, err)

            # 基线工作区跑的是 worktree 里 P1 提交的 rtc.py。
            base_rtc = base_wt / "code" / "rtc.py"
            base_report = base_wt / "code" / "rtc_report.py"
            commands_base: dict[str, list[str]] = {
                "status": [sys.executable, str(base_rtc), "--workspace", str(ws_base), "status", "--json"],
                "history": [
                    sys.executable,
                    str(base_rtc),
                    "--workspace",
                    str(ws_base),
                    "history",
                    FIXTURE_DAY,
                ],
                "schedule": [
                    sys.executable,
                    str(base_rtc),
                    "--workspace",
                    str(ws_base),
                    "schedule",
                    FIXTURE_DAY,
                ],
                "due": [sys.executable, str(base_rtc), "--workspace", str(ws_base), "due", "--now", DUE_NOW],
                "report": [
                    sys.executable,
                    str(base_report),
                    "--workspace",
                    str(ws_base),
                    "--date",
                    FIXTURE_DAY,
                ],
            }
            base_snap: dict[str, dict[str, str]] = {}
            for name, cmd in commands_base.items():
                rc, out, err = _run(cmd)
                base_snap[name] = {"rc": str(rc), "stdout": _sha(out), "stderr": _sha(err)}

            mine_snap = _capture_five(ws_mine)
            for name in base_snap:
                with self.subTest(command=name):
                    self.assertEqual(base_snap[name]["rc"], mine_snap[name]["rc"], "退出码变了")
                    self.assertEqual(base_snap[name]["stdout"], mine_snap[name]["stdout"], "stdout 不一致")
                    self.assertEqual(base_snap[name]["stderr"], mine_snap[name]["stderr"], "stderr 不一致")
        finally:
            subprocess.run(
                ["git", "-C", str(REPO), "worktree", "remove", "--force", str(base_wt)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=ENV,
            )
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
