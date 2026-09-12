# 格式规则目录

本目录同时保存通用规则和模板画像，但二者不得混用。

- `common-format-rules.json`：所有中文学位论文转换任务都需要的确定性正确性规则；
- `templates/<template>-format-profile.json`：由对应模板适配器选择的模板专有规则；
- `ouc-format-spec.md`：OUC 画像的规范条目来源，不是系统默认标准；
- `generated/<template>/format-spec.md`：由对应 Word 规范稳定生成，供模板注册审查使用，不参与当前论文运行时加载；
- `common-issues.md`：历史问题说明，仅作设计参考，不参与运行时加载。

新增模板不能修改通用规则来模拟本校标准，应新增模板适配器和规则画像。暂时无法检测的适用规则必须保留为 `not_implemented`，并明确选择 `block` 或 `degrade`。

模板画像的 `format_contract` 是 Agent 可执行边界，当前只包含：官方 Word 哈希、必要 LaTeX 文件、锁定规则、条件规则和模板能力。页边距、字号、行距和线宽等锁定规则不得由 Agent 调整；跨页表格等能力只能在画像声明支持时使用。双语题注是否必需也由模板画像决定。
