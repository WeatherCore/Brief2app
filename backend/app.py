"""主入口:组装 FastAPI 应用(中间件 + 挂接口路由 + 前端页面)。

启动:
    cd hierarchical-software-delivery
    ../.venv-hier/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8090
"""
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND
from .server import router

app = FastAPI(title="hierarchical · 软件交付多团队(LangGraph)")
# 教学案例全开 CORS:前后端本地联调零配置;若真要对外部署,allow_origins 必须收紧成白名单
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)            # /api/* 接口在 server.py


@app.get("/")
async def index():                    # 前端单页
    # no-cache:前端页面迭代时刷新即见新版,不被浏览器缓存糊脸
    return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-cache"})
