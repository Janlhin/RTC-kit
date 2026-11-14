#!/usr/bin/env python3
"""Hermes cron 入口：日切生成当天窗口表。

工作区写在这里，不靠 cwd——Hermes 从 scripts 目录执行脚本。
成功且无异常时 stdout 为空（静默）。自检发现「可能没送到」的情况才输出，
宿主会把这几行原样投递出去。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rtc  # noqa: E402

WS = os.environ.get("RTC_WORKSPACE", r"C:\Users\Janlhin\RTC")
argv = ["--workspace", WS, "plan"]
if os.environ.get("RTC_NOW"):
    argv += ["--now", os.environ["RTC_NOW"]]

raise SystemExit(rtc.main(argv))
