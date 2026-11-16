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
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
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
            "python {{CODE_DIR}}/rtc.py --workspace {{WORKSPACE_DIR}} ack", self.code, self.ws
        )
        # 「混斜线」= 出现了非本平台分隔符；POSIX 上 / 本来就是对的，不能反过来断言
        wrong = "/" if os.sep == "\\" else "\\"
        self.assertNotIn(str(self.code) + wrong, out, "拼出了混斜线路径")
        self.assertNotIn(str(self.ws) + wrong, out)
        self.assertIn(str(self.code) + os.sep + "rtc.py", out)

    def test_user_fill_placeholders_are_only_reported(self):
        out = install.substitute_skill("td-{{DATE_COMPACT}}-a", self.code, self.ws)
        self.assertEqual(
            install.remaining_placeholders(out), ["{{DATE_COMPACT}}"], "安装器不该替用户猜日期这类值"
        )


class TestInstallRun(unittest.TestCase):
    def test_installs_every_skill(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-run-"))
        args = Namespace(
            workspace=str(base / "ws"),
            harness_scripts_dir=str(base / "scripts"),
            harness_skills_dir=str(base / "skills"),
            code_src=None,
            tz_offset="+08:00",
            dry_run=False,
            setup_cron=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rc = install.run(args)
        self.assertEqual(rc, 0)

        expected = sorted(p.parent.name for p in (REPO / "skills").glob("*/SKILL.md"))
        written = sorted(p.parent.name for p in (base / "skills" / "rtc").glob("*/SKILL.md"))
        self.assertEqual(written, expected, "仓库里的技能应当全部装上")

        alarm = (base / "skills" / "rtc" / "alarm-clock" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(install.remaining_placeholders(alarm), [], "alarm-clock 不该留任何占位符")
        wrong = "/" if os.sep == "\\" else "\\"
        self.assertNotIn(str(base / "scripts") + wrong, alarm, "装出来的技能里不该有混斜线路径")

        routine = base / "ws" / "rules" / "routines.json"
        self.assertTrue(routine.is_file(), "工作区应当有可运行的初始 routines.json")

    def test_skills_are_nested_under_category(self):
        """D-01：宿主按父目录识别技能；平铺写等于装上了却看不见。"""
        base = Path(tempfile.mkdtemp(prefix="rtc-cat-"))
        args = Namespace(
            workspace=str(base / "ws"),
            harness_scripts_dir=str(base / "scripts"),
            harness_skills_dir=str(base / "skills"),
            code_src=None,
            tz_offset="+08:00",
            dry_run=False,
            setup_cron=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            install.run(args)
        self.assertTrue(
            (base / "skills" / "rtc" / "alarm-clock" / "SKILL.md").is_file(),
            "技能必须落在 skills/<类别>/<技能>/ 下",
        )
        self.assertFalse((base / "skills" / "alarm-clock").exists(), "不该再平铺一份到 skills/<技能>/")

    def test_flat_layout_is_opt_in(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-flat-"))
        args = Namespace(
            workspace=str(base / "ws"),
            harness_scripts_dir=str(base / "scripts"),
            harness_skills_dir=str(base / "skills"),
            code_src=None,
            tz_offset="+08:00",
            dry_run=False,
            setup_cron=False,
            skills_category="",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            install.run(args)
        self.assertTrue(
            (base / "skills" / "alarm-clock" / "SKILL.md").is_file(),
            "skills_category 为空时应当平铺（给非 Hermes 宿主留的路）",
        )


class TestScaffoldWorkspace(unittest.TestCase):
    """装完就得是一个「能跑」的工作区，而不是让用户面对空白模板。"""

    def _base(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-scaf-"))
        self.base = base
        return Namespace(
            workspace=str(base / "ws"),
            harness_scripts_dir=str(base / "scripts"),
            harness_skills_dir=str(base / "skills"),
            code_src=None,
            tz_offset="+08:00",
            dry_run=False,
            setup_cron=False,
        )

    def _run(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = install.run(args)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_creates_a_runnable_workspace(self):
        args = self._base()
        self._run(args)
        ws = Path(args.workspace)

        routine = ws / "rules" / "routines.json"
        data = json.loads(routine.read_text(encoding="utf-8"))
        self.assertEqual(len(data["alarmWindows"]), 4, "开箱默认时刻表要有内容，不是空白示例")

        backup = ws / "rules" / "backup.json"
        self.assertTrue(backup.is_file(), "备份配置要装好，否则每日备份永远不跑")
        cfg = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(cfg["keep"], 30)
        self.assertIsInstance(cfg["targets"], list)
        # 目标里不能出现源盘自己的盘符（同盘不算备份）
        src = ws.drive.upper()
        for t in cfg["targets"]:
            self.assertNotEqual(Path(t).drive.upper(), src, f"{t} 落在源盘上，不算多盘备份")

        for rel in ("AGENTS.md", "tasks.md", "errors.md"):
            path = ws / rel
            self.assertTrue(path.is_file(), f"工作区缺 {rel}")
            self.assertTrue(path.read_text(encoding="utf-8").strip(), f"{rel} 是空的")
        agents = (ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(install.remaining_placeholders(agents), [], "AGENTS.md 里的路径占位符应当被填死")

    def test_default_routine_actually_plans(self):
        args = self._base()
        self._run(args)
        ws = Path(args.workspace)
        proc = subprocess.run(
            [sys.executable, str(self.base / "scripts" / "rtc.py"), "--workspace", str(ws), "plan"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"plan 失败：{proc.stderr[-400:]}")
        alarms = json.loads((ws / "state" / "alarms.json").read_text(encoding="utf-8"))
        self.assertEqual(len(alarms["windows"]), 4, "默认时刻表应能生成 4 个今天的窗口")

    def test_rerun_keeps_user_data(self):
        args = self._base()
        self._run(args)
        ws = Path(args.workspace)
        routine = ws / "rules" / "routines.json"
        mine = json.loads(routine.read_text(encoding="utf-8"))
        mine["alarmWindows"][0]["start"] = "07:30"
        routine.write_text(json.dumps(mine, ensure_ascii=False), encoding="utf-8")
        tasks = ws / "tasks.md"
        tasks.write_text(tasks.read_text(encoding="utf-8") + "\n- [ ] 我的任务\n", encoding="utf-8")

        out = self._run(Namespace(**vars(args)))
        self.assertIn("已存在，跳过", out)
        self.assertEqual(
            json.loads(routine.read_text(encoding="utf-8"))["alarmWindows"][0]["start"],
            "07:30",
            "重装把用户改过的时刻表覆盖了",
        )
        self.assertIn("我的任务", tasks.read_text(encoding="utf-8"), "重装把用户的任务清掉了")

    def test_force_overwrites_on_purpose(self):
        args = self._base()
        self._run(args)
        routine = Path(args.workspace) / "rules" / "routines.json"
        mine = json.loads(routine.read_text(encoding="utf-8"))
        mine["alarmWindows"][0]["start"] = "07:30"
        routine.write_text(json.dumps(mine, ensure_ascii=False), encoding="utf-8")

        args.force = True
        self._run(args)
        self.assertEqual(
            json.loads(routine.read_text(encoding="utf-8"))["alarmWindows"][0]["start"],
            install.default_routine()["alarmWindows"][0]["start"],
            "--force 应当明确地重建默认时刻表",
        )

    def test_dry_run_touches_nothing(self):
        args = self._base()
        args.dry_run = True
        self._run(args)
        self.assertFalse(
            (Path(args.workspace) / "rules" / "routines.json").exists(), "--dry-run 不该写任何文件"
        )


class TestSmoke(unittest.TestCase):
    def test_target_is_two_minutes_later(self):
        now = datetime(2026, 11, 16, 23, 58)
        self.assertEqual(install.smoke_target(now), "00:00", "跨零点也要算对")
        self.assertEqual(install.smoke_target(datetime(2026, 11, 16, 9, 3)), "09:05")

    def test_window_is_wide_enough_for_a_tick(self):
        # 每 5 分钟一跳，窗口只要 >= 4 分钟就必然盖住至少一跳
        self.assertGreaterEqual(install.SMOKE_WINDOW_MIN, 4)

    def test_smoke_without_scripts_fails_loudly(self):
        base = Path(tempfile.mkdtemp(prefix="rtc-smoke-"))
        rc = install.run_smoke(base / "nope", base / "ws", wait_sec=1)
        self.assertEqual(rc, 2, "脚本缺失时应当明确失败，而不是假装通过")


class TestConsoleEncoding(unittest.TestCase):
    """CI 的 Windows runner 控制台是 cp1252：打印中文不能把安装器打死。"""

    def test_dry_run_survives_non_utf8_console(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO / "install.py"),
                "--workspace",
                str(Path(tempfile.mkdtemp(prefix="rtc-enc-"))) + os.sep + "ws",
                "--dry-run",
            ],
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"cp1252 控制台下安装器崩了：{(proc.stderr or '')[-400:]}")


if __name__ == "__main__":
    unittest.main()
