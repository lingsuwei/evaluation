"""
OpenTelemetry Trace数据接入服务
提供LLM应用的trace数据接收、存储和查询功能
"""
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Float, Text, DateTime, JSON
from database import Base, engine, async_session


class TraceStatus(str, Enum):
    """Trace状态"""
    ACTIVE = "active"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Span:
    """OpenTelemetry Span数据模型"""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    name: str = ""
    kind: str = "internal"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_ms: Optional[float] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict] = field(default_factory=list)
    status: str = "ok"
    status_message: Optional[str] = None


class TraceData(BaseModel):
    """Trace数据接收模型"""
    trace_id: str = Field(..., description="Trace ID")
    spans: List[Dict] = Field(..., description="Span列表")
    resource: Optional[Dict] = Field(None, description="资源信息")
    timestamp: Optional[str] = Field(None, description="时间戳")


class TraceSpan(BaseModel):
    """单个Span数据模型"""
    span_id: str = Field(..., description="Span ID")
    parent_span_id: Optional[str] = Field(None, description="父Span ID")
    name: str = Field(..., description="Span名称")
    kind: str = Field(default="internal", description="Span类型")
    start_time: str = Field(..., description="开始时间")
    end_time: Optional[str] = Field(None, description="结束时间")
    attributes: Dict[str, Any] = Field(default_factory=dict, description="属性")
    events: List[Dict] = Field(default_factory=list, description="事件")
    status: str = Field(default="ok", description="状态")
    status_message: Optional[str] = Field(None, description="状态消息")


class TraceQueryRequest(BaseModel):
    """Trace查询请求"""
    trace_id: Optional[str] = None
    service_name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    min_duration_ms: Optional[float] = None
    status: Optional[str] = None
    limit: int = Field(default=100, description="返回数量限制")


class LLMCallAttributes(BaseModel):
    """LLM调用属性"""
    model: str = Field(..., description="模型名称")
    prompt: str = Field(..., description="提示词")
    completion: Optional[str] = Field(None, description="生成的文本")
    tokens_prompt: Optional[int] = Field(None, description="输入token数")
    tokens_completion: Optional[int] = Field(None, description="输出token数")
    latency_ms: Optional[float] = Field(None, description="延迟毫秒")
    cost: Optional[float] = Field(None, description="成本")


class TraceStore(Base):
    """Trace数据存储表"""
    __tablename__ = "llm_traces"
    
    id = Column(String, default=lambda: f"trace_{uuid.uuid4().hex[:12]}", primary_key=True)
    trace_id = Column(String, index=True)
    span_id = Column(String, index=True)
    parent_span_id = Column(String, nullable=True, index=True)
    name = Column(String)
    kind = Column(String)
    service_name = Column(String, index=True)
    start_time = Column(DateTime)
    end_time = Column(DateTime, nullable=True)
    duration_ms = Column(Float, nullable=True)
    attributes_json = Column(Text, nullable=True)
    events_json = Column(Text, nullable=True)
    status = Column(String, default="ok")
    status_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # LLM特定字段
    llm_model = Column(String, nullable=True)
    llm_prompt = Column(Text, nullable=True)
    llm_completion = Column(Text, nullable=True)
    llm_tokens = Column(Float, nullable=True)
    llm_latency_ms = Column(Float, nullable=True)


class OTelService:
    """OpenTelemetry服务类"""
    
    @staticmethod
    async def init_db():
        """初始化数据库表"""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    @staticmethod
    async def store_trace(trace_data: TraceData) -> Dict[str, Any]:
        """存储Trace数据"""
        async with async_session() as session:
            stored_spans = []
            
            for span_dict in trace_data.spans:
                span = TraceSpan(**span_dict)
                
                trace_id = trace_data.trace_id
                span_id = span.span_id
                
                # 提取服务名称
                service_name = "unknown"
                if span.attributes:
                    service_name = span.attributes.get("service.name", "unknown")
                elif trace_data.resource:
                    service_name = trace_data.resource.get("service.name", "unknown")
                
                # 解析时间
                start_dt = datetime.fromisoformat(span.start_time.replace('Z', '+00:00')) if span.start_time else datetime.utcnow()
                end_dt = None
                duration_ms = None
                
                if span.end_time:
                    end_dt = datetime.fromisoformat(span.end_time.replace('Z', '+00:00'))
                    duration_ms = (end_dt - start_dt).total_seconds() * 1000
                
                # 提取LLM相关属性
                llm_model = None
                llm_prompt = None
                llm_completion = None
                llm_tokens = None
                llm_latency_ms = None
                
                if span.attributes:
                    llm_attrs = span.attributes.get("llm", {})
                    if not llm_attrs:
                        llm_attrs = {k.replace("llm.", ""): v for k, v in span.attributes.items() if k.startswith("llm.")}
                    
                    if llm_attrs or "model" in span.attributes:
                        llm_model = llm_attrs.get("model") or span.attributes.get("model")
                        llm_prompt = llm_attrs.get("prompt") or span.attributes.get("prompt")
                        llm_completion = llm_attrs.get("completion") or span.attributes.get("completion")
                        llm_tokens = llm_attrs.get("tokens") or span.attributes.get("tokens")
                        llm_latency_ms = llm_attrs.get("latency_ms") or span.attributes.get("latency_ms")
                
                # 创建存储记录
                trace_record = TraceStore(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=span.parent_span_id,
                    name=span.name,
                    kind=span.kind,
                    service_name=service_name,
                    start_time=start_dt,
                    end_time=end_dt,
                    duration_ms=duration_ms,
                    attributes_json=json.dumps(span.attributes) if span.attributes else None,
                    events_json=json.dumps(span.events) if span.events else None,
                    status=span.status,
                    status_message=span.status_message,
                    llm_model=llm_model,
                    llm_prompt=llm_prompt,
                    llm_completion=llm_completion,
                    llm_tokens=llm_tokens,
                    llm_latency_ms=llm_latency_ms
                )
                
                session.add(trace_record)
                stored_spans.append({
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "name": span.name,
                    "status": span.status
                })
            
            await session.commit()
            
            return {
                "trace_id": trace_data.trace_id,
                "spans_stored": len(stored_spans),
                "spans": stored_spans
            }
    
    @staticmethod
    async def query_traces(
        trace_id: Optional[str] = None,
        service_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        min_duration_ms: Optional[float] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        """查询Trace数据"""
        from sqlalchemy import select, func
        
        async with async_session() as session:
            query = select(TraceStore)
            
            if trace_id:
                query = query.where(TraceStore.trace_id == trace_id)
            if service_name:
                query = query.where(TraceStore.service_name == service_name)
            if start_time:
                query = query.where(TraceStore.start_time >= start_time)
            if end_time:
                query = query.where(TraceStore.start_time <= end_time)
            if min_duration_ms:
                query = query.where(TraceStore.duration_ms >= min_duration_ms)
            if status:
                query = query.where(TraceStore.status == status)
            
            query = query.order_by(TraceStore.start_time.desc()).limit(limit)
            
            result = await session.execute(query)
            traces = result.scalars().all()
            
            return [
                {
                    "id": t.id,
                    "trace_id": t.trace_id,
                    "span_id": t.span_id,
                    "parent_span_id": t.parent_span_id,
                    "name": t.name,
                    "kind": t.kind,
                    "service_name": t.service_name,
                    "start_time": t.start_time.isoformat() if t.start_time else None,
                    "end_time": t.end_time.isoformat() if t.end_time else None,
                    "duration_ms": round(t.duration_ms, 2) if t.duration_ms else None,
                    "status": t.status,
                    "llm_model": t.llm_model,
                    "llm_latency_ms": round(t.llm_latency_ms, 2) if t.llm_latency_ms else None
                }
                for t in traces
            ]
    
    @staticmethod
    async def get_trace_spans(trace_id: str) -> List[Dict]:
        """获取指定Trace的所有Span"""
        async with async_session() as session:
            from sqlalchemy import select
            
            result = await session.execute(
                select(TraceStore)
                .where(TraceStore.trace_id == trace_id)
                .order_by(TraceStore.start_time)
            )
            spans = result.scalars().all()
            
            return [
                {
                    "span_id": s.span_id,
                    "parent_span_id": s.parent_span_id,
                    "name": s.name,
                    "kind": s.kind,
                    "start_time": s.start_time.isoformat() if s.start_time else None,
                    "end_time": s.end_time.isoformat() if s.end_time else None,
                    "duration_ms": round(s.duration_ms, 2) if s.duration_ms else None,
                    "attributes": json.loads(s.attributes_json) if s.attributes_json else {},
                    "events": json.loads(s.events_json) if s.events_json else [],
                    "status": s.status,
                    "status_message": s.status_message,
                    "llm_model": s.llm_model,
                    "llm_prompt": s.llm_prompt,
                    "llm_completion": s.llm_completion,
                    "llm_tokens": s.llm_tokens,
                    "llm_latency_ms": round(s.llm_latency_ms, 2) if s.llm_latency_ms else None
                }
                for s in spans
            ]
    
    @staticmethod
    async def get_llm_stats(service_name: Optional[str] = None, 
                          start_time: Optional[datetime] = None,
                          end_time: Optional[datetime] = None) -> Dict[str, Any]:
        """获取LLM调用统计"""
        async with async_session() as session:
            from sqlalchemy import select, func
            
            query = select(TraceStore).where(TraceStore.llm_model.isnot(None))
            
            if service_name:
                query = query.where(TraceStore.service_name == service_name)
            if start_time:
                query = query.where(TraceStore.start_time >= start_time)
            if end_time:
                query = query.where(TraceStore.start_time <= end_time)
            
            result = await session.execute(query)
            traces = result.scalars().all()
            
            if not traces:
                return {
                    "total_calls": 0,
                    "avg_latency_ms": 0,
                    "total_tokens": 0,
                    "model_distribution": {},
                    "status_distribution": {}
                }
            
            # 计算统计数据
            latencies = [t.llm_latency_ms for t in traces if t.llm_latency_ms]
            total_tokens = sum(t.llm_tokens for t in traces if t.llm_tokens)
            
            model_dist = {}
            status_dist = {}
            
            for t in traces:
                if t.llm_model:
                    model_dist[t.llm_model] = model_dist.get(t.llm_model, 0) + 1
                if t.status:
                    status_dist[t.status] = status_dist.get(t.status, 0) + 1
            
            return {
                "total_calls": len(traces),
                "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
                "min_latency_ms": round(min(latencies), 2) if latencies else 0,
                "max_latency_ms": round(max(latencies), 2) if latencies else 0,
                "total_tokens": total_tokens,
                "avg_tokens_per_call": round(total_tokens / len(traces), 2) if traces else 0,
                "model_distribution": model_dist,
                "status_distribution": status_dist,
                "time_range": {
                    "earliest": min(t.start_time for t in traces).isoformat() if traces else None,
                    "latest": max(t.start_time for t in traces).isoformat() if traces else None
                }
            }


# FastAPI应用
otel_app = FastAPI(
    title="OpenTelemetry LLM Trace Service",
    version="1.0.0",
    description="LLM应用Trace数据接入和分析服务"
)

@otel_app.on_event("startup")
async def startup():
    await OTelService.init_db()


@otel_app.post("/traces", summary="接收Trace数据")
async def receive_trace(data: TraceData):
    """
    接收OpenTelemetry格式的Trace数据
    
    支持标准OpenTelemetry JSON格式，用于记录LLM调用链路
    """
    try:
        result = await OTelService.store_trace(data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"存储Trace失败: {str(e)}")


@otel_app.post("/traces/batch", summary="批量接收Trace数据")
async def receive_traces_batch(datas: List[TraceData], background_tasks: BackgroundTasks):
    """
    批量接收Trace数据
    适用于大量Trace数据的批量导入
    """
    results = []
    for data in datas:
        try:
            result = await OTelService.store_trace(data)
            results.append(result)
        except Exception as e:
            results.append({
                "trace_id": data.trace_id,
                "error": str(e)
            })
    
    return {
        "total": len(datas),
        "success": sum(1 for r in results if "error" not in r),
        "failed": sum(1 for r in results if "error" in r),
        "results": results
    }


@otel_app.get("/traces", summary="查询Trace列表")
async def query_traces(
    trace_id: Optional[str] = None,
    service_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    min_duration_ms: Optional[float] = None,
    status: Optional[str] = None,
    limit: int = 100
):
    """
    查询Trace列表
    
    支持按Trace ID、服务名、时间范围、状态等条件筛选
    """
    start_dt = None
    end_dt = None
    
    if start_time:
        start_dt = datetime.fromisoformat(start_time)
    if end_time:
        end_dt = datetime.fromisoformat(end_time)
    
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


@otel_app.get("/traces/{trace_id}", summary="获取Trace详情")
async def get_trace_detail(trace_id: str):
    """
    获取指定Trace的详细信息，包括所有Span
    """
    spans = await OTelService.get_trace_spans(trace_id)
    
    if not spans:
        raise HTTPException(status_code=404, detail=f"Trace {trace_id} 不存在")
    
    # 构建Span树
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
        "root_spans": root_spans,
        "all_spans": spans
    }


@otel_app.get("/traces/{trace_id}/spans", summary="获取Trace Span列表")
async def get_trace_spans(trace_id: str):
    """获取指定Trace的所有Span列表"""
    spans = await OTelService.get_trace_spans(trace_id)
    
    if not spans:
        raise HTTPException(status_code=404, detail=f"Trace {trace_id} 不存在")
    
    return {
        "trace_id": trace_id,
        "spans": spans
    }


@otel_app.get("/llm/stats", summary="获取LLM调用统计")
async def get_llm_statistics(
    service_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
):
    """
    获取LLM调用统计分析
    
    统计内容包括：
    - 总调用次数
    - 平均延迟
    - Token使用量
    - 模型分布
    - 状态分布
    """
    start_dt = None
    end_dt = None
    
    if start_time:
        start_dt = datetime.fromisoformat(start_time)
    if end_time:
        end_dt = datetime.fromisoformat(end_time)
    
    stats = await OTelService.get_llm_stats(
        service_name=service_name,
        start_time=start_dt,
        end_time=end_dt
    )
    
    return stats


@otel_app.get("/health", summary="健康检查")
async def health():
    return {"status": "ok", "service": "otel-trace-service"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(otel_app, host="0.0.0.0", port=8001)
