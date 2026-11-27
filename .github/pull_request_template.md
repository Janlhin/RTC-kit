## 变更摘要

<!-- 本次改了什么：规则 / 脚本 / 文档，属于哪一层（contract / rules / skills / code / platform）。 -->

## 验证命令与证据

<!-- 本地跑过的检查命令与结果，例如：
pwsh -NoLogo -NoProfile -File scripts/verify.ps1
python -m unittest discover -s code/tests -p "test_*.py"
python install.py --workspace ./_smoke-ws --dry-run
未安装依赖导致的跳过请如实标注。 -->

## CI 结果

<!-- 填写 CI 工作流名称与结论（通过 / 未触发 / 无 runner）；未跑的检查不要写“通过”。 -->

## 风险与回滚

- 风险：<!-- 可能影响的范围（规则语义 / 状态读写 / 安装器 / 平台适配） -->
- 回滚：未合并时关闭本 PR 即可，影响面为 0；合并后用 `git revert <commit>` 撤销。
