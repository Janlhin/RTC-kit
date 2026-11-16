"""让子进程也计入覆盖率。

测试是进程级 E2E：每个用例通过 subprocess 调 code/rtc.py。没有这一段，
覆盖率只统计父进程里 import 到的那点代码（实测 16%，等于没测）。
覆盖率的唯一真实来源必须包含子进程，否则阈值可以随便刷。
"""

import coverage

coverage.process_startup()
