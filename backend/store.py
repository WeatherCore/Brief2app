"""持久层:SQLite(backend/data/runs.db)。

一张表 runs,每一轮交付规划存一行:事件流(供复现树状动画)+ 最终交付计划。
撑起:历史列表 / 详情 / 删除 + 多轮记忆(load_history 重建上下文,服务重启不丢)。
"""
import json
import time
import uuid
import sqlite3

from .config import RUNS_DB


def _db():
    # 每次操作各开各的连接、用完即关:sqlite3 默认禁止跨线程共用同一连接(check_same_thread),
    # 短连接是最省心的线程安全姿势;顺带 CREATE IF NOT EXISTS 兜底建表
    c = sqlite3.connect(RUNS_DB)
    c.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, ts REAL, session TEXT, query TEXT, events TEXT, brief TEXT)")
    return c


# 模块导入时建一次表并立即关闭:启动瞬间验证 runs.db 可写,别等第一轮交付存档才发现磁盘/权限出问题
_db().close()


def load_history(session: str, limit: int = None):
    """默认不限轮数:同会话可以一直追问下去,历史全量带入。"""
    try:
        c = _db()
        rows = c.execute("SELECT query, brief FROM runs WHERE session=? ORDER BY ts ASC", (session,)).fetchall()
        c.close()
        return rows[-limit:] if limit else rows
    except Exception:
        return []


def save_run(session: str, query: str, events: list, brief: str):
    """整轮交付落库。全程静默失败:历史存档是"锦上添花",不能因磁盘满/表锁掀翻主流程。"""
    try:
        c = _db()
        c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:12], time.time(), session, query,
                   json.dumps(events, ensure_ascii=False), brief))
        c.commit()
        c.close()
    except Exception:
        pass


def list_conversations():
    c = _db()
    rows = c.execute("SELECT session, query, ts FROM runs ORDER BY ts ASC").fetchall()
    c.close()
    conv = {}
    # 按 session 内存聚合:多轮折叠成一个会话卡片,title 取该会话首条 query,轮数/最后活跃时间顺手带出
    for session, query, ts in rows:
        conv.setdefault(session, {"session": session, "title": query, "n_turns": 0, "last_ts": ts})
        conv[session]["n_turns"] += 1
        conv[session]["last_ts"] = ts
    return sorted(conv.values(), key=lambda x: x["last_ts"], reverse=True)


def get_conversation(session: str):
    c = _db()
    rows = c.execute("SELECT id, ts, query, events, brief FROM runs WHERE session=? ORDER BY ts ASC", (session,)).fetchall()
    c.close()
    # events 列在库里是 JSON 字符串,读出来要 parse——前端"回放历史动画"直接吃这个结构
    return [{"id": r[0], "ts": r[1], "query": r[2], "events": json.loads(r[3]), "brief": r[4]} for r in rows]


def delete_conversation(session: str):
    c = _db()
    n = c.execute("DELETE FROM runs WHERE session=?", (session,)).rowcount
    c.commit()
    c.close()
    return n
