# 阶段验收产物生成命令

本目录保存 W01/W03/W05/W07/W09 的正式验收产物生成入口，不参与应用运行。普通回归测试仍位于 `tests/unit/`、`tests/integration/`、`tests/real_excerpt/` 和 `tests/e2e/`。

从仓库根目录使用模块方式运行：

~~~powershell
python -m tests.acceptance.run_p2_w01
python -m tests.acceptance.run_p3_w03
python -m tests.acceptance.run_p3_w05
python -m tests.acceptance.run_p3_w07
python -m tests.acceptance.run_p3_w09
~~~

这些命令会更新 `tests/artifacts/` 中对应阶段的正式证据，不能作为普通单元测试自动执行。命令只使用离线测试客户端，不得调用真实外部模型；运行前后都必须确认固定模板 `source/` 未被写入，生成和编译仅发生在 staging 副本。
