"""issue #87 第 3 条：连续两次推迟，快照排除策略必须同一套语义。

方案 A（见 PR 正文「待裁决」留档）：快照 windows 是「剩余计划」，排除集合 =
当日所有 `state=skipped` 且 `missedReason=postponed` 的窗口；其它 skipped
原因保持可见；既有 results 不被重建抹掉，推迟证据留在 results / history。

运行：
    python -m unittest discover -s code/tests -p "test_postpone_snapshot_policy.py"
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


class TestSuccessivePostponeSnapshot(RtcCase):
    """同日连续推迟：第一次被推迟的窗口不许被第二次推迟带回快照。"""

    DAY = "2026-11-18"

    def setUp(self):
        super().setUp()
        self.write_routine(_two_window_routine())
        rc, _, err = self.cli("plan", now=f"{self.DAY}T06:00:00+08:00")
        self.assertEqual(rc, 0, err)
        rtc.set_workspace(self.ws)

    def _snap(self) -> dict:
        p = rtc.SCHEDULE_DIR / f"{self.DAY}.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def _ids(self) -> list[str]:
        return [w["id"] for w in self._snap()["windows"]]

    def test_two_successive_postpones_neither_reappears(self):
        rc, out, err = self.cli(
            "postpone", "--window", "audit", "--to", "2026-11-21", now=f"{self.DAY}T06:00:00+08:00"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(self._ids(), ["peer"], "第一次推迟后快照只剩未推迟的 peer")

        rc, out, err = self.cli(
            "postpone", "--window", "peer", "--to", "2026-11-21", now=f"{self.DAY}T06:00:00+08:00"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(self._ids(), [], "第二次推迟后 audit 不得被带回剩余计划")

        state = self.state()
        ledger = [(w["id"], w["state"], w.get("missedReason")) for w in state["windows"]]
        self.assertEqual(
            sorted(ledger),
            [("audit", "skipped", "postponed"), ("peer", "skipped", "postponed")],
            "账本两条仍都是推迟终态，证据不抹",
        )

    def test_other_skipped_reason_stays_visible(self):
        """取消（cancelled）不是推迟：重建快照时必须保留可见，不能一并删掉。"""
        rc, _, err = self.cli("cancel", "--window", "peer", now=f"{self.DAY}T08:10:00+08:00")
        self.assertEqual(rc, 0, err)

        rc, _, err = self.cli(
            "postpone", "--window", "audit", "--to", "2026-11-21", now=f"{self.DAY}T08:20:00+08:00"
        )
        self.assertEqual(rc, 0, err)

        snap = self._snap()
        ids = [w["id"] for w in snap["windows"]]
        self.assertIn("peer", ids, "cancelled 的 peer 必须留在快照里可见")
        self.assertNotIn("audit", ids, "postponed 的 audit 从剩余计划排除")
        peer = next(w for w in self.state()["windows"] if w["id"] == "peer")
        self.assertEqual(peer["state"], "cancelled")

    def test_rebuild_keeps_unrelated_results(self):
        """重建剩余计划快照时，已有的无关 results 不得被清空。"""
        snap_path = rtc.SCHEDULE_DIR / f"{self.DAY}.json"
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        snap["results"] = {"peer": {"state": "pending", "fires": 0, "ackedAt": None, "missedReason": None}}
        snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

        rc, _, err = self.cli(
            "postpone", "--window", "audit", "--to", "2026-11-21", now=f"{self.DAY}T08:20:00+08:00"
        )
        self.assertEqual(rc, 0, err)

        results = self._snap().get("results") or {}
        self.assertIn("peer", results, "无关窗口的既有 results 必须保留")
