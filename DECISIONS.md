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
- **后续（2026-11-21）**：8 批全部收完，`strict = true` 已提到 `[tool.mypy]` 顶层，不再有 per-module overrides；新增模块自动落在 strict 之下。
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

## D-009 覆盖率阈值由「逐季上调」改为「直接落在目标值」

- **日期**：2026-11-20
- **决策**：语句阈值从 77% 提到 80%，并新增分支 70% 的独立判定（`scripts/check-coverage.ps1`），两者都由 `verify.ps1` 执行。D-005 的「逐季上调」提前结束。
- **背景**：2026-11-20 实测语句 82%、分支 76%，已越过 80/70。继续停在 77% 等于给回归留了 5 个百分点（约 170 行）的余量——阈值低于实测值越多，越拦不住「慢慢变松」。
- **被采纳**：语句阈值仍只写在 `pyproject.toml` 的 `fail_under`；分支阈值 coverage.py 没有配置项，写进 `scripts/check-coverage.ps1` 并读同一份实测数据判定。两条路径都拦：`coverage report` 与阈值脚本各判一次。
- **被否决**：① 一次跳到 90%——当前局部空洞（`rtc_plan.py` 0%、`rtc_once.py` 62%）会立刻变红，只能靠堆无断言用例刷分，正是「自证式测试」；② 只保留总阈值——总数会把局部空洞盖住，所以另开 T-003 按模块追；③ 装 `pytest-cov` 统一管阈值——为一条配置引入第二套测试入口，与「入口唯一」冲突。
- **影响**：`pyproject.toml`、`scripts/check-coverage.ps1`、`scripts/verify.ps1`、`README.md` 已知限制、`TASKS.md` T-002 / T-003。
- **复审条件**：局部空洞补齐后，按季度再上调（语句 85%、分支 80%）。

## D-010 死代码门禁用 vulture，置信度取 60，测试目录不进扫描

- **日期**：2026-11-20
- **决策**：`vulture`（PyPI，MIT）作为死代码通道，命令固定为 `--min-confidence 60 --ignore-names row_factory --exclude code/tests`，接入 `verify` 静态段与 CI。
- **背景**：仓库里曾有三个「看起来像配置、实际没人引用」的符号（`rrule_from_window`、`SCHEMA_VERSION`、`WINDOW_COLUMNS`），它们不会被 ruff 或 mypy 拦下——未被引用不等于写错。先按 `--min-confidence 80` 试跑，结果对「从未被调用的函数」根本不报（函数默认 60%），门禁形同虚设；降到 60 才抓到真货，代价是三类误报。
- **被采纳**：60% + 两类豁免——`row_factory`（SQLite 按属性名动态取用，静态工具看不见）与 `code/tests`（测试基类靠继承使用，且测试本身就是入口，扫它只会制造噪音）。豁免写在命令行里、跟着 `verify` 走，不另建白名单文件。
- **被否决**：① `--min-confidence 80`——首轮实测对真死函数零命中，等于没装；② 建 `vulture_whitelist.py`——多一个要维护的文件，且豁免项只有两条，命令行够用；③ 用 `pylint` 的未使用检查替代——为一条通道引入整套 pylint 与其配置，与「入口唯一」冲突。
- **影响**：`pyproject.toml`（开发依赖）、`MANIFEST.md`（来源与许可）、`scripts/verify.ps1`、`.github/workflows/tests.yml`、`AGENTS.md`、`TASKS.md` T-005。
- **复审条件**：豁免名单超过 3 条时，改为独立白名单文件；`vulture` 若出现回归版本，按依赖升级流程处理。

## D-011 根目录登记 CHANGELOG.md 与 SECURITY.md

- **日期**：2026-11-26
- **决策**：根目录白名单新增 `CHANGELOG.md`（变更记录）与 `SECURITY.md`（安全政策）；贡献指南仍留在 `docs/contributing.md`，不回根目录（沿用 D-001）。
- **背景**：治理骨架需要变更记录与私密报告入口；GitHub 对根级 `SECURITY.md` 有标准发现路径。两者都属根目录常设文档，不是临时补丁。
- **被采纳**：两个文件进根目录，并同步 `scripts/check-root-whitelist.ps1` 的 `allowedFiles`。
- **被否决**：① 把安全政策也挪进 `docs/`——`docs/incident-reporting.md` 只覆盖故障上报流程，不等于漏洞私密报告政策，且根级发现路径缺失；② 为两份文档新建子目录——单文件文档进子目录增加跳转成本。
- **影响**：`CHANGELOG.md`、`SECURITY.md`、`scripts/check-root-whitelist.ps1`、`.github/dependabot.yml`、`.github/pull_request_template.md`、`docs/README.md`。
- **复审条件**：无；随下次根目录布局评审一并复核。

## D-012 时间模型改存 IANA 时区名，旧固定偏移保留兼容回退（#91）

- **日期**：2026-11-27
- **决策**：时刻表时区以 `timezone`（IANA 名，标准库 `zoneinfo`）为准；`tzOffset`（`+HH:MM` 固定偏移）降级为缺 `timezone` 时的兼容回退。`.ics` 事件必须带时区（UTC 用 `Z`，本地用 `TZID` + `VTIMEZONE`），禁止浮动时间。
- **背景**：旧实现全程按 `tzOffset` 固定偏移解析，不跟随夏令时；跨时区行程无法按目的地时区排。issue #91 要求时间模型存 IANA 名，同时存量固定偏移配置不能报错。
- **被采纳**：① `resolve_tz` 回退链 = IANA 名 → `tzOffset` 固定偏移 → 默认 `+08:00`；② 缺 tzdata / 时区名非法时回退固定偏移，并往 `NOTICES` 留一条提示，不静默吞掉；③ `tzdata` 只进 dev/test 依赖与 CI 测试层，运行时零第三方依赖；④ `.ics` 无可用 IANA 时区时换算成 UTC 瞬时用 `Z` 输出，保证非浮动。
- **被否决**：① 运行时引入第三方时区库（如 `backports.zoneinfo` 或 `pendulum`）——违反「tick 路径零第三方依赖」；② 直接删掉 `tzOffset`——存量配置会集体报错，违背兼容要求；③ 把所有内部瞬时都锁成 UTC 存储——周期窗口要保留本地墙钟，锁瞬时会让夏令时下的本地时刻漂移。
- **影响**：`code/rtc_core.py`、`code/rtc_brief.py`、`code/rtc_ics.py`、`contract/routine.schema.json`、`contract/README.md`、`rules/routines.example.md`、`docs/timezone.md`、`docs/behavior-contract.md`、`pyproject.toml`、`.github/workflows/tests.yml`、测试 `code/tests/test_timezone.py` 与 `test_ics.py`。
- **复审条件**：若后续要做国家→时区映射、歧义/不存在本地时间消歧、单事件多时区，单独立项；本轮边界见 `docs/timezone.md` §6。

## D-014 L2 P1 事件模型先落派生影子账本，旧 JSON 真相源不动

- **日期**：2026-11-28
- **决策**：L2 行程本第一段（P1）先建 Event 一等公民契约与离线投影，影子账本 `state/events.json` 由旧 `routines.json` / `alarms.json` 投影生成；旧 schema 与旧命令面原样保留，P2 起 `.ics` 才成为源真相，本 JSON 转只读缓存。
- **背景**：L2 要把一次性 / 周期 / 倒计时 / DDL 统一成带稳定 `uid`、`kind`、RRULE、`owner`、`deadline`、`status` 的事件一等公民。但旧 `routine` / `alarm-state` schema 被 373 条回归与旧命令逐字节口径钉死，直接改旧 schema 会把行为改动与模型迁移混在一起。
- **被采纳**：① 新 `contract/event.schema.json` 与旧两份 schema 并存，旧 schema 一字不动；② `rtc_events.py` 纯标准库做影子账本读写、旧窗口 → Event 投影、旧 id ↔ uid 双向别名；`rtc_migrate.py` 是离线迁移骨架，默认 dry-run、写前自动备份、幂等跳过、`--rollback` 可恢复；③ 默认口径：`owner` 默认 `self`、替别人记 `other:<name>`；`uid` = UUIDv4 + `@rtc-kit`，旧 id / ocid 进 `aliases`；`rrule` 直接存 RFC 5545 标准字符串，不造自研 DSL；④ 事件栈不进 `plan` / `due` / `ack` tick 热路径，静态断言测试守住。
- **被否决**：① 直接改旧 `alarm-state` / `routine` schema 承载事件字段——旧命令面与既有测试会被波及，违背「一条不改全绿」；② P1 就写 `.ics` 真相源——日历序列化是 P2 的事，提前做会把两套真相源缝在一起；③ 事件模块被 `due` / `plan` 直接 import——tick 路径变重，违反确定性与零第三方依赖边界。
- **影响**：`contract/event.schema.json`、`code/rtc_events.py`、`code/rtc_migrate.py`、`code/tests/test_events_p1.py`、`docs/architecture.md`、`docs/behavior-contract.md` §5c、`contract/README.md`、`code/README.md`、`TASKS.md` T-016。
- **复审条件**：P2 切 `.ics` 真相源时复核影子缓存的重建与删除路径；P5 接入提醒策略时复核 `reminderPolicy` 形状；`owner` 词汇表收紧或校验规则另提案；`holiday` 类型不进 RRULE，留外挂日期类型表。

## D-015 L2 事件台账以 `.ics` 为源真相，SQLite 仅作可重建只读索引（P2）

- **日期**：2026-11-28
- **决策**：L2 行程本的事件台账以 RFC 5545 `calendar/<YYYY>.ics` 为唯一真相；`state/calendar-index.db` 只是可删可重建的只读查询索引；旧 `routines.json` / `alarms.json` 迁移后留在原地作只读历史。
- **背景**：NFR-2.5 跨代可读（30–40 年）是决定性约束——30 年后软件可能全死，数据必须仍是开放标准纯文本。SQLite 与私有 JSON 库不可人类直读、会被 schema 演进绑架，不能作真相。P1 的 `state/events.json` 只是过渡期派生影子副本，P2 起 `.ics` 为准。
- **被采纳**：① 一年一个 `.ics`，按 `DTSTART` 年份归文件；跨年事件在相邻年份文件各留一段 `X-RTC-CROSS-YEAR` 交叉引用；② 索引 `rebuild` 从零扫描全部 `.ics` 灌入，查询结果与直扫 `.ics` 逐条对账；③ 冲突以 `.ics` 为准，索引陈旧只告警剔除、不反写真相；④ 迁移默认 dry-run，`--apply` 前自动备份（含 manifest 哈希），重跑幂等（别名命中保留原 UID），`--rollback` 按 manifest 恢复；⑤ RRULE 直接存 RFC 5545 标准字符串；owner / deadline / reminderPolicy / aliases / rev 等 RTC 专有字段走 `X-RTC-*` 扩展，未知属性原样保留带回。
- **被否决**：① SQLite 作事件真相——跨代不可读，NFR-2.5 直接否决；② 单一大 `.ics` 文件——多年后单文件过大、折行与合并冲突难处理，按年份拆分；③ 索引不一致时自动重建并覆盖——历史教训（三套修复器互相重建清空数据，见 `docs/architecture.md` §5 规则 3），读取路径只告警不修复。
- **影响**：`code/rtc_ics.py` 双向序列化/解析层、`code/rtc_calindex.py`、`code/rtc_migrate.py --to-ics`、`docs/architecture.md` §5、`docs/behavior-contract.md` §3.2、测试 `test_ics_p2.py` / `test_calindex.py` / `test_migrate_ics.py`。
- **复审条件**：P3 RRULE 展开、P5 事件写路径接入后，复核迁移期双写口径；若后续做系统日历同步（NFR-3.6），单向导出副本另立决策，不回流改写 `.ics`。

## D-019 L2 P6 查询：事件 + 旧窗口只读合并视图，周期事件先按有效期重叠归属

- **日期**：2026-11-28
- **决策**：P6 先交付只读查询面 `list` / `show`，事件（P1 影子账本；P2 起 `.ics` 为准）与旧窗口（`history.db`）两条来源并排呈现、用 `source` 列区分；周期事件本阶段按「规则有效期与查询区间重叠」做归属，不逐条展开 occurrence；`.ics` 导出复用 P2 的 `rtc_ics.py` 序列化层，不自写第二份。
- **背景**：L2 验收要求任意日期 / 年份 / 跨年可查、关键词与类型 / 归属筛选、`.ics` 导出。但 occurrence 展开引擎在 P3、`.ics` 序列化在 P2，P6 不能等它们拍板；旧命令输出又被既有回归钉死，查询层不能改写旧窗口读路径。
- **被采纳**：① 新区块独立模块 `rtc_query.py`，纯标准库、不被 tick 热路径 import；② 区间三选一（`--date` / `--year` / `--from`+`--to`），筛选 `--q`（大小写不敏感子串、AND）/ `--kind once|recurring` / `--owner self|other:<name>`，口径与 #112 对齐、待收敛；③ 跨年归属：once 按 start 日期落年；recurring 按有效期重叠（start ≤ 区间末、UNTIL ≥ 区间始），无 UNTIL 跨年两年都列出；④ 先按日期 / 年份过滤再聚合，单次返回上限 5000 条，超限退出 4 提示收窄范围；⑤ 空结果退出 0，`--json` 纯 JSON 走 stdout，诊断走 stderr；⑥ 旧窗口行 `type` 补进 history 查询 SELECT（展示面不变），映射 once/recurring。
- **被否决**：① P6 自己写一份 RRULE occurrence 展开——引擎归 P3，重复实现会在合流时打架；② P6 自写 `.ics` 序列化——复用 P2 模块，避免两套序列化口径；③ 合并两条来源时按别名去重——影子账本与执行历史是两个视角，并列展示更不容易让用户误以为旧窗口被事件取代；④ 把 `--q` 分词或接 LLM 兜底——L2 保持确定性，不支持的表达明确无命中。
- **影响**：`code/rtc_query.py`、`code/rtc_core.py`（新子命令接线）、`code/rtc_history.py`（SELECT 补 `type`）、`code/tests/test_query_p6.py`、`docs/behavior-contract.md` §5d、`code/README.md`、`TASKS.md` T-021。
- **复审条件**：P3 引擎合入后把 occurrence 枚举接到 `list` 实例级过滤与条数预算（替换定义级重叠匹配）；#112 收敛后合并事件侧与旧命令侧的 `--q` / `--kind` 匹配函数；事件 `title` / 标签字段由 P5 写入侧落地后扩 `--q` 检索池。
