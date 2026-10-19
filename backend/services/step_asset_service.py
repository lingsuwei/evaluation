"""场景子步骤参考答案 / 评测提示词文件管理。

目录结构：
  backend/data/step_assets/{dataset_id}/{scenario_key}/
    ├── ground_truth/{step_key}/
    │   ├── file1.txt        # 参考答案文件
    │   ├── file2.csv
    │   └── ...
    └── llm_prompts/{step_key}/
        └── prompt.txt       # 评测提示词

文件持久化到 backend/data/ 目录（docker-compose 卷挂载，容器重建不丢失）。
"""
from __future__ import annotations

import os
from typing import List, Optional

from logger import log

# 存储根目录
_BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "step_assets",
)

# 支持的扩展名
_ALLOWED_EXTS = {".txt", ".csv"}


def _safe_part(name: str) -> str:
    """将名称转换为安全的路径片段"""
    import re

    name = name.strip()
    name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", name)
    return name[:200] if name else "default"


def _asset_step_dir(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
) -> str:
    """获取步骤资源目录路径"""
    return os.path.join(
        _BASE,
        _safe_part(dataset_id),
        _safe_part(scenario_key),
        kind,
        _safe_part(step_key),
    )


def _safe_filename(filename: str) -> str:
    """安全检查文件名，拒绝路径穿越，仅保留安全的文件名"""
    import re

    basename = os.path.basename(filename).strip()
    if not basename:
        return "untitled.txt"
    # 移除危险字符
    basename = re.sub(r"[^\w\u4e00-\u9fff\-\.\+\(\)\s]", "_", basename)
    return basename[:200] if basename else "untitled.txt"


def _validate_ext(filename: str) -> str:
    """验证文件扩展名，返回标准化后的扩展名"""
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext in (".txt", ".csv"):
        return ext
    # 无扩展名或有其他扩展名，默认 .txt
    return ".txt"


def save_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    content: str,
    kind: str,
    filename: str = "",
) -> str:
    """保存文件内容，返回相对路径。

    filename 为空时使用默认名（如 step_key.txt），否则使用原文件名。
    """
    if not filename:
        filename = f"{_safe_part(step_key)}.txt"
    safe_name = _safe_filename(filename)
    _, ext = os.path.splitext(safe_name)
    if ext.lower() not in _ALLOWED_EXTS:
        safe_name += ".txt"

    dir_path = _asset_step_dir(dataset_id, scenario_key, step_key, kind)
    path = os.path.join(dir_path, safe_name)
    os.makedirs(dir_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    rel = os.path.relpath(path, _BASE)
    log("INFO", "STEP_ASSET", f"保存 {kind} 文件", path=rel, size=len(content))
    return rel


def list_assets(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
) -> List[str]:
    """列出步骤目录下所有资源文件名"""
    dir_path = _asset_step_dir(dataset_id, scenario_key, step_key, kind)
    if not os.path.isdir(dir_path):
        return []
    files: List[str] = []
    for fname in sorted(os.listdir(dir_path)):
        fpath = os.path.join(dir_path, fname)
        if os.path.isfile(fpath):
            _, ext = os.path.splitext(fname)
            if ext.lower() in _ALLOWED_EXTS:
                files.append(fname)
    return files


def load_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
    filename: Optional[str] = None,
) -> Optional[str]:
    """读取文件内容，不存在返回 None。

    filename 为空时读取第一个文件（兼容旧逻辑）。
    """
    if filename:
        safe_name = _safe_filename(filename)
        path = os.path.join(
            _asset_step_dir(dataset_id, scenario_key, step_key, kind),
            safe_name,
        )
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # 向后兼容：没有指定文件名时读取旧的单文件路径
    old_path = os.path.join(
        _BASE,
        _safe_part(dataset_id),
        _safe_part(scenario_key),
        kind,
        f"{_safe_part(step_key)}.txt",
    )
    if os.path.exists(old_path):
        with open(old_path, "r", encoding="utf-8") as f:
            return f.read()

    # 尝试新目录结构下的第一个文件
    files = list_assets(dataset_id, scenario_key, step_key, kind)
    if not files:
        return None
    path = os.path.join(
        _asset_step_dir(dataset_id, scenario_key, step_key, kind),
        files[0],
    )
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def delete_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
    filename: str = "",
) -> bool:
    """删除文件，返回是否删除成功。

    filename 为空时删除整个步骤资源目录（兼容旧逻辑）。
    """
    if filename:
        safe_name = _safe_filename(filename)
        path = os.path.join(
            _asset_step_dir(dataset_id, scenario_key, step_key, kind),
            safe_name,
        )
        if os.path.exists(path):
            os.remove(path)
            log("INFO", "STEP_ASSET", f"删除 {kind} 文件", path=path)
            return True
        return False

    # 向后兼容：删除旧的单文件
    old_path = os.path.join(
        _BASE,
        _safe_part(dataset_id),
        _safe_part(scenario_key),
        kind,
        f"{_safe_part(step_key)}.txt",
    )
    if os.path.exists(old_path):
        os.remove(old_path)
        return True

    # 删除整个步骤目录
    dir_path = _asset_step_dir(dataset_id, scenario_key, step_key, kind)
    if os.path.isdir(dir_path):
        import shutil
        shutil.rmtree(dir_path)
        log("INFO", "STEP_ASSET", f"删除 {kind} 目录", dir=dir_path)
        return True
    return False


def asset_exists(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
) -> bool:
    """检查步骤是否有资源文件"""
    # 检查新目录结构
    files = list_assets(dataset_id, scenario_key, step_key, kind)
    if files:
        return True
    # 检查旧的单文件
    old_path = os.path.join(
        _BASE,
        _safe_part(dataset_id),
        _safe_part(scenario_key),
        kind,
        f"{_safe_part(step_key)}.txt",
    )
    return os.path.isfile(old_path)


# 常量
KIND_GROUND_TRUTH = "ground_truth"
KIND_LLM_PROMPT = "llm_prompts"
