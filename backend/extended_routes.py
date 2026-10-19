"""
扩展API路由
将OpenTelemetry和版本对比功能集成到主应用中
"""
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from database import async_session, get_session
from schemas import DatasetItem
from services.otel_service import OTelService, TraceData as OTelTraceData, TraceSpan as OTelTraceSpan
from datetime import datetime


extended_router = APIRouter()


class TraceDataRequest(BaseModel):
    """Trace数据请求（简化版）"""
    trace_id: str = Field(..., description="Trace ID")
    spans: List[dict] = Field(..., description="Span列表")
    resource: Optional[dict] = Field(None, description="资源信息")
    timestamp: Optional[str] = Field(None, description="时间戳")


class VersionDataItem(BaseModel):
    """版本数据项"""
    question: str = Field(..., description="问题")
    context: Optional[str] = Field(None, description="上下文")
    ground_truth: Optional[str] = Field(None, description="标准答案")
    version_a_answer: str = Field(..., description="版本A答案")
    version_b_answer: str = Field(..., description="版本B答案")


class BatchVersionComparisonRequest(BaseModel):
    """批量版本对比请求"""
    comparisons: List[VersionDataItem] = Field(..., description="对比数据")


class VersionComparisonResponse(BaseModel):
    """版本对比响应"""
    comparison_id: str
    version_a_name: str
    version_b_name: str
    question_count: int
    overall_improved_count: int
    overall_degraded_count: int
    overall_stable_count: int
    conclusion: str
    improvement_percentage: float
    created_at: str


@extended_router.post("/otel/traces", summary="接收OpenTelemetry Trace数据")
async def receive_trace(data: TraceDataRequest):
    """
    接收OpenTelemetry格式的Trace数据
    
    用于记录LLM应用的调用链路，支持：
    - 标准OpenTelemetry JSON格式
    - 自动提取LLM相关属性（模型、prompt、completion等）
    - Trace数据查询和分析
    """
    try:
        trace_data = OTelTraceData(
            trace_id=data.trace_id,
            spans=data.spans,
            resource=data.resource,
            timestamp=data.timestamp
        )
        result = await OTelService.store_trace(trace_data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"存储Trace失败: {str(e)}")


@extended_router.get("/otel/traces", summary="查询Trace列表")
async def query_traces(
    trace_id: Optional[str] = None,
    service_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    min_duration_ms: Optional[float] = None,
    status: Optional[str] = None,
    limit: int = Query(default=100, le=1000)
):
    """
    查询Trace列表
    
    支持多条件筛选：
    - trace_id: 精确匹配
    - service_name: 服务名称
    - start_time/end_time: 时间范围
    - min_duration_ms: 最小持续时间
    - status: 状态（ok, error）
    """
    start_dt = None
    end_dt = None
    
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
        except:
            raise HTTPException(status_code=400, detail="start_time格式无效")
    
    if end_time:
        try:
            end_dt = datetime.fromisoformat(end_time)
        except:
            raise HTTPException(status_code=400, detail="end_time格式无效")
    
    traces = await OTelService.query_traces(
        trace_id=trace_id,
        service_name=service_name,
        start_time=start_dt,
        end_time=end_dt,
        min_duration_ms=min_duration_ms,
        status=status,
        limit=limit
    )
    
    return {
        "count": len(traces),
        "traces": traces
    }


@extended_router.get("/otel/traces/{trace_id}", summary="获取Trace详情")
async def get_trace_detail(trace_id: str):
    """
    获取指定Trace的详细信息
    
    返回Trace的所有Span，以及Span的层级关系
    """
    spans = await OTelService.get_trace_spans(trace_id)
    
    if not spans:
        raise HTTPException(status_code=404, detail=f"Trace {trace_id} 不存在")
    
    span_map = {s["span_id"]: s for s in spans}
    root_spans = []
    
    for span in spans:
        if not span["parent_span_id"]:
            root_spans.append(span)
        elif span["parent_span_id"] in span_map:
            parent = span_map[span["parent_span_id"]]
            if "children" not in parent:
                parent["children"] = []
            parent["children"].append(span)
    
    return {
        "trace_id": trace_id,
        "span_count": len(spans),
        "root_spans": root_spans
    }


@extended_router.get("/otel/llm/stats", summary="获取LLM调用统计")
async def get_llm_statistics(
    service_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
):
    """
    获取LLM调用统计分析
    
    统计内容包括：
    - 总调用次数
    - 平均/最小/最大延迟
    - Token使用量
    - 模型调用分布
    - 状态分布（成功/失败）
    """
    start_dt = None
    end_dt = None
    
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time)
        except:
            raise HTTPException(status_code=400, detail="start_time格式无效")
    
    if end_time:
        try:
            end_dt = datetime.fromisoformat(end_time)
        except:
            raise HTTPException(status_code=400, detail="end_time格式无效")
    
    stats = await OTelService.get_llm_stats(
        service_name=service_name,
        start_time=start_dt,
        end_time=end_dt
    )
    
    return stats





@extended_router.get("/versions/conclusions", summary="获取结论说明")
async def get_conclusion_descriptions():
    """
    获取版本对比结论的详细说明
    
    解释每种结论的含义和建议操作
    """
    descriptions = {
        ComparisonConclusion.IMPROVED.value: {
            "conclusion": "improved",
            "conclusion_cn": "提升",
            "description": f"新版本（version_b）在多数问题上的表现优于旧版本（version_a）",
            "action_suggestion": "建议将新版本部署到生产环境"
        },
        ComparisonConclusion.DEGRADED.value: {
            "conclusion": "degraded",
            "conclusion_cn": "劣化",
            "description": f"新版本（version_b）在多数问题上的表现劣于旧版本（version_a）",
            "action_suggestion": "建议回滚到旧版本，并分析劣化原因"
        },
        ComparisonConclusion.STABLE.value: {
            "conclusion": "stable",
            "conclusion_cn": "稳定",
            "description": "两个版本的表现相当，无显著差异",
            "action_suggestion": "可以按需部署新版本，建议持续监控"
        },
        ComparisonConclusion.MIXED.value: {
            "conclusion": "mixed",
            "conclusion_cn": "混合",
            "description": "两个版本在不同问题上各有优劣",
            "action_suggestion": "建议深入分析具体问题，确定新版本是否适合部署"
        }
    }
    
    return {
        "conclusions": descriptions
    }


class InferenceConfig(BaseModel):
    """推理配置"""
    url: str = Field(..., description="推理接口地址")
    endpoint: str = Field(default="infer", description="推理接口端点")
    headers: Dict[str, str] = Field(default={}, description="请求头")
    auth: Dict[str, str] = Field(default={"type": "none"}, description="认证信息")
    timeout: int = Field(default=1800, description="超时时间（秒）")
    request_delay: float = Field(default=0.1, description="请求间隔（秒）")
    payload_template: Dict[str, Any] = Field(default={}, description="请求体模板")
