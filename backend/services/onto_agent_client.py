"""
Onto Agent 客户端
提供调用 onto-agent API 的能力，支持认证、工作流调用等功能
"""
import asyncio
import json
import aiohttp
from typing import Dict, Any, Optional, AsyncIterator, List
from dataclasses import dataclass
from logger import log


@dataclass
class OntoAgentConfig:
    """Onto Agent 配置"""
    base_url: str  # onto-agent 地址，如 http://localhost:5055
    api_token: str  # Bearer Token
    workspace_id: Optional[str] = None  # 可选的工作空间ID
    timeout: int = 1800  # 请求超时时间（秒）


class OntoAgentClient:
    """
    Onto Agent 客户端
    
    用于调用 onto-agent 的 API，包括：
    - 认证接口
    - 工作流运行
    - Session 管理
    - 任务状态查询
    """
    
    def __init__(self, config: OntoAgentConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
        }
        if config.workspace_id:
            self.headers["X-Workspace-Id"] = config.workspace_id
    
    async def authenticate(self) -> Dict[str, Any]:
        """
        验证当前 Token 是否有效
        
        Returns:
            {"user": {"username": "...", "role": "..."}} 或 {"error": "..."}
        """
        url = f"{self.base_url}/auth/me"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error_text = await resp.text()
                        log("ERROR", "ONTO_AGENT", "认证失败", status=resp.status, error=error_text)
                        return {"error": f"Authentication failed: {resp.status}"}
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "认证请求异常", error=str(e))
            return {"error": str(e)}
    
    async def create_session(self, title: Optional[str] = None) -> str:
        """
        创建新的 Chat Session
        
        Args:
            title: Session 标题
            
        Returns:
            session_id
        """
        url = f"{self.base_url}/agentic/sessions"
        payload = {"title": title or "OntoEval Session", "llm_request_mode": "stream"}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload) as resp:
                    if resp.status == 201:
                        data = await resp.json()
                        session_id = data.get("id")
                        log("INFO", "ONTO_AGENT", "Session 创建成功", session_id=session_id)
                        return session_id
                    else:
                        error_text = await resp.text()
                        log("ERROR", "ONTO_AGENT", "Session 创建失败", status=resp.status, error=error_text)
                        raise Exception(f"Failed to create session: {resp.status}")
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "Session 创建异常", error=str(e))
            raise
    
    async def run_workflow(
        self,
        request: str,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> AsyncIterator[str]:
        """
        运行 onto-agent 工作流（流式响应）
        
        Args:
            request: 用户请求文本
            session_id: 可选的 session ID，不提供则自动创建
            context: 可选的上下文数据
            
        Yields:
            流式响应的文本片段
        """
        # 如果没有 session_id，先创建一个
        if not session_id:
            session_id = await self.create_session()
        
        url = f"{self.base_url}/agentic/runs/stream"
        payload = {
            "request": request,
            "session_id": session_id,
            "context": context or {}
        }
        
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=self.headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        log("ERROR", "ONTO_AGENT", "工作流调用失败", status=resp.status, error=error_text)
                        yield f"[ERROR] 工作流调用失败: {resp.status}"
                        return
                    
                    # 解析 SSE 流
                    async for line in resp.content:
                        line = line.decode('utf-8').strip()
                        if line.startswith('event:'):
                            continue
                        if line.startswith('data:'):
                            data_str = line[5:].strip()
                            if not data_str:
                                continue
                            try:
                                event_data = json.loads(data_str)
                                # 提取最终答案
                                if event_data.get("type") == "turn.finished":
                                    payload_data = event_data.get("payload", {})
                                    result = payload_data.get("result", {})
                                    if result:
                                        answer = result.get("final_answer", "")
                                        if answer:
                                            yield answer
                                # 也可以实时返回流式内容
                                elif event_data.get("type") == "token":
                                    token = event_data.get("payload", {}).get("text", "")
                                    if token:
                                        yield token
                            except json.JSONDecodeError:
                                continue
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "工作流调用异常", error=str(e))
            yield f"[ERROR] {str(e)}"
    
    async def run_workflow_sync(
        self,
        request: str,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        同步运行 onto-agent 工作流，返回完整结果
        
        Args:
            request: 用户请求文本
            session_id: 可选的 session ID
            context: 可选的上下文数据
            
        Returns:
            完整的模型回答
        """
        result_parts = []
        async for chunk in self.run_workflow(request, session_id, context):
            if chunk.startswith("[ERROR]"):
                return chunk
            result_parts.append(chunk)
        return "".join(result_parts)
    
    async def get_run_status(self, run_id: str) -> Dict[str, Any]:
        """
        获取运行任务状态
        
        Args:
            run_id: 运行任务 ID
            
        Returns:
            任务状态信息
        """
        url = f"{self.base_url}/agentic/runs/{run_id}/status"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        return {"error": f"Failed to get status: {resp.status}"}
        except Exception as e:
            return {"error": str(e)}
    
    async def list_sessions(self) -> List[Dict[str, Any]]:
        """
        列出所有 Session
        
        Returns:
            Session 列表
        """
        url = f"{self.base_url}/agentic/sessions"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("sessions", [])
                    else:
                        return []
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "获取 Session 列表失败", error=str(e))
            return []
    
    async def cancel_run(self, run_id: str) -> bool:
        """
        取消正在运行的任务
        
        Args:
            run_id: 运行任务 ID
            
        Returns:
            是否成功取消
        """
        url = f"{self.base_url}/agentic/runs/{run_id}/cancel"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers) as resp:
                    return resp.status == 200
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "取消任务失败", error=str(e))
            return False


class OntoAgentInferenceAdapter:
    """
    Onto Agent 推理适配器
    兼容 onto-eval 的推理服务接口
    """
    
    def __init__(self, base_url: str, api_token: str, workspace_id: Optional[str] = None):
        self.client = OntoAgentClient(OntoAgentConfig(
            base_url=base_url,
            api_token=api_token,
            workspace_id=workspace_id
        ))
    
    async def infer(self, question: str, context: Optional[str] = None) -> str:
        """
        推理接口，兼容 onto-eval 的调用方式
        
        Args:
            question: 问题文本
            context: 可选的上下文
            
        Returns:
            模型回答
        """
        ctx = {}
        if context:
            ctx["context"] = context
        
        return await self.client.run_workflow_sync(request=question, context=ctx)


# ============ 便捷函数 ============

async def call_onto_agent(
    question: str,
    base_url: str = "http://localhost:5055",
    api_token: str = "",
    workspace_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None
) -> str:
    """
    便捷函数：直接调用 onto-agent 获取回答
    
    使用示例:
        answer = await call_onto_agent(
            question="什么是本体?",
            base_url="http://localhost:5055",
            api_token="your-token"
        )
    """
    config = OntoAgentConfig(
        base_url=base_url,
        api_token=api_token,
        workspace_id=workspace_id
    )
    client = OntoAgentClient(config)
    return await client.run_workflow_sync(question, context=context)


async def test_connection(config: OntoAgentConfig) -> bool:
    """
    测试 onto-agent 连接是否正常
    
    Returns:
        连接是否成功
    """
    client = OntoAgentClient(config)
    result = await client.authenticate()
    return "error" not in result
