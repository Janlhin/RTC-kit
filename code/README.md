# code — 确定性脚本

这一层只放**不依赖模型**的代码。目前只有一个文件，它是可选的旁路，不参与提醒主链路。

## metrics.mjs

执行情况的统计账本。用运行时的内置 SQLite 模块，**零外部依赖、零构建步骤**（JSDoc + `// @ts-check` 提供类型检查）。

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
