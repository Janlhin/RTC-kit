import contextlib
import io
import json
from unittest.mock import patch

from test_rtc import RtcCase, base_routine, rtc


class TestPostponeStorageErrors(RtcCase):
    def setUp(self):
        super().setUp()
        self.write_routine(base_routine())
        self.cli("plan", now="2026-11-18T06:00:00+08:00")
        rtc.set_workspace(self.ws)

    def invoke(self):
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
