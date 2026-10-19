# -*- coding: utf-8 -*-
"""自定义「代码指标」的受限执行器——确定性、无需 LLM。

用户在平台写一段 Python:读测试用例字段(actual_output / expected_output / input /
retrieval_context / context / tools_called,以及 tc 字典),最后设 `score`(0~1)与可选 `reason`。

⚠️ 安全:这是「运行平台上的用户代码」。本执行器做了受限沙箱(白名单 builtins、禁 import/文件/网络、
超时),但 Python 沙箱无法 100% 隔离——**仅向可信作者开放**。如需强隔离请改子进程/容器执行。

源: agent_eval.evaluators.metrics.code_metric（零改动移植）。
"""
from __future__ import annotations

import json
import math
import re
import threading
from typing import Any

# 白名单内置函数(不含 __import__/open/eval/exec/compile/getattr/setattr/globals/locals/input/vars 等危险项)
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len",
    "list", "map", "max", "min", "range", "repr", "reversed", "round", "set", "sorted",
    "str", "sum", "tuple", "zip", "True", "False", "None",
)


def _safe_builtins() -> dict[str, Any]:
    import builtins

    return {n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(builtins, n)}


def tc_fields_from_case(test_case: Any) -> dict[str, Any]:
    """从 DeepEval LLMTestCase / ConversationalTestCase 抽出用户代码可读的字段。"""
    g = lambda a: getattr(test_case, a, None)  # noqa: E731
    tools = []
    for t in (g("tools_called") or []):
        tools.append(
            {"name": getattr(t, "name", None), "arguments": getattr(t, "arguments", None), "output": getattr(t, "output", None)}
        )
    output = g("actual_output")
    if not output:  # 多轮:取最后一条 assistant 内容兜底
        turns = g("turns") or []
        for tt in reversed(turns):
            if getattr(tt, "role", "") == "assistant" and getattr(tt, "content", ""):
                output = tt.content
                break
    return {
        "input": g("input") or "",
        "actual_output": output or "",
        "expected_output": g("expected_output") or "",
        "retrieval_context": list(g("retrieval_context") or []),
        "context": list(g("context") or []),
        "tools_called": tools,
    }


def run_code_metric(code: str, tc: dict[str, Any], *, timeout: float = 5.0) -> tuple[float, str]:
    """在受限命名空间执行用户代码,返回 (score[0~1], reason)。失败抛 ValueError(由上层记非阻塞)。"""
    if not str(code or "").strip():
        raise ValueError("代码为空")

    ns: dict[str, Any] = {
        "__builtins__": _safe_builtins(),
        "re": re,
        "json": json,
        "math": math,
        "tc": dict(tc),
        **{k: tc.get(k) for k in ("input", "actual_output", "expected_output", "retrieval_context", "context", "tools_called")},
        "score": None,
        "reason": "",
    }

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _exec() -> None:
        try:
            compiled = compile(code, "<custom_code_metric>", "exec")
            exec(compiled, ns)  # noqa: S102 - 受限命名空间执行用户指标代码
            result["score"] = ns.get("score")
            result["reason"] = ns.get("reason")
        except BaseException as exc:  # noqa: BLE001 - 用户代码任何异常都收回上层
            error["e"] = exc

    th = threading.Thread(target=_exec, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise ValueError(f"代码执行超时(>{timeout:.0f}s)")
    if "e" in error:
        raise ValueError(f"代码报错:{type(error['e']).__name__}: {error['e']}")

    raw = result.get("score")
    if raw is None:
        raise ValueError("代码未设置 score(请在结尾把 score 设为 0~1 的数)")
    try:
        score = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score 不是数字:{raw!r}") from exc
    score = max(0.0, min(1.0, score))  # 夹到 [0,1]
    reason = str(result.get("reason") or "")[:2000]
    return score, reason
