---
name: daily-task-prep
description: 每日 {{CUTOVER_TIME}} 日切。归档昨天完成项、把到期任务提升到今天、按时刻表重生成当天的窗口表。
---

# Daily Task Prep

每天 {{CUTOVER_TIME}} 跑一次（`isolated` 会话）。**必须早于 {{WAKE_TIME}} 第一次响铃。**

## §0 幂等

先读 `state/alarms.json`。如果 `planDate` 已经是今天，且 `generatedBy` 是 `rtc-plan` 或 `daily-task-prep`：

**直接输出 `已 prep 过，跳过`，什么都不改。** 跑两次不能生成两套窗口。

## §1 步骤（顺序别换）

1. 判今天日期类型（`routines.md` §一）
2. **归档**：读 `tasks.md`，把 `[x]` 和 `[~]` 的条目**移动到** `tasks-completed.md` 的 `## 今天` 下（新建该日期节，放最上面），`tasks.md` 里删掉这些行
3. **提升到期**：`待安排（有日期）` 里截止日 ≤ 今天的，移到 `Today` 对应用户段，并从原分区删掉（**同一次编辑内完成，不许留副本**）
4. **清理 Today**：`Today` 超过 12 条就保持原样不删（截断是人的决定，不是你的），但在输出里提一句
5. **生成窗口**：优先调用 `code/rtc.py plan`（确定性、幂等），它写的 `generatedBy` 是 `rtc-plan`；没有运行环境时才按 `rules/routines.md` §五 的日切规则手写 {{WINDOW_COUNT}} 个窗口，`planDate` = 今天，`rev + 1`，`generatedBy` 写 `daily-task-prep`（标明这是手写的降级产物）
6. **校验**：窗口数 = {{WINDOW_COUNT}}、`expectFires` 合计 = {{EXPECTED_FIRES}}、每个 `start/end` 都带 `{{TZ_OFFSET}}`
7. 校验不过 → **不写 alarms.json**，往 `errors.md` 记一条 `prep_validation_failed`，输出告警

## §2 写回

| 文件 | 写什么 |
|---|---|
| `tasks.md` | 删掉已归档行、提升到期项、更新文件头 `最后更新` |
| `tasks-completed.md` | 添加今天日期节 |
| `state/alarms.json` | 整份重写（你是唯一有这个权限的角色） |

写 `{{STATE_DIR}}/alarms.json` 前把旧文件复制成 `alarms.json.bak`（覆盖式，只留一份）。写坏了还能回退。

**写完之后顺手落账（可选，失败就跳过）**：

```
node {{CODE_DIR}}/metrics.mjs sync past --state {{STATE_DIR}}
```

它把"今天以前"的窗口写进 `metrics.db`。这一步是**单向旁路**：只读 `{{STATE_DIR}}/alarms.json`、只写自己的库，不参与提醒。报错不要重试、不要修，继续往下走。

为什么放在这里：prep 是唯一会压缩 `history` 的角色，压缩前后是同一轮，落账最不容易漏。20:00 总结还会再跑一次全量（幂等），双保险。

## §3 昨天的窗口怎么办

昨天遗留的 `pending` / `ringing`：

- 压进 `history`，`state: "missed"`，`fires` = 当时的 `fireCount`
- `history` 只留最近 14 天，更早的丢掉

`history` 是你唯一的趋势数据来源，别写成空数组。

## §4 输出

一行摘要就够，不要写小作文：

```
prep 完成：归档 3 条，提升 1 条，今日 {{WINDOW_COUNT}} 个窗口（{{EXPECTED_FIRES}} 次）
```

异常时：

```
prep 异常：窗口数 3（期望 4），未写入 alarms.json，已记 errors
```

## §5 禁止

- ❌ 改 `routines.md`（那是人的宪法，只读）
- ❌ 改 `tasks.md` 里的任务措辞（除了移动和加时间戳）
- ❌ 自己判断"这条应该删"（归档 ≠ 删除）
- ❌ 在 {{CUTOVER_TIME}} 发任何提醒消息（这条任务永远 `delivery: none`）
- ❌ 校验失败还硬写
