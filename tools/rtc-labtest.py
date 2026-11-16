#!/usr/bin/env python3
"""RTC-kit 实机自检 —— 一条命令跑完所有复杂场景。

设计要点
  1. 逻辑层全部在**隔离沙箱**里跑（每个用例一个干净 workspace，拷贝你真实的 routines.json），
     用 rtc.py 自带的 `--now` 注入时间，所以「连响 6 次 / 过期 / 跨日 / 明天」都是秒级，
     不用真等到早上 7 点。
  2. 只有 `--live` 会碰真实工作区：注入一条 now+N 分钟的一次性提醒，真等宿主 cron 触发，
     并读 hermes cron 的运行记录来判定「有没有真的投递」。跑完自动清理。
  3. 已知缺陷标 xfail：不算失败，但仍然记录在案，不会被"绿灯"掩盖。

用法
  python rtc-labtest.py                     # 只跑沙箱套件（安全，不碰实机）
  python rtc-labtest.py --live               # 追加实机投递验证（会等 5-8 分钟）
  python rtc-labtest.py --live --at 3 --wait 10
  python rtc-labtest.py -v                   # 打印每条命令的原始输出
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
DEFAULT_SCRIPTS = Path(LOCALAPPDATA) / "hermes" / "scripts"
DEFAULT_WS = Path.home() / "RTC"

VALID_STATES = {"pending", "ringing", "delivered", "acked", "missed", "skipped", "cancelled"}


# ─────────────────────────────────────────────────────────── 结果记录
@dataclass
class Case:
    cid: str
    title: str
    status: str = "PASS"  # PASS / FAIL / XFAIL / XPASS / SKIP
    detail: str = ""
    evidence: list = field(default_factory=list)


class Lab:
    def __init__(self, scripts: Path, real_ws: Path, root: Path, verbose=False):
        self.rtc = scripts / "rtc.py"
        self.once = scripts / "rtc_once.py"
        self.real_ws = real_ws
        self.root = root
        self.verbose = verbose
        self.cases: list[Case] = []
        self.ws: Path | None = None

    # ── 进程调用 ───────────────────────────────────────────
    def run(self, cmd: list[str], timeout=60, expect_ok=True):
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        if self.verbose:
            print(f"      $ {' '.join(str(c) for c in cmd)}")
            print(
                f"        exit={p.returncode} out={p.stdout.strip()[:200]!r} err={p.stderr.strip()[:200]!r}"
            )
        return p

    def rt(self, *args, timeout=60):
        return self.run([sys.executable, str(self.rtc), "--workspace", str(self.ws)] + list(args), timeout)

    def rt_now(self, sub: str, when: str, *args, timeout=60):
        return self.rt(sub, *args, "--now", when, timeout=timeout)

    def status(self) -> dict:
        p = self.rt("status", "--json")
        if not p.stdout.strip():
            raise AssertionError(f"status 没有输出：{p.stderr.strip()[:200]}")
        return json.loads(p.stdout)

    def win(self, wid: str) -> dict | None:
        for w in self.status().get("windows", []):
            if w["id"] == wid:
                return w
        return None

    def routines(self) -> dict:
        return json.loads((self.ws / "rules" / "routines.json").read_text(encoding="utf-8"))

    # ── 沙箱 ─────────────────────────────────────────────
    def fresh(self, tag: str) -> None:
        self.ws = self.root / tag / "ws"
        (self.ws / "rules").mkdir(parents=True, exist_ok=True)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        src = self.real_ws / "rules" / "routines.json"
        if src.exists():
            shutil.copy(src, self.ws / "rules" / "routines.json")
        else:  # 没有实机配置时退化为空时刻表
            (self.ws / "rules" / "routines.json").write_text(
                json.dumps(
                    {"version": 1, "timezone": "Asia/Shanghai", "tzOffset": "+08:00", "alarmWindows": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    def add_window(self, **win) -> None:
        r = self.routines()
        r["alarmWindows"].append(win)
        (self.ws / "rules" / "routines.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 断言 ─────────────────────────────────────────────
    def case(self, cid, title, fn, xfail=None):
        c = Case(cid, title)
        try:
            fn()
        except AssertionError as e:
            c.status = "XFAIL" if (xfail and xfail in str(e)) else "FAIL"
            c.detail = str(e)[:220]
        except Exception:
            c.status = "FAIL"
            c.detail = traceback.format_exc(limit=2).splitlines()[-1][:220]
        else:
            if xfail:
                c.status = "XPASS"
                c.detail = f"已知缺陷已消失（原缺陷：{xfail}）"
        self.cases.append(c)
        mark = {"PASS": "✓", "XFAIL": "!", "FAIL": "✗", "XPASS": "★", "SKIP": "-"}[c.status]
        print(f"  {mark} {cid:26} {title}" + (f"   ← {c.detail}" if c.detail and c.status != "PASS" else ""))
        return c

    def ok(self, cond, msg):
        if not cond:
            raise AssertionError(msg)


# ─────────────────────────────────────────────────────────── 沙箱套件
def suite(lab: Lab) -> None:
    print("\n[1/6] 计划与日期（时间注入，不用真等）")

    def t_plan_basic():
        lab.fresh("plan_basic")
        p = lab.rt_now("plan", "2026-11-17T06:00")
        lab.ok(p.returncode == 0, f"plan 退出码 {p.returncode}")
        s = lab.status()
        lab.ok(s["planDate"] == "2026-11-17", f"planDate={s['planDate']}")
        lab.ok(len(s["windows"]) == 4, f"窗口数 {len(s['windows'])}（真实 routines.json 应为 4）")
        lab.ok(s["rev"] == 1, f"首次 plan rev 应为 1，实际 {s['rev']}")

    def t_plan_dedupe():
        lab.rt_now("plan", "2026-11-17T06:00")
        rev = lab.status()["rev"]
        p = lab.rt_now("plan", "2026-11-17T06:00")
        lab.ok(p.returncode == 0, "重复 plan 不应报错")
        lab.ok("已存在" in p.stderr, f"应提示已存在，实际 {p.stderr.strip()[:80]!r}")
        s = lab.status()
        lab.ok(len(s["windows"]) == 4, f"窗口被重复创建：{len(s['windows'])}")
        lab.ok(s["rev"] == rev, f"rev 不应变：{rev}→{s['rev']}")

    def t_force_keeps_state():
        lab.rt_now("plan", "2026-11-17T06:00")
        lab.rt_now("due", "2026-11-17T07:00")
        lab.rt_now("ack", "2026-11-17T07:02", "--window", "wake", "--text", "起了")
        before = lab.win("wake")
        lab.ok(before["state"] == "acked", f"前置：应 acked，实际 {before['state']}")
        lab.rt_now("plan", "2026-11-17T07:03", "--force")
        after = lab.win("wake")
        lab.ok(after["state"] == "acked", f"plan --force 把已确认窗口改回 {after['state']}")
        lab.ok(after["fireCount"] == before["fireCount"], f"plan --force 改了已响数：{after['fireCount']}")

    def t_force_resets_ringing():
        """已知缺陷：ringing 中的窗口被 --force 重置（rtc_once 每次都跑 plan --force）。"""
        lab.fresh("force_ringing")
        lab.rt_now("plan", "2026-11-17T06:00")
        lab.rt_now("due", "2026-11-17T07:00")
        before = lab.win("wake")
        lab.ok(before["fireCount"] == 1, f"前置：应响 1 次，实际 {before['fireCount']}")
        lab.rt_now("plan", "2026-11-17T07:01", "--force")
        after = lab.win("wake")
        lab.ok(
            after["fireCount"] == 1,
            f"ringing 窗口被 --force 重置：fireCount {before['fireCount']}→{after['fireCount']}，"
            f"state {before['state']}→{after['state']}",
        )

    def t_weekend():
        lab.fresh("weekend")
        p = lab.rt_now("plan", "2026-11-21T06:00")  # 2026-11-21 周六
        lab.ok(p.returncode == 0, "周末 plan 失败")
        lab.ok("weekend" in p.stderr, f"未识别为周末：{p.stderr.strip()[:80]!r}")

    def t_rollover():
        lab.rt_now("plan", "2026-11-21T23:59")
        p = lab.rt_now("plan", "2026-11-22T00:05")
        lab.ok(p.returncode == 0, "跨日 plan 失败")
        lab.ok(lab.status()["planDate"] == "2026-11-22", f"跨日未换 planDate：{lab.status()['planDate']}")

    lab.case("A1 plan 生成今日计划", "plan 在指定日期生成 4 窗口", t_plan_basic)
    lab.case("A2 plan 幂等", "同日重复 plan 不重复建窗口", t_plan_dedupe)
    lab.case("A3 plan --force 保已确认", "--force 不丢 acked 与已响数", t_force_keeps_state)
    lab.case("A3b plan --force 不重置 ringing", "响到一半被 --force 后计数连续", t_force_resets_ringing)
    lab.case("A4 周末判定", "周六识别为 weekend", t_weekend)
    lab.case("A5 跨日切换", "00:05 换到新一天", t_rollover)

    print("\n[2/6] 响的节奏与上限（核心）")

    def t_fire_and_cadence():
        lab.fresh("cadence")
        lab.rt_now("plan", "2026-11-18T06:00")
        fired = []
        for t in ["07:00", "07:05", "07:10", "07:15", "07:20", "07:25", "07:30", "07:35"]:
            p = lab.rt_now("due", "2026-11-18T" + t)
            if p.stdout.strip():
                fired.append((t, p.stdout.strip()))
        lab.ok(len(fired) == 6, f"应响 6 次（expectFires），实际 {len(fired)} 次：{[f[0] for f in fired]}")
        lab.ok("第 2 次" in fired[1][1], f"第 2 次文案缺「第 2 次」：{fired[1][1][:80]!r}")
        lab.ok("回复「起了」" in fired[0][1], f"第 1 次缺 ackHint：{fired[0][1][:80]!r}")
        w = lab.win("wake")
        lab.ok(w["state"] == "missed", f"响满后应 missed，实际 {w['state']}")
        lab.ok(w["fireCount"] == 6, f"fireCount 应封顶 6，实际 {w['fireCount']}")

    def t_silent():
        lab.fresh("silent")
        lab.rt_now("plan", "2026-11-18T06:00")
        p = lab.rt_now("due", "2026-11-18T03:00")  # 没有任何窗口
        lab.ok(p.returncode == 0, f"静默时退出码应 0，实际 {p.returncode}")
        lab.ok(p.stdout.strip() == "", f"静默时不该有输出：{p.stdout.strip()[:80]!r}")

    lab.case("B1 六连响 + 封顶", "6 次后停、标 missed、计数封顶", t_fire_and_cadence)
    lab.case("B2 静默", "无窗口时段零输出、退出码 0", t_silent)

    print("\n[3/6] ack（确认）")

    def t_ack():
        lab.fresh("ack")
        lab.rt_now("plan", "2026-11-19T06:00")
        lab.rt_now("due", "2026-11-19T07:00")
        rev = lab.status()["rev"]
        p = lab.rt_now("ack", "2026-11-19T07:03", "--text", "好了")
        lab.ok(p.returncode == 0, f"ack 退出码 {p.returncode}：{p.stderr.strip()[:80]}")
        w = lab.win("wake")
        lab.ok(w["state"] == "acked", f"应 acked，实际 {w['state']}")
        lab.ok(w["ackedAt"], "ackedAt 为空")
        lab.ok(lab.status()["rev"] > rev, "ack 后 rev 未增")

    def t_ack_after_missed():
        lab.rt_now("plan", "2026-11-19T06:00")
        lab.rt_now("due", "2026-11-19T12:30")  # 12:25 之后 → 过期标 missed
        w = lab.win("meds-noon")
        lab.ok(w["state"] == "missed", f"前置：过期后应 missed，实际 {w['state']}")
        p = lab.rt_now("ack", "2026-11-19T12:30", "--window", "meds-noon", "--text", "好了")
        lab.ok(p.returncode == 0, f"当天过期窗口应仍可 ack，实际退出码 {p.returncode}")
        lab.ok(
            lab.win("meds-noon")["state"] == "acked", f"ack 后应 acked，实际 {lab.win('meds-noon')['state']}"
        )

    def t_ack_none():
        p = lab.rt_now("ack", "2026-11-19T12:40", "--window", "nope", "--text", "好了")
        lab.ok(p.returncode == 4, f"不存在的窗口应 exit 4，实际 {p.returncode}")

    def t_states_enum():
        bad = [w for w in lab.status()["windows"] if w["state"] not in VALID_STATES]
        lab.ok(not bad, f"出现未知状态值：{[w['state'] for w in bad]}")

    lab.case("C1 ack 生效", "state/ackedAt/rev 三者都变", t_ack)
    lab.case("C2 过期后 ack", "当天 missed 仍可确认", t_ack_after_missed)
    lab.case("C3 ack 不存在窗口", "exit 4", t_ack_none)
    lab.case("C4 状态枚举合法", "只允许 7 种状态值", t_states_enum)

    print("\n[4/6] 一次性提醒与清单")

    def inject_once(tag, at, label, date):
        lab.fresh(tag)
        p = lab.run(
            [
                sys.executable,
                str(lab.once),
                "--workspace",
                str(lab.ws),
                at,
                label,
                "--date",
                date,
                "--window-min",
                "5",
            ],
            timeout=60,
        )
        lab.ok(p.returncode == 0, f"rtc_once 失败：{p.stderr.strip()[:120]}")
        oos = [w for w in lab.routines()["alarmWindows"] if w.get("oneOff")]
        lab.ok(oos, "routines.json 里没有新增 oneOff 窗口")
        return oos[-1]["id"]

    def t_oneoff():
        wid = inject_once("oneoff", "15:00", "【自检】一次性", "2026-11-20")
        lab.rt_now("plan", "2026-11-20T06:00")
        p = lab.rt_now("due", "2026-11-20T15:00")
        lab.ok("【自检】一次性" in p.stdout, f"没响：{p.stdout.strip()[:80]!r}")
        w = lab.win(wid)
        lab.ok(w["state"] == "delivered", f"一次性送达即完成，应 delivered，实际 {w['state']}")
        lab.ok(w["fireCount"] == 1, f"一次性只响 1 次，实际 {w['fireCount']}")
        p2 = lab.rt_now("due", "2026-11-20T15:10")
        lab.ok(p2.stdout.strip() == "", f"送达后仍重复响：{p2.stdout.strip()[:80]!r}")

    def t_oneoff_next_day():
        lab.rt_now("plan", "2026-11-21T06:00")
        ids = [w["id"] for w in lab.status()["windows"]]
        lab.ok(not any(i.startswith("once-") for i in ids), f"次日仍生成一次性窗口：{ids}")

    def t_oneoff_dedupe():
        lab.fresh("once_dedupe")
        for _ in range(2):
            lab.run(
                [
                    sys.executable,
                    str(lab.once),
                    "--workspace",
                    str(lab.ws),
                    "14:00",
                    "重复",
                    "--date",
                    "2026-11-21",
                ],
                timeout=60,
            )
        mine = [w for w in lab.routines()["alarmWindows"] if w.get("oneOff") and w.get("label") == "重复"]
        lab.ok(len(mine) == 1, f"同一时刻重复注入应只 1 条，实际 {len(mine)}")

    def t_checklist():
        lab.fresh("check")
        lab.add_window(
            id="pack",
            label="清点并带走",
            start="20:00",
            end="20:15",
            intervalMin=5,
            expectFires=1,
            ackHints=["好了"],
            checklist=["电饭煲", "电脑", "被子枕头"],
        )
        lab.rt_now("plan", "2026-11-22T06:00")
        p = lab.rt_now("due", "2026-11-22T20:00")
        lab.ok("还剩 3 项" in p.stdout, f"清单提醒未标剩余项：{p.stdout.strip()[:90]!r}")
        lab.ok("☐ 电饭煲" in p.stdout, "清单项没打出来")
        p = lab.rt_now("check", "2026-11-22T20:02", "--window", "pack", "--item", "电饭煲")
        lab.ok(p.returncode == 0, f"check 退出码 {p.returncode}")
        lab.ok("1/3" in p.stderr, f"剩余计数不对：{p.stderr.strip()[:80]!r}")
        p = lab.rt_now("check", "2026-11-22T20:02", "--window", "pack", "--item", "不存在")
        lab.ok(p.returncode == 4, f"不存在项应 exit 4，实际 {p.returncode}")
        for it in ["电脑", "被子枕头"]:
            lab.rt_now("check", "2026-11-22T20:03", "--window", "pack", "--item", it)
        w = lab.win("pack")
        lab.ok(w["state"] in ("acked", "delivered", "missed"), f"全勾完后状态异常：{w['state']}")

    lab.case("D1 一次性送达即完成", "delivered、只响 1 次、不重复", t_oneoff)
    lab.case("D2 一次性只当天存在", "次日计划不含它", t_oneoff_next_day)
    lab.case("D3 一次性去重", "同一时刻重复注入只 1 条", t_oneoff_dedupe)
    lab.case("D4 清单勾选", "剩余项/计数/不存在项/全勾完", t_checklist)

    print("\n[5/6] 生命周期命令（取消/完成/停用/推迟/改时间）")

    def t_cancel():
        lab.fresh("cancel")
        lab.rt_now("plan", "2026-11-23T06:00")
        p = lab.rt_now("cancel", "2026-11-23T09:00", "--window", "wake")
        lab.ok(p.returncode == 0, f"cancel 退出码 {p.returncode}")
        lab.ok(lab.win("wake")["state"] == "cancelled", f"应 cancelled，实际 {lab.win('wake')['state']}")
        lab.rt_now("plan", "2026-11-24T06:00")
        lab.ok("wake" in [w["id"] for w in lab.status()["windows"]], "cancel 只该影响今天，明天应恢复")

    def t_complete():
        p = lab.rt_now("complete", "2026-11-23T09:00", "--window", "meds-am", "--text", "做完了")
        lab.ok(p.returncode == 0, f"complete 退出码 {p.returncode}")
        lab.rt_now("plan", "2026-11-25T06:00", "--force")
        lab.ok(
            "meds-am" not in [w["id"] for w in lab.status()["windows"]],
            "complete 后次日仍在计划里（应永久停用）",
        )
        dw = [w.get("disabled") for w in lab.routines()["alarmWindows"] if w["id"] == "meds-am"]
        lab.ok(dw and dw[0] is True, f"routines 里未标 disabled：{dw}")

    def t_disable_enable():
        p = lab.rt_now("disable", "2026-11-23T09:00", "--window", "meds-noon")
        lab.ok(p.returncode == 0, f"disable 退出码 {p.returncode}")
        lab.rt_now("plan", "2026-11-26T06:00", "--force")
        lab.ok("meds-noon" not in [w["id"] for w in lab.status()["windows"]], "disable 后次日仍生成")
        p = lab.rt_now("enable", "2026-11-23T09:00", "--window", "meds-noon")
        lab.ok(p.returncode == 0, f"enable 退出码 {p.returncode}")
        lab.rt_now("plan", "2026-11-27T06:00", "--force")
        lab.ok("meds-noon" in [w["id"] for w in lab.status()["windows"]], "enable 后次日仍不生成")

    def t_disable_nope():
        p = lab.rt_now("disable", "2026-11-23T09:00", "--window", "nope")
        lab.ok(p.returncode == 4, f"不存在窗口应 exit 4，实际 {p.returncode}")

    def t_postpone():
        lab.fresh("postpone")
        lab.rt_now("plan", "2026-11-28T06:00")
        p = lab.rt_now("postpone", "2026-11-28T09:00", "--window", "meds-pm", "--days", "1")
        lab.ok(p.returncode == 0, f"postpone 退出码 {p.returncode}")
        lab.ok(
            lab.win("meds-pm")["state"] == "skipped", f"今天应 skipped，实际 {lab.win('meds-pm')['state']}"
        )
        lab.rt_now("plan", "2026-11-29T06:00")
        lab.ok("meds-pm" in [w["id"] for w in lab.status()["windows"]], "次日应重建")
        p = lab.rt_now("postpone", "2026-11-28T09:00", "--window", "wake", "--to", "2026-11-27")
        lab.ok(p.returncode == 3, f"推到过去日期应 exit 3，实际 {p.returncode}")

    def t_set_time():
        lab.fresh("settime")
        lab.rt_now("plan", "2026-11-30T06:00")
        today_before = lab.win("wake")["window"]
        p = lab.run(
            [
                sys.executable,
                str(lab.rtc),
                "--workspace",
                str(lab.ws),
                "set-time",
                "--window",
                "wake",
                "--start",
                "07:30",
                "--end",
                "07:55",
            ]
        )
        lab.ok(p.returncode == 0, f"set-time 退出码 {p.returncode}：{p.stderr.strip()[:90]}")
        lab.ok(lab.win("wake")["window"] == today_before, "今天的窗口不该被改（次日生效）")
        starts = [w["start"] for w in lab.routines()["alarmWindows"] if w["id"] == "wake"]
        lab.ok(starts and starts[0] == "07:30", f"routines 未写成 07:30：{starts}")
        lab.rt_now("plan", "2026-12-01T06:00")
        lab.ok(lab.win("wake")["window"].startswith("07:30"), f"次日未生效：{lab.win('wake')['window']}")
        p = lab.run(
            [
                sys.executable,
                str(lab.rtc),
                "--workspace",
                str(lab.ws),
                "set-time",
                "--window",
                "wake",
                "--start",
                "08:00",
            ]
        )  # end 仍 07:55 < start
        lab.ok(p.returncode == 3, f"start 晚于 end 应 exit 3，实际 {p.returncode}")

    lab.case("E1 cancel 只影响今天", "今天 cancelled、明天恢复", t_cancel)
    lab.case("E2 complete 永久停用", "次日不再生成 + routines.disabled", t_complete)
    lab.case("E3 disable/enable 往返", "停用后不生成、启用后恢复", t_disable_enable)
    lab.case("E4 disable 不存在窗口", "exit 4", t_disable_nope)
    lab.case("E5 postpone", "今天 skipped、次日重建、过去日期 exit 3", t_postpone)
    lab.case("E6 set-time", "今天不变、次日生效、end 冲突 exit 3", t_set_time)

    print("\n[6/6] 并发、锁、坏文件、观测命令")

    def t_concurrent():
        _lock_window("concur", "2026-12-02")
        rev0 = lab.status()["rev"]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(lab.rtc),
                    "--workspace",
                    str(lab.ws),
                    "due",
                    "--now",
                    "2026-12-02T07:00",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(3)
        ]
        outs = [p.communicate() for p in procs]
        spoke = [o for o in outs if o[0].strip()]
        lab.ok(len(spoke) == 1, f"3 个并发 due 应只有一个出文案，实际 {len(spoke)} 个")
        lab.ok(all(p.returncode == 0 for p in procs), "并发时不该有进程报错退出")
        try:
            json.loads((lab.ws / "state" / "alarms.json").read_text(encoding="utf-8"))
        except Exception as e:
            raise AssertionError(f"alarms.json 被写坏：{e}")
        lab.ok(lab.status()["rev"] >= rev0, "rev 倒退")
        lab.ok(lab.win("lk")["fireCount"] == 1, f"并发多写了 fireCount：{lab.win('lk')['fireCount']}")

    def _hold_lock(seconds):
        """起一个子进程真正持有 state/alarms.json 的操作系统锁（不是只建一个文件）。"""
        lock_target = lab.ws / "state" / "alarms.json"
        script = lab.root / "hold-lock.py"
        script.write_text(
            "import pathlib, sys, time\n"
            f"sys.path.insert(0, {str(lab.rtc.parent)!r})\n"
            "import rtc\n"
            "with rtc._exclusive_file_lock(pathlib.Path(sys.argv[1])):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n",
            encoding="utf-8",
        )
        child = subprocess.Popen(
            [sys.executable, str(script), str(lock_target), str(seconds)],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        child.stdout.readline()
        return child

    def _lock_window(tag, date):
        """沙箱里自建一个覆盖 07:00 的一次性窗口，不依赖实机时刻表里有没有 wake。"""
        lab.fresh(tag)
        lab.add_window(
            id="lk",
            label="锁测试",
            start="07:00",
            end="07:15",
            intervalMin=5,
            expectFires=1,
            ackHints=["好了"],
            oneOff=True,
            validFrom=date,
            validUntil=date,
        )
        lab.rt_now("plan", f"{date}T06:00")

    def t_lock_fresh():
        _lock_window("lock1", "2026-12-03")
        child = _hold_lock(20)  # 比 RTC 的 10s 等待更长，确保它等不到锁
        try:
            p = lab.rt_now("due", "2026-12-03T07:00")
            lab.ok("写入冲突" in p.stderr, f"锁被别的进程持有时未提示冲突：{p.stderr.strip()[:90]!r}")
            lab.ok(p.stdout.strip() == "", f"锁被占时不该响：{p.stdout.strip()[:60]!r}")
            lab.ok(lab.win("lk")["fireCount"] == 0, "锁被占时仍写了状态")
        finally:
            child.kill()
            child.wait()

    def t_lock_stale():
        """sidecar 的 mtime 再旧也不接管；持有者一退出立刻能拿到，且 sidecar 不删。"""
        _lock_window("lock2", "2026-12-04")
        lock = lab.ws / "state" / "alarms.json.lock"
        child = _hold_lock(20)
        try:
            old = time.time() - 3600
            os.utime(lock, (old, old))
            p = lab.rt_now("due", "2026-12-04T07:00")
            lab.ok(
                p.stdout.strip() == "", f"旧 mtime 被当成「可接管」，锁没起作用：{p.stdout.strip()[:60]!r}"
            )
            lab.ok("写入冲突" in p.stderr, f"旧 mtime 时未提示冲突：{p.stderr.strip()[:90]!r}")
            lab.ok(lab.win("lk")["fireCount"] == 0, "锁被占时仍写了状态")
        finally:
            child.kill()
            child.wait()
        p = lab.rt_now("due", "2026-12-04T07:00")
        lab.ok(p.stdout.strip() != "", f"持有者已退出，提醒仍未响：{p.stderr.strip()[:90]!r}")
        lab.ok(lock.exists(), "sidecar 应常驻，不应被删除或按年龄清理")

    def t_corrupt():
        lab.fresh("corrupt")
        lab.rt_now("plan", "2026-12-05T06:00")
        f = lab.ws / "state" / "alarms.json"
        f.write_text("{oops", encoding="utf-8")
        p = lab.rt_now("due", "2026-12-05T07:00")
        lab.ok(
            p.returncode != 1 or "Traceback" not in p.stderr,
            "状态文件损坏时抛 Traceback（应给一行可读错误并安全退出）",
        )
        lab.ok(f.read_text(encoding="utf-8") == "{oops", "损坏文件被覆盖（更严重）")

    def t_observe():
        lab.fresh("obs")
        lab.rt_now("plan", "2026-12-06T06:00")
        lab.rt_now("due", "2026-12-06T07:00")
        for cmd in (
            ["history", "--json", "2026-12-06"],
            ["schedule", "--json", "2026-12-07"],
            ["metrics", "report", "--days", "7"],
        ):
            p = lab.rt(cmd[0], *cmd[1:])
            lab.ok(p.returncode == 0, f"{cmd[0]} 退出码 {p.returncode}：{p.stderr.strip()[:80]}")
        p = lab.rt("history", "--json", "2026-12-06")
        h = json.loads(p.stdout)
        lab.ok("results" in h and h["date"] == "2026-12-06", f"history --json 结构异常：{list(h)[:5]}")

    lab.case("F1 三路并发写", "只有一个响、文件不坏、rev 不倒退", t_concurrent)
    lab.case("F2 新鲜锁", "提示写入冲突、不响、不改状态", t_lock_fresh)
    lab.case("F3 锁不按年龄接管", "mtime 一小时前也不接管、持有者退出后立刻可写、sidecar 常驻", t_lock_stale)
    lab.case("F4 坏状态文件", "不抛 Traceback、不覆盖原文件", t_corrupt)
    lab.case("F5 观测命令", "history/schedule/metrics 可跑可解析", t_observe)


# ─────────────────────────────────────────────────────────── 实机投递
def live(lab: Lab, at_min: int, wait_min: int) -> None:
    print("\n[LIVE] 实机链路：注入 → 等宿主 cron 真触发 → 判定投递 → 清理")
    real = lab.real_ws
    backup = lab.root / "live-backup"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("rules/routines.json", "state/alarms.json"):
        src = real / name
        if src.exists():
            shutil.copy(src, backup / name.replace("/", "_"))
    ws_bak = lab.ws
    lab.ws = real

    rev0 = lab.status()["rev"]

    def t_channel():
        log = Path(LOCALAPPDATA) / "hermes" / "logs" / "gateway.log"
        if not log.exists():
            raise AssertionError(f"找不到 gateway.log：{log}")
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
        fails = [l for l in tail if "send failed" in l or "session not ready" in l]
        if fails:
            raise AssertionError(f"渠道仍在丢消息（最后一条：{fails[-1].strip()[:120]}）")

    lab.case("L0 渠道健康", "gateway.log 近期无 send failed（丢消息就别往下测）", t_channel)

    when = (datetime.now() + timedelta(minutes=at_min)).strftime("%H:%M")
    label = f"【实机自检】{datetime.now():%H:%M}"
    print(f"   注入一次性提醒：今天 {when}（{at_min} 分钟后）")

    p = lab.run(
        [sys.executable, str(lab.once), "--workspace", str(real), when, label, "--window-min", "5"],
        timeout=120,
    )
    print(f"   rtc_once exit={p.returncode}  {p.stderr.strip()[:120]}")
    ids = [w["id"] for w in lab.routines()["alarmWindows"] if w.get("oneOff") and label in w.get("label", "")]
    lab.ok(ids, "注入失败：routines.json 里找不到本次测试窗口")
    wid = ids[-1]
    print(f"   测试窗口 id = {wid}")

    c = lab.case("L1 注入成功", "真实工作区出现测试窗口", lambda: None)
    c.detail = wid

    # 等待真触发
    deadline = time.time() + wait_min * 60
    fired = None
    while time.time() < deadline:
        s = lab.status()
        w = next((x for x in s["windows"] if x["id"] == wid), None)
        if w and (w["fireCount"] >= 1 or w["state"] in ("delivered", "ringing")):
            fired = w
            break
        time.sleep(15)
    if fired:
        c = lab.case("L2 宿主真触发", "cron 在窗口内跑到了这条提醒", lambda: None)
        c.detail = f"state={fired['state']} fire={fired['fireCount']}"
    else:
        lab.case(
            "L2 宿主真触发",
            "cron 在窗口内跑到了这条提醒",
            lambda: (_ for _ in ()).throw(AssertionError(f"{wait_min} 分钟内没触发")),
        )

    # cron 侧证据
    def t_cron_ok():
        out = lab.run(["hermes", "cron", "list"], timeout=90).stdout
        idx = out.find("1fba3b9593ba")
        lab.ok(idx >= 0, "cron list 里找不到 rtc-due")
        last = next((l.strip() for l in out[idx:].splitlines() if "Last run:" in l), "")
        lab.ok(last, f"读不到 rtc-due 的 Last run 行：{out[idx:][:120]!r}")
        lab.ok("not delivered" not in last, f"执行成功但投递失败（渠道侧）：{last[:150]}")

    lab.case("L3 cron 侧投递", "rtc-due 最近一次执行 ok 且已投递", t_cron_ok)

    # 清理
    def t_cleanup():
        p = lab.rt("cancel", "--window", wid)
        lab.ok(p.returncode == 0, f"cancel 测试窗口失败：{p.stderr.strip()[:90]}")
        s = lab.status()
        w = next((x for x in s["windows"] if x["id"] == wid), None)
        lab.ok(w is None or w["state"] in ("cancelled", "delivered"), f"测试窗口未清理：{w}")
        # cancel 只管今天的窗口，时刻表里的 oneOff 条目要还原备份才会消失
        src = backup / "rules_routines.json"
        if src.exists():
            shutil.copy(src, real / "rules" / "routines.json")
            print("   已用备份还原 routines.json")
        left = [x["id"] for x in lab.routines()["alarmWindows"] if x["id"] == wid]
        lab.ok(not left, f"routines.json 仍残留测试条目 {left}")
        lab.ok(lab.status()["rev"] >= rev0, "rev 倒退")

    lab.case("L4 清理无残留", "窗口与时刻表都不留测试痕迹", t_cleanup)
    lab.ws = ws_bak


# ─────────────────────────────────────────────────────────── 报告
def report(lab: Lab, out: Path) -> int:
    total = len(lab.cases)
    n = {s: sum(1 for c in lab.cases if c.status == s) for s in ("PASS", "FAIL", "XFAIL", "XPASS", "SKIP")}
    print("\n" + "=" * 72)
    print(
        f"结果：{n['PASS']}/{total} 通过 · 失败 {n['FAIL']} · 已知缺陷 {n['XFAIL']} · 意外通过 {n['XPASS']}"
    )
    if n["FAIL"] or n["XPASS"]:
        print("需要处理：")
        for c in lab.cases:
            if c.status in ("FAIL", "XPASS"):
                print(f"  ✗ {c.cid} {c.title}\n    {c.detail}")
    if n["XFAIL"]:
        print("已知缺陷（已在文档记录，不算回归失败）：")
        for c in lab.cases:
            if c.status == "XFAIL":
                print(f"  ! {c.cid} {c.title}\n    {c.detail}")
    print("=" * 72)

    data = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "workspace": str(lab.real_ws),
        "rtc": str(lab.rtc),
        "summary": {**n, "total": total},
        "cases": [{"id": c.cid, "title": c.title, "status": c.status, "detail": c.detail} for c in lab.cases],
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{out}")
    return 0 if not n["FAIL"] and not n["XPASS"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="RTC-kit 实机自检（沙箱逻辑 + 可选实机投递）")
    ap.add_argument("--scripts-dir", default=str(DEFAULT_SCRIPTS), help="宿主 scripts 目录")
    ap.add_argument("--workspace", default=str(DEFAULT_WS), help="RTC 工作区")
    ap.add_argument("--live", action="store_true", help="追加实机投递验证（会真发提醒）")
    ap.add_argument("--at", type=int, default=4, help="--live 时 N 分钟后触发（默认 4）")
    ap.add_argument("--wait", type=int, default=9, help="--live 最多等几分钟（默认 9）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱目录便于复查")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    scripts, ws = Path(a.scripts_dir), Path(a.workspace)
    for p, what in ((scripts / "rtc.py", "rtc.py"), (scripts / "rtc_once.py", "rtc_once.py")):
        if not p.exists():
            print(f"找不到 {what}：{p}")
            return 2
    if not (ws / "rules" / "routines.json").exists():
        print(f"工作区缺少 rules/routines.json：{ws}")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(tempfile.mkdtemp(prefix=f"rtc-lab-{stamp}-"))
    print(f"RTC-kit 实机自检  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  scripts = {scripts}\n  workspace = {ws}\n  沙箱 = {root}")

    lab = Lab(scripts, ws, root, verbose=a.verbose)
    try:
        suite(lab)
        if a.live:
            live(lab, a.at, a.wait)
    except KeyboardInterrupt:
        print("\n已中断（沙箱随进程结束，实机未改动或已按 L4 清理）")
        return 130

    rc = report(lab, root / "report.json")
    if not a.keep:
        print(f"（沙箱保留在 {root}，--keep 可显式保留；不想要可直接删）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
