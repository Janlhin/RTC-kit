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

## 改表前先预演（simulate）

改 `rules/routines.json` 之后，别等明天被吵到或漏掉才发现问题。预演一条命令就能回答「那天会响几次、几点」：

```bash
python code/rtc.py --workspace {{WORKSPACE_DIR}} simulate
python code/rtc.py --workspace {{WORKSPACE_DIR}} simulate --date 2026-11-17
python code/rtc.py --workspace {{WORKSPACE_DIR}} simulate --date 2026-11-17 --json
```

```
2026-11-17（周二）预计响 7 次
  07:00–07:25  起床  6 次  每天
  15:25–15:29  出门  1 次  一次性
最早 07:00   最晚 15:25   同一时刻重叠：无
跳过：meds-pm（已停用）
```

**只读**：不写 `state/alarms.json`、不写 `routines.json`、不碰印章文件。
判定用的是 `plan` 那一套（有效期 / `skipDates` / `disabled` / 日期类型 / `expectFires` 算术校验），
所以预演结果和真跑逐条一致——写两份逻辑迟早会各说一套。


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

## 技术栈与版本

| 项 | 值 | 登记位置 |
|---|---|---|
| 语言 | Python 3.11 / 3.13（CI 双版本） | `.python-version`、`.github/workflows/tests.yml` |
| 包管理 | pip + `pyproject.toml`（无 lockfile，CI 固定版本区间） | `pyproject.toml` |
| 测试框架 | `unittest`（标准库） | `code/tests/` |
| 覆盖率 | `coverage.py`：语句阈值在 `pyproject.toml`，分支阈值在 `scripts/check-coverage.ps1` | 两处各一条 |
| Lint / 格式 | `ruff` | `pyproject.toml` |
| 类型检查 | `mypy` | `pyproject.toml` |
| 死代码 | `vulture`（置信度 ≥ 80 才报） | `pyproject.toml` |
| 脚本入口 | PowerShell 7.4+（`pwsh`），禁止 `.cmd` / `.bat` | `scripts/*.ps1` |

## 目录结构

| 路径 | 职责 |
|---|---|
| `code/` | 确定性脚本（`rtc.py` 主链路、`rtc_plan.py` / `rtc_due.py` / `rtc_once.py` 宿主入口）与回归测试 |
| `contract/` | 机器可读契约（JSON Schema）与可校验示例 |
| `rules/` | 声明式规则：`routines` / `priority-map` / `auto-resolver` / `formats` |
| `skills/` | 5 份技能说明，由运行时加载 |
| `templates/` | 工作区模板，全部为 `{{占位符}}` |
| `platform/` | 运行时适配示例（`hermes` / `openclaw`） |
| `docs/` | 架构、协作、故障处置、审计记录 |
| `scripts/` | 门禁脚本（`verify.ps1` 为唯一验收入口） |
| `tools/` | 实验与一次性工具，不属于主链路 |
| `.github/` | CI 工作流 |

## 验收标准

以下命令全部退出码 0，才算「改对了」：

```powershell
pwsh -NoLogo -NoProfile -File scripts/verify.ps1
python -m unittest discover -s code/tests -p "test_*.py"
python install.py --workspace ./_smoke-ws --dry-run
```

`verify.ps1` 内部按「快 → 慢」执行：脚本基线 → 根目录白名单 → 冲突标记 → 文档链接 → SemVer → 文档命令 → 格式 → Lint → 类型 → 死代码 → 测试与覆盖率（含阈值双判定） → 安装器冒烟。`-Fast` 只跑静态部分（pre-commit 用），`-SkipStatic` 只跑测试、覆盖率与冒烟（CI 的 test 层用）。

### 校验要用哪个 python

`ruff`、`mypy`、`coverage` 不在标准库里，`verify.ps1` 取解释器的顺序是：

1. `$env:PYTHON`（显式指定，优先级最高）
2. 仓库内 `.venv`（Windows 找 `.venv\Scripts\python.exe`，其他平台找 `.venv/bin/python`）
3. PATH 上的 `python`

前两条都不满足时，如果 PATH 上的 python 没装这些包，会看到 `No module named ruff` —— 那不是门禁坏了，是解释器选错了。本地这样准备一次即可：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ruff mypy coverage jsonschema
# 或者用已有的虚拟环境：
$env:PYTHON = "C:\path\to\venv\Scripts\python.exe"
```

`git commit` 时 pre-commit 钩子跑的是同一个脚本，所以钩子拦下「No module named ruff」也是这个原因，不是钩子装错了。

## 目录一览

| 路径 | 内容 |
|---|---|
| `README.md` | 本文件：定位、技术栈、目录、验收、已知限制 |
| `AGENTS.md` | AI 协作约束（只放入口、禁止清单与链接） |
| `DECISIONS.md` | 决策日志（含被否决方案与理由） |
| `TASKS.md` | 任务队列与待裁决台账 |
| `MANIFEST.md` | 第三方来源登记（URL / 版本 / 许可结论） |
| `LICENSE` | AGPL-3.0（仅适用于本仓库原创内容） |
| `docs/architecture.md` | 分层架构、职责边界、为什么这样切、演进路径 |
| `docs/behavior-contract.md` | 业务契约：plan → due → ack → rollover 的状态转换、输出/退出码、冲突与空日 |
| `docs/contributing.md` | 怎么改、怎么填占位符、什么不能提交 |
| `docs/troubleshooting.md` | 故障处置、备份与恢复 |
| `docs/attribution.md` | 与上游项目的关系、许可边界 |
| `docs/audit/2026-11-09-response.md` | 外部审计的逐条处置记录 |

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
| `code/rtc.py` | `{{CODE_DIR}}/rtc.py` | 提醒主链路：plan / due / ack / status / metrics / backup |
| `code/metrics.mjs` | `{{CODE_DIR}}/metrics.mjs` | 统计旁路，可选 |
| `skills/*/SKILL.md` | `skills/<name>/SKILL.md` | 由运行时加载 |
| `platform/jobs.example.json` | 不部署 | 只作参数参考，按平台文档建任务 |

---

## 已知限制

- 这是一套**规格与模板**，不是开箱即用的产品：需要填配置、选运行时、建定时任务。
- 提醒主链路的调度由 `code/rtc.py` 承担，但**投递**取决于宿主；不同宿主的 payload 与失败语义不同，本仓库只给示例。
- `code/metrics.mjs` 是可选旁路统计，**不随主链路维护**；主链路统计走 `rtc.py metrics`。
- 覆盖率阈值已按实测基线收紧到语句 80% / 分支 70%（2026-11-20 实测 82% / 76%），但局部仍有空洞（`rtc_plan.py` 0%、`rtc_once.py` 62%），见 `TASKS.md` T-003。
- `mypy` 已全量收紧到 `--strict`（`[tool.mypy]` 顶层开关，2026-11-21），`code/` 与 `install.py` 零错误。
- 执行历史在 `state/history.db`（`windows` / `daily` / `checklist` / `events` 四表，唯一真相源）；`state/schedule/*.json` 是它的人读副本，删掉整个目录历史照样查得到。查询用 `rtc.py history` / `history --range` / `events` / `stats`。
- 每日档案 `state/schedule/*.json` 永久保留；`rtc-plan` 每天压一份 zip 到**其他本地盘**的 `RTC-Backup`（目标盘由 `rules/backup.json` 指定），也可按天提交进私有 git 仓库。恢复办法见 `docs/troubleshooting.md` §7 / §8。
- 上游 `snarktank/clawchief` 没有附带许可证；本仓库不分发其任何原文，详见 `docs/attribution.md`。

## 许可

本仓库原创内容采用 **AGPL-3.0**，见 `LICENSE`。若你修改后以网络服务形式提供，AGPL 第 13 条要求向使用者提供对应源码。

注意：AGPL 只覆盖本仓库原创部分，不覆盖任何第三方材料。
