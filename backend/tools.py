"""工具层:给工程师/测试/审查 真正能调用的本地工具(免费、零配置、真执行)。

这些就是各 agent「工具列表」里能点开看定义的那些工具 —— 它们不是摆设:
- write_code_file / read_code_file  真把代码写到磁盘 / 读回来(产真 diff)
- static_check                      真跑 py_compile 语法编译 + pyflakes 静态检查
- run_functional_test               用 FastAPI TestClient 真打生成的 app(POST 再 GET,验证读写打通)
- run_perf_test                     用 TestClient 连打 N 次,统计真实延迟
- launch_app                        真用 uvicorn 把生成的应用跑起来,返回可访问 URL(前端 iframe 预览)

为什么用本地函数工具,而不是远程 MCP:这些等价于「文件系统 MCP / shell MCP」暴露的能力,
但本地实现零配置、零网络、可靠、可控,做教学演示最稳。每个工具都有清晰签名(见 TOOLSPECS),
前端点节点能看到「这个 agent 有哪些工具 + 工具怎么用」。
"""
import os
import re
import sys
import time
import json
import socket
import difflib
import pathlib
import threading
import subprocess
import urllib.request

from .runtime import WORKSPACE, FILES, TESTS

# 组内成员并行写文件时,保护 FILES 清单的就地修改(asyncio 把工具丢到线程池跑,要加锁)
_files_lock = threading.Lock()

# ── 工具定义(给前端「点击查看定义」展示用)──
TOOLSPECS = {
    "write_code_file": {
        "sig": "write_code_file(relpath: str, content: str) -> {path, diff, bytes}",
        "desc": "把代码写到本次工作区的指定相对路径(真落盘到 workspace/<run_id>/),返回绝对路径、与上一版的 unified diff、字节数。"},
    "read_code_file": {
        "sig": "read_code_file(relpath: str) -> str",
        "desc": "读取工作区里已有文件的内容(联调 / 审查时拿来参考真实代码)。"},
    "list_files": {
        "sig": "list_files() -> {count, files[]}",
        "desc": "列出当前工作区已有的文件清单(相对路径 + 字节数),开工前 / 审查时摸清仓库现状。"},
    "static_check": {
        "sig": "static_check() -> {ok, files[], pyflakes}",
        "desc": "对工作区所有 .py 跑 py_compile 语法编译 + pyflakes 静态检查(真执行子进程),返回每个文件的编译结果与告警。"},
    "run_functional_test": {
        "sig": "run_functional_test() -> {ok, checks[]}",
        "desc": "用 FastAPI TestClient 真打生成的 app:GET 首页 → POST 一条留言 → 再 GET 看读不读得到,逐项断言读写是否打通。"},
    "run_perf_test": {
        "sig": "run_perf_test(n: int = 40) -> {avg_ms, p95_ms, ...}",
        "desc": "用 TestClient 连打 N 次 GET /api/messages,统计平均 / p95 / 最大延迟(真测真实代码的响应)。"},
    "launch_app": {
        "sig": "launch_app() -> {url, port, pid}",
        "desc": "用 uvicorn 在空闲端口真起生成的应用,健康检查通过后返回可访问 URL(前端用 iframe 预览实际效果)。"},
    "inspect_frontend": {
        "sig": "inspect_frontend() -> {ok, checks[]}",
        "desc": "前端组长自检:读 index.html / app.js,核对必需元素 id 与接口调用是否齐全(真读文件比对)。"},
    "inspect_backend": {
        "sig": "inspect_backend() -> {ok, checks[]}",
        "desc": "后端组长自检:对 db.py / app.py 跑 py_compile,并核对必需函数名与接口路由是否齐全(真编译+比对)。"},
    "quality_gate": {
        "sig": "quality_gate() -> {ok, checks[]}",
        "desc": "测试组长质量门:汇总代码审查 / 功能 / 性能三项结论,判断本轮能否放行。"},
    "acceptance_check": {
        "sig": "acceptance_check() -> {ok, checks[]}",
        "desc": "技术总监验收:清点产物文件 + 汇总质量结论,判断是否达到可交付标准。"},
}


def toolspecs(names):
    """把工具名列表展开成带签名/说明的列表(graph_info 给前端用)。"""
    return [{"name": n, **TOOLSPECS[n]} for n in names if n in TOOLSPECS]


# ── 工作区辅助 ──
# 工具层不接收工作区参数、一律从上下文自取:暴露给 LLM 的工具签名保持极简(它不需要知道"我在哪干活")
def _ws() -> pathlib.Path:
    ws = WORKSPACE.get()
    if ws is None:
        raise RuntimeError("WORKSPACE 上下文未设置(应在请求开始处 set)")
    return ws


_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n|\n?```\s*$")


def _strip_fence(s: str) -> str:
    """LLM 有时会把代码包在 ```python ... ``` 里,落盘前剥掉围栏。"""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*\n", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return s.strip() + "\n"


# ── 文件读写(产 diff)──
def write_code_file(relpath: str, content: str) -> dict:
    """把内容写到 workspace/<run_id>/<relpath>(真落盘),返回路径 + 与上一版的 diff + 字节数。"""
    ws = _ws()
    p = (ws / relpath).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    new = _strip_fence(content)
    # 空内容不落盘:防 LLM 抽风输出空串把已有文件清空 —— 保留旧文件原样,只刷新清单记录
    if old and not new.strip():
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), old.splitlines(keepends=True),
            fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))
        rec = {"relpath": relpath, "path": str(p), "bytes": len(old.encode("utf-8")),
               "diff": diff, "content": old}
        with _files_lock:
            files = FILES.get()
            if files is not None:
                # [:] 原地替换而非重新赋值:外层(server/agent)持有的是同一列表引用,重新赋值就断线了
                files[:] = [f for f in files if f["relpath"] != relpath] + [rec]
        return {k: rec[k] for k in ("relpath", "path", "bytes", "diff", "content")}
    p.write_text(new, encoding="utf-8")
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{relpath}", tofile=f"b/{relpath}"))
    rec = {"relpath": relpath, "path": str(p), "bytes": len(new.encode("utf-8")),
           "diff": diff, "content": new}
    with _files_lock:                           # 组内并行写多文件,保护清单修改
        files = FILES.get()
        if files is not None:                   # 同名重写则替换(只保留最新),否则追加
            files[:] = [f for f in files if f["relpath"] != relpath] + [rec]
    return {k: rec[k] for k in ("relpath", "path", "bytes", "diff", "content")}


def read_code_file(relpath: str) -> str:
    """读工作区里某个文件的内容;不存在则返回提示串。"""
    p = (_ws() / relpath).resolve()
    return p.read_text(encoding="utf-8") if p.exists() else f"(文件不存在:{relpath})"


def list_files() -> dict:
    """列出当前工作区的文件清单(相对路径 + 字节数),排除缓存 / 日志 / sqlite 临时文件。"""
    ws = _ws()
    skip_suffix = (".pyc", ".db-shm", ".db-wal")
    items = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.name == "_run.log" or p.name.endswith(skip_suffix):
            continue
        items.append({"relpath": str(p.relative_to(ws)), "bytes": p.stat().st_size})
    return {"count": len(items), "files": items}


# ── 组长 / 技术总监 的「检查本组(整体)情况」工具(真读文件 / 真编译 / 真汇总)──
# 这些检查项不再写死某个应用,而是按技术总监动态制定的契约逐项核对。
def inspect_frontend(contract: dict) -> dict:
    """前端组长自检:按契约核对页面元素 id 与脚本接口调用是否齐全。"""
    html = read_code_file("static/index.html")
    js = read_code_file("static/app.js")
    def has_id(h, i):
        return (f'id="{i}"' in h) or (f"id='{i}'" in h)
    checks = [{"name": f"index.html 含「{f.get('label', f['name'])}」输入 id={f['name']}", "pass": has_id(html, f["name"])}
              for f in contract.get("fields", [])]
    # add/list 两个 id 是固定页面骨架的一部分:不管契约怎么变,提交按钮和列表容器必须在
    checks.append({"name": "index.html 含提交按钮 id=add", "pass": has_id(html, "add")})
    checks.append({"name": "index.html 含列表容器 id=list", "pass": has_id(html, "list")})
    for s in contract.get("stats_elems", []):
        checks.append({"name": f"index.html 含统计展示 id={s['id']}", "pass": s["id"] in html})
    base = contract.get("api_base", "")
    checks.append({"name": f"app.js 调用 {base}", "pass": base in js})
    return {"ok": all(c["pass"] for c in checks), "checks": checks}


def inspect_backend(contract: dict) -> dict:
    """后端组长自检:py_compile 编译 + 按契约核对函数名与接口路由。"""
    ws = _ws()
    checks = []
    for rel in ("db.py", "app.py"):
        r = subprocess.run([sys.executable, "-m", "py_compile", rel], cwd=str(ws),
                           capture_output=True, text=True, timeout=20)
        checks.append({"name": f"{rel} 语法编译通过", "pass": r.returncode == 0})
    db = read_code_file("db.py")
    app = read_code_file("app.py")
    for fn in ("init_db", "add_item", "list_items", "delete_item", "stats"):
        checks.append({"name": f"db.py 暴露 {fn}()", "pass": f"def {fn}" in db})
    base = contract.get("api_base", "")
    checks.append({"name": f"app.py 提供 {base}", "pass": base in app})
    checks.append({"name": "app.py 提供 /api/stats", "pass": "/api/stats" in app})
    checks.append({"name": "app.py 提供删除接口(DELETE)", "pass": "delete" in app.lower()})
    return {"ok": all(c["pass"] for c in checks), "checks": checks}


def quality_gate() -> dict:
    """测试组长质量门:汇总本次 TESTS 里的审查 / 功能 / 性能结论。"""
    tests = TESTS.get() or []
    # reversed 取同类最后一次结果:返工重测后以最新结论为准,不翻旧账
    rev = next((t for t in reversed(tests) if t.get("kind") == "review"), None)
    func = next((t for t in reversed(tests) if t.get("kind") == "functional"), None)
    perf = next((t for t in reversed(tests) if t.get("kind") == "perf"), None)
    checks = [
        {"name": "代码审查编译通过", "pass": bool(rev and rev.get("ok"))},
        {"name": "功能测试通过", "pass": bool(func and func.get("ok"))},
        {"name": "性能测试完成", "pass": bool(perf and perf.get("ok"))},
    ]
    return {"ok": all(c["pass"] for c in checks), "checks": checks}


def acceptance_check() -> dict:
    """技术总监验收:清点产物文件 + 汇总质量结论,判断能否交付。"""
    tests = TESTS.get() or []
    files = list_files()
    rev = next((t for t in reversed(tests) if t.get("kind") == "review"), None)
    func = next((t for t in reversed(tests) if t.get("kind") == "functional"), None)
    perf = next((t for t in reversed(tests) if t.get("kind") == "perf"), None)
    checks = [
        {"name": f"产出文件齐全(共 {files['count']} 个)", "pass": files["count"] >= 4},
        {"name": "代码审查通过", "pass": bool(rev and rev.get("ok"))},
        {"name": "功能测试通过(可交付)", "pass": bool(func and func.get("ok"))},
        {"name": "性能达标", "pass": bool(perf and perf.get("ok"))},
    ]
    return {"ok": all(c["pass"] for c in checks), "checks": checks, "files_count": files["count"]}


# ── 在工作区里跑一段 Python 子进程,取末尾 ___RESULT___ 后的 JSON ──
def _run_snippet(snippet: str, timeout: int = 60, env: dict = None) -> dict:
    ws = _ws()
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout, env=e)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时(>{timeout}s)"}
    out = r.stdout or ""
    # 错误信息只截尾部:堆栈头部常常是重复的环境导入日志,真正的异常在最后几百字符
    marker = "___RESULT___"
    if marker in out:
        try:
            return json.loads(out.split(marker, 1)[1].strip().splitlines()[0])
        except Exception as ex:
            return {"ok": False, "error": f"解析结果失败:{ex}", "stdout": out[-600:]}
    return {"ok": False, "error": "子进程未产出结果", "stderr": (r.stderr or "")[-800:]}


# ── 静态检查:py_compile(语法)+ pyflakes(静态告警)──
def static_check() -> dict:
    ws = _ws()
    pys = sorted(str(p.relative_to(ws)) for p in ws.rglob("*.py"))
    results = []
    for rel in pys:
        r = subprocess.run([sys.executable, "-m", "py_compile", rel], cwd=str(ws),
                           capture_output=True, text=True, timeout=20)
        issues = []
        compile_ok = r.returncode == 0
        if not compile_ok:
            tail = (r.stderr or "").strip().splitlines()
            issues.append({"level": "error", "msg": tail[-1] if tail else "语法错误"})
        results.append({"relpath": rel, "compile_ok": compile_ok, "issues": issues})
    # pyflakes(静态告警);没装就跳过,不报错
    flakes = None
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes", *pys], cwd=str(ws),
                           capture_output=True, text=True, timeout=30)
        flakes = ((r.stdout or "") + (r.stderr or "")).strip() or "(无告警)"
        by_file = {x["relpath"]: x for x in results}
        for line in (r.stdout or "").splitlines():
            m = re.match(r"^(.+?):(\d+):(?:\d+:)?\s*(.+)$", line.strip())
            if m and m.group(1) in by_file:
                by_file[m.group(1)]["issues"].append({"level": "warning", "msg": f"L{m.group(2)}: {m.group(3)}"})
    except Exception:
        flakes = "(未安装 pyflakes,仅做了语法编译检查)"
    return {"ok": all(x["compile_ok"] for x in results), "files": results, "pyflakes": flakes}


# ── 功能测试:TestClient 真打 app(读写是否打通)──
_SNIP_FUNC = r'''
import sys, os, json
sys.path.insert(0, os.getcwd())
C = json.loads(os.environ["CONTRACT_JSON"])   # 契约由环境变量传入(JSON,杜绝代码拼接)
res = {"ok": False, "checks": []}
def chk(name, cond): res["checks"].append({"name": name, "pass": bool(cond)})
try:
    import time as _t
    from app import app
    from fastapi.testclient import TestClient
    base = C["api_base"]
    body = dict(C.get("test_post") or {})
    # 毫秒级时间戳做特征值:把"本轮刚写入的记录"与库里的历史数据区分开,读回断言才不会拿旧数据误判通过
    marker = "功能测试_" + str(int(_t.time()*1000))
    mfield = C.get("marker_field")
    if mfield: body[mfield] = marker          # 往 TEXT 字段塞特征值,读回时断言
    with TestClient(app) as c:
        r = c.get("/"); chk("GET / 返回页面(2xx)", 200 <= r.status_code < 300)
        p = c.post(base, json=body)
        chk(f"POST {base} 创建成功(2xx)", 200 <= p.status_code < 300)
        g = c.get(base)
        try: data = g.json()
        except Exception: data = None
        chk(f"GET {base} 返回列表", isinstance(data, list))
        found = isinstance(data, list) and (not mfield or any(marker in json.dumps(m, ensure_ascii=False) for m in data))
        chk("刚创建的记录能被读回(读写打通)", found)
        s = c.get("/api/stats")
        try: st = s.json()
        except Exception: st = None
        chk("GET /api/stats 返回统计 dict", isinstance(st, dict))
    res["ok"] = all(x["pass"] for x in res["checks"])
except Exception as e:
    import traceback
    res["error"] = f"{type(e).__name__}: {e}"
    res["trace"] = traceback.format_exc()[-700:]
print("___RESULT___" + json.dumps(res, ensure_ascii=False))
'''


def run_functional_test(contract: dict) -> dict:
    return _run_snippet(_SNIP_FUNC, timeout=90,
                        env={"CONTRACT_JSON": json.dumps(contract, ensure_ascii=False)})


# ── 性能测试:TestClient 连打 N 次 GET,统计延迟 ──
_SNIP_PERF = r'''
import sys, os, json, time
sys.path.insert(0, os.getcwd())
try:
    from app import app
    from fastapi.testclient import TestClient
    N = int(os.environ.get("PERF_N", "40"))
    BASE = json.loads(os.environ["CONTRACT_JSON"])["api_base"]   # 压测端点来自契约
    with TestClient(app) as c:
        warm = c.get(BASE)  # 预热 + 确认端点存在(避免 404 也被当成"响应"假通过)
        if warm.status_code >= 400:
            raise RuntimeError(f"{BASE} 返回 {warm.status_code}")
        ts = []
        for _ in range(N):
            s = time.perf_counter(); c.get(BASE); ts.append((time.perf_counter()-s)*1000)
    ts.sort()
    res = {"ok": True, "n": N, "avg_ms": round(sum(ts)/len(ts), 2),
           "p95_ms": round(ts[max(0, int(len(ts)*0.95)-1)], 2),
           "min_ms": round(ts[0], 2), "max_ms": round(ts[-1], 2)}
except Exception as e:
    res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
print("___RESULT___" + json.dumps(res, ensure_ascii=False))
'''


def run_perf_test(contract: dict, n: int = 40) -> dict:
    return _run_snippet(_SNIP_PERF, timeout=90,
                        env={"PERF_N": str(n), "CONTRACT_JSON": json.dumps(contract, ensure_ascii=False)})


# ── 启动应用(uvicorn 子进程)+ 健康检查,返回可访问 URL ──
_LAUNCHED = {}   # {run_id: {"proc":Popen, "port":int}}


def _free_port() -> int:
    # bind(0) 让 OS 分发空闲端口,拿到号立即关闭;理论上到 uvicorn 真绑定之间有极小竞态,教学场景可接受
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_app(ws: pathlib.Path, run_id: str) -> dict:
    """在空闲端口真起生成的应用,健康检查通过后返回 URL。重复点同一构建则复用已起进程。"""
    ws = pathlib.Path(ws)
    cur = _LAUNCHED.get(run_id)
    if cur and cur["proc"].poll() is None:
        return {"ok": True, "url": f"http://127.0.0.1:{cur['port']}/", "port": cur["port"],
                "pid": cur["proc"].pid, "reused": True}
    port = _free_port()
    # 应用 stdout/stderr 追加到工作区日志:启动失败时读尾部即可定位原因,不用瞎猜
    logf = open(ws / "_run.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--app-dir", str(ws)],
        cwd=str(ws), stdout=logf, stderr=logf)
    # 14 秒健康检查窗口:uvicorn 冷启动通常秒级,超时即判失败;先看进程死没死,再轮询 HTTP
    deadline = time.time() + 14
    ok = False
    while time.time() < deadline:
        # 进程已退出(典型如生成代码语法错误秒崩),没必要再等健康检查
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            time.sleep(0.4)
    if not ok:
        tail = ""
        try:
            tail = (ws / "_run.log").read_text(encoding="utf-8", errors="ignore")[-1200:]
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
        return {"ok": False, "error": "应用启动失败或健康检查超时,看日志定位", "log": tail}
    _LAUNCHED[run_id] = {"proc": proc, "port": port}
    return {"ok": True, "url": f"http://127.0.0.1:{port}/", "port": port, "pid": proc.pid}


def stop_app(run_id: str) -> bool:
    cur = _LAUNCHED.pop(run_id, None)
    if cur and cur["proc"].poll() is None:
        cur["proc"].terminate()
        return True
    return False
