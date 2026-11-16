#!/usr/bin/env python3
"""install.py 的接地契约。

装出来的技能文本必须是「可直接照着跑」的：

- 路径类占位符（`{{CODE_DIR}}` / `{{WORKSPACE_DIR}}` / `{{STATE_DIR}}` / `{{RULES_DIR}}`）全部填死；
- Windows 上不留混斜线路径（`C:\\x\\scripts/rtc.py` 这类）；
- 仓库里的技能**全部**装上（只装一个等于让模型手上只有一份能用的说明书）；
- 用户填的占位符（`{{DATE_COMPACT}}` 之类）安装器不猜，只提示。
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import install  # noqa: E402


class TestSubstituteSkill(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-ins-"))
        self.code = base / "scripts"
        self.ws = base / "ws"

    def test_path_placeholders_all_filled(self):
        for key in ("{{CODE_DIR}}", "{{WORKSPACE_DIR}}", "{{STATE_DIR}}", "{{RULES_DIR}}"):
            out = install.substitute_skill(key, self.code, self.ws)
            self.assertEqual(install.remaining_placeholders(out), [], f"{key} 未被填死")

    def test_no_mixed_separators(self):
        out = install.substitute_skill(
            "python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} ack", self.code, self.ws)
        # 「混斜线」= 出现了非本平台分隔符；POSIX 上 / 本来就是对的，不能反过来断言
        wrong = "/" if os.sep == "\\" else "\\"
        self.assertNotIn(str(self.code) + wrong, out, "拼出了混斜线路径")
        self.assertNotIn(str(self.ws) + wrong, out)
        self.assertIn(str(self.code) + os.sep + "rtc.py", out)

    def test_user_fill_placeholders_are_only_reported(self):
        out = install.substitute_skill("td-{{DATE_COMPACT}}-a", self.code, self.ws)
        self.assertEqual(install.remaining_placeholders(out), ["{{DATE_COMPACT}}"],
                         "安装器不该替用户猜日期这类值")


class TestInstallRun(unittest.TestCase):
    def test_installs_every_skill(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-run-"))
        args = Namespace(
            workspace=str(base / "ws"),
            harness_scripts_dir=str(base / "scripts"),
            harness_skills_dir=str(base / "skills"),
            code_src=None, tz_offset="+08:00", dry_run=False, setup_cron=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rc = install.run(args)
        self.assertEqual(rc, 0)

        expected = sorted(p.parent.name for p in (REPO / "skills").glob("*/SKILL.md"))
        written = sorted(p.parent.name for p in (base / "skills").glob("*/SKILL.md"))
        self.assertEqual(written, expected, "仓库里的技能应当全部装上")

        alarm = (base / "skills" / "alarm-clock" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(install.remaining_placeholders(alarm), [],
                         "alarm-clock 不该留任何占位符")
        wrong = "/" if os.sep == "\\" else "\\"
        self.assertNotIn(str(base / "scripts") + wrong, alarm, "装出来的技能里不该有混斜线路径")

        routine = base / "ws" / "rules" / "routines.json"
        self.assertTrue(routine.is_file(), "工作区应当有可运行的初始 routines.json")


class TestConsoleEncoding(unittest.TestCase):
    """CI 的 Windows runner 控制台是 cp1252：打印中文不能把安装器打死。"""

    def test_dry_run_survives_non_utf8_console(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [sys.executable, str(REPO / "install.py"),
             "--workspace", str(Path(tempfile.mkdtemp(prefix="rtc-enc-"))) + os.sep + "ws",
             "--dry-run"],
            env=env, capture_output=True, text=True, errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"cp1252 控制台下安装器崩了：{(proc.stderr or '')[-400:]}")


if __name__ == "__main__":
    unittest.main()
