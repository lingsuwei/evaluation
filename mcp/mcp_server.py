#!/usr/bin/env python3
"""onto-eval MCP server: 单题评测能力暴露给 AI 工具。

stdio 传输，注册两个工具：
- evaluate_answer: 单题同步评测
- list_metrics: 列出可用指标

依赖：mcp SDK（唯一第三方依赖），HTTP 走标准库 urllib。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server import ServerRequestContext
from mcp.types import Tool, TextContent, ListToolsResult, CallToolRequestParams, CallToolResult

from backend_client import BackendClient
from config import load_config


async def list_tools(context: ServerRequestContext, params: Any) -> ListToolsResult:
    """列出可用工具。"""
    tools = [
        Tool(
            name="evaluate_answer",
            description=(
                "评测一道题的回答质量。返回分数(0-1)、是否通过、评估理由。"
                "支持指标：faithfulness(需 context)、answer_relevancy(仅需 question+answer)、"
                "correctness(需 ground_truth)。先用 list_metrics 查可用指标。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "问题内容"},
                    "answer": {"type": "string", "description": "待评估的回答"},
                    "metric": {
                        "type": "string",
                        "description": "指标名: faithfulness / answer_relevancy / correctness",
                    },
                    "context": {"type": "string", "description": "上下文(faithfulness 需要)"},
                    "ground_truth": {"type": "string", "description": "标准答案(correctness 需要)"},
                    "threshold": {
                        "type": "number",
                        "description": "通过阈值(0-1)，默认用指标默认值 0.8",
                    },
                },
                "required": ["question", "answer", "metric"],
            },
        ),
        Tool(
            name="list_metrics",
            description="列出 onto-eval 后端可用的评测指标及所需输入。",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]
    return ListToolsResult(tools=tools)


async def call_tool_handler(context: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    """工具调用分发（MCP 协议版本）。"""
    name = params.name
    arguments = params.arguments if hasattr(params, 'arguments') and params.arguments else {}

    result = await call_tool(name, arguments)
    return CallToolResult(content=result)


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """工具调用分发（直接调用版本，供测试使用）。"""
    config = load_config()
    client = BackendClient.from_config(config)

    if name == "evaluate_answer":
        # 过滤 None 值，避免传给后端
        body = {k: v for k, v in arguments.items() if v is not None}
        result = client.post("/api/v1/evaluate/single", body)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    if name == "list_metrics":
        result = client.get("/metrics")
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    raise ValueError(f"Unknown tool: {name}")


server = Server(
    "onto-eval-mcp",
    on_list_tools=list_tools,
    on_call_tool=call_tool_handler,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())