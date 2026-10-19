"""
Langfuse 多项目注册表 — 每个项目独立 API Key（与 Langfuse UI 项目切换一致）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import (
    BASE_DIR,
    LANGFUSE_DEFAULT_PROJECT,
    LANGFUSE_HOST,
    LANGFUSE_PROJECTS_FILE,
    LANGFUSE_PROJECTS_RAW,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
)
from logger import log
from services.langfuse_client import LangfuseClient


# 兼容旧配置：从 .env 读取默认 host，新项目配置可覆盖
_DEFAULT_LANGFUSE_HOST = (LANGFUSE_HOST or "").rstrip("/")

# 兼容 onto-agent 历史上报的"幻影" Langfuse 项目名 ——
# 此类名称在 Langfuse 服务端不存在（onto-agent 端 SDK 把 project 字段写成了
# onto-agent 内部别名，但 trace 实际落到了默认项目）。这里按业务规则把它们
# 映射到 eval 注册表中真实可用的项目。
_PROJECT_ALIASES: Dict[str, str] = {
    "df-agent-project": "df-interface",
    "cc-consume-2.0": "cc-consumer2-0-bq-project",
    "cc-consumer-2.0": "cc-consumer2-0-bq-project",
}


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: str):
        self.project_id = project_id
        super().__init__(
            f"Langfuse 项目未注册: {project_id}"
            "，请在 langfuse.projects.json 中配置该项目的 host/public_key/secret_key"
            "，或在 _PROJECT_ALIASES 中添加别名映射"
        )


@dataclass(frozen=True)
class LangfuseProjectConfig:
    """onto-eval 侧项目配置（id 为内部标识，非 Langfuse 的 projectId）
    host: Langfuse 服务器地址，须在 langfuse.projects.json 中配置
    organization: Langfuse 组织名（如 cc-consumer-2.0），与 langfuse_project_id 共同构成 org/project 映射
    langfuse_project_id: Langfuse 实际项目 ID（如 perf），与 organization 组合定位数据集
    """

    id: str
    name: str
    public_key: str
    secret_key: str
    host: Optional[str] = None
    organization: Optional[str] = None
    langfuse_project_id: Optional[str] = None


def _normalize_project_entry(raw: Dict[str, Any], index: int) -> LangfuseProjectConfig:
    pid = str(raw.get("id") or raw.get("key") or f"project_{index}").strip()
    name = str(raw.get("name") or pid).strip()
    public_key = str(raw.get("public_key") or "").strip()
    secret_key = str(raw.get("secret_key") or "").strip()
    if not pid or not public_key or not secret_key:
        raise ValueError(
            f"Langfuse 项目配置第 {index} 项缺少 id/name/public_key/secret_key: {raw!r}"
        )
    host = raw.get("host")
    if host is not None:
        host = str(host).strip().rstrip("/") or None
    organization = raw.get("organization")
    if organization is not None:
        organization = str(organization).strip() or None
    langfuse_project_id = raw.get("langfuse_project_id")
    if langfuse_project_id is not None:
        langfuse_project_id = str(langfuse_project_id).strip() or None
    return LangfuseProjectConfig(
        id=pid,
        name=name,
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        organization=organization,
        langfuse_project_id=langfuse_project_id,
    )


def load_langfuse_project_configs() -> List[LangfuseProjectConfig]:
    """从 LANGFUSE_PROJECTS JSON、LANGFUSE_PROJECTS_FILE 或 legacy 单 Key 加载"""
    file_path = LANGFUSE_PROJECTS_FILE
    raw_json = LANGFUSE_PROJECTS_RAW

    items: List[Dict[str, Any]] = []
    file_loaded = False
    if file_path:
        path = file_path if os.path.isabs(file_path) else os.path.join(BASE_DIR, file_path)
        if not os.path.isfile(path):
            log(
                "WARN",
                "LANGFUSE",
                "LANGFUSE_PROJECTS_FILE 不存在，回退到 LANGFUSE_PROJECTS 或单 Key 配置",
                path=path,
            )
        else:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError("LANGFUSE_PROJECTS_FILE 必须为 JSON 数组")
            items = loaded
            file_loaded = True
    if not items and raw_json:
        loaded = json.loads(raw_json)
        if not isinstance(loaded, list):
            raise ValueError("LANGFUSE_PROJECTS 必须为 JSON 数组")
        items = loaded
    # Legacy 单 Key 回退：仅当 LANGFUSE_PROJECTS_FILE 未配置（或文件不存在）且
    # LANGFUSE_PROJECTS 未配置时才生效。若项目文件已成功加载（即使是空数组 []），
    # 说明用户已显式清空项目列表，不应再凭 LANGFUSE_PUBLIC_KEY/SECRET_KEY 重建幻影项目。
    if not items and not file_loaded and not raw_json and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
        items = [
            {
                "id": LANGFUSE_DEFAULT_PROJECT or "default",
                "name": os.getenv("LANGFUSE_DEFAULT_PROJECT_NAME", "默认项目"),
                "public_key": LANGFUSE_PUBLIC_KEY,
                "secret_key": LANGFUSE_SECRET_KEY,
            }
        ]

    configs = [_normalize_project_entry(item, i) for i, item in enumerate(items)]
    seen: set[str] = set()
    for cfg in configs:
        if cfg.id in seen:
            raise ValueError(f"Langfuse 项目 id 重复: {cfg.id}")
        seen.add(cfg.id)
    return configs


class LangfuseProjectRegistry:
    def __init__(self, projects: List[LangfuseProjectConfig]):
        self._projects: Dict[str, LangfuseProjectConfig] = {p.id: p for p in projects}
        self._clients: Dict[str, LangfuseClient] = {}
        default = (LANGFUSE_DEFAULT_PROJECT or "").strip()
        if default and default in self._projects:
            self._default_project_id = default
        elif projects:
            self._default_project_id = projects[0].id
        else:
            self._default_project_id = ""

    @property
    def enabled(self) -> bool:
        return bool(LANGFUSE_HOST and self._projects)

    @property
    def default_project_id(self) -> str:
        return self._default_project_id

    def resolve_project_id(self, project: Optional[str]) -> str:
        raw = (project or "").strip() or self._default_project_id
        if not raw:
            raise ProjectNotFoundError(project or "(empty)")
        # 1) 精确别名映射（onto-agent 幻影项目名等）
        pid = _PROJECT_ALIASES.get(raw, raw)
        if pid in self._projects:
            return pid
        # 2) 大小写/分隔符不敏感匹配（df_interface ↔ df-interface ↔ DF-Interface）
        normalized = raw.lower().replace("_", "-").replace(" ", "-")
        for key, cfg in self._projects.items():
            if key.lower().replace("_", "-").replace(" ", "-") == normalized:
                return key
            if (cfg.name or "").lower().replace("_", "-").replace(" ", "-") == normalized:
                return key
        # 3) 前缀匹配：用户输入 "langfuse_df-interface" 等含前缀变体
        for key in self._projects:
            if normalized.endswith(key.lower().replace("_", "-")):
                return key
        raise ProjectNotFoundError(raw)

    def all_configs(self) -> List[LangfuseProjectConfig]:
        return list(self._projects.values())

    def list_projects_public(self) -> List[Dict[str, Any]]:
        # public_key 按 Langfuse 设计本就公开（客户端 SDK 初始化用），可下发展示；
        # secret_key 仍不下发以避免泄露。
        return [
            {
                "id": p.id,
                "name": p.name,
                "host": p.host,
                "organization": p.organization,
                "langfuse_project_id": p.langfuse_project_id,
                "public_key": p.public_key,
            }
            for p in sorted(self._projects.values(), key=lambda x: x.name)
        ]

    def get_config(self, project: Optional[str] = None) -> LangfuseProjectConfig:
        pid = self.resolve_project_id(project)
        return self._projects[pid]

    def get_client(self, project: Optional[str] = None) -> LangfuseClient:
        pid = self.resolve_project_id(project)
        if pid not in self._clients:
            cfg = self._projects[pid]
            self._clients[pid] = LangfuseClient(
                host=cfg.host or _DEFAULT_LANGFUSE_HOST,
                public_key=cfg.public_key,
                secret_key=cfg.secret_key,
                project_key=pid,
            )
        return self._clients[pid]

    def get_write_client(self) -> LangfuseClient:
        """评测埋点写入使用的项目（LANGFUSE_WRITE_PROJECT 或默认项目）"""
        write_id = (os.getenv("LANGFUSE_WRITE_PROJECT") or "").strip()
        return self.get_client(write_id or None)

    def find_by_public_key(self, public_key: str) -> Optional[LangfuseProjectConfig]:
        needle = (public_key or "").strip()
        if not needle:
            return None
        for cfg in self._projects.values():
            if cfg.public_key.strip() == needle:
                return cfg
        return None


_registry: Optional[LangfuseProjectRegistry] = None


def get_registry() -> LangfuseProjectRegistry:
    global _registry
    if _registry is None:
        _registry = LangfuseProjectRegistry(load_langfuse_project_configs())
    return _registry


def find_project_by_public_key(public_key: str) -> Optional[LangfuseProjectConfig]:
    """onto-agent public_key → eval 注册表项目（未启用 Langfuse 时返回 None）。"""
    try:
        registry = get_registry()
    except Exception:
        return None
    if not registry.enabled:
        return None
    return registry.find_by_public_key(public_key)


def reset_registry_for_tests() -> None:
    global _registry
    _registry = None


def _resolve_projects_file_path() -> Optional[str]:
    file_path = LANGFUSE_PROJECTS_FILE
    if not file_path:
        return None
    if os.path.isabs(file_path):
        return file_path
    return os.path.join(BASE_DIR, file_path)


def save_langfuse_project_configs(configs: List[LangfuseProjectConfig]) -> None:
    file_path = _resolve_projects_file_path()
    if not file_path:
        raise RuntimeError("LANGFUSE_PROJECTS_FILE 未配置，无法持久化项目配置")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    # Docker bind mount 在宿主文件不存在时会自动创建空目录，导致 open("w") 抛
    # IsADirectoryError；检测到目标路径是目录时先移除，使首次写入可自愈。
    # 但绑定挂载的目录无法从容器内删除（EBUSY），此时须提示用户在宿主修复。
    if os.path.isdir(file_path):
        try:
            os.rmdir(file_path)
        except OSError:
            raise RuntimeError(
                f"LANGFUSE_PROJECTS_FILE 路径 '{file_path}' 当前是一个目录"
                "（Docker 绑定挂载宿主不存在的文件时自动创建）。"
                "请在宿主机执行："
                f" rm -rf langfuse.projects.json && echo '[]' > langfuse.projects.json"
                "，然后重启容器（docker compose restart app）。"
            ) from None
    items = []
    for cfg in configs:
        item: Dict[str, Any] = {
            "id": cfg.id,
            "name": cfg.name,
            "public_key": cfg.public_key,
            "secret_key": cfg.secret_key,
        }
        if cfg.host:
            item["host"] = cfg.host
        if cfg.organization:
            item["organization"] = cfg.organization
        if cfg.langfuse_project_id:
            item["langfuse_project_id"] = cfg.langfuse_project_id
        items.append(item)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    log("INFO", "LANGFUSE", "项目配置已持久化", path=file_path, count=len(items))
