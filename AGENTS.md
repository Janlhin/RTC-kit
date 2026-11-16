# AGENTS.md

本文件是**目录**，不是手册：只放强制入口、禁止清单与链接。深层内容在 `docs/` 里，按需读。

## 强制入口

| 用途 | 命令 | 期望 |
|---|---|---|
| 全量验收（唯一入口） | `pwsh -NoLogo -NoProfile -File scripts/verify.ps1` | 退出码 0 |
| 回归测试 | `python -m unittest discover -s code/tests -p "test_*.py"` | 退出码 0 |
| 静态检查 | `python -m ruff check code install.py` | 退出码 0 |
| 格式检查 | `python -m ruff format --check code install.py` | 退出码 0 |
| 类型检查 | `python -m mypy code install.py` | 退出码 0 |
| 安装器冒烟 | `python install.py --workspace ./_smoke-ws --dry-run` | 退出码 0 |
| 启用 Git 钩子（每台机器一次） | `pwsh -NoLogo -NoProfile -File scripts/install-hooks.ps1` | `core.hooksPath = .husky` |

`verify.ps1` 是唯一验收入口；它内部按「快 → 慢」排：脚本基线 → 根白名单 → 文档链接 → SemVer → 格式 → Lint → 类型 → 测试 + 覆盖率 → 安装器冒烟。`-Fast` 只跑前七项（钩子用），`-SkipStatic` 只跑后两项（CI 的 test 层用）。CI 分两层：`static` 跑 `-Fast`，`test` 矩阵跑 `-SkipStatic`。

## 项目约定

- **唯一真相源**：`rules/routines.json` 决定「何时响」；`state/alarms.json` 是运行账本。两者只由 `code/rtc.py` 的子命令写入。
- **代码与数据分离**：`code/` 只放确定性脚本；`contract/` 是机器可读契约；`rules/`、`skills/`、`templates/` 是给人/给模型读的。
- **占位符**：仓库内一律 `{{WORKSPACE_DIR}}` 这类占位符，禁止写入本机路径、账号、密钥、真实作息。
- **命令面稳定**：`rtc.py` 的子命令名、参数、退出码属于对外契约，改名等于破坏兼容。
- **单一入口**：新增能力先加到 `rtc.py` 已有子命令上；确需新命令要有独立提案。
- **一次性脚本**：放 `tools/one-off/`，提交信息标注 `chore(script): one-off`，不得进主链路。

## 禁止清单

- ❌ `git commit --no-verify`、注释掉测试、放宽 Lint 规则来「通过」校验。
- ❌ 在功能提交里夹带依赖主版本升级；升级必须是独立 `chore(deps):` 提交。
- ❌ 一个提交做多件事；重构与行为变更必须拆开。单提交改动 > 400 行须拆分（机械性变更除外，且仍要单独提交）。
- ❌ 手改 `rules/routines.json` 或 `state/alarms.json`（走 `set-time` / `postpone` / `disable` / `ack`）。
- ❌ 在公开仓库里出现个人路径、机器人账号、真实姓名、真实作息。
- ❌ 新增 `.cmd` / `.bat`；自动化入口一律 `pwsh -NoLogo -NoProfile -File <script>.ps1`。
- ❌ 把规则抄第二份到 `CONTRIBUTING.md` 之类的文件里——规则只有本文件一个载体。
- ❌ 拿「先合了再补」当理由关掉任何校验通道。

## 歧义处理

命中以下情形**先问再动**，不猜：

1. 需求边界不清（同一个词两种实现）。
2. 多个合理方案且取舍不同（存储、同步、协议）。
3. 不可逆操作（删文件、改 Git 历史、覆盖生产数据、对外发消息）。
4. 涉及外部资源（凭据、付费、额度）。
5. 与仓库既有约定冲突。
6. 根目录新增文件或目录。

提问格式：一次 ≤4 个问题，每个给 2–4 个具体选项（做什么 + 代价），推荐项排第一，等答复。

## 先搜寻，后自研

顺序不得跳级：标准库 → 已有依赖 → 包注册表 → 代码托管平台 → 现成工具 → 借鉴后重写 → 完全自研。走到最后一步要给出「检索过什么、为什么不用」的记录，写在 `docs/decisions.md`。

## 交付

每次交付说明必须含四项：**改了什么**（文件 + 意图，对应到提交）、**验证证据**（命令原文 + 退出码）、**未验证项**（环境/权限/网络限制，不得隐去）、**遗留项**。末尾附一行当前待裁决项；没有就写「当前无待裁决项」。

## 文档地图

- `README.md` —— 这是什么、怎么跑、验收标准、已知限制
- `DECISIONS.md` —— 追加式决策日志（含被否决方案与理由）
- `TASKS.md` —— 任务队列与待裁决台账（完成判据必须可机器判定）
- `MANIFEST.md` —— 第三方来源登记（URL / SHA256 / 用途 / 许可结论）
- `docs/architecture.md` —— 分层与职责边界
- `docs/contributing.md` —— 怎么改、占位符怎么填
- `docs/troubleshooting.md` —— 故障处置与备份恢复
- `docs/audit/2026-11-09-response.md` —— 外部审计逐条处置

## 校验矩阵（本项目）

| 缺陷类型 | L1 静态 | L2 门禁 | L3 CI | L4 运行期 |
|---|---|---|---|---|
| 类型与实现不一致 | ruff / mypy | 同左 | 同左 | — |
| 未处理的外部输入 | argparse + JSON Schema | 同左 | 同左 | plan/due 运行时校验 |
| 逻辑行为错误 | — | 单测 | 单测 + 覆盖率 | 实机 `rtc.py status` |
| 状态文件并发写坏 | — | 单测 | 同左 | `rev` 冲突检测 |
| 个人路径泄露 | — | 启动扫描 | 同左 | — |
