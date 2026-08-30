"""接口层:所有 HTTP API 路由(APIRouter,由 app.py 主入口挂载)。

- /api/run     跑分层图,把节点实时发的层级事件 + 真写文件/真测试 以 SSE 流式吐给前端
- /api/launch  把某次构建出的应用真用 uvicorn 跑起来(前端 iframe 预览)
- /api/files   取某次构建的产物文件当前内容
- /api/agents  组织结构(技术总监/各组长/成员 的系统提示词 + 工具列表)
- /api/conversation(s)  多轮历史 / 删除
"""
import time
import uuid
import asyncio
import json
import shutil
from fastapi import APIRouter

from fastapi.responses import StreamingResponse

from .config import MODEL, WORKSPACE_ROOT
from .runtime import EVENT_Q, T0, RUN_ID, WORKSPACE, FILES, TESTS, _ms, sse
from .agent import run_events, graph_info, LEADS
from . import store, tools

router = APIRouter()

# 生成应用的"文件四件套"骨架 —— 契约内容再怎么变,文件结构与函数名固定;
# 播种复制(_seed_workspace_from_previous)、产物面板、审查比对全靠这份清单
CODE_FILES = ("db.py", "app.py", "static/index.html", "static/app.js")


def _latest_build_context(session: str) -> dict:
    """从历史事件里找同会话最近一次构建,用于新一轮增量修改。"""
    try:
        turns = store.get_conversation(session)
    except Exception:
        return {}
    for turn in reversed(turns):
        events = turn.get("events") or []
        # final 事件是每轮 producer 收尾时发的,自带 run_id/contract —— 它是"上一版构建"的权威快照
        final = next((e for e in reversed(events) if e.get("event") == "final"), None)
        if final and final.get("run_id"):
            return {"run_id": final.get("run_id"), "contract": final.get("contract"),
                    "app_name": final.get("app_name"), "query": turn.get("query"), "brief": turn.get("brief")}
    return {}


def _seed_workspace_from_previous(prev_run_id: str, ws) -> list:
    """把上一版代码文件复制到新工作区。只复制源码,不复制 data.db / 缓存,避免旧 schema 干扰测试。"""
    if not prev_run_id:
        return []
    src_root = WORKSPACE_ROOT / prev_run_id
    if not src_root.exists():
        return []
    copied = []
    for rel in CODE_FILES:
        src = src_root / rel
        if not src.is_file():
            continue
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append({"relpath": rel, "path": str(dst), "bytes": dst.stat().st_size})
    return copied


@router.get("/api/health")
async def health():
    return {"ok": True, "pattern": "hierarchical", "framework": "LangGraph (StateGraph)",
            "model": MODEL, "leads": len(LEADS),
            "workers": sum(len(L["workers"]) for L in LEADS),
            "memory": True, "exec": "sequential", "builds_real_app": True}


@router.get("/api/agents")
async def agents():
    """前端点节点时,查看 技术总监 / 各组长 / 各成员 的角色、系统提示词、工具列表。"""
    return graph_info()


@router.get("/api/conversations")
async def conversations():
    return store.list_conversations()


@router.get("/api/conversation/{session}")
async def conversation(session: str):
    return store.get_conversation(session)


@router.delete("/api/conversation/{session}")
async def delete_conversation(session: str):
    return {"ok": True, "deleted": session, "rows": store.delete_conversation(session)}


@router.post("/api/launch")
def launch(run_id: str):
    """把某次构建的产物用 uvicorn 真跑起来,返回可访问 URL(同步端点,FastAPI 走线程池,不阻塞事件循环)。"""
    ws = WORKSPACE_ROOT / run_id
    if not ws.exists():
        return {"ok": False, "error": "工作区不存在(可能未构建或已被清理)"}
    return tools.launch_app(ws, run_id)


@router.post("/api/stop")
def stop(run_id: str):
    return {"ok": True, "stopped": tools.stop_app(run_id)}


@router.get("/api/files")
def files(run_id: str):
    """取某次构建产物文件的当前内容(给前端「产出文件」面板兜底用,正常走 SSE 里的 content)。"""
    ws = WORKSPACE_ROOT / run_id
    if not ws.exists():
        return {"ok": False, "files": []}
    out = []
    for rel in ("db.py", "app.py", "static/index.html", "static/app.js"):
        p = ws / rel
        if p.exists():
            out.append({"relpath": rel, "bytes": p.stat().st_size,
                        # errors="ignore":历史产物可能有非常规字节,兜底面板不能因一个坏字节就 500
                        "content": p.read_text(encoding="utf-8", errors="ignore")})
    return {"ok": True, "run_id": run_id, "files": out}


@router.get("/api/run")
async def run(query: str, session: str = "default"):
    """一次真·软件交付(造一个能跑的留言板),SSE 流式返回分层全过程(生产者-消费者,边产边发)。"""
    async def gen():
        # 一进请求先把 contextvars 六件套置好:接下来 producer / agent / tools 深处全靠它们拿上下文
        T0.set(time.time())
        # 8 位短 id 兼作 workspace 子目录名;单机教学场景下冲突概率可忽略
        rid = uuid.uuid4().hex[:8]
        RUN_ID.set(rid)
        ws = WORKSPACE_ROOT / rid
        ws.mkdir(parents=True, exist_ok=True)
        prev_ctx = _latest_build_context(session)
        seeded_files = _seed_workspace_from_previous(prev_ctx.get("run_id"), ws)
        WORKSPACE.set(ws)
        # 必须在 create_task 之前 set 好这两个列表对象:子任务复制的是同一个列表引用,
        # 节点里只 append(就地改),外层这里才看得到产物 / 测试结果(切忌在节点里再 .set())
        FILES.set([])
        TESTS.set([])
        q = asyncio.Queue()
        EVENT_Q.set(q)
        events = []

        async def producer():
            try:
                gi = graph_info()
                # 第一帧:组织结构(技术总监 + 3 组长 + 各成员 + 接口契约 + run_id),前端据此画整棵树
                await q.put({"event": "start", "topic": query, "run_id": rid, "cto": "技术总监",
                             "base_run_id": prev_ctx.get("run_id"), "seeded_files": seeded_files,
                             "contract": gi["contract"], "leads": gi["leads"], "session": session, "t": 0})
                if seeded_files:
                    await q.put({"event": "workspace_seeded", "run_id": rid, "base_run_id": prev_ctx.get("run_id"),
                                 "files": seeded_files, "t": _ms()})

                # 多轮记忆:从 SQLite 重建上下文(追问"把列表做美观些"之类时带上前几轮)
                model_input = query
                hist = store.load_history(session)
                if hist:
                    # 三段拼装:历史回顾(brief 截 300 字控上下文) + 上一版构建 + 修改策略,最后才放新需求
                    ctx = ("【与你之前几轮对话的回顾(供追问参考)】\n"
                           + "\n".join(f"{i+1}. 我提过「{qq}」,交付计划摘要:{(b or '')[:300]}" for i, (qq, b) in enumerate(hist)))
                    prev_contract = prev_ctx.get("contract")
                    build_ctx = (f"\n\n【上一版构建】run_id={prev_ctx.get('run_id') or '(无)'}"
                                 f"\n已复制到当前工作区的源码文件:{', '.join(f['relpath'] for f in seeded_files) or '(无)'}")
                    if prev_contract:
                        build_ctx += "\n上一版接口契约 JSON:\n" + json.dumps(prev_contract, ensure_ascii=False, indent=2)
                    build_ctx += ("\n\n【本轮修改策略】如果现在的新需求是调整/追加/优化,请基于上一版代码和契约增量修改,"
                                  "保留未被要求改变的功能、接口和文件结构;不要重新生成一个无关的新应用。")
                    model_input = ctx + build_ctx + "\n\n【现在的新需求】" + query

                r = await run_events(model_input, previous_contract=prev_ctx.get("contract"), is_revision=bool(hist))
                fs = [{"relpath": f["relpath"], "path": f["path"], "bytes": f["bytes"]} for f in (FILES.get() or [])]
                # runnable 决定前端「启动预览」按钮亮不亮:只有功能测试真过了才算可运行
                runnable = any(t.get("kind") == "functional" and t.get("ok") for t in (TESTS.get() or []))
                await q.put({"event": "final", "brief": r["final"], "run_id": rid,
                             "base_run_id": prev_ctx.get("run_id"), "contract": r.get("contract"),
                             "app_name": (r.get("contract") or {}).get("app_name", ""),
                             "files": fs, "runnable": runnable, "t": _ms()})
            except Exception as e:
                await q.put({"event": "error", "message": f"{type(e).__name__}: {e}", "t": _ms()})
            finally:
                await q.put(None)

        # 生产者后台跑图、消费者(下面的 while)逐帧吐 SSE —— 边产边发;
        # 客户端断开时生成器被取消,producer 在 q.put(None) 处自然收尾
        asyncio.create_task(producer())
        while True:
            d = await q.get()
            if d is None:
                break
            # 边发边收集:整轮事件流最终落库,前端"回放历史动画"吃的就是这份
            events.append(d)
            yield sse(d["event"], {k: v for k, v in d.items() if k != "event"})

        brief = next((e["brief"] for e in events if e["event"] == "final"), "")
        # 流走完才存档(含 final 帧):同会话下次追问,靠它重建"上一版"上下文
        store.save_run(session, query, events, brief)

    # X-Accel-Buffering: no —— 防 nginx 反代把 SSE 攒成一大坨最后一次性吐,流式就废了
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
