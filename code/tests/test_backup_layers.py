#!/usr/bin/env python3
"""备份冗余回归测试：cloud/nas 可选异地层。

运行：
    python code/tests/test_backup_layers.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))
import rtc_archive as archive  # noqa: E402
import rtc_core as core  # noqa: E402


class BackupLayerCase(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="rtc-bkl-"))
        (self.ws / "rules").mkdir()
        (self.ws / "state").mkdir()
        self.old_ws = getattr(core, "WORKSPACE", None)
        core.WORKSPACE = self.ws

    def tearDown(self):
        if self.old_ws is not None:
            core.WORKSPACE = self.old_ws
        shutil.rmtree(self.ws, ignore_errors=True)

    def write_cfg(self, data):
        (self.ws / "rules" / "backup.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_legacy_targets_only(self):
        """老格式 targets 数组照常工作。"""
        self.write_cfg({"targets": ["D:/bk", "E:/bk"], "keep": 30})
        targets, keep = archive.backup_config()
        self.assertEqual(targets, [Path("D:/bk"), Path("E:/bk")])
        self.assertEqual(keep, 30)

    def test_cloud_and_nas_added(self):
        """cloud/nas 填了就加入目标列表。"""
        self.write_cfg(
            {
                "targets": ["D:/bk"],
                "cloud": "C:/Users/me/OneDrive/RTC",
                "nas": "//nas/share/RTC",
                "keep": 30,
            }
        )
        targets, _ = archive.backup_config()
        self.assertIn(Path("D:/bk"), targets)
        self.assertIn(Path("C:/Users/me/OneDrive/RTC"), targets)
        self.assertTrue(any("nas" in str(p).lower() for p in targets))

    def test_empty_layers_skipped(self):
        """cloud/nas 为 null 或空串时跳过，不报错。"""
        self.write_cfg({"targets": ["D:/bk"], "cloud": None, "nas": "", "keep": 30})
        targets, _ = archive.backup_config()
        self.assertEqual(targets, [Path("D:/bk")])

    def test_no_duplicate_path(self):
        """同一路径在 targets 和 cloud 都写了，只保留一份。"""
        self.write_cfg({"targets": ["D:/bk"], "cloud": "D:/bk", "keep": 30})
        targets, _ = archive.backup_config()
        self.assertEqual(len(targets), 1)

    def test_no_config_returns_empty(self):
        """没配置文件返回空，不猜盘。"""
        targets, keep = archive.backup_config()
        self.assertEqual(targets, [])
        self.assertGreater(keep, 0)


if __name__ == "__main__":
    unittest.main()
