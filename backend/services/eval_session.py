"""
评测 session_id 单点生成与解析（v2.1 / P1）。
格式: {task_id}:{question_id}，question_id 为数据集内从 0 起的题目序号。
"""
from __future__ import annotations

SESSION_ID_SEP = ":"


def build_eval_session_id(task_id: str, question_id: int) -> str:
    return f"{task_id}{SESSION_ID_SEP}{question_id}"


def parse_eval_session_id(session_id: str) -> tuple[str, str]:
    """返回 (task_id, question_id_str)；无法解析时返回 ("", "")。"""
    if not session_id or SESSION_ID_SEP not in session_id:
        return "", ""
    idx = session_id.index(SESSION_ID_SEP)
    return session_id[:idx], session_id[idx + 1 :]
