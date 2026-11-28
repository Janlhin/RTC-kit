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
| T-016 | doing | L2 P1：事件一等公民模型、影子账本、旧窗口投影与迁移骨架 | 契约 `event.schema.json` + `rtc_events.py` + `rtc_migrate.py` + golden 比对；默认口径见 D-014 | 迁移前后五条命令（`status --json` / `history <date>` / `schedule <date>` / `due --now` / `report`）stdout+stderr+退出码 SHA256 逐字节 diff=0；现有测试一条不改全绿；`pwsh -File scripts/verify.ps1` → 0 | 2026-11-28 | — |
| T-017 | doing | P2：`.ics` 成为事件台账源真相，SQLite 索引可删可重建，旧数据可迁移可回滚 | `rtc_ics` 双向 RFC 5545 序列化/解析（一年一文件、折行转义、未知属性保留）；`rtc_calindex` rebuild 从零重建并与直扫逐条对账；`rtc_migrate --to-ics` dry-run/备份/幂等/rollback，迁移后从 `.ics` 重建 `events.json` 影子视图，旧文件不删（见 D-015） | `pwsh -File scripts/verify.ps1` → 0；索引与直扫 `.ics` 逐条一致；迁移后影子视图与 `.ics` 核心字段逐条一致、重跑不漂移；迁移跑两次产物不变；rollback 恢复备份态；空工作区五条命令与 `origin/main` 基线 SHA256 diff=0 | 2026-11-28 | — |
| T-018 | todo | L2 P3：周期事件需要 RRULE 解析与 occurrence 展开，旧 `oneOff+expectFires+validFrom` 拼装表达不了跨月/跨年重复 | 引擎：自研 `code/rtc_rrule.py`（`parse_rrule` + `expand_occurrences`，时区感知、COUNT/UNTIL 全局预算、条数与扫描年数双硬上限）；接线：`code/rtc_occurrence.py` 把 `kind=recurring` 事件按当天窗口实例化进账本，去重键 `(uid, occurrence start)`，due/ack 只作用当前 occurrence | 引擎：`test_rrule.py` 九类边界全绿；接线：`test_occurrence.py` 覆盖跨天各响一次、重复 tick 不重响、ack 只确认当次、COUNT 用尽、UNTIL 边界、跨年、DST 秋切、无 events 时 golden 逐字节一致 | 2026-11-28 | — |

## 待裁决台账

| 编号 | 问题 | 选项 | 不裁决时的默认 |
|---|---|---|---|
| Q-001 | `tools/one-off/patch_metrics.py` 是已完成使命的一次性脚本，留还是删？ | ① 留（已从主链路移出）② 删（历史在 Git） | 留，不删用户文件 |
| Q-002 | 公开仓库是否保留外部审计处置记录（含旧实现细节）？ | ① 保留在 `docs/audit/` ② 只留结论、细节移入私有仓 | 保留，已确认无个人信息 |
| Q-003 | `code/metrics.mjs` 仍被 README 引用，是否随主链路一起维护？ | ① 保留为可选旁路 ② 标注「仅示例、不再维护」 ③ 删除 | 保留并标注为可选 |
