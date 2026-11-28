# TASKS

任务队列。字段：编号 · 状态 · 问题 · 方法 · 完成判据（命令 + 期望退出码）· 创建时间 · 完成时间。

状态取值：`todo` / `doing` / `done` / `blocked`。**已决条目保留，不删除。**

---

## 门禁与质量

| 编号 | 状态 | 问题 | 方法 | 完成判据 | 创建 | 完成 |
|---|---|---|---|---|---|---|
| T-001 | done | 仓库没有 Lint / 类型 / 覆盖率门禁，缺陷只能靠人工 review | 接入 `ruff`（Lint + 格式）与 `mypy`，`verify.ps1` 聚合，CI 执行 | `pwsh -File scripts/verify.ps1` → 0 | 2026-11-16 | 2026-11-16 |
| T-002 | done | 覆盖率无阈值，测试只增不减的假设没有任何机器判据 | 用 `coverage.py` 测基线并接入 `verify`；2026-11-20 实测 82% / 分支 76% 后上调到目标值 | `pwsh -File scripts/check-coverage.ps1 -Python <venv>` → 0（语句 80 / 分支 70 双判定） | 2026-11-16 | 2026-11-20 |
| T-003 | todo | 总覆盖率已达标，但局部有空洞（`rtc_plan.py` 0%、`rtc_once.py` 62%），总数字把它们盖住了 | 按模块逐个补边界用例，先补 `rtc_once` 的 plan/due 互斥路径，再给 `rtc_plan` 补入口用例 | `python -m coverage report --include="code/rtc_once.py" --fail-under=70` → 0 | 2026-11-16 | — |
| T-004 | done | `mypy` 尚未收紧到 `--strict` | 分批补注解、消 `Any`，每批一个独立提交（共 8 批：`rtc_ics` / `rtc_report` / `rtc_archive` / `rtc_history` / `rtc_metrics` / `install.py` / `rtc_brief` / `rtc_core`）；收完后把 `strict = true` 从 overrides 提到 `[tool.mypy]` 顶层 | `python -m mypy --strict code install.py` → 0 | 2026-11-16 | 2026-11-21 |
| T-005 | done | 仓库内没有死代码检查，删了函数没人发现还有调用方 | 接入 `vulture`，置信度取 60（80 会放过真正的死函数），测试目录排除、`row_factory` 这类动态属性忽略；接入 `verify` 静态段与 CI | `python -m vulture code install.py --min-confidence 60 --ignore-names row_factory --exclude code/tests` → 0 | 2026-11-16 | 2026-11-20 |
| T-009 | todo | 重复代码没有度量（目标 < 3%），同类逻辑散在多个模块里只能靠人眼发现 | 评估 `jscpd`（Node）与 `pylint` 重复块报告两条路，选不需要第二套包管理器的那个，接入 CI | 选定工具后：报告退出码 0，且重复率 < 3% | 2026-11-20 | — |
| T-006 | done | 文档里的命令片段没有被实际执行过，写错了没人发现 | 人工抽取 30 份 Markdown 的 79 条命令，在隔离工作区逐条实跑并记录退出码（`docs/verification-2026-11-16.md`） | 每条命令退出码符合预期，0 条失败 | 2026-11-16 | 2026-11-16 |
| T-006b | done | 上面是人工一次性验证，没有机器判据——下次文档改错不会有人跑 | 写 `scripts/verify-docs.ps1`：抽代码块里的 `rtc.py` 命令比对真实子命令/参数，再在临时工作区跑一条链路；已接入 `verify.ps1` | `pwsh -File scripts/verify-docs.ps1` → 0（故意写错子命令/参数 → 1） | 2026-11-16 | 2026-11-16 |
| T-007 | done | 文档断链无门禁 | 接入 `scripts/check-doc-links.ps1`，覆盖全部 `.md` 的相对链接（不校验锚点） | `pwsh -File scripts/check-doc-links.ps1` → 0 | 2026-11-16 | 2026-11-16 |
| T-008 | done | README 里残留了合并冲突标记，所有门禁都没拦住 | 新增 `scripts/check-conflict-markers.ps1`（`git grep` 三个形态），接入 `verify.ps1` 静态段 | `pwsh -File scripts/check-conflict-markers.ps1` → 0；故意写入 `>>>>>>> origin/main` → 1 | 2026-11-20 | 2026-11-20 |

## 功能与修复

| 编号 | 状态 | 问题 | 方法 | 完成判据 | 创建 | 完成 |
|---|---|---|---|---|---|---|
| T-010 | done | `rtc.py` 2788 行、8 个领域挤在一个文件，改一处容易误伤另一处 | 拆成 `rtc_core` / `rtc_archive` / `rtc_metrics` / `rtc_brief`，`rtc.py` 退成单命名空间薄壳（见 D-008），命令面零变化 | 149 条测试一条不改全绿；拆前/拆后 6 条命令输出逐字节一致；`rtc.py` 30 行 | 2026-11-16 | 2026-11-16 |
| T-011 | todo | 备份方案只差恢复文档：没有 `docs/RESTORE.md`，也没真删过 `alarms.json` 演练 | 写恢复文档并做一次删除恢复演练 | 删除 `state/alarms.json` 后按文档恢复，`rtc.py status` 与备份时刻一致 → 0 | 2026-11-16 | — |
| T-012 | todo | 外部审计提出的锁实现（持久 sidecar + 操作系统持锁）尚未合并与实机验证 | 先停旧写手，再按两套锁协议不可重叠的前提合并并实测 | 两个进程并发写同一 `alarms.json`，一成一败且无静默丢写 → 0 | 2026-11-16 | — |
| T-013 | todo | `status` 不显示备份状态，备份失败只有当天一行 stdout | 增加「上次成功 / 目标盘 / 错误」字段 | `python code/rtc.py --workspace <ws> status --json` 含 `backup` 字段 → 0 | 2026-11-16 | — |
| T-014 | todo | NLU 仍不支持的周期形状：每月最后一个工作日、每年第 N 周的周几、工作日 / 节假日、隔周 / 每两周（需工作日历或 BYSETPOS 定位） | 排程层定型工作日历与 BYSETPOS 后再解析；基础天 / 周 / 月 / 年与每 N 周期已由 P4（T-019）解析 | 上述形状可经入口登记并正确重复响铃；不支持清单见 `docs/nlu-l0-l1.md` §4 | 2026-11-28 | — |
| T-015 | todo | 裸周几 / 月锚点缺时刻时解析器固定默认 09:00，可能不符用户预期 | 入口层向用户确认时刻，或在结果里显式标注「默认 09:00」 | 缺时刻短语不再静默套默认值；用例覆盖确认 / 改时刻两条路径 | 2026-11-28 | — |
| T-016 | doing | L2 P1：事件一等公民模型、影子账本、旧窗口投影与迁移骨架 | 契约 `event.schema.json` + `rtc_events.py` + `rtc_migrate.py` + golden 比对；默认口径见 D-014 | 迁移前后五条命令（`status --json` / `history <date>` / `schedule <date>` / `due --now` / `report`）stdout+stderr+退出码 SHA256 逐字节 diff=0；现有测试一条不改全绿；`pwsh -File scripts/verify.ps1` → 0 | 2026-11-28 | — |
| T-019 | done | P4 上半段：把天 / 周 / 月 / 年与每 N 周期表达解析为 RFC 5545 RRULE + DTSTART | `rtc_nlu` 增加 recurring 分支，`recurring` 开关门控；解析层不接写路径 | 五类必测 + 三类每 N + 三类拒绝 + 跨年 + 一次性回归全绿；`pwsh -File scripts/verify.ps1` → 0 | 2026-11-28 | 2026-11-28 |
| T-020 | doing | L2 P5：事件级 CRUD（create/edit/complete/cancel/undo）+ owner/deadline/reminderPolicy | `rtc_event_cmd.py` 事件写命令层；摘要守卫 + 排他锁；审计 + history 留痕；默认口径见 D-018 | create→edit→complete→cancel→undo 全链路用例；同 uid 幂等；跨日 undo 拒绝；无痕迹 undo 失败；并发 Conflict 不覆盖；旧五条命令逐字节 diff=0；`pwsh -File scripts/verify.ps1` → 0 | 2026-11-28 | — |
| T-021 | doing | P4 下半段：把 NLU 周期 / 一次性短语接到 `create --when`，落账 `start/end/rrule`；occurrence 展开联动留 P3 | `rtc_event_cmd.py` 接 `rtc_nlu`；`--when` 与 `--start/--end/--rrule` 互斥；不支持短语沿用 rc=2；映射口径见 D-018 ⑩ | 五类周期 + 三类每 N + 四句一次性 create 落账字段正确；冲突参数报错；不支持短语 rc=2；`pwsh -File scripts/verify.ps1` → 0 | 2026-11-28 | — |
| T-022 | todo | 周期事件 occurrence 展开：当前 `create --when` 只落账 `rrule` + 首个 `start`，`plan` / `due` 尚不展开后续 occurrence | P3 展开引擎接线：按 RRULE 生成命中槽位，接 plan/due/ack 幂等键；本 lane 不展开、不写 `.ics` | 一条 `FREQ=WEEKLY;BYDAY=WE` 事件在一周内 plan 出对应槽位并可重复响铃；旧五条命令逐字节 diff=0 | 2026-11-28 | — |

## 待裁决台账

| 编号 | 问题 | 选项 | 不裁决时的默认 |
|---|---|---|---|
| Q-001 | `tools/one-off/patch_metrics.py` 是已完成使命的一次性脚本，留还是删？ | ① 留（已从主链路移出）② 删（历史在 Git） | 留，不删用户文件 |
| Q-002 | 公开仓库是否保留外部审计处置记录（含旧实现细节）？ | ① 保留在 `docs/audit/` ② 只留结论、细节移入私有仓 | 保留，已确认无个人信息 |
| Q-003 | `code/metrics.mjs` 仍被 README 引用，是否随主链路一起维护？ | ① 保留为可选旁路 ② 标注「仅示例、不再维护」 ③ 删除 | 保留并标注为可选 |
| Q-004 | 命中不支持的周期 / 集合表达时，入口除报错外是否要回退给 L2 模型层兜底？ | ① 本轮仅报错，留待 T-014 ② 接宿主模型做二次解析 ③ 提示用户改写为一次性时刻 | 本轮仅报错；是否接 L2 兜底待排程层与模型层方案定型后裁决 |
