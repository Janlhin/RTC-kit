import contextlib
import io
import json
import shlex
from unittest.mock import patch

from test_rtc import RtcCase, base_routine, rtc


class TestPostponeStorageErrors(RtcCase):
    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        rtc.set_workspace(self.ws)

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rtc.main(
                [
                    "--workspace",
                    str(self.ws),
                    "postpone",
                    "--window",
                    "wake",
                    "--to",
                    "2026-11-21",
                    "--now",
                    "2026-11-18T06:00:00+08:00",
                ]
                + list(extra)
            )
        return rc, out.getvalue(), err.getvalue()

    def test_bad_ledger_changes_no_schedule(self):
        before = rtc.ROUTINE.read_bytes()
        rtc.STATE.write_text("{", encoding="utf-8")
        rc, out, _ = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertEqual(rtc.ROUTINE.read_bytes(), before)

    def test_io_partial_and_explicit_retry(self):
        original = rtc.atomic_write

        def fail(path, *a, **kw):
            if path == rtc.STATE:
                raise OSError("injected storage failure")
            return original(path, *a, **kw)

        with patch.object(rtc, "atomic_write", fail):
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("partially applied", err)
        self.assertIn("--to 2026-11-21", err)
        self.assertEqual(self.window("wake")["state"], "pending")
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.window("wake")["state"], "skipped")

    def test_io_preserves_competing_edit(self):
        original = rtc.atomic_write

        def fail(path, *a, **kw):
            if path == rtc.STATE:
                rival = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
                rival["version"] = 77
                rtc.ROUTINE.write_text(json.dumps(rival), encoding="utf-8")
                raise OSError("injected failure after competing edit")
            return original(path, *a, **kw)

        with patch.object(rtc, "atomic_write", fail):
            rc, _, err = self.invoke()
        self.assertEqual(rc, 3)
        self.assertIn("partially applied", err)
        self.assertEqual(json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))["version"], 77)

    def test_invalid_required_fields_leave_routine_unchanged(self):
        for state in [
            {"windows": []},
            {"rev": "bad", "windows": []},
            {"rev": 1, "windows": False},
            {"rev": 1, "windows": [{"id": "wake"}]},
        ]:
            with self.subTest(state=state):
                rtc.STATE.write_text(json.dumps(state), encoding="utf-8")
                before = rtc.ROUTINE.read_bytes()
                rc, out, _ = self.invoke()
                self.assertEqual((rc, out), (3, ""))
                self.assertEqual(rtc.ROUTINE.read_bytes(), before)

    def test_deadline_retry_preserves_range(self):
        routine = base_routine()
        routine["alarmWindows"][0].update(validFrom="2026-11-18", validUntil="2026-11-20")
        self.write_routine(routine)
        original = rtc.atomic_write

        def fail(path, *a, **kw):
            if path == rtc.STATE:
                raise OSError("injected storage failure")
            return original(path, *a, **kw)

        with patch.object(rtc, "atomic_write", fail):
            rc, _, err = self.invoke("--to", "2026-11-25", "--deadline")
        self.assertEqual(rc, 3)
        retry = shlex.split(err.split("explicit target: ", 1)[1].strip())
        self.assertIn("--deadline", retry)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = rtc.main(["--workspace", str(self.ws), *retry, "--now", "2026-11-18T06:00:00+08:00"])
        self.assertEqual(rc, 0)
        window = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))["alarmWindows"][0]
        self.assertEqual((window["validFrom"], window["validUntil"]), ("2026-11-23", "2026-11-25"))

    def test_plan_creating_ledger_after_preflight_is_observed(self):
        rtc.STATE.unlink()
        original = rtc.atomic_write

        def interleave(path, *a, **kw):
            if path == rtc.ROUTINE:
                rc, _, err = self.cli("plan", now="2026-11-18T06:00:00+08:00")
                self.assertEqual(rc, 0, err)
            return original(path, *a, **kw)

        with patch.object(rtc, "atomic_write", interleave):
            rc, out, _ = self.invoke()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.window("wake")["state"], "skipped")

    def test_malformed_competing_state_reports_partial(self):
        original = rtc.atomic_write

        def interleave(path, *a, **kw):
            if path == rtc.STATE:
                path.write_text("[]", encoding="utf-8")
            return original(path, *a, **kw)

        with patch.object(rtc, "atomic_write", interleave):
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("partially applied", err)

    def test_empty_ledger_is_not_planned(self):
        rtc.STATE.write_text("{}", encoding="utf-8")
        rc, out, err = self.invoke()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(json.loads(rtc.STATE.read_text(encoding="utf-8")), {})
        routine = json.loads(rtc.ROUTINE.read_text(encoding="utf-8"))
        self.assertIn("2026-11-18", routine["alarmWindows"][0]["skipDates"])
        self.assertNotIn("partially applied", err)

    def test_empty_ledger_created_after_preflight_is_not_planned(self):
        original = rtc.atomic_write

        def interleave(path, *a, **kw):
            result = original(path, *a, **kw)
            if path == rtc.ROUTINE:
                rtc.STATE.write_text("{}", encoding="utf-8")
            return result

        with patch.object(rtc, "atomic_write", interleave):
            self.assertEqual(self.invoke()[:2], (0, ""))

    def test_history_exception_keeps_committed_state_and_syncs_snapshot(self):
        with patch.object(rtc, "_record_history", side_effect=OSError("history unavailable")):
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertEqual(self.window("wake")["state"], "skipped")
        snap = json.loads((rtc.SCHEDULE_DIR / "2026-11-18.json").read_text(encoding="utf-8"))
        self.assertNotIn("wake", [w["id"] for w in snap["windows"]])
        self.assertIn("state committed", err)
        self.assertIn("history update failed", err)
        self.assertIn("snapshot synchronized", err)
        self.assertNotIn("may still fire", err)
        self.assertNotIn("retry the same explicit target", err)

    def test_history_and_snapshot_exceptions_report_both(self):
        before = (rtc.SCHEDULE_DIR / "2026-11-18.json").read_bytes()
        with (
            patch.object(rtc, "_record_history", side_effect=OSError("history unavailable")),
            patch.object(rtc, "write_schedule_snapshot", side_effect=OSError("snapshot unavailable")),
        ):
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertEqual(self.window("wake")["state"], "skipped")
        self.assertIn("history update failed", err)
        self.assertIn("snapshot update failed", err)
        self.assertNotIn("may still fire", err)
        self.assertEqual((rtc.SCHEDULE_DIR / "2026-11-18.json").read_bytes(), before)

    def test_preflight_validation_has_one_command_prefix(self):
        rtc.STATE.write_text('{"rev":"bad","windows":[]}', encoding="utf-8")
        before = rtc.ROUTINE.read_bytes()
        rc, out, err = self.invoke()
        self.assertEqual((rc, out), (3, ""))
        self.assertEqual(err.count("postpone:"), 1)
        self.assertIn("schedule unchanged", err)
        self.assertEqual(rtc.ROUTINE.read_bytes(), before)
