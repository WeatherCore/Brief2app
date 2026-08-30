"""智能体层:Hierarchical(LangGraph StateGraph)真·软件交付 —— 本案例要讲清的核心协作模式。

【这门案例做什么】
不是"嘴上规划",而是【真的造一个能跑的小软件】——而且做什么软件由用户说了算:
技术总监先根据需求【动态制定接口契约】(应用名/数据表/字段/端点/页面元素),三个组长各带工程师
照契约真写代码、真跑测试、真做代码审查,组长用工具自检、不合格打回重写,技术总监验收后综合交付。
页面上点「启动预览」用 uvicorn 把成果真跑起来。

【为了"任意软件"仍然可靠,哪些是固定骨架】
- 文件四件套固定:db.py / app.py / static/index.html / static/app.js
- db 函数名固定:init_db / add_item / list_items / delete_item / stats
- 端点模式固定:GET /、GET /app.js、GET {api_base}、POST {api_base}、DELETE {api_base}/{id}、GET /api/stats
- 页面元素约定:每个业务字段一个输入(id=字段名)+ add 按钮 + list 容器 + 可选 stat-* 统计
变的只是契约内容(表名/字段/元素/测试样例),由技术总监按用户需求生成;解析失败自动回退到记账本契约。

【组织 = 真·人事层级】技术总监 → 前端组长(UI/交互) + 后端组长(DB/API) + 测试组长(审查/功能/性能)。
组间顺序下钻,组内并行;组长自检不过会把工程师打回重写(最多 2 次返工)。

【为什么 StateGraph 手搭】create_supervisor 把控制流交给模型发 handoff,deepseek 上不稳;
StateGraph 把"谁交给谁"写死在边里,组长/工程师仍是真 LLM、工具仍是真执行。
"""
import re
import json
import asyncio
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END

from .config import MODEL, BASE_URL, KEY
from .runtime import EVENT_Q, FILES, TESTS, _ms
from . import tools

# 写代码/定契约用低温(稳、可编译);汇总/综合用稍高温(语言自然)。都加超时重试,防 pro 偶发卡死
code_model = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=KEY, temperature=0.1, timeout=180, max_retries=1)
prose_model = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=KEY, temperature=0.5, timeout=180, max_retries=1)


# ── 契约:技术总监按用户需求动态制定;解析失败回退到这份记账本契约 ──
FALLBACK_CONTRACT = {
    "app_name": "极简记账本",
    "table": "expenses",
    "fields": [
        {"name": "amount", "type": "REAL", "label": "金额", "required": True},
        {"name": "category", "type": "TEXT", "label": "分类", "required": True},
        {"name": "note", "type": "TEXT", "label": "备注", "required": False},
    ],
    "api_base": "/api/expenses",
    "stats_elems": [{"id": "stat-total", "label": "总支出"}, {"id": "stat-count", "label": "笔数"}],
    "test_post": {"amount": 12.5, "category": "餐饮", "note": "功能测试"},
}

_CTO_CONTRACT_SYS = (
    "你是技术总监。根据用户的软件需求,制定一份最小可行的单表 Web 应用接口契约,只输出一个 JSON(不要解释):\n"
    '{"app_name":"应用中文名","table":"英文小写表名",'
    '"fields":[{"name":"英文字段名","type":"TEXT|REAL|INTEGER","label":"中文名","required":true}],'
    '"api_base":"/api/英文复数资源名",'
    '"stats_elems":[{"id":"stat-total","label":"统计项中文名"}],'
    '"test_post":{"字段名":"一个合理的测试值"}}\n'
    "约定(不要写进 JSON,系统自动处理):表会自动加 id 自增主键和 created_at;"
    "db 函数名固定 init_db/add_item/list_items/delete_item/stats;无登录、单表。\n"
    "要求:fields 取 2-4 个最核心的业务字段;stats_elems 取 1-2 个有意义的统计;test_post 覆盖所有必填字段。\n"
    "多轮修改规则:如果上下文里提供了上一版契约,且用户只是要求调整/追加/优化,优先沿用上一版 app_name/table/api_base/字段,"
    "只做必要增量修改;不要把已有应用重新换题生成。只有用户明确要求重做新应用时,才制定全新契约。")


def _normalize_contract(c: dict) -> dict:
    """校验 + 补全契约;字段不合法直接抛(由上层回退)。"""
    # 表名/字段名限定 [a-z_][a-z0-9_]*:这些值会被拼进 SQLite 建表语句,畸形名等于埋 SQL 隐患
    assert isinstance(c.get("table"), str) and re.match(r"^[a-z_][a-z0-9_]*$", c["table"])
    assert isinstance(c.get("fields"), list) and 1 <= len(c["fields"]) <= 6
    for f in c["fields"]:
        assert re.match(r"^[a-z_][a-z0-9_]*$", f.get("name", ""))
        assert f.get("type") in ("TEXT", "REAL", "INTEGER")
        f.setdefault("label", f["name"])
        f.setdefault("required", True)
    assert isinstance(c.get("api_base"), str) and re.match(r"^/api/[a-z0-9_]+$", c["api_base"])
    c.setdefault("app_name", "小应用")
    sts = c.get("stats_elems") or [{"id": "stat-total", "label": "总数"}]
    c["stats_elems"] = [{"id": s.get("id", "stat-total"), "label": s.get("label", "统计")} for s in sts[:2]
                        if re.match(r"^[a-z0-9-]+$", s.get("id", ""))] or [{"id": "stat-total", "label": "总数"}]
    tp = c.get("test_post") or {}
    # 测试样例必须覆盖全部字段:功能测试"POST 再读回断言"才有米下锅,缺的按类型补默认值
    for f in c["fields"]:
        if f["name"] not in tp:
            tp[f["name"]] = "测试" if f["type"] == "TEXT" else (1.5 if f["type"] == "REAL" else 1)
    c["test_post"] = {k: v for k, v in tp.items() if k in {f["name"] for f in c["fields"]}}
    # 功能测试"读回断言"用的特征字段:挑第一个 TEXT 字段
    c["marker_field"] = next((f["name"] for f in c["fields"] if f["type"] == "TEXT"), None)
    return c


_FIELD_HINTS = {
    "优先级": ("priority", "TEXT"),
    "截止日期": ("due_date", "TEXT"),
    "日期": ("date", "TEXT"),
    "备注": ("note", "TEXT"),
    "分类": ("category", "TEXT"),
    "状态": ("status", "TEXT"),
    "标题": ("title", "TEXT"),
    "内容": ("content", "TEXT"),
    "名称": ("name", "TEXT"),
    "金额": ("amount", "REAL"),
    "数量": ("quantity", "INTEGER"),
}


def _apply_revision_hints(topic: str, c: dict) -> dict:
    """补上常见中文追问里的确定性修改,避免 LLM 漏改契约导致测试按旧 schema 跑。"""
    text = topic or ""
    # 「标题改成X」这类确定性修改不走 LLM——正则直接落实;LLM 只负责剩下说不清的部分
    m = re.search(r"(?:页面标题|应用名|标题)\s*(?:改成|改为|换成)[「『\"]?([^」』\"\n]+)[」』\"]?", text)
    if m:
        c["app_name"] = m.group(1).strip()
    existing = {f["name"] for f in c.get("fields", [])}
    for label, (name, typ) in _FIELD_HINTS.items():
        if name in existing:
            continue
        if re.search(rf"(?:增加|新增|添加).{{0,12}}{re.escape(label)}.{{0,6}}字段", text):
            c.setdefault("fields", []).append({"name": name, "type": typ, "label": label, "required": False})
            c.setdefault("test_post", {})[name] = "中" if typ == "TEXT" else (1.5 if typ == "REAL" else 1)
            existing.add(name)
    return _normalize_contract(c)


async def _make_contract(topic: str, previous_contract: dict = None, is_revision: bool = False):
    """技术总监按需求定契约。返回 (contract, ok);解析失败回退记账本契约。"""
    try:
        human = f"用户需求:{topic}\n\n只输出契约 JSON。"
        if previous_contract:
            human = ("上一版接口契约 JSON:\n"
                     f"{json.dumps(previous_contract, ensure_ascii=False, indent=2)}\n\n"
                     + ("这是一轮基于上一版的修改。请优先沿用上一版契约,只按新需求做必要调整。\n\n"
                        if is_revision else "")
                     + f"当前需求/上下文:\n{topic}\n\n只输出更新后的契约 JSON。")
        raw = await _llm(_CTO_CONTRACT_SYS, human, model=code_model,
                         lead="cto", worker="技术总监", phase="制定接口契约")
        # LLM 输出哪怕夹带解释文字,也只抠出第一个 {...} 当 JSON;抠不出/校验不过统统走下面的回退
        m = re.search(r"\{.*\}", raw, re.S)
        contract = _normalize_contract(json.loads(m.group(0)))
        return (_apply_revision_hints(topic, contract) if is_revision else contract), True
    except Exception:
        contract = json.loads(json.dumps(previous_contract or FALLBACK_CONTRACT))
        return (_apply_revision_hints(topic, contract) if is_revision else contract), False


def contract_brief(c: dict) -> str:
    """契约的人话版本(发给所有工程师当公共上下文 + 前端展示)。"""
    fl = ", ".join(f"{f['name']} {f['type']}" + ("(必填)" if f.get("required") else "") + f"[{f.get('label','')}]"
                   for f in c["fields"])
    ids = " / ".join(f["name"] for f in c["fields"])
    sts = " / ".join(f"{s['id']}[{s.get('label','')}]" for s in c.get("stats_elems", []))
    return (f"本次交付:「{c['app_name']}」Web 应用(FastAPI + SQLite + 原生前端)。技术总监定下统一接口契约,各组照此实现:\n"
            f"• 文件:db.py(持久层)/ app.py(FastAPI 服务)/ static/index.html(页面)/ static/app.js(交互)\n"
            f"• 数据表 {c['table']}(id 自增主键, {fl}, created_at 文本);db.py 暴露 init_db / add_item / list_items / delete_item / stats\n"
            f"• 接口:GET /(页面)、GET /app.js、GET {c['api_base']}(列表)、POST {c['api_base']}(新增)、"
            f"DELETE {c['api_base']}/{{id}}(删除)、GET /api/stats(统计)\n"
            f"• 页面元素 id:{ids} / add(提交按钮)/ list(列表容器)" + (f" / {sts}(统计展示)" if sts else ""))


# ── 四位工程师的 SPEC:按契约动态渲染(职责结构固定,内容来自契约)──
def _py_type(t):
    return {"TEXT": "str", "REAL": "float", "INTEGER": "int"}[t]


def _db_spec(c):
    # 数据库工程师的岗位说明书:连"sqlite 游标默认返回 tuple,严禁直接 dict(row)"这个高频坑都提前写死,从源头减少返工
    cols = ", ".join(f"{f['name']} {f['type']}" + (" NOT NULL" if f.get("required") else "") for f in c["fields"])
    args = ", ".join(f["name"] for f in c["fields"])
    return (f"你是数据库工程师,负责 SQLite 持久层 db.py。严格按下面接口实现(其他文件靠这些函数名联调,务必一致):\n"
            f"- 用标准库 sqlite3;DB 路径:DB = pathlib.Path(__file__).resolve().parent / 'data.db'\n"
            f"- 表 {c['table']}(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols}, created_at TEXT NOT NULL)\n"
            f"- def init_db() -> None:建表(IF NOT EXISTS)\n"
            f"- def add_item({args}) -> dict:插入一行,created_at 用本地时间字符串,返回新行完整 dict\n"
            f"- def list_items() -> list[dict]:返回全部记录,按 id 倒序,每项是 dict\n"
            f"- def delete_item(item_id: int) -> bool:按 id 删除,返回 rowcount>0\n"
            f"- def stats() -> dict:至少含 'count'(总条数);可再加 1-2 个对「{c['app_name']}」有意义的聚合\n"
            f"- 每次操作各自 sqlite3.connect 开关连接\n"
            f"- 注意:sqlite 默认游标返回 tuple,严禁直接 dict(row);组 dict 要显式写键值对(如 {{'id': row[0], ...}}),"
            f"或先设 conn.row_factory = sqlite3.Row 再 dict(row)\n"
            f"只输出 db.py 的完整代码,不要解释、不要 markdown 代码围栏。")


def _api_spec(c):
    # API 工程师说明书:锚死"uvicorn app:app 能直接跑"的最低要求——模块顶层先 init_db(),端点/导入一个不能少
    pyd = "; ".join(f"{f['name']}: {_py_type(f['type'])}" + ("" if f.get("required") else
                    (" = ''" if f["type"] == "TEXT" else " = 0")) for f in c["fields"])
    args = ", ".join(f"m.{f['name']}" for f in c["fields"])
    return (f"你是 API 工程师,负责 FastAPI 服务 app.py。严格按下面实现(保证 uvicorn app:app 能直接跑):\n"
            f"- from fastapi import FastAPI;from fastapi.responses import FileResponse;from pydantic import BaseModel;import pathlib\n"
            f"- from db import init_db, add_item, list_items, delete_item, stats\n"
            f"- BASE = pathlib.Path(__file__).resolve().parent;模块顶层调用 init_db()\n"
            f"- app = FastAPI()\n"
            f"- @app.get('/') -> return FileResponse(BASE / 'static' / 'index.html')\n"
            f"- @app.get('/app.js') -> return FileResponse(BASE / 'static' / 'app.js', media_type='text/javascript')\n"
            f"- @app.get('{c['api_base']}') -> return list_items()\n"
            f"- @app.get('/api/stats') -> return stats()\n"
            f"- class ItemIn(BaseModel): {pyd}\n"
            f"- @app.post('{c['api_base']}') 入参 ItemIn m -> return add_item({args})\n"
            f"- @app.delete('{c['api_base']}/{{item_id}}') -> return {{'ok': delete_item(item_id)}}\n"
            f"只输出 app.py 的完整代码,不要解释、不要 markdown 代码围栏。")


def _ui_spec(c):
    inputs = "、".join(f"{f.get('label','')} id='{f['name']}'" +
                      ("(type=number)" if f["type"] in ("REAL", "INTEGER") else "")
                      for f in c["fields"])
    sts = "、".join(f"{s.get('label','')} id='{s['id']}'" for s in c.get("stats_elems", []))
    return (f"你是 UI 工程师,负责「{c['app_name']}」页面 static/index.html(纯 HTML+CSS,JS 交给交互工程师写在 app.js)。要求:\n"
            f"- 简洁美观的中文「{c['app_name']}」,卡片式、配色温暖清爽、移动端友好,内联 <style>\n"
            + (f"- 顶部统计卡:{sts}\n" if sts else "")
            + f"- 表单:{inputs},提交按钮 id='add'\n"
            f"- 列表容器 id='list'(留空,由 app.js 填充)\n"
            f"- 页面底部用 <script src='/app.js'></script> 引入交互脚本(不要内联 JS)\n"
            f"只输出 index.html 的完整代码,不要解释、不要 markdown 代码围栏。")


def _js_spec(c):
    fids = "、".join(f"#{f['name']}({f.get('label','')})" for f in c["fields"])
    req = "、".join(f"#{f['name']}" for f in c["fields"] if f.get("required"))
    body = ", ".join(f["name"] for f in c["fields"])
    sts = "、".join(f"#{s['id']}" for s in c.get("stats_elems", []))
    return (f"你是交互工程师,负责前端逻辑 static/app.js(被 index.html 以 <script src='/app.js'> 引入)。\n"
            f"页面已有元素:{fids}、#add(提交)、#list(列表)" + (f"、统计展示 {sts}" if sts else "") + "。要求(原生 fetch,不依赖库):\n"
            f"- 页面加载:GET {c['api_base']} 把记录渲染进 #list(显示各字段与时间,最新在上,每条带删除按钮)"
            + (f";GET /api/stats 渲染统计到 {sts}\n" if sts else "\n")
            + f"- 点 #add:读取输入({req} 为必填,空则提示不提交),POST {c['api_base']},body 为 JSON {{{body}}};成功后清空输入并刷新列表与统计\n"
            f"- 点某条的删除按钮:DELETE {c['api_base']}/{{id}},成功后刷新\n"
            f"- 数字字段注意转成数字再提交;渲染文本做转义,避免注入\n"
            f"只输出 app.js 的完整代码,不要解释、不要 markdown 代码围栏。")


# ── 组织结构:3 个组长,每个组长带成员 + 自己的检查工具;节点 = 组长 ──
# 这张名册一表两用:后端拿它编排建图(_build_graph 遍历它),前端拿它画组织架构(graph_info 原样透出)
LEADS = [
    {"id": "frontend", "lead": "前端组长", "label": "前端", "icon": "🎨",
     "lead_system": "你是前端组长,带 UI 工程师和交互工程师。结合你的自检结果,小结前端这一轮交付了什么、是否齐全。",
     "lead_tools": ["inspect_frontend"], "inspect_tool": "inspect_frontend",
     "workers": [
         {"name": "ui_engineer",          "label": "UI 工程师",  "role": "UI 工程师 · 页面/样式",
          "kind": "coder", "file": "static/index.html", "spec": _ui_spec, "tools": ["list_files", "read_code_file", "write_code_file"]},
         {"name": "interaction_engineer", "label": "交互工程师", "role": "交互工程师 · 取数/提交/统计",
          "kind": "coder", "file": "static/app.js", "spec": _js_spec, "tools": ["list_files", "read_code_file", "write_code_file"]}]},
    {"id": "backend", "lead": "后端组长", "label": "后端", "icon": "⚙️",
     "lead_system": "你是后端组长,带数据库工程师和 API 工程师。结合你的自检结果,小结后端这一轮交付了什么、是否齐全。",
     "lead_tools": ["inspect_backend"], "inspect_tool": "inspect_backend",
     "workers": [
         {"name": "db_engineer",  "label": "数据库工程师", "role": "数据库工程师 · SQLite 持久层",
          "kind": "coder", "file": "db.py", "spec": _db_spec, "tools": ["list_files", "read_code_file", "write_code_file"]},
         {"name": "api_engineer", "label": "API 工程师",   "role": "API 工程师 · FastAPI 服务",
          "kind": "coder", "file": "app.py", "spec": _api_spec, "tools": ["list_files", "read_code_file", "write_code_file"]}]},
    {"id": "test", "lead": "测试组长", "label": "测试", "icon": "🧪",
     "lead_system": "你是测试组长,带代码审查、功能测试、性能测试。结合你的质量门结果,小结这一关是否放行。",
     "lead_tools": ["quality_gate"], "inspect_tool": "quality_gate",
     "workers": [
         {"name": "code_reviewer", "label": "代码审查", "role": "代码审查工程师 · 静态检查+评审",
          "kind": "review", "tools": ["list_files", "read_code_file", "static_check"]},
         {"name": "func_tester",   "label": "功能测试", "role": "功能测试工程师 · TestClient 真打",
          "kind": "functional", "tools": ["read_code_file", "run_functional_test"]},
         {"name": "perf_tester",   "label": "性能测试", "role": "性能测试工程师 · 延迟统计",
          "kind": "perf", "tools": ["read_code_file", "run_perf_test"]}]},
]

_REVIEWER_SYSTEM = (
    "你是资深代码审查工程师。下面给你本次自动构建出的真实代码文件内容,以及 py_compile + pyflakes 的静态检查结果。"
    "做一次简洁专业的中文代码审查:先给一句总体结论(能否合入),再分文件列出问题或改进点(标注文件名,有静态告警的优先说),"
    "没问题就写「未见明显问题」。重点看:接口契约是否一致(db 函数名/接口路径/前端元素 id)、错误处理、安全(注入/转义)、可读性。3-8 条,务实不空话。")

_CTO_SYSTEM = (
    "你是技术总监。基于下面【真实】的产物文件清单、质量结论和你的验收结果,写一份中文 Markdown 交付总结:"
    "一段总览 → 『已实现的文件与职责』→『质量结论(代码审查/功能/性能)』→『验收结论』→『如何运行(点页面上的「🚀 启动预览」即可在浏览器使用)』→ 风险/后续建议。"
    "只基于给你的真实信息写,不要编造未列出的文件或未发生的测试。结构清晰、不啰嗦。")


class DeliveryState(TypedDict):
    topic: str
    contract: dict       # 技术总监动态制定的接口契约
    leads_done: dict     # 各组长小结按 lead_id 归档,CTO 综合交付时可引用
    final: str


async def _emit(ev, **kw):
    """全项目唯一的事件出口:统一带相对时间戳;队列未设置(模块导入/图外自测)时静默丢弃。"""
    q = EVENT_Q.get()
    if q is not None:
        await q.put({"event": ev, "t": _ms(), **kw})


def _rec_test(rec: dict):
    """就地追加测试/审查结果到 TESTS(切忌 `(x or []).append()`,空列表 falsy 会丢)。"""
    t = TESTS.get()
    if t is not None:
        t.append(rec)


async def _llm(system: str, human: str, model=prose_model, *, lead: str = None, worker: str = None, phase: str = "LLM") -> str:
    # 把 system/human 全文先作为 prompt 事件发出去:前端点任意节点,能直接看到这个 Agent 收到的提示词原文
    if lead or worker:
        await _emit("prompt", lead=lead or "", worker=worker or "", phase=phase, system=system, human=human)
    r = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=human)])
    c = r.content
    return c if isinstance(c, str) else str(c)


async def _llm_or_fallback(system: str, human: str, fallback: str, model=prose_model, **kw) -> str:
    """汇总/审查类文本允许降级,避免代码已产出且测试已过时被临时网络错误打断整轮。"""
    try:
        return await _llm(system, human, model=model, **kw)
    except Exception as e:
        await _emit("llm_error", lead=kw.get("lead") or "", worker=kw.get("worker") or "",
                    phase=kw.get("phase") or "LLM", message=f"{type(e).__name__}: {e}")
        return fallback + f"\n\n> 注:本段由系统降级生成,原因是 LLM 文本生成临时失败({type(e).__name__})。"


# ── 单个成员干活:按 kind 分派。coder 的 SPEC 按契约动态渲染 ──
async def _run_worker(lead_id: str, w: dict, topic: str, contract: dict, feedback: str = None) -> str:
    name = w["name"]
    brief = contract_brief(contract)
    await _emit("worker_dispatch", lead=lead_id, worker=name, rework=bool(feedback))

    if w["kind"] == "coder":
        await _emit("tool_call", lead=lead_id, worker=name, tool="list_files", args={})
        lf = await asyncio.to_thread(tools.list_files)
        await _emit("tool_done", lead=lead_id, worker=name, tool="list_files", result=f"工作区现有 {lf['count']} 个文件")
        # 工作区已有自己负责的文件就先读上一版:这是增量修改的第一手依据,也让 LLM 少凭空重造
        existing = ""
        if any(x.get("relpath") == w["file"] for x in lf.get("files", [])):
            await _emit("tool_call", lead=lead_id, worker=name, tool="read_code_file", args={"relpath": w["file"]})
            existing = await asyncio.to_thread(tools.read_code_file, w["file"])
            await _emit("tool_done", lead=lead_id, worker=name, tool="read_code_file",
                        result=f"已读取上一版 {w['file']}({len(existing)} 字符),本轮将在此基础上修改")
        human = f"软件需求:{topic}\n\n接口契约:\n{brief}\n\n现在请实现你负责的文件:{w['file']}"
        # prompt 硬性要求"最小必要修改":防 LLM 借增量之名整个重写、顺手丢掉既有功能
        if existing:
            human += (f"\n\n【当前工作区已有 {w['file']}】\n"
                      "下面是上一版完整内容。请基于它做最小必要修改,保留未被新需求要求改变的功能和接口;"
                      "仍然只输出修改后的完整文件内容。\n"
                      f"```text\n{existing}\n```")
        else:
            human += "\n\n【当前工作区没有这个文件】请创建完整文件。"
        if feedback:
            human += f"\n\n【组长打回 · 上一版自检未通过,请针对性修正后重写完整文件】\n{feedback}"
        await _emit("tool_call", lead=lead_id, worker=name, tool="write_code_file", args={"relpath": w["file"]})
        code = await _llm(w["spec"](contract), human, model=code_model,
                          lead=lead_id, worker=name, phase=("返工修正文件" if feedback else "实现/修改文件"))
        rec = await asyncio.to_thread(tools.write_code_file, w["file"], code)
        await _emit("file_written", lead=lead_id, worker=name, relpath=rec["relpath"],
                    path=rec["path"], bytes=rec["bytes"], diff=rec["diff"], content=rec["content"])
        await _emit("tool_done", lead=lead_id, worker=name, tool="write_code_file",
                    result=f"已写 {rec['relpath']}({rec['bytes']} 字节)")
        out = f"已实现 `{rec['relpath']}`({rec['bytes']} 字节)"
        await _emit("worker_done", lead=lead_id, worker=name, output=out, relpath=rec["relpath"])
        return out

    if w["kind"] == "review":
        await _emit("tool_call", lead=lead_id, worker=name, tool="list_files", args={})
        lf = await asyncio.to_thread(tools.list_files)
        await _emit("tool_done", lead=lead_id, worker=name, tool="list_files", result=f"工作区现有 {lf['count']} 个文件,逐个过一遍")
        await _emit("tool_call", lead=lead_id, worker=name, tool="static_check", args={})
        sc = await asyncio.to_thread(tools.static_check)
        await _emit("tool_done", lead=lead_id, worker=name, tool="static_check",
                    result=("编译全过" if sc["ok"] else "存在编译错误"))
        srcs = []
        for rel in ("db.py", "app.py", "static/index.html", "static/app.js"):
            srcs.append(f"===== {rel} =====\n" + await asyncio.to_thread(tools.read_code_file, rel))
        review = await _llm_or_fallback(
            _REVIEWER_SYSTEM,
            f"接口契约:\n{brief}\n\n静态检查结果:\n{sc}\n\n代码文件:\n" + "\n\n".join(srcs),
            fallback=("总体结论:静态检查" + ("通过" if sc["ok"] else "未通过") +
                      "。请以 py_compile/pyflakes 结果为准继续处理。"),
            lead=lead_id, worker=name, phase="代码审查")
        # 关键裁决:审查"通过与否"以 py_compile 结果(sc["ok"])为准,LLM 文字结论只作参考——防审查员嘴上说没问题
        _rec_test({"kind": "review", "ok": sc["ok"], "static": sc, "review": review})
        await _emit("review_result", lead=lead_id, worker=name, ok=sc["ok"], static=sc, review=review)
        await _emit("worker_done", lead=lead_id, worker=name, output=review)
        return "代码审查完成:" + ("编译通过" if sc["ok"] else "发现编译错误")

    if w["kind"] == "functional":
        await _emit("tool_call", lead=lead_id, worker=name, tool="read_code_file", args={"relpath": "app.py"})
        src = await asyncio.to_thread(tools.read_code_file, "app.py")
        await _emit("tool_done", lead=lead_id, worker=name, tool="read_code_file", result=f"已读 app.py 确认接口({len(src)} 字符)")
        await _emit("tool_call", lead=lead_id, worker=name, tool="run_functional_test", args={})
        res = await asyncio.to_thread(tools.run_functional_test, contract)
        _rec_test({"kind": "functional", **res})
        await _emit("test_result", lead=lead_id, worker=name, kind="functional", result=res)
        await _emit("tool_done", lead=lead_id, worker=name, tool="run_functional_test",
                    result=("全部通过" if res.get("ok") else "未通过"))
        if res.get("ok"):
            out = "功能测试通过:" + "、".join(c["name"] for c in res.get("checks", []))
        else:
            out = "功能测试未通过:" + (res.get("error") or ";".join(c["name"] for c in res.get("checks", []) if not c["pass"]))
        await _emit("worker_done", lead=lead_id, worker=name, output=out)
        return out

    if w["kind"] == "perf":
        await _emit("tool_call", lead=lead_id, worker=name, tool="read_code_file", args={"relpath": "app.py"})
        src = await asyncio.to_thread(tools.read_code_file, "app.py")
        await _emit("tool_done", lead=lead_id, worker=name, tool="read_code_file", result=f"已读 app.py 确认压测端点({len(src)} 字符)")
        await _emit("tool_call", lead=lead_id, worker=name, tool="run_perf_test", args={"n": 40})
        res = await asyncio.to_thread(tools.run_perf_test, contract, 40)
        _rec_test({"kind": "perf", **res})
        await _emit("test_result", lead=lead_id, worker=name, kind="perf", result=res)
        await _emit("tool_done", lead=lead_id, worker=name, tool="run_perf_test",
                    result=(f"avg {res.get('avg_ms')}ms / p95 {res.get('p95_ms')}ms" if res.get("ok") else "失败"))
        out = (f"性能测试({res.get('n')} 次 GET):平均 {res.get('avg_ms')}ms、p95 {res.get('p95_ms')}ms、最大 {res.get('max_ms')}ms"
               if res.get("ok") else "性能测试失败:" + str(res.get("error")))
        await _emit("worker_done", lead=lead_id, worker=name, output=out)
        return out

    return ""


_INSPECT_FN = {
    "inspect_frontend": lambda c: tools.inspect_frontend(c),
    "inspect_backend": lambda c: tools.inspect_backend(c),
    "quality_gate": lambda c: tools.quality_gate(),
}


async def _lead_inspect(L: dict, contract: dict) -> dict:
    """组长自检统一入口:真调工具核对契约,结果以 lead_check 事件发前端;返回值直接驱动下面的返工循环。"""
    tool = L["inspect_tool"]
    await _emit("tool_call", lead=L["id"], worker=L["lead"], tool=tool, args={})
    res = await asyncio.to_thread(_INSPECT_FN[tool], contract)
    await _emit("lead_check", lead=L["id"], tool=tool, ok=res.get("ok"), checks=res.get("checks", []))
    await _emit("tool_done", lead=L["id"], worker=L["lead"], tool=tool,
                result=("检查通过" if res.get("ok") else "发现待改进项"))
    return res


def _make_lead_node(L: dict):
    """组长节点:派发 → 组内并行 → 自检 →【不合格打回重写,最多 2 次返工】→ 汇总。"""
    async def lead_node(state: DeliveryState):
        topic, contract = state["topic"], state["contract"]
        await _emit("lead_dispatch", lead=L["id"])
        # 组内并行:成员一起 gather 出工,结果按派发顺序收齐(并行提速,互不依赖)
        outs = list(await asyncio.gather(*[_run_worker(L["id"], w, topic, contract) for w in L["workers"]]))

        # 测试组专属:功能测试失败 → 带错误堆栈跨组打回后端工程师重写 → 重测一次。
        # 这是"测试驱动返工":静态编译查不出的实现级 bug(如 dict(tuple)),由真实测试逼出来再修。
        if L["id"] == "test":
            func = next((t for t in reversed(TESTS.get() or []) if t.get("kind") == "functional"), None)
            if func and not func.get("ok"):
                err = func.get("error") or ";".join(c["name"] for c in func.get("checks", []) if not c["pass"])
                fb = (f"功能测试未通过:{err}\n错误堆栈片段:\n{(func.get('trace') or '')[-600:]}\n"
                      "请定位并修复问题后,重写你负责的完整文件。")
                await _emit("lead_rework", lead="test", attempt=1, issues=[f"功能测试未通过:{str(err)[:80]},打回后端组修复"])
                backend = next(x for x in LEADS if x["id"] == "backend")
                await asyncio.gather(*[_run_worker("backend", w, topic, contract, feedback=fb)
                                       for w in backend["workers"] if w["kind"] == "coder"])
                ftw = next(w for w in L["workers"] if w["kind"] == "functional")
                outs.append(await _run_worker(L["id"], ftw, topic, contract))

        chk = await _lead_inspect(L, contract)
        coders = [w for w in L["workers"] if w["kind"] == "coder"]
        # 限次返工:最多 2 次 —— 防 LLM 反复修不好无限烧钱;2 次后带病放行,交由 CTO 验收环节定夺
        attempt = 0
        while coders and not chk.get("ok") and attempt < 2:
            attempt += 1
            failed = [c["name"] for c in chk.get("checks", []) if not c["pass"]]
            fb = "组长自检未通过项:" + "、".join(failed) + "。请针对性修正后重写完整文件。"
            await _emit("lead_rework", lead=L["id"], attempt=attempt, issues=failed)
            outs = await asyncio.gather(*[_run_worker(L["id"], w, topic, contract, feedback=fb) for w in coders])
            chk = await _lead_inspect(L, contract)
        chk_txt = ";".join(f"{c['name']}={'✓' if c['pass'] else '✗'}" for c in chk.get("checks", []))
        rework_txt = f"(经 {attempt} 次返工)" if attempt else ""
        agg = await _llm_or_fallback(
            L["lead_system"],
            f"软件需求:{topic}\n\n成员产出:\n" + "\n".join(f"- {o}" for o in outs)
            + f"\n\n我(组长)的自检{rework_txt}:{'通过' if chk.get('ok') else '仍有问题'} —— {chk_txt}\n\n请用 2-4 条中文小结本组({L['lead']})这一轮交付/把关了什么。",
            fallback=(f"{L['lead']}完成本组任务;自检" + ("通过" if chk.get("ok") else "仍有问题") +
                      f":{chk_txt}"),
            lead=L["id"], worker=L["lead"], phase="组长汇总")
        await _emit("lead_done", lead=L["id"], output=agg[:600], ok=chk.get("ok"), reworks=attempt)
        return {"leads_done": {**state.get("leads_done", {}), L["id"]: agg}}

    return lead_node


async def cto_synthesize(state: DeliveryState):
    """技术总监 验收(真调用 acceptance_check)+ 综合交付计划。"""
    await _emit("tool_call", lead="cto", worker="技术总监", tool="acceptance_check", args={})
    acc = await asyncio.to_thread(tools.acceptance_check)
    await _emit("lead_check", lead="cto", tool="acceptance_check", ok=acc.get("ok"), checks=acc.get("checks", []))
    await _emit("tool_done", lead="cto", worker="技术总监", tool="acceptance_check",
                result=("验收通过,可交付" if acc.get("ok") else "验收发现问题"))

    files = FILES.get() or []
    tests = TESTS.get() or []
    # 只把真实产物清单与真实测试结论喂给 CTO(prompt 也要求"只基于真实信息写")——从两头堵 LLM 编造交付内容
    files_txt = "\n".join(f"- {f['relpath']}({f['bytes']} 字节)" for f in files) or "(无)"
    parts = []
    for t in tests:
        if t["kind"] == "review":
            parts.append("【代码审查】编译" + ("通过" if t["ok"] else "失败") + ":\n" + (t.get("review") or "")[:600])
        elif t["kind"] == "functional":
            parts.append("【功能测试】" + ("通过" if t.get("ok") else "未通过") +
                         ":" + "、".join(f"{c['name']}={'✓' if c['pass'] else '✗'}" for c in t.get("checks", [])))
        elif t["kind"] == "perf":
            parts.append("【性能测试】" + (f"平均 {t.get('avg_ms')}ms,p95 {t.get('p95_ms')}ms,共 {t.get('n')} 次" if t.get("ok") else "失败"))
    tests_txt = "\n".join(parts) or "(无)"
    acc_txt = ("通过" if acc.get("ok") else "未通过") + ";" + "、".join(f"{c['name']}={'✓' if c['pass'] else '✗'}" for c in acc.get("checks", []))
    final = await _llm_or_fallback(
        _CTO_SYSTEM,
        f"软件需求:{state['topic']}\n\n应用:「{state['contract'].get('app_name','')}」\n\n真实产物文件:\n{files_txt}\n\n真实质量结论:\n{tests_txt}\n\n我的验收结论:{acc_txt}",
        fallback=(f"## 交付总结\n\n应用「{state['contract'].get('app_name','')}」已完成本轮构建。\n\n"
                  f"### 已实现的文件与职责\n{files_txt}\n\n"
                  f"### 质量结论\n{tests_txt}\n\n"
                  f"### 验收结论\n{acc_txt}\n\n"
                  "### 如何运行\n点页面上的「🚀 启动预览」即可在浏览器使用。"),
        lead="cto", worker="技术总监", phase="综合交付计划")
    return {"final": final}


def _build_graph():
    # 手搭串行主线:前端 → 后端 → 测试 → 总监 —— 测试必须等代码写完,总监必须等质量结论齐;
    # 组内并行藏在每个组长节点内部,图层面保持一眼能看懂的简单
    g = StateGraph(DeliveryState)
    for L in LEADS:
        g.add_node(L["id"], _make_lead_node(L))
    g.add_node("cto_synthesize", cto_synthesize)
    g.add_edge(START, LEADS[0]["id"])
    for a, b in zip(LEADS, LEADS[1:]):
        g.add_edge(a["id"], b["id"])
    g.add_edge(LEADS[-1]["id"], "cto_synthesize")
    g.add_edge("cto_synthesize", END)
    return g.compile()


# 模块级编译一次、全局复用:图结构不变,可变状态全在 contextvars 里,复用无副作用
GRAPH = _build_graph()


def graph_info():
    """/api/agents:技术总监 + 各组长(含工具)+ 成员(含 SPEC 说明 + 工具列表)。"""
    return {
        "cto": {"name": "技术总监", "role": "技术总监 · 顶层",
                "system": "按用户需求动态制定接口契约(应用/表/字段/端点/页面元素),顺序调度三个组长;最后用 acceptance_check 验收并综合交付计划,可 launch_app 启动预览。",
                "tools": tools.toolspecs(["list_files", "acceptance_check", "launch_app"])},
        "contract": "由技术总监按本轮需求动态制定(见运行轨迹中的「定契约」步骤)",
        "leads": [{"id": L["id"], "lead": L["lead"], "label": L["label"], "icon": L["icon"],
                   "lead_system": L["lead_system"], "tools": tools.toolspecs(L["lead_tools"]),
                   "workers": [{"name": w["name"], "label": w["label"], "role": w["role"],
                                "system": (w["spec"](FALLBACK_CONTRACT) + "\n\n(示例为回退契约;实际内容由技术总监按需求动态生成)")
                                          if w.get("spec") else (_REVIEWER_SYSTEM if w["kind"] == "review" else "运行所属工具,报告真实结果。"),
                                "file": w.get("file"), "tools": tools.toolspecs(w["tools"])}
                               for w in L["workers"]]}
                  for L in LEADS]}


async def run_events(topic: str, previous_contract: dict = None, is_revision: bool = False) -> dict:
    """先定契约(动态)→ 跑分层图 → 返回 {final, contract}。"""
    contract, ok = await _make_contract(topic, previous_contract=previous_contract, is_revision=is_revision)
    await _emit("contract", app_name=contract.get("app_name", ""), ok=ok,
                brief=contract_brief(contract), table=contract.get("table"), api_base=contract.get("api_base"))
    result = await GRAPH.ainvoke({"topic": topic, "contract": contract, "leads_done": {}})
    return {"final": result.get("final", ""), "contract": contract}


# ── 自测:python -m backend.agent —— 用"待办清单"需求验证动态契约全链 ──
if __name__ == "__main__":
    import time
    import uuid
    import pathlib as _pl
    from .runtime import T0, RUN_ID, WORKSPACE
    from .config import WORKSPACE_ROOT

    async def _test():
        q = asyncio.Queue()
        EVENT_Q.set(q)
        T0.set(time.time())
        rid = "selftest-" + uuid.uuid4().hex[:6]
        RUN_ID.set(rid)
        ws = _pl.Path(WORKSPACE_ROOT) / rid
        ws.mkdir(parents=True, exist_ok=True)
        WORKSPACE.set(ws)
        FILES.set([])
        TESTS.set([])

        async def producer():
            await q.put({"event": "start", "topic": "x", "t": 0})
            r = await run_events("做一个待办清单:能添加任务(标题/优先级)、查看列表、删除任务")
            await q.put({"event": "final", "brief": r["final"], "t": _ms()})
            await q.put(None)

        asyncio.create_task(producer())
        n = 0
        while True:
            d = await q.get()
            if d is None:
                break
            n += 1
            who = d.get("worker") or d.get("lead") or ""
            extra = d.get("tool") or d.get("relpath") or d.get("app_name") or ""
            print(f"+{d['t']:>6}ms  {d['event']:15} {who:18} {extra}")
        print(f"\n共 {n} 个事件 · 工作区 {ws}")
        print("产物:", [f["relpath"] for f in (FILES.get() or [])])
        print("质量:", [(t['kind'], t.get('ok')) for t in (TESTS.get() or [])])

    asyncio.run(_test())
