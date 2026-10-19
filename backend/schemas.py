from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Dict, List, Any
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class DatasetItem(BaseModel):
    question: str
    prompt: Optional[str] = None  # 提示词（场景评测用）
    context: Optional[str] = None
    ground_truth: Optional[str] = None  # 标准答案（给 golden_match/correctness 用）
    model_answer: str = ""  # 模型回答（场景导入时允许空，由 ground_truth 回退）
    judging_mode: Optional[str] = None  # 评测模式（Ground_Truth评测/基于Prompt评测）
    scenario_key: Optional[str] = Field(
        None, description="可选：绑定场景模板 scenario_key，如 generic_l0 或 ditem_*"
    )
    # 多轮对话/工具类指标所需的自然语言字段(Phase 1 加,向后兼容老用例):
    # scenario: 多轮对话场景描述(用户模拟器据此生成 user 回合)
    # expected_outcome: 期望结果(给 task_completion/conversation_completeness 等 LLM judge 指标用,
    #   与 ground_truth 语义不同:ground_truth 是标准答案文本,expected_outcome 是结果导向描述)
    # user_description: 用户画像(多轮模拟器据此生成符合角色的 user 回合)
    scenario: Optional[str] = Field(None, description="多轮对话场景描述（自然语言）")
    expected_outcome: Optional[str] = Field(
        None, description="期望结果（自然语言，给工具/多轮类 LLM judge 指标用）"
    )
    user_description: Optional[str] = Field(
        None, description="用户画像（多轮模拟器用，自然语言）"
    )


class DatasetUploadResponse(BaseModel):
    dataset_id: str
    item_count: int
    quality_warnings: List[str] = Field(
        default_factory=list,
        description="评测集质量预校验警告（不阻断导入，仅提示）",
    )


class UpdateDatasetRequest(BaseModel):
    data: List[Any]  # 更新的数据内容
    name: Optional[str] = None  # 可选的数据集名称


class WorkflowConfigSchema(BaseModel):
    """工作流配置；inference_config.workflow 存在则该 Agent 派生为 workflow 类型（W1-2）"""

    orchestration_pattern: str = Field(
        default="supervisor_predefined",
        description="编排模式：supervisor_predefined/hierarchical/sequential/parallel_dag",
    )


class InferenceConfigSchema(BaseModel):
    """Agent配置"""
    url: str = Field(default="", description="Agent地址")
    endpoint: str = Field(default="/agentic/runs/sync", description="接口端点")
    headers: Dict[str, str] = Field(default_factory=dict, description="请求头")
    auth: Dict[str, Any] = Field(default_factory=lambda: {"type": "none"}, description="认证信息")
    timeout: int = Field(default=1800, description="超时时间（秒）")
    request_delay: float = Field(default=0.1, description="请求间隔（秒）")
    payload_template: Dict[str, Any] = Field(default_factory=dict, description="请求体模板")
    workspace_id: Optional[str] = Field(None, description="工作空间ID")
    workspace_name: Optional[str] = Field(
        None, description="工作空间展示名（与 Agent 配置页下拉选项一致，供历史列表展示）"
    )
    provider: Optional[str] = Field(None, description="被测推理模型供应商，如 智谱 / DeepSeek")
    model: Optional[str] = Field(None, description="被测推理模型名称，如 glm-4.7")
    model_version: Optional[str] = Field(None, description="被测推理模型版本或快照")
    model_id: Optional[str] = Field(None, description="被测推理模型 ID 或部署 ID")
    workflow: Optional[WorkflowConfigSchema] = Field(
        None,
        description="工作流配置；填则该 Agent 派生为 workflow 类型（agent_kind 后端派生）",
    )


class AgentProbeCheck(BaseModel):
    name: str
    ok: bool
    message: str


class AgentProbeResponse(BaseModel):
    ok: bool
    has_warnings: bool = False
    resolved_url: Optional[str] = None
    workspace_id: Optional[str] = None
    langfuse_project: Optional[str] = None
    langfuse_project_status: Optional[str] = None
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)


class AgentProbeRequest(BaseModel):
    inference_config: InferenceConfigSchema = Field(..., description="待预检的推理配置")


class AgentRegisterRequest(BaseModel):
    name: str = Field(..., description="Agent 名称")
    description: Optional[str] = Field(None, description="Agent 描述")
    inference_config: Optional[InferenceConfigSchema] = Field(None, description="Agent配置")
    skip_probe: bool = Field(
        default=False,
        description="为 true 时跳过 sync 健康预检（仅建议在明确知晓风险时使用）",
    )
    logical_agent_id: Optional[str] = Field(None, description="逻辑 Agent ID，多版本共享")
    version: Optional[str] = Field(None, description="版本号，如 v1.0.0")
    version_tag: Optional[str] = Field(None, description="版本标签，如 stable / canary")
    parent_agent_id: Optional[str] = Field(
        None,
        description="注册新版本时传入源 agent_id，与 logical_agent_id 同族共享名称",
    )
    langfuse_project: Optional[str] = Field(
        None,
        description="Langfuse 项目 id（通常留空，由注册时从 onto-agent 自动匹配）",
    )
    seed_default_checkpoints: bool = Field(
        default=True,
        description="onto-agent 类 Agent 注册时预置 5 条默认识别过程点（仅 Agent 过程点）",
    )


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, description="Agent 名称")
    description: Optional[str] = Field(None, description="Agent 描述")
    inference_config: Optional[InferenceConfigSchema] = Field(None, description="Agent配置")
    skip_probe: bool = Field(
        default=False,
        description="为 true 时跳过 sync 健康预检",
    )
    is_active: Optional[bool] = Field(None, description="是否激活")
    logical_agent_id: Optional[str] = Field(None, description="逻辑 Agent ID")
    version: Optional[str] = Field(None, description="版本号")
    version_tag: Optional[str] = Field(None, description="版本标签")
    langfuse_project: Optional[str] = Field(
        None,
        description="Langfuse 项目 id；空字符串表示清除并由 onto-agent 重新自动匹配",
    )


class CheckpointSeedSummary(BaseModel):
    created: int = 0
    skipped: bool = False
    reason: Optional[str] = None


class AgentResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    inference_config: Optional[Dict[str, Any]] = None
    logical_agent_id: Optional[str] = None
    version: Optional[str] = None
    version_tag: Optional[str] = None
    langfuse_project: Optional[str] = None
    created_at: str
    is_active: bool
    agent_kind: str = Field(default="single", description="single | workflow（由 inference_config.workflow 派生）")
    checkpoint_seed: Optional[CheckpointSeedSummary] = None


class LangfuseSourceConfig(BaseModel):
    project: Optional[str] = Field(None, description="Langfuse 项目 id（onto-eval 项目标识）")
    source_task_id: Optional[str] = Field(None, description="来源评测任务 ID，拉取其下所有 trace 的答案")
    trace_ids: Optional[List[str]] = Field(None, description="指定 trace ID 列表，按顺序对应评测集题目")
    tags: Optional[str] = Field(None, description="按标签筛选 trace")
    session_id: Optional[str] = Field(None, description="按 session ID 筛选 trace")
    agent_name: Optional[str] = Field(None, description="按 Agent 名称筛选 trace")
    enable_process_eval: bool = Field(
        default=True,
        description="启用过程回放：拉取每个 trace 的 observations 跑 Checkpoint + Scenario 子步骤",
    )


class EvaluateRequest(BaseModel):
    dataset_id: str
    model_name: Optional[str] = None  # 可选的模型名称标识，离线评估时使用
    agent_id: Optional[str] = None  # Agent ID，用于验证身份或在线评估
    online_mode: bool = False  # 是否在线评估模式（调用推理接口获取答案）
    inference_rounds: int = Field(
        default=1,
        ge=1,
        le=10,
        description="每题 Agent 推理运行次数（稳定性评测 K 轮）",
    )
    llm_model: Optional[str] = Field(default="glm", description="评估使用的LLM模型 (glm)")
    selected_indices: Optional[List[int]] = Field(default=None, description="要评估的评测项索引列表，None表示评估所有项")
    selected_metrics: Optional[List[str]] = Field(default=None, description="要使用的评估因子列表，None表示使用所有可用因子")
    use_bm25: bool = Field(default=False, description="是否使用BM25算法计算忠实度(仅对faithfulness指标有效)")
    use_doc_library: bool = Field(default=False, description="是否使用文档库作为BM25评估的参考(仅当use_bm25=True时有效)")
    ontology_package: Optional[str] = Field(default=None, description="本体包名称(使用指定目录的md文档进行BM25评估，仅对faithfulness且use_bm25=True时有效)")
    custom_scripts: Optional[Dict[str, str]] = Field(default=None, description="自定义脚本映射：{用例索引(str): 脚本ID}，指定用例使用脚本评估代替LLM评估")
    custom_metric_ids: Optional[List[int]] = Field(default=None, description="自定义评测指标 ID 列表（CustomMetric 表的 ID）")
    llm_max_tokens: Optional[int] = Field(None, ge=512, le=8192, description="临时覆盖评测LLM的最大输出tokens")
    llm_temperature: Optional[float] = Field(None, ge=0.0, le=1.0, description="临时覆盖评测LLM的温度参数")
    langfuse_source: Optional[LangfuseSourceConfig] = Field(None, description="离线评估时从 Langfuse 获取历史答案的配置")
    auto_scenario_score: Optional[bool] = Field(
        default=True,
        description="是否在评估 Pipeline 结束后自动跑场景子步骤打分（在线 / 离线均支持）",
    )
    enable_process_consistency: Optional[bool] = Field(
        default=True,
        description="是否评测过程一致性维度（在线 Checkpoint / 离线 Langfuse 回放）",
    )
    enable_stability: Optional[bool] = Field(
        default=True,
        description="是否评测运行稳定性维度（仅控制结论显示，不影响推理轮次）",
    )
    parallel_inference: bool = Field(
        default=True,
        description="在线推理是否并发下发到 Agent（True=并发，False=题目+轮次全串行，适合小参数模型 Agent）",
    )


class EvaluateResponse(BaseModel):
    task_uuid: str


class MetricResult(BaseModel):
    name: str
    score: float
    threshold: float
    passed: bool
    duration_ms: int
    cot: Optional[str] = None


class LLMModelCreateRequest(BaseModel):
    """创建LLM模型配置"""
    id: str = Field(..., description="模型标识，如 glm")
    name: str = Field(..., description="显示名称，如 智谱GLM-4.7")
    provider: Optional[str] = Field(None, description="模型厂商，如 智谱AI")
    base_url: str = Field(..., description="API基础URL")
    model: str = Field(..., description="模型名称，如 glm-4.7")
    api_key: Optional[str] = Field(None, description="API Key")
    is_default: bool = Field(default=False, description="是否为默认模型")
    max_tokens: Optional[int] = Field(None, ge=1, description="最大输出tokens，默认4096")
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0, description="温度参数，默认0")
    timeout: Optional[float] = Field(None, ge=30, le=600, description="API超时(秒)，默认180")


class LLMModelUpdateRequest(BaseModel):
    """更新LLM模型配置"""
    name: Optional[str] = Field(None, description="显示名称")
    provider: Optional[str] = Field(None, description="模型厂商")
    base_url: Optional[str] = Field(None, description="API基础URL")
    model: Optional[str] = Field(None, description="模型名称")
    api_key: Optional[str] = Field(None, description="API Key")
    is_default: Optional[bool] = Field(None, description="是否为默认模型")
    max_tokens: Optional[int] = Field(None, ge=1, description="最大输出tokens")
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0, description="温度参数")
    timeout: Optional[float] = Field(None, ge=30, le=600, description="API超时(秒)")


class LLMModelResponse(BaseModel):
    id: str
    name: str
    provider: Optional[str] = None
    base_url: str
    model: str
    api_key: Optional[str] = None  # 返回时脱敏
    is_default: bool
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CheckpointTemplateCreateRequest(BaseModel):
    agent_name: Optional[str] = Field(None, description="Agent 名称（与逻辑 Agent 一一对应，推荐）")
    logical_agent_id: Optional[str] = Field(None, description="逻辑 Agent ID（兼容，与 agent_name 二选一）")
    name: str = Field(..., description="过程点名称")
    description: Optional[str] = None
    order: Optional[int] = Field(None, ge=1, description="期望顺序序号，空表示只要求出现")
    enforce_order: bool = Field(
        False,
        description="启用顺序依赖：命中过程点且顺序须符合 order；未启用时 order 仅用于展示排序",
    )
    required: bool = True
    match_rules: Dict[str, Any] = Field(..., description="匹配规则 JSON")
    score_weight: float = Field(1.0, ge=0)
    match_mode: str = Field("rule", description="rule | llm_judge")
    langfuse_evaluator_id: Optional[str] = None
    template_set_name: Optional[str] = None
    enabled: bool = True

    @field_validator("match_mode")
    @classmethod
    def _match_mode(cls, v: str) -> str:
        m = (v or "rule").strip().lower()
        if m not in ("rule", "llm_judge"):
            raise ValueError("match_mode 必须为 rule 或 llm_judge")
        return m

    @model_validator(mode="after")
    def _require_agent_ref(self):
        if not (self.agent_name or "").strip() and not (self.logical_agent_id or "").strip():
            raise ValueError("请提供 agent_name 或 logical_agent_id")
        return self


class CheckpointTemplateUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    order: Optional[int] = Field(None, ge=1)
    enforce_order: Optional[bool] = None
    required: Optional[bool] = None
    match_rules: Optional[Dict[str, Any]] = None
    score_weight: Optional[float] = Field(None, ge=0)
    match_mode: Optional[str] = None
    langfuse_evaluator_id: Optional[str] = None
    template_set_name: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("match_mode")
    @classmethod
    def _match_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        m = v.strip().lower()
        if m not in ("rule", "llm_judge"):
            raise ValueError("match_mode 必须为 rule 或 llm_judge")
        return m


class CheckpointReorderRequest(BaseModel):
    agent_name: str = Field(..., description="Agent 名称")
    template_ids: List[str] = Field(..., min_length=1, description="按新顺序排列的 template id 列表")


class CheckpointTemplateResponse(BaseModel):
    id: str
    logical_agent_id: str
    agent_name: str = Field("", description="Agent 展示名称")
    name: str
    description: Optional[str] = None
    order: Optional[int] = None
    enforce_order: bool = False
    required: bool
    match_rules: Dict[str, Any]
    score_weight: float
    match_mode: str
    langfuse_evaluator_id: Optional[str] = None
    template_set_name: Optional[str] = None
    enabled: bool
    created_at: str
    updated_at: str


class CheckpointPreviewRequest(BaseModel):
    trace_id: str = Field(..., description="Langfuse trace id 或 agent_run / trace_id")
    project: Optional[str] = Field(None, description="onto-eval 项目 id")
    source: Optional[str] = Field(
        None,
        description="预览数据源：默认 fact_layer 优先；langfuse 强制拉 Langfuse",
    )
    match_rules: Optional[Dict[str, Any]] = Field(
        None,
        description="可选：用表单中的规则试跑（未保存时覆盖库内模板）",
    )


class CheckpointPreviewMatchRequest(BaseModel):
    trace_id: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1, description="用于解析 Langfuse 项目")
    checkpoint_name: str = Field("未命名过程点", description="过程点名称（仅展示）")
    order: Optional[int] = Field(None, ge=1)
    required: bool = True
    match_rules: Dict[str, Any] = Field(..., description="当前表单中的 match_rules")
    project: Optional[str] = None
    source: Optional[str] = None


class CheckpointReferenceTemplateItem(BaseModel):
    name: str
    description: Optional[str] = None
    order: Optional[int] = None
    required: bool = True
    score_weight: float = 1.0
    match_mode: str = "rule"
    enabled: bool = True
    enforce_order: bool = False
    match_rules: Dict[str, Any] = Field(default_factory=dict)


class CheckpointReferenceCatalogResponse(BaseModel):
    id: str
    description: Optional[str] = None
    source: Optional[str] = None
    template_set_name: str
    count: int
    templates: List[CheckpointReferenceTemplateItem]


class CheckpointSeedFromReferenceRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, description="Agent 名称")
    template_names: Optional[List[str]] = Field(
        None,
        description="仅初始化指定名称；空则补缺全部参考项",
    )


class CheckpointSeedFromReferenceResponse(BaseModel):
    agent_name: str
    logical_agent_id: str
    created: int
    created_names: List[str] = Field(default_factory=list)
    skipped_names: List[str] = Field(default_factory=list)


class CheckpointWeightSummary(BaseModel):
    enabled_count: int
    total_count: int
    weight_sum: float
    normalized: bool


class CheckpointNormalizeWeightsRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, description="Agent 名称")


class CheckpointNormalizeWeightsResponse(BaseModel):
    agent_name: str
    logical_agent_id: str
    weight_summary: CheckpointWeightSummary


class CheckpointSampleRunItem(BaseModel):
    agent_run_id: str
    trace_id: str
    question_index: Optional[int] = None
    question_preview: str = ""
    started_at: str = ""
    status: str = ""
    trace_sync_status: Optional[str] = None
    has_span_tree: bool = False


class CheckpointSampleRunsResponse(BaseModel):
    agent_name: str
    logical_agent_id: str
    count: int
    runs: List[CheckpointSampleRunItem] = Field(default_factory=list)


class CheckpointBatchPreviewRequest(BaseModel):
    agent_name: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    project: Optional[str] = None
    source: Optional[str] = None


class CheckpointBatchPreviewResultItem(BaseModel):
    template_id: str
    template_name: str
    enabled: bool
    required: bool
    order: Optional[int] = None
    hit: bool
    passed: bool
    skipped: bool = False
    skip_reason: Optional[str] = None
    matched_observation_name: Optional[str] = None
    suggestion: Optional[str] = None


class CheckpointBatchPreviewResponse(BaseModel):
    agent_name: str
    logical_agent_id: str
    trace_id: str
    project: Optional[str] = None
    observation_count: int
    observation_source: str
    enabled_total: int
    hit_count: int
    required_miss_count: int
    all_required_hit: bool
    results: List[CheckpointBatchPreviewResultItem]


class CheckpointPreviewResult(BaseModel):
    template_id: str
    template_name: str
    hit: bool
    required: bool
    order: Optional[int] = None
    order_ok: Optional[bool] = None
    matched_count: int = 0
    matched_observation_id: Optional[str] = None
    matched_observation_name: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    skipped: bool = False
    skip_reason: Optional[str] = None


# ── AI 对话式配置（阶段 1）──

class ChatSessionCreateRequest(BaseModel):
    intent_text: Optional[str] = Field(None, description="用户原始意图文本（阶段1快速模式用；阶段2对话模式可留空）")
    model_key: Optional[str] = Field(None, description="对话引擎模型标识，None 用默认")


class ChatDraftPatchRequest(BaseModel):
    """草稿部分更新，合并到 draft_request（覆盖同名键）。"""
    # 用 Dict 宽松接收，避免与 EvaluateRequest 字段耦合
    fields: Dict[str, Any] = Field(default_factory=dict, description="要合并的 EvaluateRequest 字段")


class ChatInterpretRequest(BaseModel):
    """触发意图解析（入参为空，用 session.intent_text）。"""
    pass


class ChatMessageCreateRequest(BaseModel):
    """阶段 2：追加用户消息并触发 agent loop。"""
    content: str = Field(..., description="用户消息内容")


class ChatMessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[Any] = None
    tool_result: Optional[Any] = None
    tokens: Optional[int] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class ChatSessionOut(BaseModel):
    id: str
    title: Optional[str] = None
    status: Optional[str] = None
    intent: Optional[str] = None
    model_key: Optional[str] = None
    intent_text: Optional[str] = None
    draft_request: Optional[Any] = None
    confidence: Optional[float] = None
    missing_fields: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ChatSessionListItem(BaseModel):
    """会话列表项（不含 draft_request / intent_text 大字段）。"""
    id: str
    title: Optional[str] = None
    status: Optional[str] = None
    intent: Optional[str] = None
    model_key: Optional[str] = None
    confidence: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ChatInterpretResponse(BaseModel):
    session_id: str
    status: str
    draft: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None
    missing_fields: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    intent: Optional[str] = None
