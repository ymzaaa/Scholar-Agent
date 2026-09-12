"""FastAPI 应用装配。

对应 01 文档第 1 节的技术形态：本地进程，监听 localhost 固定端口，
前端网页请求这个端口。

当前只挂载 projects 路由（步骤1-2）。后续步骤的路由实现后，在这里
一并 include_router 即可，main 本身不承载业务逻辑。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import (
    constraints, extract, generations, projects, recognition_reviews,
    revisions, structure, tasks, templates, versions,
)

app = FastAPI(title="Scholar Agent Backend", version="0.2.0")

# 本地单用户场景：前端 Vue 开发服务器（如 http://localhost:5173）
# 需要跨源访问本后端。本地场景直接放开即可。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(templates.router)
app.include_router(extract.router)
app.include_router(tasks.router)
app.include_router(structure.router)
app.include_router(recognition_reviews.router)
app.include_router(generations.router)
app.include_router(versions.router)
app.include_router(revisions.router)
app.include_router(constraints.router)


@app.get("/health", tags=["meta"])
def health():
    """存活检查，便于前端确认后端进程已就绪。"""
    return {"status": "ok"}
