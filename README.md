# RTC-kit

**一套可复用的「提醒 + 任务 + 判断」规则包。** 它不是应用，也不是服务——是一组 Markdown 规则、技能说明和机器可读的 JSON Schema，装进任意兼容的 agent 运行时即可使用；也可以作为将来自行开发软件的规格书。

> 这个仓库是**干净模板**：所有个人信息、真实作息、本机路径、账号与投递目标都已替换成 `{{占位符}}`。
> 仓库里没有任何人的实际使用数据。

---

## 它解决什么问题

个人助理类 agent 最常见的失败方式，是把「提醒」建模成「N 条一次性定时任务」，于是每天要重建这些任务。重建脚本一出错，提醒全线静默；自动修复器再补一遍，又可能翻倍。最后维护成本超过收益，整套系统被关掉。

RTC-kit 的做法：

- **一条周期触发器** 覆盖一类重复提醒，状态存在数据里，不在任务条数里
- **用户确认（ack）是一个字段**，不是「删掉剩余 N 条任务」
- **单一真相源、单向依赖**，禁止两个自动修复器互相纠正
- **定时与状态交给普通代码**，模型只负责理解请求、组织措辞、守住边界

---

## 五分钟理解结构

```
contract/     机器可读契约（JSON Schema）—— 未来写软件时读这里
rules/        声明式规则 —— 人或模型读，描述「什么重要、能不能自己办、什么时候响」
skills/       技能说明 —— 告诉 agent 什么时候做什么、怎么说、什么不能做
templates/    工作区模板 —— 全部是 {{占位符}}，复制后填自己的值
code/         确定性脚本 —— 时间计算、状态读写、统计
platform/     运行时适配示例 —— 如何把规则接到某个具体平台
```

三层分离是核心：**规则是数据，技能是流程，代码是手脚，运行时只是宿主。** 换平台不改写规则；将来做成独立软件，直接消费 `contract/` 里的 schema。

---

## 快速开始

以 OpenClaw 为例（其他运行时见 `platform/README.md` 的思路）：

1. 把 `templates/` 里的 `*.example.md` 复制进工作区，去掉 `.example`，把 `{{...}}` 换成你自己的值
2. 把 `rules/` 和 `skills/` 放进工作区
3. 按 `platform/openclaw.md` 建定时任务
4. 按 `rules/routines.example.md` 填你的时刻表 —— **这是唯一需要你决定的东西**

最小可用版本：一个提醒窗口、一个投递渠道、一个 `tasks.md`。别一次把功能全开。

---

## 只包含四个操作（建议的 v1）

```
create     添加提醒
list       列出提醒
reschedule 改期
cancel     取消
```

主动关心、情绪判断、优先级打分、推测意图——**都不在第一版**。它们应该从稳定的基础上长出来，而不是先写在规则里。

---

## 规则怎么复用

`contract/` 下的 JSON Schema 定义了三类数据的字段与枚举：

| Schema | 描述 |
|---|---|
| `routine.schema.json` | 时刻表：窗口、间隔、期望次数、日期类型覆盖 |
| `alarm-state.schema.json` | 运行状态：窗口状态机、ack、missed、跨日归档 |
| `task.schema.json` | 任务：ID、状态、截止日、分区 |

`rules/*.md` 是这些契约的**人类可读渲染**；`contract/examples/*.json` 是可直接校验的示例。两者字段一一对应。将来写软件时，以 JSON 为源、Markdown 为视图即可，不需要重新推规则。

---

## 目录一览

| 路径 | 内容 |
|---|---|
| `README.md` | 本文件 |
| `ARCHITECTURE.md` | 分层架构、职责边界、为什么这样切、演进路径 |
| `AUDIT-2026-11-09-RESPONSE.md` | 对 2026-11-09 外部审计的逐条处置记录 |
| `CONTRIBUTING.md` | 怎么改、怎么填占位符、什么不能提交 |
| `ATTRIBUTION.md` | 与上游项目的关系、许可边界 |
| `LICENSE` | AGPL-3.0（仅适用于本仓库原创内容） |
| `contract/` | JSON Schema + 可校验的示例实例 |
| `rules/` | 声明式规则（`routines` / `priority-map` / `auto-resolver` / `formats`） |
| `skills/` | 5 个技能说明 |
| `templates/` | 工作区模板（全部 `*.example.md`） |
| `code/` | `rtc.py`（主链路实现）、`metrics.mjs`（旁路统计）、`tests/`（回归测试） |
| `platform/` | 运行时适配示例 |

## 包路径 → 运行时工作区路径

包是**源码**，运行时工作区是**部署产物**。技能里写的都是后者：

| 包内 | 装到工作区后 | 说明 |
|---|---|---|
| `rules/routines.example.md` | `rules/routines.md` | 填好占位符的时刻表，唯一「何时响」真相源 |
| `rules/priority-map.md` | `rules/priority-map.md` | 直接复制 |
| `rules/auto-resolver.md` | `rules/auto-resolver.md` | 直接复制 |
| `rules/formats.md` | `rules/formats.md` | 直接复制 |
| `templates/AGENTS.example.md` | `AGENTS.md` | 追加或替换入口文件 |
| `templates/USER.example.md` | `USER.md` | 填自己的信息 |
| `templates/TOOLS.example.md` | `TOOLS.md` | 填本机事实 |
| `templates/tasks.example.md` | `tasks.md` | 之后由你增删任务 |
| `contract/examples/alarms.example.json` | `{{STATE_DIR}}/alarms.json` | 运行时状态，不入库 |
| `contract/examples/tasks.example.json` | 可选 | 逻辑结构的机器可读示例 |
| `code/rtc.py` | `{{CODE_DIR}}/rtc.py` | 提醒主链路：plan / due / ack / status / metrics |
| `code/metrics.mjs` | `{{CODE_DIR}}/metrics.mjs` | 统计旁路，可选 |
| `skills/*/SKILL.md` | `skills/<name>/SKILL.md` | 由运行时加载 |
| `platform/jobs.example.json` | 不部署 | 只作参数参考，按平台文档建任务 |

---

## 状态与边界

- 这是一套**规格与模板**，不是开箱即用的产品。它需要你填配置、选运行时、建定时任务。
- 示例代码只覆盖「统计记账」这一条旁路，**提醒主链路的调度实现由使用方自己决定**——这正是刻意留出的解耦点。
- 上游 `snarktank/clawchief` 没有附带许可证；本仓库不分发其任何原文。详见 `ATTRIBUTION.md`。

## 许可

本仓库原创内容采用 **AGPL-3.0**，见 `LICENSE`。若你修改后以网络服务形式提供，AGPL 第 13 条要求向使用者提供对应源码。

注意：AGPL 只覆盖本仓库原创部分，不覆盖任何第三方材料。
