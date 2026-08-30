"""per-request 运行期上下文(让深层节点无需层层传参,就能拿到事件队列 / 本次工作区 / 产物清单)。

和 flagship 一样用 contextvars:每个 HTTP 请求一套独立的上下文,互不串。
本案例的工程师要【真写文件、真跑测试】,所以多了:
- RUN_ID    本次构建的唯一 id(也是 workspace 子目录名、前端「启动预览」要带的参数)
- WORKSPACE 本次代码落盘的目录(workspace/<run_id>/)
- FILES     本次已写出的文件清单(路径 + diff),技术总监综合 / 前端「产出文件」面板都读它
- TESTS     本次测试结果(功能 / 性能),技术总监综合时引用
"""
import json
import time
import contextvars

# 本次请求的实时事件队列:节点把层级事件 put 进来,SSE 循环 get 出去发前端画树状动画
EVENT_Q = contextvars.ContextVar("event_q", default=None)
# 本次请求起点(秒)。_ms() 算相对毫秒数,前端据此排时间线
T0 = contextvars.ContextVar("t0", default=0.0)

# 本次构建 id + 工作区目录(工程师写的代码真落到 WORKSPACE 下)
RUN_ID = contextvars.ContextVar("run_id", default="")
WORKSPACE = contextvars.ContextVar("workspace", default=None)   # pathlib.Path
# 本次产物清单 / 测试结果(随节点执行不断追加)
FILES = contextvars.ContextVar("files", default=None)           # list[dict]
TESTS = contextvars.ContextVar("tests", default=None)           # list[dict]
# 全部 ContextVar 都带空默认值:让"图外"场景(模块导入、自测)也能跑通,
# 用到时判空兜底 —— 例如 _emit 里队列是 None 就静默丢事件,绝不掀翻主流程


def _ms():
    """距本次请求开始的相对毫秒数(每个事件都带它)。"""
    return int((time.time() - T0.get()) * 1000)


def sse(event, data):
    """序列化成一帧 SSE。格式:event: <名>\\n data: <json>\\n\\n。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
