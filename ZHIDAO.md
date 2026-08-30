# 📖 Brief2app（Hierarchical · 软件交付多团队）项目导读指南

> 本文件是 `Brief2app` 项目的中文导读，帮助你从零开始理解这个基于 LangGraph 的分层多 Agent 软件交付系统的架构、代码和运行方式。

---

## 目录

- [1. 这个项目是干什么的？](#1-这个项目是干什么的)
- [2. 核心概念速览](#2-核心概念速览)
- [3. 项目目录结构详解](#3-项目目录结构详解)
- [4. 运行流程全景图](#4-运行流程全景图)
- [5. 逐文件代码导读](#5-逐文件代码导读)
- [6. 关键设计模式解析](#6-关键设计模式解析)
- [7. 配置系统详解](#7-配置系统详解)
- [8. 如何运行和测试](#8-如何运行和测试)
- [9. 复刻建议与学习路线](#9-复刻建议与学习路线)
- [10. 常见问题](#10-常见问题)
- [附录：关键术语对照表](#附录关键术语对照表)

---

## 1. 这个项目是干什么的？

**一句话**：一个"输入一句需求、输出一个能跑的 Web 应用"的分层多 Agent 系统——技术总监、前端/后端/测试三个组长、八名工程师全员是 LLM，他们真的写代码、真的跑测试、真的交付一个 FastAPI + SQLite 小应用。

```
用户需求(Brief)                        最终产物
    │                                    │
    ▼                                    ▼
┌─────────┐   ┌──────────────────┐   ┌──────────────────┐
│ 技术总监 │ → │ 3 个组长 × 各自团队 │ → │ 可运行的 Web 应用  │
│ 定接口契约│   │ 写代码/测试/审查   │   │ + 交付总结(Markdown)│
└─────────┘   └──────────────────┘   └──────────────────┘
   agent.py         agent.py+tools.py      workspace/<run_id>/
```

它**不是**一个"AI 帮你生成代码片段的聊天工具"，**而是**一条模拟真实软件公司人事层级的交付流水线：做什么应用由技术总监根据你的需求动态定契约，工程师照契约写代码，测试组用真实 HTTP 请求验证，组长自检不合格会打回重写，最后技术总监验收综合。

**类比**：把 LangGraph 的 `StateGraph` 想象成公司的"流程制度"（谁向谁汇报、什么顺序干活写死在边里），每个 Agent 是"拿着 JD（系统提示词）和工具（文件读写/编译器/测试器）的员工"，接口契约是"需求评审会定下的开发任务书"——所有人照任务书干活，联调才不会鸡同鸭讲。

**为什么值得读**：它示范了多 Agent 系统最难的几件事——怎么让"生成任意软件"仍然可靠（固定骨架 + 动态契约）、怎么让弱模型也能稳定编排（手搭 StateGraph 而非模型自主 handoff）、怎么让交付质量可验证（真执行的工具 + 测试驱动的返工循环）。

---

## 2. 核心概念速览

读代码前，先弄懂这 6 个概念。每个概念先讲"是什么"，再给类比和伪代码。

### 2.1 Agent（智能体）= LLM + 角色设定 + 工具

- **是什么**：本项目里的 Agent 不是自主漫游的机器人，而是"带系统提示词的 LLM 调用"。每个成员有固定角色（如"数据库工程师"）、一段按契约渲染的任务说明（SPEC）、一份可用工具清单。
- **把它理解为**：一个新员工入职时拿到的《岗位说明书》——职责、交付物格式、能用的办公设备全写在上面。
- **伪代码**：

```python
async def run_worker(worker, contract):
    spec = worker["spec"](contract)          # 按契约渲染岗位说明书
    code = await llm(spec, human_prompt)     # LLM 干活
    await tools.write_code_file(worker["file"], code)  # 真落盘
```

### 2.2 接口契约（Contract）= 动态生成的"开发任务书"

- **是什么**：一份 JSON——应用名、表名、字段、端点、页面元素 id、测试样例。技术总监按用户需求现生成，校验补全后发给所有 Agent 当公共上下文。
- **把它理解为**：装修队的《施工图》。没有它，水电工和木工各干各的，最后门装不上。
- **关键代码**：`agent.py` 的 `_normalize_contract`（校验 + 补全 + 选特征字段）、`FALLBACK_CONTRACT`（解析失败的兜底记账本契约）、`contract_brief`（契约转人话版本）。

### 2.3 分层图（Hierarchical Graph）= 组间串行、组内并行

- **是什么**：`StateGraph` 手搭的两级结构——顶层 `START → 前端组 → 后端组 → 测试组 → 技术总监 → END` 串行；每个组长节点内部用 `asyncio.gather` 让组内成员并行干活。
- **把它理解为**：项目排期表——前端、后端、测试三个阶段按序推进（测试必须等代码写完），但每个阶段内部成员同时开工。

### 2.4 per-request 上下文（contextvars）= 每个请求一个"独立办公室"

- **是什么**：`runtime.py` 用 `contextvars.ContextVar` 存 6 个请求级变量：事件队列 `EVENT_Q`、计时起点 `T0`、构建 id `RUN_ID`、工作区 `WORKSPACE`、产物清单 `FILES`、测试结果 `TESTS`。
- **把它理解为**：每个 HTTP 请求开一间独立办公室，办公室里放着本次构建的记事板；深处任何员工（节点/工具）都能看到自己这间办公室的记事板，不用层层传话，多个请求互不串门。

### 2.5 SSE 事件流 = 生产者-消费者式的"直播弹幕"

- **是什么**：`/api/run` 接口里，`producer` 协程跑 Agent 图、把事件 put 进 `asyncio.Queue`；外层 while 循环 get 出来逐帧转成 SSE 发给前端。前端据此画出树状协作动画。
- **把它理解为**：施工现场装了直播摄像头，每个工人每干一步（派活/调工具/写文件/测试结果）都发一条弹幕。

### 2.6 真执行的工具（tools.py）= 不是嘴上说说

- **是什么**：10 个本地工具——`write_code_file`（真落盘 + 产 unified diff）、`static_check`（真跑 `py_compile` + `pyflakes` 子进程）、`run_functional_test`（真用 TestClient 打生成的 app）、`run_perf_test`（真压测统计延迟）、`launch_app`（真用 uvicorn 起应用 + 健康检查）等。
- **把它理解为**：公司不发"模拟考卷"，验收就是真刀真枪上线试跑。
- **为什么本地实现而非 MCP**：零配置、零网络、可靠可控——教学演示最稳（见 `tools.py` 模块 docstring）。

---

## 3. 项目目录结构详解

```
Brief2app/
├── backend/                          # 后端主包（全部业务逻辑）
│   ├── __init__.py        📖 13 行   # 模块地图：一句话讲清各模块职责
│   ├── config.py          ⭐ 31 行   # 配置单一真相源：路径 + LLM 连接参数(.env)
│   ├── runtime.py         ⭐ 35 行   # per-request 上下文：contextvars × 6 + SSE 帧格式
│   ├── app.py             🚪 22 行   # 主入口：组装 FastAPI + CORS + 挂路由 + 前端页
│   ├── store.py           💾 71 行   # 持久层：SQLite runs.db(历史/多轮记忆/删除)
│   ├── server.py          🌐 191 行  # 接口层：全部 /api/* 路由 + SSE 流式端点
│   ├── tools.py           🔧 394 行  # 工具层：写文件/静态检查/功能测试/压测/起应用
│   ├── agent.py           🧠 583 行  # 智能体层：契约 + 组织结构 + 分层图 + 返工循环
│   ├── data/
│   │   └── runs.db                   # SQLite(运行时生成)：每轮交付存一行
│   ├── logs/                         # 运行日志(config.py 自动创建)
│   └── workspace/                    # 工程师真写代码落盘处，每次构建一个子目录
│       └── <run_id>/                 # db.py / app.py / static/index.html / static/app.js
├── frontend/
│   └── index.html         🎨         # 前端单页：树状协作动画 + 产出文件面板 + iframe 预览
├── requirements.txt       📋         # langgraph / langchain-openai / fastapi / pyflakes ...
├── .env.example           📋         # 环境变量模板(OPENROUTER_API_KEY 等)
├── .gitignore                        # .env / data / workspace 等不入库
├── ZHIDAO.md                         # 本文件
└── README.md                         # 项目门面
```

**⭐ 核心　🚪 入口　🔧 工具　🌐 接口　💾 存储　🎨 前端　📋 配置**

注意三个"运行时才出现"的目录：`data/`（历史库）、`logs/`（日志）、`workspace/<run_id>/`（每次构建的产物），都由 `config.py` 在启动时自动创建——`workspace` 里每个子目录就是一个"交付出来的小软件"，可以在页面上直接点「🚀 启动预览」跑起来。

---

## 4. 运行流程全景图

一次完整交付（`GET /api/run?query=做一个待办清单&session=abc`）的全过程：

```
GET /api/run (server.py:318)
│
▼
┌──────────────────────────────────────────────────────────────────┐
│ ① 准备阶段(server.py gen())                                       │
│  职责:T0/RUN_ID/WORKSPACE/FILES/TESTS.set() → 建事件队列          │
│  查多轮历史(_latest_build_context) → 播种上一版源码到新工作区       │
│  退出条件:上下文就绪,create_task(producer) 启动                   │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│ ② 定契约(agent.py run_events → _make_contract)                    │
│  职责:技术总监 LLM 生成契约 JSON → _normalize_contract 校验补全    │
│       → 多轮追问时 _apply_revision_hints 正则兜底确定性修改        │
│  退出条件:拿到契约;解析失败 → 回退 FALLBACK_CONTRACT(记账本)      │
│  事件:contract(app_name/table/api_base/人话版 brief)              │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│ ③ 分层图执行(agent.py GRAPH.ainvoke)                              │
│                                                                  │
│  START → [前端组长] ──→ [后端组长] ──→ [测试组长] ──→ [技术总监] → END
│           组内并行        组内并行        组内并行        验收+综合   │
│           UI工程师        DB工程师        代码审查       acceptance_  │
│           交互工程师      API工程师       功能测试↑      check        │
│           (index.html)   (db.py/app.py)  性能测试       交付总结      │
│                                                                  │
│  组长节点内部循环(_make_lead_node):                                │
│    派活(worker_dispatch) → asyncio.gather 并行干活                │
│    → 组长自检(inspect_*) → 不合格? ──是──→ 打回重写(≤2 次) ─┐      │
│        │                                    ▲            │      │
│        └────────────否──────────────────────┴────────────┘      │
│    → 组长 LLM 小结(lead_done)                                    │
│                                                                  │
│  ★ 测试组专属分支:功能测试失败 → 带错误堆栈跨组打回后端组重写       │
│    → 重测一次(测试驱动返工,agent.py:426-439)                      │
│  退出条件:全部组长完成 → cto_synthesize                           │
└──────────────────────────────────────────────────────────────────┘
│
▼
┌──────────────────────────────────────────────────────────────────┐
│ ④ 收尾(server.py gen() 尾部)                                      │
│  事件:final(brief/文件清单/runnable) → 队列结束哨兵 None          │
│  store.save_run 存整轮事件流(复现动画 + 多轮记忆)                  │
│  退出条件:SSE 流关闭                                              │
└──────────────────────────────────────────────────────────────────┘

旁路:POST /api/launch → tools.launch_app → uvicorn 子进程跑 workspace/<run_id>
      → 健康检查(14s 内 GET / 返回 200) → 返回 URL 给前端 iframe 预览
```

---

## 5. 逐文件代码导读

> 阅读顺序说明：skeleton 的 quick_start 以 `backend/__init__.py` 为入口（入度 0，模块地图，读者最先接触）。在语义上补充一句：本项目"入口"是 `app.py`，但理解全局的关键在 `agent.py`——建议按"地图 → 入口 → 外围 → 核心引擎"的顺序读，总耗时约 60-90 分钟。

### 5.1 `backend/__init__.py`（13 行 · 约 1 分钟）

- **作用**：模块地图——用一段 docstring 讲清六个模块各自的职责和依赖方向。
- **阅读顺序建议**：第一个读，读完对全项目有 80% 的骨架认知。

**要点**：① 这是"先宏观后微观"的项目自述；② 本案例无联网检索工具（纯设计推理 + 本地工具）。

### 5.2 `backend/config.py`（31 行 · 约 2 分钟）

- **作用**：配置单一真相源——路径常量 + LLM 连接参数，全部从项目根 `.env` 读。
- **关键内容**：

| 常量 | 值/来源 | 说明 |
|---|---|---|
| `BACKEND/ROOT/FRONTEND` | `__file__` 推导 | 路径基准，不依赖 cwd |
| `DATA/LOGS/WORKSPACE_ROOT` | `backend/data` 等 | 启动时 `mkdir(exist_ok=True)` 自动建 |
| `RUNS_DB` | `data/runs.db` | SQLite 历史库 |
| `MODEL` | `.env`，兜底 `deepseek/deepseek-v4-flash` | 注意 `.env.example` 示例写的是 `v4-pro` |
| `BASE_URL` | 兜底 OpenRouter | OpenAI 兼容端点 |
| `KEY` | `.env` 必填 | 缺失直接 `raise RuntimeError`（fail-fast） |

**要点**：① 本文件只提供"值"，不实例化客户端（客户端在 agent 层建）；② 密钥绝不硬编码，`.env` 不入库。

### 5.3 `backend/runtime.py`（35 行 · 约 3 分钟）

- **作用**：per-request 运行期上下文——6 个 `ContextVar` + 2 个工具函数。
- **关键数据结构**：

| ContextVar | 类型 | 消费者 |
|---|---|---|
| `EVENT_Q` | `asyncio.Queue` | 节点 put、SSE 循环 get |
| `T0` | float 秒 | `_ms()` 算相对毫秒，前端排时间线 |
| `RUN_ID` | str | workspace 子目录名 / 前端启动预览参数 |
| `WORKSPACE` | pathlib.Path | 工具层真写文件的落盘根 |
| `FILES` | list[dict] | 产物清单（路径+diff），CTO 综合时引用 |
| `TESTS` | list[dict] | 测试结果，quality_gate/acceptance_check 读 |

**要点**：① 深层节点无需层层传参；② `sse()` 把 dict 序列化成一帧 SSE（`event: x\ndata: {...}\n\n`）。

### 5.4 `backend/app.py`（22 行 · 约 1 分钟）

- **作用**：主入口——组装 FastAPI 应用（CORS 中间件 + 挂 `server.router` + 前端单页路由）。
- **阅读顺序建议**：启动方式在模块 docstring 里（`uvicorn backend.app:app --port 8090`）。

**要点**：前端就一个 `index.html`，由 `FileResponse` 托管。

### 5.5 `backend/store.py`（71 行 · 约 5 分钟）

- **作用**：SQLite 持久层。一张 `runs` 表，每轮交付存一行（事件流 JSON + 最终总结）。
- **关键函数**：

| 函数 | 行号(~) | 作用 |
|---|---|---|
| `_db()` | 136 | 懒建表（`CREATE TABLE IF NOT EXISTS`） |
| `load_history(session)` | 145 | 按 ts 升序取全会话历史（多轮记忆的来源） |
| `save_run(...)` | 156 | 插入一行：uuid 前 12 位作 id |
| `list_conversations()` | 168 | 按 session 聚合成会话列表（标题=首条 query） |
| `get_conversation(session)` | 180 | 全轮次详情（前端回放动画用） |
| `delete_conversation(session)` | 187 | 删会话 |

**要点**：① 所有读写包 try/except 静默降级——历史功能坏了不影响主流程；② `load_history` 默认不限轮数，同会话可以一直追问。

### 5.6 `backend/server.py`（191 行 · 约 15 分钟）

- **作用**：接口层——所有 `/api/*` 路由 + 最核心的 SSE 流式端点 `/api/run`。
- **阅读顺序建议**：先读 5 个简单路由（health/agents/conversations/launch/files，~63-314），再精读 `/api/run`（~318-387）。
- **关键函数**：

| 函数 | 行号(~) | 作用 |
|---|---|---|
| `_latest_build_context(session)` | 28 | 从历史事件里找同会话最近一次构建（增量修改的上下文） |
| `_seed_workspace_from_previous` | 43 | 把上一版 4 个源码文件复制到新工作区（不带 data.db，防旧 schema 干扰） |
| `launch / stop` | 92/101 | 真起/真停生成的应用（同步端点，FastAPI 走线程池） |
| `run()` → `gen()` | 318 | SSE 生产者-消费者：producer 跑图 put 事件，外层 get + yield |

**要点**：① `FILES.set([])`/`TESTS.set([])` 必须在 `create_task` 之前——子任务复制的是同一列表引用，节点里只 append 就地改，外层才看得到（~329-331 有专门注释）；② 多轮记忆的拼装逻辑在 producer 里（~348-361）：历史回顾 + 上一版契约 + 修改策略三段拼成 `model_input`。

### 5.7 `backend/tools.py`（394 行 · 约 20 分钟）

- **作用**：工具层——给各 Agent 真正能调用的本地工具（免费、零配置、真执行）。
- **阅读顺序建议**：先读 `TOOLSPECS`（~37-71，每个工具的签名+说明，前端"点击查看定义"展示用），再按"文件读写 → 检查 → 测试 → 启动"四段读。
- **关键工具**：

| 工具 | 行号(~) | 真在哪里 |
|---|---|---|
| `write_code_file` | 100 | 真落盘 + `difflib.unified_diff` 产 diff + 线程锁保护 FILES 清单 |
| `_strip_fence` | 90 | 剥掉 LLM 输出里的 ``` 围栏再落盘 |
| `inspect_frontend/backend` | 151/168 | 真读文件比对契约（元素 id/函数名/路由） |
| `static_check` | 239 | 真跑 `py_compile`（语法）+ `pyflakes`（告警）子进程 |
| `run_functional_test` | 307 | 子进程里 TestClient 真打生成的 app：GET→POST→GET 断言读写打通 |
| `run_perf_test` | 338 | 连打 N 次统计 avg/p95/max 延迟 |
| `launch_app` | 355 | 空闲端口起 uvicorn + 14s 健康检查 + 复用已起进程 |

**要点**：① 测试脚本以**代码字符串**（`_SNIP_FUNC`/`_SNIP_PERF`）形式存在，经环境变量 `CONTRACT_JSON` 传契约——杜绝代码拼接注入；② 子进程结果约定打印 `___RESULT___` 后跟一行 JSON（`_run_snippet` 解析）；③ `_LAUNCHED` 记住 run_id→进程，重复点启动复用不重起。

### 5.8 `backend/agent.py`（583 行 · 约 30 分钟 · 核心中的核心）

- **作用**：智能体层——契约机制 + 组织结构定义 + 分层图构建 + 返工循环。
- **阅读顺序建议**：按"契约（~40-156）→ SPEC（~158-225）→ 组织 LEADS（~229-266）→ 事件与 LLM 封装（~276-304）→ 干活（~308-399）→ 组长/CTO（~402-497）→ 建图（~500-513）→ 入口（~533-583）"顺序。
- **关键数据结构**：

| 结构 | 行号(~) | 说明 |
|---|---|---|
| `FALLBACK_CONTRACT` | 41 | 契约解析失败的兜底（极简记账本） |
| `_normalize_contract(c)` | 68 | 校验表名/字段/端点格式，补 label/required/测试值，选 marker_field |
| `_apply_revision_hints(topic, c)` | 107 | 正则识别"新增 X 字段/标题改成 Y"做确定性修改，防 LLM 漏改 |
| `_db_spec/_api_spec/_ui_spec/_js_spec` | 163-225 | 四个工程师的岗位说明书，按契约动态渲染（连"sqlite 游标返回 tuple 不能直接 dict(row)"的坑都写进去了） |
| `LEADS` | 229 | 3 个组长 × 8 成员的组织结构（id/提示词/工具/产出文件） |
| `DeliveryState` | 269 | 图状态：topic/contract/leads_done/final |
| `_run_worker(...)` | 308 | 按 kind 分派：coder 写文件 / review 审查 / functional 测试 / perf 压测 |
| `_make_lead_node(L)` | 419 | 组长节点闭包：并行派活 → 自检 → 打回重写≤2 次 → LLM 小结 |
| `cto_synthesize(state)` | 466 | CTO 验收（acceptance_check）+ 综合交付总结 |
| `_build_graph()` | 500 | StateGraph 手搭：START→前端→后端→测试→CTO→END |
| `run_events(...)` | 533 | 对外入口：先定契约再跑图，返回 {final, contract} |

**要点**：① `code_model`（temperature=0.1）写代码定契约，`prose_model`（0.5）写总结——低温求稳可编译，高温求语言自然；② `_llm_or_fallback` 让汇总/审查类文本可降级——代码已产出、测试已过时，不被临时网络错误打断整轮；③ 测试组专属"测试驱动返工"分支（~426-439）：功能测试失败 → 带堆栈跨组打回后端重写 → 重测一次，这是静态编译查不出的实现级 bug（如 `dict(tuple)`）的最终防线；④ 文件末尾有 `__main__` 自测入口（"待办清单"需求全链验证）。

---

## 6. 关键设计模式解析

### 模式一：契约驱动生成（Contract-Driven Generation）

```
                ┌──────────────┐
   用户需求 ──→ │  技术总监 LLM  │ ──→ 契约 JSON ──┬──→ _db_spec()   ──→ DB 工程师
                │  _make_contract│                ├──→ _api_spec()  ──→ API 工程师
                └──────┬───────┘                ├──→ _ui_spec()   ──→ UI 工程师
                       │ 解析失败                └──→ _js_spec()   ──→ 交互工程师
                       ▼
                FALLBACK_CONTRACT（记账本兜底）
```

**意图**：解决"多 Agent 各写各的、联调对不上"的问题。四个工程师的 SPEC 都从同一份契约渲染，db 函数名、端点路径、页面元素 id 天然一致——审查工具（`inspect_*`）也按同一契约核对，形成闭环。

### 模式二：固定骨架 + 动态内容（Pin the Skeleton）

**意图**：让"生成任意软件"仍然可靠。变的只是契约内容（表名/字段/元素），不变的是：文件四件套（db.py/app.py/static/index.html/static/app.js）、db 函数五件套（init_db/add_item/list_items/delete_item/stats）、端点模式、页面元素约定。测试脚本才能用统一的断言模板真打生成的 app（`agent.py` 模块 docstring 里明确列了这张"固定清单"）。

### 模式三：测试驱动返工（Test-Driven Rework）

```
功能测试失败
   │  带错误堆栈(截取末 600 字符)
   ▼
跨组打回:测试组长 → 后端组 2 名工程师(feedback=错误详情)
   │  asyncio.gather 并行重写
   ▼
重测一次(不无限循环)
```

**意图**：静态编译查不出实现级 bug（如 SQLite 游标返回 tuple 却 `dict(row)`），只有真实 HTTP 请求能逼出来。打回信息带真实错误堆栈，工程师 LLM 才能"对症下药"。

### 模式四：生产者-消费者 SSE（Producer-Consumer Streaming）

```python
q = asyncio.Queue()
asyncio.create_task(producer())   # 跑 Agent 图,事件 put 进 q
while True:
    d = await q.get()             # 外层逐帧取出
    yield sse(d["event"], ...)    # 边产边发,不断不缓存
```

**意图**：一次交付几分钟长，SSE 必须边产边发才能让前端实时画树状动画。事件同时 append 进 `events` 列表，结束后整体存 SQLite——前端回放历史动画复用同一数据。

### 模式五：contextvars 请求隔离（Request-Scoped Context）

**意图**：不把 6 个上下文对象沿着"节点→工具→子进程"层层传参。代价是必须小心两件事：① 引用类型（FILES/TESTS）要在 `create_task` 前 set 好，子任务共享同一引用、只就地 append；② 空列表 falsy——`_rec_test` 里"切忌 `(x or []).append()`"的注释就是在防这个坑（会往丢弃的临时列表里 append）。

### 模式六：降级链（Graceful Degradation，处处有 Plan B）

| 失败点 | 降级方案 | 代码位置 |
|---|---|---|
| 契约解析失败 | 回退 FALLBACK_CONTRACT | agent.py ~139 |
| LLM 漏改契约 | 正则确定性修改兜底 | agent.py ~107 |
| 汇总/审查 LLM 失败 | `fallback` 文本 + 降级说明 | agent.py ~297 |
| pyflakes 未安装 | 只做 py_compile，不报错 | tools.py ~264 |
| 功能测试失败 | 跨组打回重写再测 | agent.py ~426 |
| 组长自检不过 | 打回重写（≤2 次） | agent.py ~444 |

**意图**：整条链路上任何一个 LLM/环境环节抖动，交付流程都不会整体中断——要么降级继续，要么定向返工。

---

## 7. 配置系统详解

配置唯一来源：项目根 `.env`（`python-dotenv` 读取，`override=True`）。模板见 `.env.example`。

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | 无（缺失启动即报错） | OpenRouter 密钥，https://openrouter.ai/keys |
| `MODEL` | ⬜ | `deepseek/deepseek-v4-flash` | 模型名（`.env.example` 示例为 `deepseek/deepseek-v4-pro`；代码兜底是 flash） |
| `OPENROUTER_BASE_URL` | ⬜ | `https://openrouter.ai/api/v1` | OpenAI 兼容端点，可换其他供应商 |

加载优先级：`.env`（override=True）→ 代码兜底值 → 都没有则 `raise RuntimeError`。没有其他配置文件、没有系统环境变量依赖、没有命令行参数——刻意保持"单一真相源"。

💡 最低可用配置：只填 `OPENROUTER_API_KEY` 一项即可跑起来。

---

## 8. 如何运行和测试

### 8.1 环境准备

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.12 | requirements.txt 注释推荐 3.12 |
| pip 依赖 | 见 requirements.txt | langgraph 1.2.4 / langchain-openai 1.2.2 / fastapi 0.136.3 / uvicorn 0.49.0 / httpx / pyflakes / python-dotenv |

```bash
# ① 建虚拟环境并装依赖（Windows PowerShell 下激活命令为 .venv\Scripts\activate）
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip install -r requirements.txt

# ② 配置密钥
cp .env.example .env                            # Windows: copy .env.example .env
#   编辑 .env，填入你的 OPENROUTER_API_KEY
```

### 8.2 启动主服务

```bash
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8090
# 浏览器打开 http://127.0.0.1:8090
```

### 8.3 体验核心链路

1. 页面输入需求（如"做一个待办清单：能添加任务（标题/优先级）、查看列表、删除任务"）→ 点开始
2. 观察树状动画：技术总监定契约 → 三组长依次点亮 → 组内成员并行 → 工具调用/写文件/测试结果实时弹出
3. 交付完成后读技术总监的 Markdown 总结 → 点「🚀 启动预览」在 iframe 里用真应用
4. 追问一句"把标题改成『我的番茄钟』"→ 观察增量修改（workspace 播种 + 契约沿用）

### 8.4 自测（不开浏览器验证全链路）

```bash
.venv/bin/python -m backend.agent
# 用"待办清单"需求跑一遍动态契约全链,终端打印全部事件与产物清单
```

### 8.5 手动查数据

```bash
sqlite3 backend/data/runs.db "SELECT id, session, query FROM runs ORDER BY ts DESC;"
# workspace/<run_id>/ 下是每次交付的真实源码
```

---

## 9. 复刻建议与学习路线

> 起点沿用 quick_start_files 的数据驱动结论：从 `backend/__init__.py`（入口，模块地图）开始，随后进入 `agent.py` 与 `app.py`。

| 阶段 | 做什么 | 产出 | 耗时估计 |
|---|---|---|---|
| 阶段 1：跑通 | 装依赖、配 key、起服务、完整体验一次交付；`python -m backend.agent` 看事件流 | 直觉认知："分层图长什么样" | 0.5 天 |
| 阶段 2：读图 | 按 5.8 的行号顺序精读 agent.py，手画 StateGraph 的节点与边；对照 `_make_lead_node` 理解返工循环 | 一张手绘架构图 | 1 天 |
| 阶段 3：读工具 | 精读 tools.py 的四个"真执行"工具；本地跑一次 `run_functional_test` 的子进程片段看 `___RESULT___` 协议 | 理解"真测试"如何防 LLM 糊弄 | 1 天 |
| 阶段 4：读流式 | 精读 server.py 的 `/api/run`：contextvars 设置顺序、producer/consumer、多轮记忆拼装 | 能回答"为什么 FILES.set 要在 create_task 前" | 0.5 天 |
| 阶段 5：动手改 | 小改：给契约加一个 `dark_mode` 字段并让 inspect_frontend 核对；中改：把测试驱动返工改成最多 2 次；大改：新增一个"运维组长"节点（部署检查） | 属于你自己的分层图 | 2-3 天 |

**学习资源**：

| 资源 | 用途 |
|---|---|
| [LangGraph 官方文档](https://langchain-ai.github.io/langgraph/) | StateGraph / 节点 / 边 / compile 的权威解释 |
| [FastAPI TestClient](https://fastapi.tiangolo.com/tutorial/testing/) | tools.py 功能测试的原理 |
| [Python contextvars](https://docs.python.org/3/library/contextvars.html) | 理解 per-request 隔离 |
| SSE(MDN) | `text/event-stream` 帧格式 |

---

## 10. 常见问题

**Q1：为什么手搭 StateGraph，而不用 LangGraph 自带的 create_supervisor？**
A：`create_supervisor` 把控制流交给模型发 handoff，在 deepseek 这类模型上不稳定（agent.py 模块 docstring 明确说明）。手搭图把"谁交给谁"写死在边里，编排 100% 确定；组长/工程师仍是真 LLM、工具仍真执行——确定性和智能各取所长。

**Q2：工程师为什么没有联网检索工具？**
A：本案例定位是"纯设计/规划推理 + 本地真执行"。检索会引入不可控噪音，而"写一个单表 Web 应用"所需的知识 LLM 内置已足够（`__init__.py` 有说明）。

**Q3：契约解析失败会怎样？**
A：回退到 `FALLBACK_CONTRACT`（极简记账本），并返回 `ok=False`。流程不中断，用户仍能看到一次完整交付；前端契约事件里能看到 ok 标记。

**Q4：为什么 FILES/TESTS 要在 create_task 之前 set 空列表？**
A：asyncio 子任务复制的是 context 当前值——先 set 再 create_task，子任务才拿到**同一个列表对象**；节点里只 append（就地改），外层和工具层才能看到彼此的追加。若在节点里再 `.set()` 新列表，外层就看不见了。同理 `_rec_test` 注释里的"切忌 `(x or []).append()`"：None 时 `or []` 会新建临时列表，append 完就丢。

**Q5：write_code_file 为什么要线程锁？**
A：组内并行（`asyncio.gather`）时，LangChain 把同步工具丢进线程池跑，多个线程可能同时就地修改 FILES 清单；`_files_lock` 保护"替换同名文件记录"的读-改-写过程。

**Q6：测试脚本为什么写成字符串常量，而不是函数？**
A：测试必须在**子进程**里以生成的应用为 cwd 运行（隔离导入环境、真打真 app）。契约经环境变量 `CONTRACT_JSON` 传入而非拼进代码字符串——既杜绝注入，也让同一段脚本能测任意契约的应用。子进程打印 `___RESULT___` + 一行 JSON，父进程解析（`_run_snippet`）。

**Q7：一轮交付结束后，Agent 的进程/应用还在吗？**
A：交付产物是静态文件（workspace/<run_id>/）。只有点「启动预览」才会用 uvicorn 起真进程，`_LAUNCHED` 记住 run_id→进程映射，重复点复用；「停止」或服务重启后由 `stop_app`/进程生命周期自然回收。

**Q8：多轮追问时怎么保证"增量修改"而不是重新生成？**
A：三重保险：① `_latest_build_context` 找到上一版契约与 run_id；② `_seed_workspace_from_previous` 把上一版 4 个源码文件播种到新工作区，工程师 prompt 里带上"基于已有文件最小必要修改"；③ `_apply_revision_hints` 用正则把"标题改成 X/增加 Y 字段"这类确定性修改直接落实，防 LLM 漏改契约导致测试按旧 schema 跑。

**Q9：能看到每个 Agent 的系统提示词吗？**
A：能。`GET /api/agents` 返回技术总监 + 3 组长 + 8 成员的角色、系统提示词与工具列表（coder 成员的 SPEC 用回退契约渲染作示例）；前端点节点即看。

---

## 附录：关键术语对照表

| 英文 | 中文 | 说明 |
|---|---|---|
| Agent | 智能体 | LLM + 系统提示词 + 工具的组合 |
| Lead / Worker | 组长 / 成员 | 本项目两级人事：3 组长、8 成员 |
| Contract | 接口契约 | 动态生成的 JSON 任务书（表/字段/端点/元素） |
| StateGraph | 状态图 | LangGraph 的图编排原语，本项目手搭 |
| Node / Edge | 节点 / 边 | 节点=组长或 CTO；边=固定的汇报顺序 |
| SSE (Server-Sent Events) | 服务器推送事件 | 单向流式协议，前端动画的数据来源 |
| contextvars | 上下文变量 | per-request 隔离的"独立办公室" |
| TestClient | 测试客户端 | FastAPI 自带，进程内真打 HTTP 请求 |
| pyflakes | 静态检查器 | 找未用变量/未定义名等告警 |
| unified diff | 统一差异格式 | write_code_file 产出的变更对比 |
| handoff | 移交 | Supervisor 模式中模型自主决定"交给谁"（本项目弃用） |
| rework | 返工 | 组长/测试打回重写，限 2 次 |
| marker_field | 特征字段 | 功能测试往 TEXT 字段塞特征值、读回断言用 |

---

*本导读由 Code Explain Expert 生成 · 基于 2026-08 源码版本 · 全文 8 个源码文件约 1340 行*
