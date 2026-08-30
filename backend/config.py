"""配置(单一真相源):路径 + LLM 连接参数。

全部来自项目根 .env(python-dotenv 读),不依赖系统环境变量、不读外部 creds。
本文件只提供"值"(MODEL / BASE_URL / KEY / 路径),不实例化任何客户端 ——
LLM 客户端由 agent 层按这些值来建(见 agent.py)。
- 密钥绝不硬编码;.env 不入库(见 .gitignore),仓库里只放 .env.example 模板
- 存储统一 backend/data/,日志统一 backend/logs/
"""
import pathlib
from dotenv import load_dotenv, dotenv_values

# 路径锚定 __file__ 而非 cwd:uvicorn 可以从任意工作目录启动,路径绝不能跟着 cwd 漂移
BACKEND = pathlib.Path(__file__).resolve().parent   # backend/
ROOT = BACKEND.parent                                # hierarchical-software-delivery/
FRONTEND = ROOT / "frontend"
DATA = BACKEND / "data"
LOGS = BACKEND / "logs"
WORKSPACE_ROOT = BACKEND / "workspace"               # 工程师真写代码落到这里:workspace/<run_id>/
DATA.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)
WORKSPACE_ROOT.mkdir(exist_ok=True)
RUNS_DB = str(DATA / "runs.db")

# override=True:.env 永远压过系统环境变量 —— 单一真相源,免得 shell 里残留的旧 KEY 造出"配置灵异事件"
load_dotenv(ROOT / ".env", override=True)
_cfg = dotenv_values(ROOT / ".env")

MODEL = _cfg.get("MODEL") or "deepseek/deepseek-v4-flash"
BASE_URL = _cfg.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
KEY = _cfg.get("OPENROUTER_API_KEY") or ""
# fail-fast:与其等请求飞一半在第一次 LLM 调用时才炸,不如启动瞬间就报错并给出修复指引
if not KEY:
    raise RuntimeError("缺少 OPENROUTER_API_KEY —— 请先 `cp .env.example .env` 并在 .env 里填入你的 key")
