# DECISIONS

追加式决策日志。**只增不改**：某条被推翻时，新增一条说明「推翻哪条、为什么」，旧条保留。

字段：编号 · 日期 · 决策 · 背景 · 被采纳方案 · 被否决的替代方案与理由 · 影响范围 · 复审条件。

---

## D-001 根目录实行白名单

- **日期**：2026-11-16
- **决策**：根目录只允许五份文档、包管理与锁文件、构建与静态检查配置、版本与忽略规则、协作平台目录、许可证与变更记录。其余文件一律移入子目录。
- **背景**：根目录原有 `ARCHITECTURE.md`、`ATTRIBUTION.md`、`AUDIT-2026-11-09-RESPONSE.md`、`CONTRIBUTING.md` 与一次性补丁脚本，任何工具默认查找的路径上堆着五份「不知道还准不准」的文件。
- **被采纳**：移入 `docs/`（审计记录进 `docs/audit/`），一次性脚本进 `tools/one-off/`，全仓引用改为相对路径。
- **被否决**：① 保留在根目录、只在 README 里标注「已过期」——读者仍会先看到它；② 直接删除旧审计记录——历史应由 Git 与 `docs/audit/` 承担，删除会让处置结论无从追溯。
- **影响**：所有文档内引用路径；`scripts/check-root-whitelist.ps1` 从此有明确比对基准。
- **复审条件**：新增根目录文件时必须重跑白名单检查；若某类配置工具只认根目录路径，需先提案再放行。

## D-002 静态检查与类型检查选 ruff + mypy

- **日期**：2026-11-16
- **决策**：Python 侧统一用 `ruff`（Lint + 格式）与 `mypy`（类型），测试沿用 `unittest` + `coverage.py`。
- **背景**：仓库此前没有任何静态检查，缺陷只能靠 149 条行为测试与人工 review；`code/rtc.py` 已 2721 行，类型漂移无从拦截。
- **被采纳**：`ruff format` + `ruff check`（规则集 `E,F,W,I,B,UP,SIM`）+ `mypy`（在 `pyproject.toml` 配置）。两者都在 `scripts/verify.ps1` 与 CI 中执行。
- **被否决**：① 引入 `black` + `isort` + `flake8` 三件套——三套配置、三套规则，冲突时会被互相豁免；② 迁移到 `pytest`——149 条用例全为 `unittest` 风格，迁移属机械性大改，与本轮「先建门禁」目标无关，单独提案再做。
- **影响**：`pyproject.toml`、CI、`scripts/verify.ps1`；代码需一次性格式化（独立提交）。
- **复审条件**：ruff 规则集若与既有约定冲突，新增豁免必须写明理由与到期时间。

## D-003 类型检查先覆盖「可判定」范围，逐步收紧

- **日期**：2026-11-16
- **决策**：`mypy` 先以 `check_untyped_defs`、`ignore_missing_imports` 的基线跑通零错误，再按 `TASKS.md` 的条目逐步收紧到 `--strict`。
- **背景**：首轮实测 35 个类型错误集中在「`load_state()` 可能返回 `None`」这类真实缺陷上；一次性全量 strict 会把行为改动与类型改动混在一个提交里。
- **被采纳**：先修真实缺陷（`None` 收窄、缺注解、`except` 变量逃逸），配置随代码一起收紧；`TASKS.md` 登记剩余收紧项与机器判据。
- **被否决**：① 直接 `# type: ignore` 全量压平——等于关掉通道；② 只对新增代码做检查、存量不查——存量正是缺陷密度最高的地方。
- **影响**：`pyproject.toml` 的 `[tool.mypy]`；`code/*.py` 的注解与收窄。
- **复审条件**：每完成一批注解，阈值上调一次；上调必须在 `DECISIONS.md` 留痕。

## D-004 自动化入口统一 pwsh，禁止 cmd / bat

- **日期**：2026-11-16
- **决策**：所有脚本入口、Git hooks、CI 步骤、文档命令片段使用 PowerShell 7.4+（`pwsh`）；仓库内不出现 `.cmd` / `.bat`，不出现 `cmd /c`、`powershell.exe`。
- **背景**：宿主是 Windows，项目历史上出现过手工 `.bat` 包装；两套解析器会让同一命令在本地、hooks、CI 里语义不同（转义、退出码、变量展开）。
- **被采纳**：`scripts/*.ps1` 为业务逻辑唯一载体；`.husky` 钩子只做 `exec pwsh -NoLogo -NoProfile -File ...` 转发，行尾 LF。
- **被否决**：① 保留 `.bat` 包装层「兼容旧环境」——包装器不是兼容层，是绕过校验的通道；② CI 用 `shell: powershell`（5.1）——与本地 7.4 语义不同，等于双基线。
- **影响**：`scripts/`、`.github/workflows/`、文档命令块。
- **复审条件**：若某工具只发 `.bat` 启动器，登记为已知豁免并写明影响范围。

## D-005 覆盖率阈值取实测基线，且必须逐季上调

- **日期**：2026-11-16
- **决策**：`coverage.py` 阈值从当前实测值起步（见 `TASKS.md` T-004），每季度上调一次直到语句 80% / 分支 70%。
- **背景**：一步到位设 80% 会让 `verify` 长期为红，进而诱导「跳过测试」；而完全不设阈值，覆盖率只降不升。
- **被采纳**：阈值写进 `pyproject.toml`，由 `verify.ps1` 与 CI 共同执行；上调动作单独 `chore(test):` 提交。
- **被否决**：① 只报告不设阈值——报告没人读；② 先设 80% 再补测试——会把「补测试」与「修功能」混在一批提交里。
- **影响**：`pyproject.toml`、CI。
- **复审条件**：季度清理节点复核，未上调即视为流程违规。

## D-006 `install.py` 留在根目录（白名单唯一技术性豁免）

- **日期**：2026-11-16
- **决策**：安装器 `install.py` 保持在仓库根目录，并列入 `scripts/check-root-whitelist.ps1` 的白名单。
- **背景**：安装器是外部用户对本产品唯一的入口动作（`python install.py --workspace ...`），文档、`README.md` 首屏、宿主部署说明都按这个相对路径书写；移入 `tools/` 会让所有既有指引失效，且用户照抄旧命令会得到 `No such file`。
- **被采纳**：根目录保留，白名单显式登记，并在 `AGENTS.md` 的入口表里写明它是安装入口而非普通脚本。
- **被否决**：① 移入 `tools/install.py` 并改全部文档——对外部用户是破坏性变更，与本轮「只做工程门禁」的范围不符；② 不登记、靠人工记住——白名单一旦靠记忆就等于没有白名单。
- **影响**：`scripts/check-root-whitelist.ps1` 的 `allowedFiles`；`README.md`、`platform/hermes.md` 的安装命令保持原样。
- **复审条件**：若将来提供打包发行（`pipx` / 单文件二进制），此豁免随之撤销。

## D-007 Git 钩子只做转发，规则唯一载体是 `verify.ps1`

- **日期**：2026-11-16
- **决策**：`.husky/pre-commit`、`.husky/commit-msg` 是 LF 行尾的 `sh` 薄壳，只 `exec pwsh -NoProfile -File scripts/hooks/*.ps1`；`pre-commit.ps1` 只调 `scripts/verify.ps1 -Fast`，`commit-msg.ps1` 只校验标题格式。规则本体不复制进钩子。
- **背景**：钩子里写判断逻辑，会与 CI、`verify.ps1` 形成多个规则载体；三处一旦不一致，以哪一处为准就没人说得清，且本地能过、CI 照红。
- **被采纳**：钩子薄壳 + `scripts/install-hooks.ps1` 设置 `core.hooksPath`，并检查钩子文件不含 CR。
- **被否决**：① 用 `pre-commit` 框架管理——为两个钩子引入一个 Python 依赖与额外配置层，收益不抵成本；② 不做钩子，只靠 CI——违规要等到推送后才发现，反馈太晚。
- **影响**：`.husky/`、`scripts/hooks/`、`scripts/install-hooks.ps1`。
- **复审条件**：钩子若需新增检查，只允许改 `verify.ps1` 或新增 `scripts/check-*.ps1`，钩子本身保持薄壳。

## D-008 核心按职责拆分，`rtc.py` 保留为单命名空间薄壳

- **日期**：2026-11-16
- **决策**：`code/rtc.py` 拆成 `rtc_core` / `rtc_archive` / `rtc_metrics` / `rtc_brief` 四个平铺模块（不建包目录），`rtc.py` 只剩入口薄壳：被 import 时用 `sys.modules` 别名指向 `rtc_core`，直接执行时调 `rtc_core.main()`。
- **背景**：拆分前 2788 行、8 个互不相干的领域挤在一个文件，`rtc.py` 已是多次 PR 的唯一冲突热点。但测试与调用方依赖「`import rtc` 是唯一命名空间」：`rtc_due.py` 调 `rtc.main()`，测试替换 `rtc.atomic_write` / `rtc.file_digest` 并直接读写 `rtc.STATE` / `rtc.ROUTINE`。若普通 `from rtc_core import *`，替换落在另一个模块对象上，`atomic_write` 内部的 `file_digest` 仍是原函数，回滚竞态等 4 条用例会假绿。
- **被采纳**：模块间用 `import rtc_archive as archive` + `archive.xxx()` 的属性查找（调用时解析，替换立即生效）；`rtc.py` 别名回 `rtc_core` 保住单一命名空间。
- **被否决**：① `code/rtc/` 包结构（`__init__.py`）——脚本被拷到宿主目录平铺执行，加包会让 `python rtc.py` 失效；② 改测试或改调用方去适配新命名空间——本轮要求「149 条测试一条不改」。
- **影响**：`code/` 五个文件、`install.py` 的拷贝清单（少拷一个模块，宿主的 `import rtc_core` 直接崩）、`code/README.md` 的文件表。
- **复审条件**：`rtc_core.py` 仍是最大文件（约 1800 行）；若继续增长，按命令组再拆，但必须保持「一次一个 commit、拆前拆后输出逐字节一致」。
