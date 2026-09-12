# OUC 本科生固定模板

本模板基于 [summitgao/OUC-LaTex-bachelor](https://github.com/summitgao/OUC-LaTex-bachelor) 做了简要修改，以适配 Scholar Agent 的只读模板复制、内容插槽和编译流程。上游项目声明代码采用 MIT 协议；学校标志版权归中国海洋大学所有。

- `source/` 是运行时唯一模板源，不得直接写入或编译。
- `example/` 完整保留原模板范文、示例图片和使用说明；运行代码不得加载该目录。
- `source/` 仅清除示例内容，`oucart.cls`、样式文件和学校资源保持不变。
- 每次转换和测试必须先把 `source/` 复制到独立 staging 目录。
