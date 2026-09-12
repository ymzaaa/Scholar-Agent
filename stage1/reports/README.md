# Runtime reports

该目录只存放流水线运行时生成的结构化报告。历史报告不能代表当前代码或当前 PDF。

每次运行创建独立的 run ID 子目录。失败运行也必须保留 `pipeline_log.json`、`render_trace.json` 和 `content_fidelity.json`；追踪账本只记录单元标识、目标位置、哈希和转换类型，不保存额外论文正文副本。

## 参考文献报告

`render_trace.json`中的`bibliography`保存当前数据源模式、编号到稳定键的映射、匹配方法、Word文末列表证据、未解析编号、问题和`pending_confirmation`。

当前产品固定使用 Word 文末参考文献列表。BibTeX 兼容代码暂时保留，但前端入口隐藏，不参与默认流程。

## 语义对象报告

内容保真报告 4.0.0 包含脚注、域、交叉引用、链接和文本框指标。文本框内容可以通过，但绝对定位以 `degraded` 记录；缺失脚注、未支持域、缺失或重复书签、危险链接和复杂文本框均为阻断失败。
