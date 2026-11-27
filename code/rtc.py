#!/usr/bin/env python3
"""rtc.py — 兼容入口（薄壳）。

实现已按职责拆到 rtc_core / rtc_archive / rtc_metrics / rtc_brief，命令面一字未改：

  python rtc.py --workspace <dir> plan   [--now ISO] [--force]
  python rtc.py --workspace <dir> due    [--now ISO]
  python rtc.py --workspace <dir> ack    [--window ID] [--text TXT] [--now ISO]
      [--date YYYY-MM-DD] [--source external --event-id ID]
  python rtc.py --workspace <dir> status [--json]
  python rtc.py --workspace <dir> schedule <YYYY-MM-DD> | --month YYYY-MM [--json]
  python rtc.py --workspace <dir> history  <YYYY-MM-DD> | --month YYYY-MM | --range A..B [--json]
  python rtc.py --workspace <dir> events   [<window_id>] [--since ISO] [--until ISO] [--json]
  python rtc.py --workspace <dir> stats    [--group-by window|date|month] [--missed-only] [--json]
  python rtc.py --workspace <dir> check --window <ID> --item <序号或名称> [--undo]
  python rtc.py --workspace <dir> postpone --window <ID> [--to YYYY-MM-DD | --days N] [--deadline]
  python rtc.py --workspace <dir> cancel   --window <ID> [--date YYYY-MM-DD] [--undo]
  python rtc.py --workspace <dir> complete --window <ID> [--text TXT]
  python rtc.py --workspace <dir> disable  --window <ID>
  python rtc.py --workspace <dir> enable   --window <ID>
  python rtc.py --workspace <dir> add-window --id ID --label TXT
      --start HH:MM --end HH:MM [--interval N] [--ack TXT ...] [--format T]
      [--expect-fires N] [--valid-from YYYY-MM-DD] [--valid-until YYYY-MM-DD]
      [--oneoff [--date YYYY-MM-DD]] [--update] [--dry-run]
      （「明天 9 点提醒我寄东西」= --oneoff --date 2026-11-20，日期绝不写进 --start）
  python rtc.py --workspace <dir> metrics sync|report|schedule [--date YYYY-MM-DD]

`import rtc` 拿到的是 rtc_core 这个模块对象——`rtc_due.py` / `rtc_plan.py` 调 `rtc.main()`
以及测试里对 `rtc.atomic_write` / `rtc.file_digest` 的替换因此继续有效。
"""

from __future__ import annotations

import sys

import rtc_core
from rtc_core import main as main  # 静态可见性：让 mypy 知道 rtc.main 存在

if __name__ == "__main__":
    raise SystemExit(rtc_core.main())
else:
    # 单一命名空间：rtc 与 rtc_core 指向同一个模块对象
    sys.modules[__name__] = rtc_core
