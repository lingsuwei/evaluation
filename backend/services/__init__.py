"""服务模块初始化"""
from .otel_service import OTelService, otel_app, TraceData, TraceSpan
from .dynamic_dataset_service import DynamicDatasetService
from .inference_service import InferenceService
from .onto_agent_client import (
    OntoAgentClient,
    OntoAgentConfig,
    OntoAgentInferenceAdapter,
    call_onto_agent,
    test_connection
)
from .onto_agent_integration import (
    OntoAgentInferenceService,
    call_onto_agent_inference,
    evaluate_with_onto_agent
)

__all__ = [
    "OTelService", 
    "otel_app", 
    "TraceData", 
    "TraceSpan",
    "DynamicDatasetService",
    "InferenceService",
    # Onto Agent 相关
    "OntoAgentClient",
    "OntoAgentConfig",
    "OntoAgentInferenceAdapter",
    "OntoAgentInferenceService",
    "call_onto_agent",
    "call_onto_agent_inference",
    "test_connection",
    "evaluate_with_onto_agent"
]
