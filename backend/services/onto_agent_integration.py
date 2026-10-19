"""
Onto Agent 集成到 onto-eval 推理服务

本文件展示如何将 onto-agent 作为 onto-eval 的推理后端使用
"""
import os
from typing import Dict, Any, Optional
from logger import log


class OntoAgentInferenceService:
    """
    Onto Agent 推理服务
    封装 onto-agent 调用，兼容 onto-eval 的推理服务接口
    """
    
    def __init__(
        self,
        base_url: str = None,
        api_token: str = None,
        workspace_id: str = None,
        timeout: int = 1800
    ):
        """
        初始化 onto-agent 推理服务
        
        Args:
            base_url: onto-agent 地址，默认从环境变量读取
            api_token: API Token，默认从环境变量读取
            workspace_id: 工作空间ID
            timeout: 请求超时时间(秒)
        """
        # 优先使用传入参数，其次从环境变量读取
        self.base_url = base_url or os.getenv("ONTO_AGENT_BASE_URL", "http://localhost:5055")
        self.api_token = api_token or os.getenv("ONTO_AGENT_API_TOKEN", "")
        self.workspace_id = workspace_id or os.getenv("ONTO_AGENT_WORKSPACE")
        self.timeout = int(timeout or os.getenv("ONTO_AGENT_TIMEOUT", "1800"))
        
        # 延迟导入，避免循环依赖
        from onto_agent_client import OntoAgentClient, OntoAgentConfig
        
        config = OntoAgentConfig(
            base_url=self.base_url,
            api_token=self.api_token,
            workspace_id=self.workspace_id,
            timeout=self.timeout
        )
        self.client = OntoAgentClient(config)
        log("INFO", "ONTO_AGENT", "推理服务初始化完成", base_url=self.base_url)
    
    async def call_inference(
        self,
        question: str,
        config: Dict[str, Any],
        ground_truth: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        调用 onto-agent 进行推理
        兼容 onto-eval 的 InferenceService 接口
        
        Args:
            question: 问题文本
            config: 配置字典（可包含 url, token 等覆盖默认配置）
            ground_truth: 真实答案（可选，用于传递给 onto-agent）
            
        Returns:
            {"model_answer": "...", "error": "..."}
        """
        try:
            # 从配置中读取参数（支持动态覆盖）
            base_url = config.get("url") or self.base_url
            api_token = config.get("api_token") or self.api_token
            workspace_id = config.get("workspace_id") or self.workspace_id
            
            # 构建上下文
            context = config.get("context", {})
            if ground_truth:
                context["ground_truth"] = ground_truth
            
            # 构建请求
            from onto_agent_client import OntoAgentClient, OntoAgentConfig
            
            agent_config = OntoAgentConfig(
                base_url=base_url,
                api_token=api_token,
                workspace_id=workspace_id,
                timeout=self.timeout
            )
            client = OntoAgentClient(agent_config)
            
            log("INFO", "ONTO_AGENT", "开始推理", question=question[:100])
            
            # 调用 onto-agent
            answer = await client.run_workflow_sync(
                request=question,
                context=context
            )
            
            if answer.startswith("[ERROR]"):
                log("ERROR", "ONTO_AGENT", "推理失败", error=answer)
                return {"model_answer": "", "error": answer}
            
            log("INFO", "ONTO_AGENT", "推理完成", answer_preview=answer[:100])
            return {"model_answer": answer, "error": None}
            
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "推理异常", error=str(e))
            return {"model_answer": "", "error": str(e)}
    
    async def infer(self, question: str, context: Optional[str] = None) -> str:
        """
        简化的推理接口
        
        Args:
            question: 问题文本
            context: 可选的上下文字符串
            
        Returns:
            模型回答
        """
        ctx = {}
        if context:
            ctx["context"] = context
        
        try:
            answer = await self.client.run_workflow_sync(
                request=question,
                context=ctx
            )
            return answer
        except Exception as e:
            log("ERROR", "ONTO_AGENT", "推理失败", error=str(e))
            return f"[ERROR] {str(e)}"


# ============ 与现有推理服务集成 ============

async def call_onto_agent_inference(
    question: str,
    config: Dict[str, Any],
    ground_truth: Optional[str] = None
) -> Dict[str, Any]:
    """
    便捷函数：调用 onto-agent 进行推理
    可直接用于替换或补充 inference_service.py 中的调用
    
    使用示例:
        result = await call_onto_agent_inference(
            question="什么是本体?",
            config={
                "url": "http://localhost:5055",
                "api_token": "your-token"
            },
            ground_truth="本体是..."
        )
    """
    service = OntoAgentInferenceService()
    return await service.call_inference(question, config, ground_truth)


# ============ 在评测任务中使用 ============

async def evaluate_with_onto_agent(
    dataset_item: Dict[str, Any],
    onto_agent_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    使用 onto-agent 评测单个数据集项
    
    Args:
        dataset_item: {"question": "...", "ground_truth": "...", "context": "..."}
        onto_agent_config: onto-agent 配置（可选）
        
    Returns:
        {"question": "...", "ground_truth": "...", "model_answer": "...", "error": "..."}
    """
    question = dataset_item.get("question", "")
    ground_truth = dataset_item.get("ground_truth", "")
    context = dataset_item.get("context", "")
    
    config = onto_agent_config or {}
    if context:
        config["context"] = {"text": context}
    
    service = OntoAgentInferenceService()
    result = await service.call_inference(question, config, ground_truth)
    
    return {
        "question": question,
        "ground_truth": ground_truth,
        "model_answer": result.get("model_answer", ""),
        "error": result.get("error")
    }
