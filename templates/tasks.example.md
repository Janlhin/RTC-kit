# tasks.example.md — 要做什么

> **全系统唯一的任务清单。** 机器可读契约见 `contract/task.schema.json`。
> 最后更新：`{{DATE}}`

---

## Today

### principal

- [ ] td-{{DATE_COMPACT}}-{{TIME_COMPACT}}-example-a 示例任务 A

### assistant

- [ ] td-{{DATE_COMPACT}}-{{TIME_COMPACT}}-example-b 示例任务 B

---

## 待安排（有日期）

- [ ] td-{{DATE_COMPACT}}-{{TIME_COMPACT}}-example-c 示例任务 C（截止 {{DATE_DUE}}）

---

## 待安排（没日期）

- [ ] td-{{DATE_COMPACT}}-example-d 示例任务 D（无约定时刻，省略 HHMM）

---

## 规则

**符号只有三种**

| 符号 | 含义 |
|---|---|
| `- [ ]` | 没做 |
| `- [x]` | 做完了，同行末尾补 `✅ HH:MM` |
| `- [~]` | 不做了 / 推迟 / 过期没做，末尾补原因 |

**ID 格式**：`td-YYYYMMDD-HHMM-slug`

- `YYYYMMDD-HHMM` 是这条任务**约定的执行时刻**（本地 24 小时制）
- **没有约定时刻 → 省略 `HHMM`**，写成 `td-YYYYMMDD-slug`；此时日期取创建日
- `slug` 只用小写字母、数字、连字符；空格是 ID 与正文的分隔符
- `0000` 不许用来表示「没时刻」——那是省略 `HHMM` 的变体
- ID 只是稳定引用，**不参与任何运行时逻辑**（不建 cron、不做匹配）

**截止日期**：正文末尾 `（截止 YYYY-MM-DD）`。没有就不写。

**分区怎么用**

- `Today`：今天真的要动的，上限 12 条
- `待安排（有日期）`：到了那天由日切提升进 Today
- `待安排（没日期）`：想做但没排期
- 分区管「什么时候做」，符号管「做没做」—— **不建「已完成」分节**

**谁能改**：主会话、`daily-task-manager`、日切。提醒与播报技能**只读**。

**归档**：今天完成的今天留着，次日日切移到 `tasks-completed.md`。
