#!/usr/bin/env python3
"""E2E 验证：MCP call_tool → urllib HTTP → 后端 → LLM 全链路。

通过本地 Docker 后端 (localhost:3001) 验证真实 LLM 调用。
用法: python e2e_verify.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 指向本地 Docker 后端
TEST_CONFIG = {
    "backend_url": "http://localhost:3001",
    "token": "",
    "username": "",
    "password": "",
    "token_expiry_skew_seconds": 60,
    "request_timeout": 120,
}


async def main():
    with patch("mcp_server.load_config", return_value=TEST_CONFIG):
        from mcp_server import call_tool

        # 1. list_metrics（免费，验证连通性）
        print("=== 1. list_metrics ===")
        result = await call_tool("list_metrics", {})
        metrics = json.loads(result[0].text)
        builtin_keys = [b["key"] for b in metrics.get("builtin", [])]
        print(f"  builtin 指标: {builtin_keys}")
        faithfulness_meta = next(b for b in metrics["builtin"] if b["key"] == "faithfulness")
        print(f"  faithfulness category: {faithfulness_meta.get('category')}")
        print(f"  faithfulness requires: {faithfulness_meta.get('requires')}")
        assert "faithfulness" in builtin_keys
        assert "answer_relevancy" in builtin_keys
        assert "correctness" in builtin_keys
        print("  [PASS] list_metrics")

        # 2. evaluate_answer — answer_relevancy（真实 LLM 调用，3-30s）
        print("\n=== 2. evaluate_answer (answer_relevancy, 真实 LLM) ===")
        print("  调用中... (等待 LLM 响应)")
        result = await call_tool("evaluate_answer", {
            "question": "什么是物料A？",
            "answer": "物料A是飞机机翼的结构件，用于承受飞行中的气动载荷。",
            "metric": "answer_relevancy",
        })
        eval_result = json.loads(result[0].text)
        print(f"  metric: {eval_result.get('metric')}")
        print(f"  score: {eval_result.get('score')}")
        print(f"  passed: {eval_result.get('passed')}")
        print(f"  threshold: {eval_result.get('threshold')}")
        print(f"  skipped: {eval_result.get('skipped')}")
        print(f"  eval_method: {eval_result.get('eval_method')}")
        reason = eval_result.get('reason', '')
        print(f"  reason: {reason[:100]}...")

        # 验证响应结构
        assert eval_result["metric"] == "answer_relevancy"
        assert isinstance(eval_result["score"], (int, float))
        assert isinstance(eval_result["passed"], bool)
        assert eval_result["eval_method"] == "llm"
        assert eval_result["skipped"] is False
        print("  [PASS] evaluate_answer")

        # 3. evaluate_answer — faithfulness（需 context，真实 LLM）
        print("\n=== 3. evaluate_answer (faithfulness, 真实 LLM) ===")
        print("  调用中... (等待 LLM 响应)")
        result = await call_tool("evaluate_answer", {
            "question": "什么是物料A？",
            "answer": "物料A是飞机机翼的结构件。",
            "metric": "faithfulness",
            "context": "物料A是飞机机翼的结构件，用于承受飞行中的气动载荷。",
        })
        eval_result = json.loads(result[0].text)
        print(f"  metric: {eval_result.get('metric')}")
        print(f"  score: {eval_result.get('score')}")
        print(f"  passed: {eval_result.get('passed')}")
        print(f"  reason: {eval_result.get('reason', '')[:100]}...")

        assert eval_result["metric"] == "faithfulness"
        assert isinstance(eval_result["score"], (int, float))
        print("  [PASS] faithfulness")

        print("\n" + "=" * 50)
        print("[PASS] E2E: MCP -> urllib HTTP -> backend -> LLM")
        print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
