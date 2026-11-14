#!/usr/bin/env python3
"""Hermes cron 入口：每 5 分钟判断该不该响。

工作区写在这里，不靠 cwd——Hermes 从 scripts 目录执行脚本。
stdout 非空 = 要投递的提醒文案；stdout 为空 = Hermes 视为静默，不投递。
调试时可用环境变量 RTC_NOW 覆盖当前时间（仅本地手工测试用）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc  # noqa: E402

WS = os.environ.get("RTC_WORKSPACE", r"C:\Users\Janlhin\RTC")
argv = ["--workspace", WS, "due"]
if os.environ.get("RTC_NOW"):
    argv += ["--now", os.environ["RTC_NOW"]]

raise SystemExit(rtc.main(argv))
