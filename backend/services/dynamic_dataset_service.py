import json
import uuid
import asyncio
import aiohttp
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import Dataset


class DynamicDatasetService:
    """动态评测集生成服务"""
    
    @staticmethod
    async def generate_dynamic_dataset(
        session: AsyncSession,
        source_dataset_id: str,
        inference_config: Dict[str, Any]
    ) -> str:
        """
        从源数据集生成动态评测集
        
        Args:
            session: 数据库会话
            source_dataset_id: 源数据集ID
            inference_config: 推理配置，包含接口地址、认证信息等
            
        Returns:
            生成的新数据集ID
        """
        # 获取源数据集
        result = await session.execute(
            select(Dataset).where(Dataset.id == source_dataset_id)
        )
        source_dataset = result.scalar_one_or_none()
        
        if not source_dataset:
            raise ValueError(f"Source dataset {source_dataset_id} not found")
        
        source_data = json.loads(source_dataset.data)
        
        # 调用推理接口，获取模型答案
        processed_data = await DynamicDatasetService._call_inference_api(
            source_data, 
            inference_config
        )
        
        # 创建新数据集
        new_dataset_id = f"generated_{uuid.uuid4().hex[:12]}"
        new_dataset = Dataset(
            id=new_dataset_id,
            data=json.dumps(processed_data),
            item_count=len(processed_data)
        )
        
        session.add(new_dataset)
        await session.commit()
        
        return new_dataset_id
    
    @staticmethod
    async def _call_inference_api(
        source_data: List[Dict[str, Any]], 
        config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        调用推理接口获取模型答案
        
        Args:
            source_data: 源数据，每项应包含question和ground_truth
            config: 推理配置
            
        Returns:
            处理后的数据，每项包含question, ground_truth, model_answer等
        """
        base_url = config.get("url", "").rstrip("/")
        headers = config.get("headers", {})
        auth = config.get("auth", {})
        
        processed_data = []
        
        # 使用 aiohttp 创建连接池
        connector = aiohttp.TCPConnector(limit=10)  # 限制并发数
        timeout = aiohttp.ClientTimeout(total=config.get("timeout", 1800))
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            for idx, item in enumerate(source_data):
                question = item.get("question", "") or item.get("input", "")
                ground_truth = item.get("ground_truth", "") or item.get("expected_output", "")
                
                # 构建请求数据
                payload_template = config.get("payload_template", {})
                
                if payload_template:
                    payload = {}
                    for key, value in payload_template.items():
                        if isinstance(value, str) and "${question}" in value:
                            payload[key] = value.replace("${question}", question)
                        else:
                            payload[key] = value
                else:
                    payload = {
                        "request": question,
                        "context": {
                            "agent_mode": "thinking"
                        }
                    }
                
                try:
                    # 发送请求到推理接口
                    url = f"{base_url}/{config.get('endpoint', 'infer')}"
                    
                    request_headers = headers.copy()
                    
                    # 如果有认证信息，添加到请求头
                    if auth.get("type") == "bearer":
                        request_headers["Authorization"] = f"Bearer {auth.get('token', '')}"
                    elif auth.get("type") == "api_key":
                        header_name = auth.get("header_name", "X-API-Key")
                        request_headers[header_name] = auth.get("api_key", "")
                    
                    async with session.post(url, json=payload, headers=request_headers) as response:
                        if response.status != 200:
                            raise Exception(f"Inference API returned status {response.status}: {await response.text()}")
                        
                        result = await response.json()
                        
                        # 解析模型答案
                        model_answer = (
                            result.get("final_answer", "") or
                            result.get("answer", "") or
                            result.get("output", "") or
                            result.get("response", "") or
                            result.get("result", "") or
                            result.get("text", "")
                        )
                        
                        # 构建新数据项
                        new_item = {
                            "question": question,
                            "ground_truth": ground_truth,
                            "model_answer": model_answer,
                            "source_index": idx
                        }
                        
                        # 保留其他可能存在的字段
                        for key, value in item.items():
                            if key not in ["question", "ground_truth", "input", "expected_output"]:
                                new_item[key] = value
                        
                        processed_data.append(new_item)
                        
                        # 添加延迟以避免过于频繁的请求
                        if idx < len(source_data) - 1:  # 不是最后一项时才延迟
                            await asyncio.sleep(config.get("request_delay", 0.1))
                            
                except Exception as e:
                    # 如果请求失败，仍然保留原始数据但标记错误
                    new_item = {
                        "question": question,
                        "ground_truth": ground_truth,
                        "model_answer": "",
                        "error": str(e),
                        "source_index": idx
                    }
                    
                    # 保留其他可能存在的字段
                    for key, value in item.items():
                        if key not in ["question", "ground_truth", "input", "expected_output"]:
                            new_item[key] = value
                    
                    processed_data.append(new_item)
        
        return processed_data