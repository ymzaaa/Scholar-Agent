# Scholar Agent Backend

FastAPI 服务实现“上传 Word → 结构确认 → generation → 门禁 → 可信版本 → 用户反馈草稿 → 产物交付”。

## 接口

- `GET /api/templates`：读取当前可用的 OUC 研究生与本科生模板；
- `POST /api/projects`：multipart 上传 `docx`，可选 `bib` 与 `citation_map`；
- `POST /api/projects/{project_id}/extract`：提交提取识别任务；
- `GET /api/tasks/{task_id}`：统一轮询任务状态；
- `GET /api/projects/{project_id}/structure`：读取结构快照；
- `PUT /api/projects/{project_id}/structure`：按修订号确认标题层级与对象绑定；
- `POST /api/projects/{project_id}/generate`：按已确认修订提交默认离线 generation；
- `GET /api/generations/{generation_id}`：查询发布、降级、失败或回退状态；
- `GET /api/generations/{generation_id}/pdf`：以内联方式预览通过门禁的 PDF；
- `GET /api/generations/{generation_id}/latex-source`：下载可重建源码包；
- `GET /api/generations/{generation_id}/reports/{name}`：下载白名单报告；
- `POST /api/versions/{parent_version_id}/candidates`：从可信父版本创建隔离的全门禁验证候选；
- `GET /api/versions/{version_id}`：读取版本谱系、可信状态、回退目标和门禁快照；
- `GET /api/projects/{project_id}/versions`：按项目内版本号读取版本列表；
- `POST /api/versions/{version_id}/accept`：将可信版本设为项目新的父基线；
- `POST /api/versions/{version_id}/reject`：拒绝未接受的候选版本；
- `POST /api/versions/{version_id}/feedback-runs`：基于正在查看的可信版本提交反馈；
- `GET /api/agent-runs/{run_id}`：读取反馈定位、候选和局部评测状态；
- `GET /health`：健康检查。

项目创建时绑定规范模板 ID；首次生成和反馈修订均使用同一选择。当前只接受 `ouc-graduate` 与 `ouc-bachelor`，不保留历史 `ouc` 别名。

## 数据与安全

客户端不能提交绝对路径或输出目录。DOCX、BibTeX 和引用映射写入服务器分配的项目 `inputs/`；DOCX 默认上限 100 MB，并检查 ZIP 与 Word XML 结构。内嵌图片由 Stage1 从 DOCX 中读取，不上传独立图片目录。

项目和任务元数据保存在 SQLite；论文内容、识别快照和确认快照保存在受控工作区。可使用：

- 默认 SQLite：`runtime/scholar.db`；
- 默认项目工作区：`runtime/project-workspace/`；

- `SCHOLAR_WORKSPACE_ROOT`：覆盖工作区根目录；
- `SCHOLAR_DATABASE_PATH`：覆盖 SQLite 文件；
- `SCHOLAR_DOCX_MAX_BYTES`、`SCHOLAR_BIB_MAX_BYTES`、`SCHOLAR_MAP_MAX_BYTES`：调整上传限额。

## 运行

```powershell
python -m pip install -r requirements.txt
Set-Location backend
uvicorn main:app --host 127.0.0.1 --port 8000
```

数据库与项目工作区默认互为同级路径，不允许把数据库放进工作区根。Stage1 默认按仓库布局自动定位，也可通过 `STAGE1_DIR` 显式指定。后端重启后，未完成任务会明确转为失败，不会永久显示运行中。工作区不可用时接口返回 `503 workspace_unavailable` 和中文说明。

generation 只有在 N6–N10 发布门禁允许时才进入最终目录。当前默认大语言模型离线；循环 A 只执行确定性修复。状态 `degraded` 表示 PDF 可交付但存在明确未实现或需人工复核的格式项，不等于完全通过。

新版本只允许从 `trusted=true` 且 PDF 清单仍通过路径、大小和哈希检查的父版本创建。每个版本冻结独立确认结构；候选失败时状态为 `reverted` 并指向父版本，父版本和已发布产物不会被覆盖。

用户接受版本通过项目指针管理：接受新候选只移动父指针并保留旧版本历史，下一反馈才从新父版本建立 `draft` 子版本。明确选择的内容单元通过 `render_trace.json` 定位；只有页码或模糊反馈时返回 `needs_user_input`，不创建空白候选。
