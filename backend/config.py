import os
from dotenv import load_dotenv

# 从项目根目录加载 .env 文件
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

# LLM Configuration — 模型配置统一由界面录入数据库，config 不再硬编码 LLM_MODELS / DEFAULT_LLM_MODEL
# （原 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL / DEFAULT_LLM_MODEL .env 变量已废弃）

# Database — 相对路径统一相对于 onto-eval 项目根目录（BASE_DIR），避免在 backend/ 下启动时路径错位
def _build_database_url() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        db_path = os.path.join(BASE_DIR, "backend", "data", "evaluation.db")
    elif raw.startswith("sqlite+aiosqlite:///./"):
        rel = raw.replace("sqlite+aiosqlite:///./", "", 1)
        db_path = os.path.join(BASE_DIR, rel.replace("/", os.sep))
    elif raw.startswith("sqlite:///./"):
        rel = raw.replace("sqlite:///./", "", 1)
        db_path = os.path.join(BASE_DIR, rel.replace("/", os.sep))
    else:
        return raw
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return "sqlite+aiosqlite:///" + db_path.replace("\\", "/")


DATABASE_URL = _build_database_url()


def is_sqlite_url(url: str | None = None) -> bool:
    u = url or DATABASE_URL
    return u.startswith("sqlite")


def is_postgresql_url(url: str | None = None) -> bool:
    u = url or DATABASE_URL
    return u.startswith("postgresql")


def alembic_database_url(url: str | None = None) -> str:
    """Alembic 迁移使用同步 psycopg2 驱动。"""
    u = url or DATABASE_URL
    if "+asyncpg" in u:
        return u.replace("+asyncpg", "+psycopg2")
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+psycopg2://", 1)
    if "+aiosqlite" in u:
        return u.replace("+aiosqlite", "")
    return u


def async_database_url(url: str | None = None) -> str:
    """应用 AsyncEngine 使用 asyncpg。"""
    u = url or DATABASE_URL
    if "+psycopg2" in u:
        return u.replace("+psycopg2", "+asyncpg")
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    return u

# Evaluation Metrics Configuration
# Format: {metric_name: threshold}
METRICS_CONFIG = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.8,
    "correctness": 0.8,
    # 运行时因子：阈值为「上限」（毫秒 / Token 数），越低越好
    "inference_latency": float(os.getenv("METRIC_THRESHOLD_INFERENCE_LATENCY_MS", "120000")),
    "tokens_total": float(os.getenv("METRIC_THRESHOLD_TOKENS_TOTAL", "1000000")),
}

# 本体包参照源检索预算(管线层统一解析:按问题 bigram 检索 top-K 片段喂给 judge,非全量以省 token)
ONTO_REF_TOP_K = int(os.getenv("ONTO_REF_TOP_K", "4"))
ONTO_REF_MAX_CHARS = int(os.getenv("ONTO_REF_MAX_CHARS", "6000"))

# Which metrics to run
_DEFAULT_ENABLED = "faithfulness,answer_relevancy,correctness,inference_latency,tokens_total"
ENABLED_METRICS = os.getenv("ENABLED_METRICS", _DEFAULT_ENABLED).split(",")

# 内存缓存：数据库中的 LLM 配置
_db_llm_models_cache = {}

# 评测任务级别的 LLM 调用参数临时覆盖（使用 contextvars 保证异步任务隔离）
import contextvars
_llm_call_override_var = contextvars.ContextVar("llm_call_override", default={})


def set_llm_call_override(max_tokens: int = None, temperature: float = None):
    """设置当前异步任务上下文的 LLM 调用参数覆盖"""
    override = {}
    if max_tokens is not None:
        override["max_tokens"] = max_tokens
    if temperature is not None:
        override["temperature"] = temperature
    _llm_call_override_var.set(override)


def clear_llm_call_override():
    """清除当前异步任务上下文的 LLM 调用参数覆盖"""
    _llm_call_override_var.set({})


def get_llm_call_override() -> dict:
    """获取当前异步任务上下文的 LLM 调用参数覆盖"""
    return dict(_llm_call_override_var.get())


def update_llm_models_cache(models_data: dict):
    """更新内存中的 LLM 模型配置缓存"""
    global _db_llm_models_cache
    _db_llm_models_cache.clear()
    _db_llm_models_cache.update(models_data)
    try:
        from evaluators.llm_client import clear_llm_client_cache
        clear_llm_client_cache()
    except ImportError:
        pass


def get_llm_config(model_key: str = None) -> dict:
    """
    获取指定模型的配置（仅从数据库缓存读取，不回退 .env）

    当 model_key="auto" 时，返回模型池中第一个可用模型的配置。

    Args:
        model_key: 模型标识 (glm / auto)

    Returns:
        包含 api_key, base_url, model 的配置字典；无匹配时返回空配置
    """
    if model_key is None:
        model_key = get_default_model_key()

    # Auto 模式：返回模型池中第一个可用模型配置（用于日志/显示）
    if model_key.strip().lower() == "auto":
        pool = get_model_pool()
        if pool:
            return pool[0]["config"]
        return {"api_key": "", "base_url": "", "model": "", "name": ""}

    if model_key in _db_llm_models_cache:
        db_cfg = _db_llm_models_cache[model_key]
        return {
            "name": db_cfg.get("name") or "",
            "provider": db_cfg.get("provider"),
            "base_url": db_cfg.get("base_url") or "",
            "model": db_cfg.get("model") or "",
            "api_key": (db_cfg.get("api_key") or "").strip(),
            "is_default": db_cfg.get("is_default", False),
            "max_tokens": db_cfg.get("max_tokens") or 4096,
            "temperature": db_cfg.get("temperature") if db_cfg.get("temperature") is not None else 0.0,
            "timeout": db_cfg.get("timeout") if db_cfg.get("timeout") is not None else 180.0,
        }

    return {"api_key": "", "base_url": "", "model": "", "name": ""}


def get_model_display_name(model_key: str | None) -> str:
    """获取模型展示名（数据库缓存优先，未命中回退到 model_key 本身）。

    取代硬编码 LLM_MODELS 字典，用于日志 / 响应显示场景。
    """
    key = model_key or ""
    cfg = get_llm_config(key)
    name = (cfg.get("name") or "").strip()
    return name or (key or "unknown")


def get_available_models() -> list:
    """获取所有可用模型的列表（含 is_default 标记和 Auto 虚拟模型）"""
    models = []
    # Auto 模型池选项（始终显示）
    from config import get_model_pool
    pool = get_model_pool()
    if len(pool) >= 1:
        models.append({
            "key": "auto",
            "name": f"Auto（模型池：{len(pool)} 个可用）",
            "is_default": False,
        })
    # 数据库中的模型
    for key, config in _db_llm_models_cache.items():
        models.append({
            "key": key,
            "name": config["name"],
            "is_default": bool(config.get("is_default")),
        })
    return models


def get_default_model_key() -> str:
    """获取默认模型标识（仅从数据库缓存读取，不回退 .env）"""
    # 检查数据库缓存中是否有默认模型
    for key, config in _db_llm_models_cache.items():
        if config.get("is_default"):
            return key
    return "auto"


def get_model_pool() -> list:
    """
    获取所有已配置 API Key 的可用模型列表，用于模型池轮换（仅从数据库缓存读取）。

    Returns:
        [{"key": "glm", "config": {...}}, ...]
    """
    pool = []
    seen_keys = set()
    # 数据库中的模型（有 api_key 的）
    for key, config in _db_llm_models_cache.items():
        api_key = (config.get("api_key") or "").strip()
        if api_key and key not in seen_keys:
            pool.append({"key": key, "config": get_llm_config(key)})
            seen_keys.add(key)
    return pool


# Onto Agent Configuration
# 用于调用 onto-agent 的 API
ONTO_AGENT_BASE_URL = os.getenv("ONTO_AGENT_BASE_URL", "http://localhost:5055")
ONTO_AGENT_API_TOKEN = os.getenv("ONTO_AGENT_API_TOKEN", "")
ONTO_AGENT_WORKSPACE = os.getenv("ONTO_AGENT_WORKSPACE", None)
ONTO_AGENT_TIMEOUT = int(os.getenv("ONTO_AGENT_TIMEOUT", "1800"))


def get_onto_agent_config():
    """
    获取 onto-agent 配置
    
    Returns:
        {
            "base_url": "http://localhost:5055",
            "api_token": "...",
            "workspace_id": None,
            "timeout": 1800
        }
    """
    return {
        "base_url": ONTO_AGENT_BASE_URL,
        "api_token": ONTO_AGENT_API_TOKEN,
        "workspace_id": ONTO_AGENT_WORKSPACE,
        "timeout": ONTO_AGENT_TIMEOUT
    }


# Langfuse Configuration (v2.1 trace observability)
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "").rstrip("/")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_DEFAULT_PROJECT = os.getenv("LANGFUSE_DEFAULT_PROJECT", "default").strip()
LANGFUSE_WRITE_PROJECT = os.getenv("LANGFUSE_WRITE_PROJECT", "").strip()
# 多项目：JSON 数组，或改用 LANGFUSE_PROJECTS_FILE 指向配置文件（推荐 ~30 个项目）
LANGFUSE_PROJECTS_RAW = os.getenv("LANGFUSE_PROJECTS", "").strip()
LANGFUSE_PROJECTS_FILE = os.getenv("LANGFUSE_PROJECTS_FILE", "").strip()
# 为 true 时 GET /trace/projects 会按各项目 Key 调 Langfuse 同步展示名（失败则用配置名）
LANGFUSE_SYNC_PROJECT_NAMES = os.getenv("LANGFUSE_SYNC_PROJECT_NAMES", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
LANGFUSE_ENABLED = bool(
    LANGFUSE_HOST
    and (LANGFUSE_PROJECTS_RAW or LANGFUSE_PROJECTS_FILE or (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY))
)
LANGFUSE_REQUEST_TIMEOUT = float(os.getenv("LANGFUSE_REQUEST_TIMEOUT", "30"))

# 评测 Pipeline 是否在 Langfuse 写 onto-eval/* wrapper trace（默认关，避免与 agent_request 双 trace）
EVAL_LANGFUSE_WRAPPER_ENABLED = os.getenv(
    "EVAL_LANGFUSE_WRAPPER_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")

# 运维：手动 drain Trace Sync 队列（设非空则要求 X-Onto-Drain-Token）
TRACE_SYNC_DRAIN_TOKEN = os.getenv("TRACE_SYNC_DRAIN_TOKEN", "").strip()

# C-02：Builder 物化 span 时是否允许 legacy:type:name 回退（默认 true；设 false 强制 agent.* 契约）
BUILDER_ALLOW_LEGACY_AGGREGATE_KEY = os.getenv(
    "BUILDER_ALLOW_LEGACY_AGGREGATE_KEY", "true"
).strip().lower() in ("1", "true", "yes")

# Trace Sync Worker：评测推理结束后自动拉取 Langfuse trace 并物化（唯一物化路径，默认开启）
TRACE_SYNC_WORKER_ENABLED = os.getenv("TRACE_SYNC_WORKER_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
TRACE_SYNC_POLL_INTERVAL_SEC = float(os.getenv("TRACE_SYNC_POLL_INTERVAL_SEC", "10"))
TRACE_SYNC_BATCH_SIZE = int(os.getenv("TRACE_SYNC_BATCH_SIZE", "50"))
TRACE_SYNC_MAX_ATTEMPTS = int(os.getenv("TRACE_SYNC_MAX_ATTEMPTS", "10"))
# 推理结束后等待多少秒才首次尝试拉取 trace（等 Langfuse 异步落库）
TRACE_SYNC_MIN_INGESTION_DELAY_SEC = int(os.getenv("TRACE_SYNC_MIN_INGESTION_DELAY_SEC", "30"))
# 观测数稳定性检查：obs count 连续无变化超过该秒数才认为 trace 上传完成
TRACE_SYNC_STABILITY_WINDOW_SEC = int(os.getenv("TRACE_SYNC_STABILITY_WINDOW_SEC", "60"))
# 稳定性检查最大等待秒数，超时后强制处理（即使 obs count 仍在变化）
TRACE_SYNC_MAX_WAIT_SEC = int(os.getenv("TRACE_SYNC_MAX_WAIT_SEC", "300"))
# 物化不完整（span_nodes=0 但 API 有 observations）独立重试上限，不消耗 attempts
TRACE_SYNC_MAX_RERUN = int(os.getenv("TRACE_SYNC_MAX_RERUN", "3"))
# synced_incomplete 后台补扫最小间隔秒数（避免每轮 drain 都扫全表）
TRACE_SYNC_SWEEP_INTERVAL_SEC = int(os.getenv("TRACE_SYNC_SWEEP_INTERVAL_SEC", "60"))
TRACE_SYNC_SWEEP_BATCH_SIZE = int(os.getenv("TRACE_SYNC_SWEEP_BATCH_SIZE", "5"))
# 补扫回溯窗口（小时），只扫最近 N 小时内 synced_incomplete 的 run
TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS = int(os.getenv("TRACE_SYNC_INCOMPLETE_LOOKBACK_HOURS", "24"))
# Trace 探索：分页与 facets 扫描上限（上千条时由后端聚合，避免前端一次加载全量 trace）
TRACE_LIST_PAGE_SIZE_DEFAULT = int(os.getenv("TRACE_LIST_PAGE_SIZE", "100"))
TRACE_LIST_PAGE_SIZE_MAX = int(os.getenv("TRACE_LIST_PAGE_SIZE_MAX", "200"))
TRACE_FACET_SCAN_PAGE_SIZE = int(os.getenv("TRACE_FACET_SCAN_PAGE_SIZE", "100"))
TRACE_FACET_SCAN_MAX_ITEMS = int(os.getenv("TRACE_FACET_SCAN_MAX_ITEMS", "5000"))

# Log Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def _resolve_log_dir() -> str:
    """相对路径相对于 onto-eval 项目根（BASE_DIR），与 DATABASE_URL 规则一致。"""
    raw = os.getenv("LOG_DIR", "").strip()
    if not raw:
        return os.path.join(BASE_DIR, "backend", "logs")
    if raw.startswith("./"):
        return os.path.join(BASE_DIR, raw[2:].replace("/", os.sep))
    if not os.path.isabs(raw):
        return os.path.join(BASE_DIR, raw.replace("/", os.sep))
    return raw


LOG_DIR = _resolve_log_dir()
LOG_FILE_MAX_SIZE = int(os.getenv("LOG_FILE_MAX_SIZE", 10)) * 1024 * 1024
LOG_FILE_BACKUP_COUNT: int = int(os.getenv("LOG_FILE_BACKUP_COUNT", 5))
