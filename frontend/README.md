# Scholar Agent 前端

G8 使用 Vue 3、TypeScript、Vite、Pinia 与 Vue Router，实现“上传 Word → 提取轮询 → 结构确认 → 生成轮询 → 门禁报告 → PDF/源码交付”。

```powershell
npm install
npm run dev
```

默认后端地址为 `http://127.0.0.1:8000`，可通过 `VITE_API_BASE` 覆盖。只有后端标记为 `success` 或 `degraded` 的 generation 才显示 PDF；阻断或回退版本仅展示报告。循环 B 与版本历史页面属于 G9。
