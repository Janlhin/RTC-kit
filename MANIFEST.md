# MANIFEST

第三方二进制、源码与参考对象的来源登记。**本仓库不分发任何第三方代码或二进制**，下表用于回答「这个东西哪来的、能不能用」。

字段：名称 · 来源 URL · 版本 · SHA256 · 用途 · 许可结论。未入库的条目标注「未入库」，SHA256 写「不适用」——不填假哈希。

| 名称 | 来源 | 版本 | SHA256 | 用途 | 许可结论 |
|---|---|---|---|---|---|
| snarktank/clawchief | https://github.com/snarktank/clawchief | commit `1fcd1b6`（2026-04-09） | 不适用（未入库） | 架构思想参考：优先级与自动决策分文件、单一真相源、单写者 | 仓库无 `LICENSE`，默认保留所有权利。**结论：不引入任何原文，只取不受著作权约束的抽象方法**，详见 `docs/attribution.md` |
| Python | https://www.python.org/ | 3.11 / 3.13（CI 双版本） | 不适用（运行时） | `code/*.py` 与 `install.py` 的运行环境 | PSF License，允许商用与再分发 |
| jsonschema | https://pypi.org/project/jsonschema/ | 4.26.0（测试依赖） | 由 pip 校验（`pip hash`） | 用 `contract/*.schema.json` 校验契约与实例 | MIT |
| ruff | https://pypi.org/project/ruff/ | 0.17.5（开发依赖） | 由 pip 校验（`pip hash`） | Lint 与格式化门禁 | MIT |
| mypy | https://pypi.org/project/mypy/ | 2.4.1（开发依赖） | 由 pip 校验（`pip hash`） | 类型检查门禁 | MIT |
| coverage | https://pypi.org/project/coverage/ | 7.17.3（开发依赖） | 由 pip 校验（`pip hash`） | 覆盖率度量与阈值 | Apache-2.0 |
| vulture | https://pypi.org/project/vulture/ | 2.16（开发依赖） | 由 pip 校验（`pip hash`） | 死代码门禁：未被引用的函数、变量、属性 | MIT |
| Node.js | https://nodejs.org/ | 未固定（可选） | 不适用（运行时） | 仅 `code/metrics.mjs` 旁路统计使用；主链路不依赖 | MIT |

## 说明

- 依赖版本的单一来源是 `pyproject.toml`；本表只登记「来源、用途、许可」三项判断，不重复版本。
- 未进入主链路的东西（如 `code/metrics.mjs`）标注为「可选」，避免被当成必需组件。
- 新增第三方二进制或源码前，必须先在本表登记并写明许可结论，否则视为未完成引入。
