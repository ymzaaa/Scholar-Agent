# 自动测试目录

本目录是公开项目的 pytest 测试代码入口，只使用合成输入和离线假客户端。真实论文、真实节选、真实模型验收及其结构化基准在私有测试环境中维护，不随仓库分发。

## 分层职责

- `unit/`：单个模块、规则、数据模型和故障边界；不运行完整流水线。
- `integration/`：多个正式模块之间的契约，包括虚拟 Word、CLI 失败传播和后端工作区。
- `acceptance/`：人工触发的 W01/W03/W05/W07/W09 双模板产物生成命令；固定使用离线假客户端，不属于 pytest 自动收集范围。
- `support/`：测试专用路径、构造器与假客户端，不包含业务实现。
- `fixtures/`：可公开的合成 Word 和反馈输入。
- `baselines/`：经人工审核的合成结构与反馈预期。

`fixture` 是复用输入，不是测试层级。虚拟 Word 应在临时目录动态创建，不能作为新的仓库事实源。

## 常用命令

~~~powershell
# 快速单元测试
python -m pytest tests/unit -q

# 虚拟与后端集成测试
python -m pytest tests/integration -q -m "not real_llm and not real_data and not latex and not slow"

# 完整公开回归
python -X utf8 -m pytest -q

~~~

## G7 测试位置

首次生成修复按相同边界验证：

- `unit/agents/quality_repair/` 与 `unit/pipeline/test_initial_repair_safety.py`：统一状态、补丁保护、动作审计和首次修复安全；
- `integration/agents/quality_repair/test_initial_repair_cli.py`：已定位来源的确定性修复、CLI 与完整门禁重跑；

后续 Agent 测试仍必须与实现同步提交，不允许提前留下必然失败的占位测试。

## 私有验收边界

真实论文与真实模型调用只能在维护者的私有测试目录中运行。公开测试不得读取 `tests/private-data`、提交 `tests/artifacts`，也不得仅因环境中存在 API Key 就自动调用外部模型。
