# OUC 研究生固定模板

本模板基于 [cornjosh/OUC-LaTex](https://github.com/cornjosh/OUC-LaTex) 做了简要修改，以适配 Scholar Agent 的只读模板复制、内容插槽和编译流程。使用与再分发时应同时遵守上游仓库及模板内第三方文件的许可声明。

- `source/` 是运行时唯一模板源，不得直接写入或编译。
- `example/` 使用与 `source/` 相同的当前格式核心，并装入可下载、可预览的示例正文和图片；运行代码不得加载该目录。
- `reference-contents/` 是早期中性结构示例，待固定模板板块统一整理时处理。
- `OUC研究生固定模板.zip` 是可上传至 Overleaf 的冻结分发包。
- 每次转换和测试必须先把 `source/` 复制到独立 staging 目录。
