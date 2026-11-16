# TASKS

任务队列。字段：编号 · 状态 · 问题 · 方法 · 完成判据（命令 + 期望退出码）· 创建时间 · 完成时间。

状态取值：`todo` / `doing` / `done` / `blocked`。**已决条目保留，不删除。**

---

## 门禁与质量

| 编号 | 状态 | 问题 | 方法 | 完成判据 | 创建 | 完成 |
|---|---|---|---|---|---|---|
| T-001 | done | 仓库没有 Lint / 类型 / 覆盖率门禁，缺陷只能靠人工 review | 接入 `ruff`（Lint + 格式）与 `mypy`，`verify.ps1` 聚合，CI 执行 | `pwsh -File scripts/verify.ps1` → 0 | 2026-11-16 | 2026-11-16 |
| T-002 | doing | 覆盖率无阈值，测试只增不减的假设没有任何机器判据 | 用 `coverage.py` 测基线，写入 `pyproject.toml` 阈值，逐季上调 | `python -m coverage report --fail-under=<当前阈值>` → 0 | 2026-11-16 | — |
| T-003 | todo | 覆盖率未达语句 80% / 分支 70% 的目标 | 补 `plan` 边界、`gitarchive` 失败路径、并发写冲突用例 | `python -m coverage report --fail-under=80` → 0 | 2026-11-16 | — |
| T-004 | todo | `mypy` 尚未收紧到 `--strict` | 分批补注解、消 `Any`，每批一个独立提交 | `python -m mypy --strict code install.py` → 0 | 2026-11-16 | — |
| T-005 | todo | 仓库内没有死代码与重复代码检查 | 接入 `vulture`（未使用符号）并接入 CI | `python -m vulture code install.py --min-confidence 80` → 0 | 2026-11-16 | — |
| T-006 | done | 文档里的命令片段没有被实际执行过，写错了没人发现 | 人工抽取 30 份 Markdown 的 79 条命令，在隔离工作区逐条实跑并记录退出码（`docs/verification-2026-11-16.md`） | 每条命令退出码符合预期，0 条失败 | 2026-11-16 | 2026-11-16 |
| T-006b | todo | 上面是人工一次性验证，没有机器判据——下次文档改错不会有人跑 | 写 `scripts/verify-docs.ps1`：抽取代码块里可安全执行的命令，在临时工作区跑，白名单外自动跳过 | `pwsh -File scripts/verify-docs.ps1` → 0 | 2026-11-16 | — |
| T-007 | done | 文档断链无门禁 | 接入 `scripts/check-doc-links.ps1`，覆盖全部 `.md` 的相对链接（不校验锚点） | `pwsh -File scripts/check-doc-links.ps1` → 0 | 2026-11-16 | 2026-11-16 |

## 功能与修复

| 编号 | 状态 | 问题 | 方法 | 完成判据 | 创建 | 完成 |
|---|---|---|---|---|---|---|
| T-010 | todo | `rtc.py` 2721 行、8 个领域挤在一个文件，改一处容易误伤另一处 | 按职责拆 `rtc_core` / `rtc_archive` / `rtc_metrics` / `rtc_brief`，命令面零变化 | `python -m unittest discover -s code/tests -p "test_*.py"` → 0，且 `rtc.py` ≤ 250 行 | 2026-11-16 | — |
| T-011 | todo | 备份方案只差恢复文档：没有 `docs/RESTORE.md`，也没真删过 `alarms.json` 演练 | 写恢复文档并做一次删除恢复演练 | 删除 `state/alarms.json` 后按文档恢复，`rtc.py status` 与备份时刻一致 → 0 | 2026-11-16 | — |
| T-012 | todo | 外部审计提出的锁实现（持久 sidecar + 操作系统持锁）尚未合并与实机验证 | 先停旧写手，再按两套锁协议不可重叠的前提合并并实测 | 两个进程并发写同一 `alarms.json`，一成一败且无静默丢写 → 0 | 2026-11-16 | — |
| T-013 | todo | `status` 不显示备份状态，备份失败只有当天一行 stdout | 增加「上次成功 / 目标盘 / 错误」字段 | `python code/rtc.py --workspace <ws> status --json` 含 `backup` 字段 → 0 | 2026-11-16 | — |

## 待裁决台账

| 编号 | 问题 | 选项 | 不裁决时的默认 |
|---|---|---|---|
| Q-001 | `tools/one-off/patch_metrics.py` 是已完成使命的一次性脚本，留还是删？ | ① 留（已从主链路移出）② 删（历史在 Git） | 留，不删用户文件 |
| Q-002 | 公开仓库是否保留外部审计处置记录（含旧实现细节）？ | ① 保留在 `docs/audit/` ② 只留结论、细节移入私有仓 | 保留，已确认无个人信息 |
| Q-003 | `code/metrics.mjs` 仍被 README 引用，是否随主链路一起维护？ | ① 保留为可选旁路 ② 标注「仅示例、不再维护」 ③ 删除 | 保留并标注为可选 |
