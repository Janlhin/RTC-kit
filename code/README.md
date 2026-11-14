# code — 确定性脚本

这一层只放**不依赖模型**的代码。两个文件，职责不同：

| 文件 | 角色 | 依赖 |
|---|---|---|
| `rtc.py` | 提醒主链路：`plan` / `due` / `ack` / `status` / `metrics` | Python 3.10+，仅标准库 |
| `metrics.mjs` | 统计旁路（旧版，保留兼容） | Node，需要 `node:sqlite` |

## rtc.py

主链路的确定性实现。把「读状态 → 判断 → 写回」从模型手里拿走，宿主只负责按时调用、把 stdout 投递出去。

```
python rtc.py --workspace <dir> plan     # 生成/补建当天窗口（只增不改）
python rtc.py --workspace <dir> due      # 该响就打印文案，不该响就什么都不打印
python rtc.py --workspace <dir> ack      # 写入确认
python rtc.py --workspace <dir> status   # 人读 / --json 机器读
python rtc.py --workspace <dir> metrics  # sync / report
```

契约：**stdout 为空即静默**；诊断信息全部走 stderr；退出码 `0` 正常 / `2` 状态冲突 / `3` 校验失败 / `4` 无匹配窗口。
写入是「临时文件 + 原子改名」，并在写入前比对 `rev`，不一致就放弃而不是覆盖。

适配示例见 `platform/hermes.md`。

## 测试

仓库的第一个回归测试套件：**进程级 E2E**——每个用例在临时工作区里用真实 CLI 调用 `rtc.py`，断言退出码、stdout（投递内容）与状态文件。

```bash
python code/tests/test_rtc.py                          # 直接跑
python -m unittest discover -s code/tests -v           # 或用 discover
```

零外部依赖，只用标准库。契约相关的两个用例需要 `jsonschema`，没装就自动跳过。

改了 `rtc.py` 或契约之后先跑这个；**新增行为必须配一条用例**。

## metrics.mjs

执行情况的统计账本。用运行时的内置 SQLite 模块，**零外部依赖、零构建步骤**（JSDoc + `// @ts-check` 提供类型检查）。

> **运行时下限**：`node:sqlite` 自 Node 22.5 起提供（当时需 `--experimental-sqlite`），22.13 / 23.4 起不再需要该 flag。也就是说，不带 flag 直接跑的最低版本是 **Node 22.13 或 23.4**；Node 20 会直接 `ERR_UNKNOWN_BUILTIN_MODULE`。不想管版本下限就用 `rtc.py metrics`。

```
node metrics.mjs init                 建表
node metrics.mjs sync                 今天 + 全部历史（幂等，最安全）
node metrics.mjs sync today           只落今天的窗口
node metrics.mjs sync past            只补今天以前（日切用）
node metrics.mjs report [天数]        确认率、按窗口、最近 7 天
node metrics.mjs today                只看今天
```

表结构：

```sql
daily(date, window, label, expect, fired, acked_at, missed)
PRIMARY KEY (date, window)   -- upsert 幂等，跑几次结果一样
```

**定位：只读旁路**

- 输入只读 `state/alarms.json`
- 输出只写自己的 `metrics.db`
- **挂了不影响提醒**。调用方应把它包在「失败即跳过」里，不重试、不修它

两个落账时点：日切后 `sync past`（把已终态的过去存下来）、总结时 `sync`（兜底补齐今天）。这样即使某天总结没跑，只要 14 天内开过一次机，历史也能补上。

## 为什么不用 TypeScript 编译

这份代码约 230 行，执行者是 agent 或调度器，只会「照一条命令跑」。多一个构建步骤就多一个会出错的地方。JSDoc + `@ts-check` 已经能拿到类型检查，运行时零构建。

将来如果它长成一个真正的 CLI（多子命令、要发布、要被别人安装），再按标准形态上 TypeScript + `tsc` → `dist/`。

## 尚未提供的部分

**提醒主链路的调度代码刻意不在这里**。理由见 `ARCHITECTURE.md` 第四节：它属于运行时适配层，不同平台的 payload 种类、会话目标、投递方式都不同。请按 `platform/` 下的示例自行实现，或用运行时的 `command` 类载荷调用你自己的 `due` 脚本。
