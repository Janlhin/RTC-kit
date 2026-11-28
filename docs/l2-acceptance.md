# L2「行程本」P7 集成验收记录

- 验收日期：2026-11-28（Asia/Shanghai）
- 集成分支：`feat/l2-p7-integration`，base `origin/main`（`991c2b5`）
- 性质：**七段 PR 的集成验收分支**，不替代各段 PR、本身不合并。各段改动仍以其自身 PR 为准。
- 验收方式：全部命令在仓库外临时工作区执行；`rules/backup.json` 的 `targets` 先指到临时备份目录。**未触碰 `C:\Users\Janlhin\RTC` 真实工作区，未写 D:/E: 备份盘。**
- 临时验收产物（工作区、命令快照、日志）在仓库外 `../_acceptance-p7/`，不纳入仓库；下文「证据路径」相对该目录。

## 1. 合并叠栈

按顺序 `git merge --no-ff`（未 rebase、未 force）：

| 序 | PR | 分支 | 合并提交 | 内容 |
|---|---|---|---|---|
| 1 | #117 | `feat/l2-p1-event-model` (`8fce211`) | `ca33663` | P1 事件模型 + 影子账本 + 旧窗口投影 |
| 2 | #118 | `feat/l2-p3-rrule-engine` (`d72bdbd`) | `0dcc639` | P3 上半：RRULE 解析/展开引擎 |
| 3 | #120 | `feat/l2-p3-occurrence` (`0024fa0`) | `6142371` | P3 下半：occurrence 接入 plan/due/ack |
| 4 | #121 | `feat/l2-p2-ics-truth` (`d40f69e`) | `2208cf6` | P2 `.ics` 源真相 + 可重建索引 + `--to-ics` |
| 5 | #123 | `feat/l2-p5-crud` (`5ad35a6`) | `10ec67d` | P5 事件级 CRUD + 审计落账 |
| 6 | #122 | `feat/l2-p6-query` (`c160c0a`) | `b6c9760` | P6 只读查询 + 筛选 + `.ics` 导出 |
| 7 | #124 | `feat/l2-p4-wire` (`063e9de`) | `2d3a605` | P4 NLU 周期/一次性短语接 `create --when` |

合并处置：

- **B1 重复去重**：#120 cherry-pick 了 #118 的引擎 commit，`D-016`/`T-018` 各出现两份；叠栈时人工去重成一份，编号序列保持有序。
- **文档行不丢**：`TASKS.md` 的 T-016~T-022 行全部保留。
- **`code/rtc_core.py`**：P3 适配层 / P5 命令路由 / P6 命令注册 / P4 接线四处都改它；合并后真跑全量测试验证（见第 3 节项 9），不只看冲突标记是否清空。

### 1.1 编号对照表（撞号顺延）

| 编号 | 归属 PR | 说明 |
|---|---|---|
| D-014 | #117 | P1 事件模型 / 影子账本 |
| D-015 | #121 | P2 `.ics` 源真相 |
| D-016 | #118 / #120 | P3 RRULE 引擎 + occurrence（共用一份） |
| D-017 | #124 | P4 周期中文表达产 RRULE，解析层门控写路径 |
| D-018 | #123 | P5 事件级 CRUD 落影子账本 |
| D-019 | #122 | P6 查询合并视图 + occurrence 实例展开 |
| T-016 | #117 | P1 |
| T-017 | #121 | P2 |
| T-018 | #118 / #120 | P3 引擎 + 接线（共用一行） |
| T-019 | #124 | P4 上半：解析层 |
| T-020 | #123 | P5 CRUD |
| T-021 | #122 | P6 查询 / 导出 |
| T-022 | #124 | occurrence 后续展开联动缺口（待后续 phase） |
| T-023 | #124 | P4 下半：`create --when` 接线（原分支标 T-021，与 #122 撞号；#122 先开 PR 保留 T-021，本行顺延 T-023） |

## 2. 验收矩阵

退出码口径沿用 `behavior-contract.md`：`0` 成功 / `2` 参数或短语不支持 / `3` 时刻表或状态缺失 / `4` 业务拒绝（终态、跨日 undo、无痕迹等）。

| # | 验收点 | 命令（摘要） | 退出码 | 结论 | 证据路径 |
|---|---|---|---|---|---|
| 1 | 事件一等公民 | `create --title 周会 --start 2026-12-02T20:00:00+08:00 --end …20:30 … --rrule "FREQ=WEEKLY;BYDAY=WE" --owner self --deadline 2026-12-01 --pre-remind 15`；`show <uid> --json` | `0` / `0` | **通过**。`show --json` 含 `uid / kind / start / end / rrule / owner / deadline / status / reminderPolicy / aliases`，字段齐全 | `ws1/state/events.json` |
| 2 | `.ics` 真相 | 迁移后 `list --year 2026 --json` 记基线；删 `state/calendar-index.db*` 与 `state/events.json*`；从 `.ics` 重建索引 + 影子；再 `list --year 2026 --json` 比对指纹 | 重建后 `0` | **通过**。删前 35 条、删后重建 35 条，`(uid, start, ocid)` 指纹集合完全一致 | `evidence/item2-before.json`、`evidence/item2-after.json`、`ws-mig/` |
| 3 | RRULE occurrence（跨周 + 跨年） | 引擎 `expand_occurrences("FREQ=WEEKLY;BYDAY=WE")`；链路 `list --date <周三> --json`；`plan --now 周三 19:00`；`plan --now 周四 09:00`；`due --now 周三 19:00` | 链路 `0` / `0` / `0` / `0` | **通过**。引擎展开 6 个周三（12-02/09/16/23/30、2027-01-06）；链路每个周三当天 1 条实例、非周三 0 条、跨年 2027-01-06 仍 1 条；plan 周三实例化 1 窗口、周四 0 窗口；due 当天只响一次、不重不漏 | `ws1/`、`ws-nlu/` |
| 4 | NLU 六句 `create --when` | `create --title … --when "明天下午三点"` / `"每周三晚上八点"` / `"每年11月5日"` / `"半小时后"` / `"3天后"` / `"下个月1号"`（`--now 2026-11-28T12:00:00+08:00`） | 六句均 `0` | **通过**。六句全部走 create 落账：一次性 `once` 无 rrule；周期产出 `FREQ=WEEKLY;BYDAY=WE` / `FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=5`；缺时刻短语套默认 09:00（见 T-015 已知缺口） | `ws-nlu/state/events.json`、`nlu_check.py` 输出 |
| 5 | CRUD 全链路 | `create` → `edit` → `complete`；终态后重复 `cancel`；`cancel` → 同天 `cancel --undo`；另一条 `cancel` → 跨日 `cancel --undo` | `0`/`0`/`0`；拒重复取消 `4`；同天 undo `0`；跨日 undo `4` | **通过**。edit/complete 落账；终态守卫拒绝重复取消；同天 undo 恢复 `scheduled`；跨日 undo 拒绝（`4`），状态仍 `cancelled` | `ws1/state/events.json`、`ws1/state/audit.jsonl` |
| 6 | 查询（日 / 年 / 跨年 / `--q` / `--kind` / 导出导入） | `list --year 2026 --json`；`--q 周会`；`--kind recurring`；`--kind once`；`--from 2026-12-30 --to 2027-01-06`；`list --date 2026-12-02`；`export --ics <file>`；新工作区导入 `.ics` 重建后回读 | 均 `0` | **通过**。年 8 / 关键词周会 5 / recurring 5 / once 3 / 跨年区间 2 / 指定日 1；导出 `.ics` 导入新工作区后回读 4 个事件定义、展开 8 条实例，与源一致 | `ws1/`、`export.ics`、`ws-import/` |
| 7 | 审计留痕与回放 | 全流程后读 `state/audit.jsonl`；`events --json` 查 history 轨迹 | `0` | **通过**。审计 9 条：`create×4 / edit×1 / complete×1 / cancel×2 / cancel-undo×1`，字段含 `action/actor/old/new/source/target/ts`；history 表可按 uid 回放整条轨迹 | `ws1/state/audit.jsonl`、`ws1/state/history.db` |
| 8 | 迁移（备份 / 回滚 / 旧命令退出码） | `rtc_migrate --to-ics`（dry-run）；`--to-ics --apply --backup-dir <临时目录>`；`--to-ics --rollback <备份目录>`；旧五条命令 `status --json` / `history <date>` / `schedule <date>` / `due --now` / `report` | dry-run `0`；apply `0`；rollback `0`；旧命令 `0`（空工作区 history `0`） | **通过**。dry-run 计划 2 条；apply 写 `.ics` + 索引 + 影子，`manifest.json` 留源文件 SHA256；rollback 回到迁移前（`calendar/`、索引、影子清理，旧文件未改）；旧五条命令退出码与迁移前一致 | `ws-mig/`、`bk/mig/<时间戳>-to-ics/manifest.json` |
| 9 | 门禁（全量 verify + 安装器冒烟 + 旧测试全绿） | `pwsh -File scripts/verify.ps1`（静态 + 测试 + 覆盖率双阈值 + 安装器 `install.py --workspace ./_smoke-ws --dry-run`） | `0` | **通过**。633 tests OK（skipped=1）；语句 87.71% / 分支 80.42% 过双阈值；安装器 dry-run 冒烟 ok；静态段全过 | `evidence/verify-p7.log` |

### 2.1 未通过 / 待后续项

- 项 4 中「每年11月5日」「下个月1号」缺时刻时套默认 09:00，属 **T-015 已知缺口**（入口确认时刻 / 显式标注待后续），非本次集成回归。
- 项 3 的后续重复响铃幂等键与 `.ics` 落账仍有残留边界，对应 **T-022**；本叠栈已合 #120，当天实例化链路已向好（见第 4 节审计 E2/E3）。

## 3. 门禁结果

来源：`evidence/verify-p7.log`（`pwsh -File scripts/verify.ps1`，本地 Win11 / py3.11）。

| 门禁项 | 结果 |
|---|---|
| 脚本基线（禁 cmd/bat） | ok |
| 根目录白名单 | ok |
| 冲突标记残留 | ok |
| 文档链接可达 | ok |
| 版本号 SemVer | ok |
| 文档命令（子命令/参数 + 链路冒烟） | ok（92 条 rtc.py 引用，31 个子命令） |
| `ruff format --check` | ok（45 文件） |
| `ruff check` | All checks passed |
| `mypy` | 22 源文件 0 错误 |
| `vulture --min-confidence 60` | ok |
| 全量测试 | **Ran 633 tests**，**OK（skipped=1）** |
| 覆盖率双阈值 | 语句 **87.71%**（阈值 80%，5948 行）；分支 **80.42%**（阈值 70%，2140 个） |
| 安装器冒烟（`install.py --workspace ./_smoke-ws --dry-run`） | ok，退出码 0，5/5 链路自检 |
| 总退出码 | **0（全部通过）** |

> 说明：测试内含子进程 golden 隔离用例，coverage 下墙钟约 1574s；`install.py` 未被测试 import 的 module-not-imported 警告为既有口径，不影响阈值判定。

### 3.1 CI 双平台结果

PR #125 触发的 GitHub Actions（run `46101353709`）：

| Job | 结果 | 测试数 |
|---|---|---|
| 静态检查 | pass | — |
| py3.11 / ubuntu-latest | pass | Ran 633 tests，OK（skipped=1） |
| py3.13 / ubuntu-latest | pass | Ran 633 tests，OK（skipped=1） |
| py3.11 / windows-latest | pass | Ran 633 tests，OK（skipped=1） |
| py3.13 / windows-latest | pass | Ran 633 tests，OK（skipped=1） |

**D4 闭环**：ubuntu 与 windows、py3.11 与 py3.13 四个测试 job 测试数一致（均 633，skipped=1），无跨 OS 测试数差值。日志中偶见的 `audit: 写入失败 FileExistsError state` 为既有隔离用例诊断输出，不影响结果判定。

## 4. 审计项逐条定性

来源：`rtc-work/L2-AUDIT.md`（只读横向比对，基线 `991c2b5`）。定性分三类：

- **已处置**：本叠栈代码 / 测试 / 文档已落地。
- **待合并时处置**：依赖后续分支或后续 phase 接线，本批未闭环。
- **接受为已知缺口**：收口重构或 latent 风险，不在本批范围，留待对应阶段。

| 编号 | 定性 | 落点 | 处置结论 |
|---|---|---|---|
| A1 | NLU `dtstart` ↔ 契约 `start` 命名 | #124 `rtc_event_cmd.py` | **已处置**。`parsed.dtstart → Event.start` 映射在 `_create_from_phrase` 落地；事件层未另造 `dtstart` 字段。 |
| A2 | `.ics` 往返字段超出 schema、无校验兜底 | #121 / #117 | **已处置**。`summary/categories/tzid/dtstamp/unknown/rev/crossYear` 已补进 `event.schema.json`；往返字段过 jsonschema 校验（`test_roundtrip_fields_validate`）。 |
| A3 | `rev` 半截悬空 | #123 / #121 | **部分已处置，余口接受为已知缺口**。`rev` 已进 schema，写路径新建 0、修订 +1；事件级并发锁 / CAS 冲突检测尚未对齐旧账本 `atomic_write(expected_rev)`，留收口阶段。 |
| A4 | date-only `UNTIL` 边界收口偏早 | #117 / #118 | **已处置**。投影层 `_until_date_time` 把 `validUntil` 产成当日 `23:59:59` 带偏移 date-time，不用 date-only。 |
| A5 | 缺省 `end` 时长规则未定义 | #117 / #120 | **已处置**。契约写明 `end` 缺省与 `start` 同刻（零时长），不在适配器藏默认分钟数。 |
| A6 | `.ics` 往返不保留 `createdAt/updatedAt/source` | #121 | **已处置**。契约明确三者是本地投影留痕、不随 `.ics` 走，缓存重建后可失。 |
| A7 | 新旧两套 UID 算法 | #121 | **已处置**。契约注明旧导出为单向出口、不参与往返；旧 `ocid/id` 进 `aliases` 兼容读取。 |
| A8 | 枚举 / BYDAY / X-RTC 命名核对 | — | **核对一致，无问题**。 |
| B1 | `D-016`/`T-018` 双份携带 | 本次合并 | **已处置**。叠栈人工去重成一份，编号有序（见 §1.1）。 |
| B2 | `D-013` 四路撞号、P1 注释指向待重排编号 | 跨批遗留 | **接受为已知缺口**。属 #106/#113/#107/#108 跨批重排；L2 各 PR 未新抢号，待跨批重排后回填 P1 注释。 |
| B3 | 编号连续无跳号核对 | — | **核对一致，无问题**。 |
| C1 | `X-RTC-STATE` / `X-RTC-STATUS` 两套属性名 | #121 | **接受为已知缺口**。旧层为单向出口、不参与往返；未来要回读旧导出时，解析端再补 `X-RTC-STATE` 兼容读取。 |
| C2 | 文本转义两套 helper | #121 | **接受为已知缺口**。旧导出被既有用例钉死暂留；新代码统一用 `escape_text`；收口阶段旧层复用新 helper 并跑 ics 回归。 |
| C3 | 迁移备份 / manifest / 回滚脚手架两套并行 | #121 | **接受为已知缺口**。JSON 路径语义未漂移；收口抽公共「备份清单 + manifest + 按清单回滚」helper。 |
| C4 | 迁移后读源仍是 `events.json`，与 `.ics` 真相错位；写盘锁/rev 未定 | #121 / #123 | **部分已处置，余口接受为已知缺口**。迁移后从 `.ics` 重建 `events.json` 影子视图，读者看到一致数据（方案①落地）；事件写盘锁 / rev CAS 口径留收口。 |
| C5 | P1 既有代码未被改写、未另造日志层 | — | **核对一致，无问题**。 |
| D1 | #121 缺 CLI golden 逐字节证据 | #121 | **已处置**。空工作区五条命令 `status --json` / `history` / `schedule` / `due --now` / `report` 的 rc + stdout/stderr SHA256 golden 已补。 |
| D2 | #118/#119 守卫证据未落到正文验证段 | #118 / #124 | **已处置（#118）/ 待合并时处置（#119 正文守卫说明）**。#118 引擎纯库、主链路零 import，隔离断言在测试；#124 解析层惰性 import `rtc_nlu`，正文验证段守卫位置说明随文档收口补注。 |
| D3 | 黄金回归证据形式一致 | — | **核对一致，无问题**。 |
| D4 | 跨 OS 测试数差值待解释 | 本次验收 | **已处置（CI 闭环）**。run `46101353709` 四个测试 job（ubuntu/windows × py3.11/3.13）均 Ran 633 tests、OK（skipped=1），测试数一致，无跨 OS 差值；跳过清单见 `evidence/verify-p7.log`。 |
| E1 | 受审 PR 未复制既有资产 | #123 / #122 | **已处置**。P5/P6 未重写 #112 筛选谓词、#113 undo、#114 snooze、#115 preRemind；P6 只在事件层补字段映射。 |
| E2 | 同源双写期旧窗口 + 投影事件双响无用例 | 读切换阶段 | **接受为已知缺口**。迁移后影子由 `.ics` 重建；同一 `routineId` 的旧窗口与 occurrence 当天去重判据（别名命中）留读切换阶段补用例。 |
| E3 | 持久化 ocid 只编码到天，不含起点时刻 | #120 | **接受为已知缺口（latent）**。当前 RRULE 子集一天至多一实例不撞；接入 BYHOUR / 同日多实例前，ocid 预留时刻分量或命中改走 `uid + occurrence start`。 |

## 5. 已知缺口与后续阶段

1. **T-015**：裸周几 / 月锚点缺时刻套默认 09:00，需入口确认或显式标注。
2. **T-022**：周期事件后续 occurrence 的 plan/due 展开幂等键与 `.ics` 落账，待后续 phase 接线。
3. **A3 / C4 写盘侧**：事件级并发锁与 rev CAS 对齐旧账本口径，收口阶段决策。
4. **C1 / C2 / C3**：旧层属性名、转义 helper、迁移脚手架的收口重构。
5. **E2 / E3**：同源双写去重用例、ocid 时刻粒度，读切换阶段处理。
