# contract — 机器可读的规则契约

这一层是「未来做成软件」的接口。JSON Schema 定义字段与枚举，Markdown 规则是同一份契约的人类可读渲染。

## 文件

| 文件 | 描述 |
|---|---|
| `routine.schema.json` | 时刻表：日期类型、提醒窗口、例行播报时点、相位 |
| `alarm-state.schema.json` | 运行状态账本：窗口状态机、ack、missed、跨日归档 |
| `task.schema.json` | 任务：ID、状态、截止日、分区 |
| `examples/*.json` | 可直接校验的示例实例 |

## 怎么用

`examples/` 里的实例含 `{{占位符}}`，**可以直接解析为 JSON，但填好占位符之前不会通过 Schema 校验**（`format: date` / `pattern` 等约束会失败）。这是刻意的：模板不该伪装成有效配置。

```bash
# 1. 复制并填好占位符
cp contract/examples/routines.example.json /tmp/routines.json
# 2. 用任意 JSON Schema 校验器验证
npx ajv-cli validate -s contract/routine.schema.json -d /tmp/routines.json --spec=draft2020
```

## 双表示的同步规则

改 `rules/` 里的字段名 → 必须同步改这里的 schema。
改 schema → 必须同步改 `rules/` 的渲染与 `examples/` 的实例。

**不要把业务逻辑写进 schema**：schema 只描述形状与约束，判断逻辑属于 `code/` 或技能。

## 字段命名约定

- 时间：`YYYY-MM-DDTHH:MM:SS{{TZ_OFFSET}}`（带偏移的 ISO 8601）
- 日期：`YYYY-MM-DD`
- 全小写 snake_case；状态用枚举字符串，不用布尔
- 所有可选字段显式标 `nullable` 或放进 `required` 之外

## 契约稳定性

`version` 字段用于演进。加字段视为向后兼容；改字段含义或删字段必须升 `version` 并在 `CONTRIBUTING.md` 里说明迁移方式。
