"""
Onto Agent 调用示例

本文件展示如何在 onto-eval 中使用 onto_agent_client 调用 onto-agent 的 API
"""
import asyncio
import os
from typing import Optional

# 从环境变量或配置文件读取配置
ONTO_AGENT_BASE_URL = os.getenv("ONTO_AGENT_BASE_URL", "http://localhost:5055")
ONTO_AGENT_API_TOKEN = os.getenv("ONTO_AGENT_API_TOKEN", "your-token-here")
ONTO_AGENT_WORKSPACE = os.getenv("ONTO_AGENT_WORKSPACE", None)


async def example_basic_usage():
    """
    示例1: 基础用法 - 直接调用 onto-agent
    """
    from onto_agent_client import call_onto_agent
    
    answer = await call_onto_agent(
        question="什么是本体工程?",
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN,
        workspace_id=ONTO_AGENT_WORKSPACE
    )
    
    print(f"回答: {answer}")
    return answer


async def example_with_context():
    """
    示例2: 带上下文的调用
    """
    from onto_agent_client import OntoAgentClient, OntoAgentConfig
    
    config = OntoAgentConfig(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN,
        workspace_id=ONTO_AGENT_WORKSPACE,
        timeout=1800
    )
    
    client = OntoAgentClient(config)
    
    # 带上下文调用
    context = {
        "ontology": "制造本体",
        "domain": "航空制造"
    }
    
    answer = await client.run_workflow_sync(
        request="基于这个本体，如何设计EBOM结构?",
        context=context
    )
    
    print(f"回答: {answer}")
    return answer


async def example_streaming():
    """
    示例3: 流式调用（实时获取响应）
    """
    from onto_agent_client import OntoAgentClient, OntoAgentConfig
    
    config = OntoAgentConfig(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN
    )
    
    client = OntoAgentClient(config)
    
    print("流式回答: ", end="", flush=True)
    async for chunk in client.run_workflow(request="解释本体推理的过程"):
        print(chunk, end="", flush=True)
    print()


async def example_with_session():
    """
    示例4: 使用 Session 保持对话上下文
    """
    from onto_agent_client import OntoAgentClient, OntoAgentConfig
    
    config = OntoAgentConfig(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN
    )
    
    client = OntoAgentClient(config)
    
    # 创建一个 Session
    session_id = await client.create_session(title="测试对话")
    print(f"创建 Session: {session_id}")
    
    # 在同一个 Session 中进行多轮对话
    answer1 = await client.run_workflow_sync(
        request="你好，我是测试用户",
        session_id=session_id
    )
    print(f"第一轮: {answer1}")
    
    answer2 = await client.run_workflow_sync(
        request="刚才我说了什么?",
        session_id=session_id
    )
    print(f"第二轮: {answer2}")
    
    # 列出所有 Sessions
    sessions = await client.list_sessions()
    print(f"当前 Sessions: {len(sessions)}")


async def example_test_connection():
    """
    示例5: 测试连接
    """
    from onto_agent_client import test_connection, OntoAgentConfig
    
    config = OntoAgentConfig(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN
    )
    
    is_connected = await test_connection(config)
    print(f"连接状态: {'成功' if is_connected else '失败'}")
    return is_connected


async def example_inference_adapter():
    """
    示例6: 使用推理适配器（兼容 onto-eval 推理服务）
    """
    from onto_agent_client import OntoAgentInferenceAdapter
    
    adapter = OntoAgentInferenceAdapter(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN,
        workspace_id=ONTO_AGENT_WORKSPACE
    )
    
    # 这种调用方式与 onto-eval 的 InferenceService 兼容
    answer = await adapter.infer(
        question="本体评测的标准是什么?",
        context="制造业本体"
    )
    
    print(f"回答: {answer}")
    return answer


async def example_for_evaluation():
    """
    示例7: 用于评测场景的调用
    
    这是 onto-eval 中实际使用的典型场景
    """
    from onto_agent_client import OntoAgentClient, OntoAgentConfig
    
    # 评测数据集中的一个样本
    question = "什么是AO工序?"
    ground_truth = "AO工序是装配大纲中的工序..."
    context = "航空制造领域，AO(Assembly Outline)是装配大纲..."
    
    config = OntoAgentConfig(
        base_url=ONTO_AGENT_BASE_URL,
        api_token=ONTO_AGENT_API_TOKEN,
        workspace_id=ONTO_AGENT_WORKSPACE,
        timeout=300  # 评测场景设置合理超时
    )
    
    client = OntoAgentClient(config)
    
    # 构建评测上下文
    eval_context = {
        "evaluation_mode": True,
        "ground_truth": ground_truth,
        "context": context,
        "question_type": "definition"
    }
    
    model_answer = await client.run_workflow_sync(
        request=question,
        context=eval_context
    )
    
    print(f"问题: {question}")
    print(f"标准答案: {ground_truth}")
    print(f"模型回答: {model_answer}")
    
    return model_answer


async def main():
    """
    运行所有示例
    """
    print("=" * 60)
    print("Onto Agent 调用示例")
    print("=" * 60)
    
    # 先测试连接
    print("\n--- 测试连接 ---")
    await example_test_connection()
    
    # 基础用法
    print("\n--- 示例1: 基础用法 ---")
    try:
        await example_basic_usage()
    except Exception as e:
        print(f"错误: {e}")
    
    # 带上下文
    print("\n--- 示例2: 带上下文 ---")
    try:
        await example_with_context()
    except Exception as e:
        print(f"错误: {e}")
    
    print("\n所有示例执行完成!")


if __name__ == "__main__":
    # 运行示例
    asyncio.run(main())
