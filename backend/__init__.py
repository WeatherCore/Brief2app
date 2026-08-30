"""Hierarchical 案例 · 软件交付多团队(LangGraph 团队套团队)—— 后端(模块化)。

模块职责:
  config.py   LLM 连接参数(凭证 / 模型)+ 路径
  runtime.py  per-request 事件管道(contextvars + 时间戳 + SSE)
  agent.py    智能体层:CTO → 3 团队 → 各工程师(create_supervisor 嵌套)+ stream→事件提取
  store.py    持久层:data/runs.db(历史 / 多轮记忆 / 删除)
  server.py   接口层:所有 /api/* 路由(APIRouter)
  app.py      主入口:组装应用 → uvicorn backend.app:app

本案例工程师不联网检索(纯设计/规划推理),所以没有 tool.py。
"""
# 本目录必须是"包"而不是裸脚本文件夹:uvicorn 以 backend.app:app 从项目根导入,
# app.py / server.py 里的相对导入(from .config import ...)全靠这里的包上下文;
# 若钻进 backend/ 里直接跑 uvicorn app:app,相对导入会 ImportError
