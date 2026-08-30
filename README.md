# Brief2app
软件交付多团队是一个基于 LangGraph 的分层多 Agent 系统：输入一句需求，技术总监带 3 组长 8 工程师（全 LLM）交付一个能跑的 FastAPI+SQLite 小应用。含金量在真编排真执行：动态接口契约校验补全、解析失败回退，四工程师照同一契约分工联调一致；StateGraph 手搭分层图（组间串行组内并行）让弱模型编排稳定；TestClient 真打生成的应用，失败带堆栈跨组打回重写，自检限次返工；写文件产 diff、py_compile+pyflakes 子进程静态检查、uvicorn 起应用带健康检查；SSE 边产边发全程可见，contextvars 按请求隔离。基于 langgraph、fastapi 构建，适合学习多 Agent 编排与二次开发
