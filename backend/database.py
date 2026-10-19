from sqlalchemy import create_engine, Column, String, Float, Integer, Boolean, DateTime, Text, JSON, LargeBinary, inspect, text, ForeignKey, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from datetime import datetime
from config import DATABASE_URL, async_database_url, is_postgresql_url, is_sqlite_url
from logger import log

# 连接池参数：PostgreSQL 并发请求（span-tree / detail / batch call-counts）时避免连接等待。
# SQLite 忽略 pool_size/max_overflow（使用默认 StaticPool / 单连接）。
_engine_kwargs: dict = {"echo": False}
if is_postgresql_url(DATABASE_URL):
    _engine_kwargs.update(
        pool_size=20,
        max_overflow=20,
        pool_pre_ping=True,   # 探活避免使用已被 DB 端关闭的连接
        pool_recycle=3600,    # 1h 回收，防止长时间持有导致连接陈旧
    )
engine = create_async_engine(async_database_url(DATABASE_URL), **_engine_kwargs)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 轻量读连接池：仅供 GET /datasets 等只读列表接口使用，独立于主连接池，
# 避免被评估任务 / 详情预热 / trace sync 等后台任务挤占导致前端"加载中"卡死。
# SQLite 走默认 StaticPool（单连接），独立 engine 仅做隔离。
_read_engine_kwargs: dict = {"echo": False}
if is_postgresql_url(DATABASE_URL):
    _read_engine_kwargs.update(
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,    # 池满时快速失败，避免前端死等
        pool_pre_ping=True,
        pool_recycle=3600,
    )
read_engine = create_async_engine(async_database_url(DATABASE_URL), **_read_engine_kwargs)
read_session = sessionmaker(read_engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


class Dataset(Base):
    __tablename__ = "datasets"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    data = Column(Text)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)
    item_count = Column(Integer)


class Agent(Base):
    __tablename__ = "agents"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    inference_config = Column(Text, nullable=True)  # JSON string: 推理接口配置
    logical_agent_id = Column(String, nullable=True, index=True)
    version = Column(String, nullable=True)
    version_tag = Column(String, nullable=True)
    langfuse_project = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    agent_kind = Column(
        String, nullable=False, default="single", server_default="single", index=True
    )  # single | workflow（W1-1：工作流 Agent 判别）


class EvaluationTask(Base):
    __tablename__ = "evaluation_tasks"

    id = Column(String, primary_key=True)
    dataset_id = Column(String)
    models = Column(Text)  # JSON array
    status = Column(String, default="pending")
    progress_current = Column(Integer, default=0)
    progress_total = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)  # 评测总结
    paused_at = Column(DateTime, nullable=True)  # 暂停时间
    resume_from = Column(Integer, default=0)  # 恢复时从哪个问题开始
    resume_agent_id = Column(String, nullable=True)  # 恢复时从哪个 Agent 版本继续（同题多版本）
    run_count = Column(Integer, default=1)  # 已废弃：DB 保留兼容；新任务固定 1，Judge 见 JUDGE_RUN_COUNT
    inference_rounds = Column(Integer, default=1, nullable=False)  # 每题 Agent 推理次数
    llm_model = Column(String, default="glm")  # 评估使用的LLM模型
    agent_id = Column(String, nullable=True)  # Agent ID（在线评估）
    use_bm25 = Column(Boolean, default=False)  # 是否使用BM25算法
    use_doc_library = Column(Boolean, default=False)  # 是否使用文档库
    ontology_package = Column(String, nullable=True)  # 本体包名称(使用指定目录的md文档进行BM25评估)
    selected_metrics = Column(Text, nullable=True)  # JSON array，创建时选定的评估因子
    selected_indices = Column(Text, nullable=True)  # JSON array，创建时选定的评测项索引
    custom_scripts = Column(Text, nullable=True)  # JSON object，用例索引 → 脚本 ID
    custom_metric_ids = Column(Text, nullable=True)  # JSON array，评测时选用的自定义指标 ID 列表，如 [1, 3]
    runtime_metric_targets = Column(Text, nullable=True)  # JSON object，本任务运行时目标阈值
    verdict_thresholds = Column(Text, nullable=True)  # JSON object，本任务四维结论阈值覆盖
    auto_scenario_score = Column(Boolean, default=True)  # 评测完成后自动场景子步骤打分（E-1，仅在线）
    evaluation_mode = Column(String, default="offline")  # online | offline
    process_eval_completed = Column(Boolean, default=False)  # 离线任务跑完 langfuse 过程回放（CheckpointResult + ScenarioStepResult 落库）
    langfuse_source = Column(JSON, nullable=True)  # 离线任务 Langfuse 配置（含 project/tags/session_id/agent_name 等）
    enable_process_consistency = Column(Boolean, default=True)  # 过程一致性维度开关（关闭后 Pipeline 跳过离线回放 + 结论显示 NA）
    enable_stability = Column(Boolean, default=True)  # 运行稳定性维度开关：控制结论显示 + 质量总分稳定性分权重（关闭后稳定性权重并入结果分）
    parallel_inference = Column(Boolean, default=True)  # 在线推理并发开关（True=并发，False=题目+轮次全串行，适合小参数模型 Agent）


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        Index("ix_evaluation_results_task_id_metric_name", "task_id", "metric_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    task_id = Column(String, index=True)
    question_id = Column(Integer)
    model = Column(String)
    metric_name = Column(String)
    score = Column(Float)  # 保留兼容，实际使用 avg_score
    threshold = Column(Float)
    passed = Column(Boolean)
    duration_ms = Column(Integer)  # 保留兼容
    cot = Column(Text, nullable=True)
    eval_reason = Column(Text, nullable=True)  # 保留兼容
    # 新增字段
    question = Column(Text, nullable=True)  # 问题内容
    ground_truth = Column(Text, nullable=True)  # 标准答案
    agent_name = Column(String, nullable=True)  # Agent名称
    agent_answer = Column(Text, nullable=True)  # Agent回答
    avg_score = Column(Float, nullable=True)  # 平均得分
    eval_model = Column(String, nullable=True)  # 评估模型
    eval_rounds = Column(Integer, nullable=True)  # 评估轮次总数
    inference_duration_ms = Column(Integer, nullable=True)  # 推理耗时(毫秒)
    eval_method = Column(String, nullable=True)  # 评估方式: "script" 或 "llm"
    script_name = Column(String, nullable=True)  # 脚本名称(仅脚本评估时有值)
    agent_run_id = Column(String, nullable=True, index=True)  # P1: 关联 AgentRun


class AgentRun(Base):
    """一次推理执行锚点（P1+）；评测语义在 eval_context，非必填列（ADR-028）"""

    __tablename__ = "agent_runs"

    id = Column(String, primary_key=True)
    origin = Column(String, nullable=False, default="evaluation")  # evaluation | live
    evaluation_mode = Column(
        String,
        nullable=True,
        default="online",
        index=True,
    )  # online | offline（langfuse 历史回放 stub）
    eval_context = Column(JSON, nullable=True)  # 评测：task_id/dataset_id/question_id/run_index
    task_id = Column(String, nullable=True, index=True)  # 过渡期镜像，PG 迁移后删除
    dataset_id = Column(String, nullable=True, index=True)
    question_id = Column(Integer, nullable=True)
    run_index = Column(Integer, nullable=True, default=0)
    agent_id = Column(String, nullable=False)
    logical_agent_id = Column(String, nullable=True)
    agent_version = Column(String, nullable=True)
    agent_name = Column(String, nullable=True)
    trace_id = Column(String, nullable=True)
    root_trace_id = Column(String, nullable=True, index=True)  # C-03 / ADR-032
    langfuse_project = Column(String, nullable=True)
    inference_model_provider = Column(String, nullable=True)
    inference_model_name = Column(String, nullable=True)
    inference_model_version = Column(String, nullable=True)
    inference_model_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    tokens_total = Column(Integer, nullable=True)
    input_snapshot = Column(JSON, nullable=True)  # P2 B-01：有界输入摘要
    output_snapshot = Column(JSON, nullable=True)  # P2 B-01：有界输出摘要
    agent_snapshot = Column(Text, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    trace_sync_status = Column(String, nullable=True)  # pending_sync / syncing / synced / failed / skipped


class RunSpanNode(Base):
    """Run 的 Span Tree 节点（P2）；含 P5 Replay 隔离字段（ADR-029）"""

    __tablename__ = "run_span_nodes"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "observation_id", name="uq_run_span_observation"),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id = Column(String, nullable=True, index=True)
    sibling_order = Column(Integer, nullable=False, default=0)
    depth = Column(Integer, nullable=True)
    observation_id = Column(String, nullable=True)
    node_type = Column(String, nullable=True)
    name = Column(String, nullable=True)
    attributes = Column(JSON, nullable=True)
    aggregate_key = Column(String, nullable=True, index=True)
    semantic_role = Column(String, nullable=True, index=True)  # RF-03 / §11.3.3
    summary_text = Column(Text, nullable=True)  # P2 B-03：≤1KB，P5 用
    status = Column(String, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    tokens = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    step_index = Column(Integer, nullable=True)
    is_deterministic_io = Column(Boolean, nullable=False, default=False)
    mock_response_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RunSemanticNode(Base):
    """Semantic Facts（P5 §11.3.7）；与 span 通过 observation_id 关联。"""

    __tablename__ = "run_semantic_nodes"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "observation_id",
            name="uq_run_semantic_observation",
        ),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    observation_id = Column(String, nullable=True, index=True)
    run_span_node_id = Column(String, nullable=True, index=True)
    reasoning_summary = Column(Text, nullable=True)
    embedding_id = Column(String, nullable=True)
    generator_meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RunMetric(Base):
    """Execution Fact Layer 指标（P2 B-04 / §11.3.4）"""

    __tablename__ = "run_metrics"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "metric_key",
            "source",
            "dims_hash",
            name="uq_run_metric_dims",
        ),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_key = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, default="runtime")  # runtime | evaluation
    value_num = Column(Float, nullable=True)
    value_json = Column(JSON, nullable=True)
    dims = Column(JSON, nullable=True)
    dims_hash = Column(String, nullable=False, default="")
    value = Column(Text, nullable=True)  # 遗留 TEXT 兼容
    created_at = Column(DateTime, default=datetime.utcnow)


class CheckpointResult(Base):
    """过程点批量评估结果（P2 B-05 / §11.3.5）"""

    __tablename__ = "checkpoint_results"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "checkpoint_id",
            name="uq_checkpoint_result_per_run",
        ),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_id = Column(String, nullable=False, index=True)
    passed = Column(Boolean, nullable=False, default=False)
    evidence = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RunComparison(Base):
    """Run 对比会话（P3 §10.2）；一次对比可产出多条 RunDiff。"""

    __tablename__ = "run_comparisons"

    id = Column(String, primary_key=True)
    comparison_scope = Column(String, nullable=False, default="manual")
    task_id = Column(String, nullable=True, index=True)
    question_id = Column(Integer, nullable=True)
    run_index = Column(Integer, nullable=True)
    left_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    right_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RunDiff(Base):
    """单类 Diff 落库（P3 §10.4）；一类 diff 一行。"""

    __tablename__ = "run_diffs"
    __table_args__ = (
        UniqueConstraint(
            "comparison_id",
            "diff_type",
            name="uq_run_diff_per_comparison_type",
        ),
    )

    id = Column(String, primary_key=True)
    comparison_id = Column(
        String,
        ForeignKey("run_comparisons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    diff_type = Column(String, nullable=False, index=True)
    schema_version = Column(String, nullable=False, default="1.0")
    left_run_id = Column(String, nullable=False, index=True)
    right_run_id = Column(String, nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Experiment(Base):
    """任务级版本实验（P3-B / TD-P3-01）。"""

    __tablename__ = "experiments"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    baseline_agent_id = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default="draft")  # draft | running | completed | failed
    comparison_scope = Column(String, nullable=False, default="version_experiment")
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class ExperimentSummary(Base):
    """单题版本对比摘要（P3 experiment_summaries）。"""

    __tablename__ = "experiment_summaries"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "question_id",
            "right_agent_id",
            name="uq_experiment_summary_per_pair",
        ),
    )

    id = Column(String, primary_key=True)
    experiment_id = Column(
        String,
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id = Column(Integer, nullable=False)
    comparison_id = Column(String, ForeignKey("run_comparisons.id", ondelete="SET NULL"), nullable=True)
    left_agent_id = Column(String, nullable=False)
    right_agent_id = Column(String, nullable=False)
    left_run_id = Column(String, nullable=False)
    right_run_id = Column(String, nullable=False)
    outcome_winner = Column(String, nullable=True)  # left | right | tie | unknown
    highlights = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentRunTraceRef(Base):
    """AgentRun 与 trace 1:N 映射（P3-B / C-03）。"""

    __tablename__ = "agent_run_trace_refs"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "trace_id", name="uq_agent_run_trace_ref"),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trace_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False, default="root")  # root | sub_agent | retry
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class TraceSyncJob(Base):
    """Langfuse trace 异步物化任务队列（P1.5）"""

    __tablename__ = "trace_sync_jobs"

    id = Column(String, primary_key=True)
    agent_run_id = Column(String, nullable=False, index=True)
    task_id = Column(String, nullable=False, index=True)
    trace_id = Column(String, nullable=True)
    langfuse_project = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending|running|success|failed|skipped
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    failure_category = Column(String, nullable=True)  # trace_not_found/ownership_mismatch/agent_run_missing/config_error/auth_error/transient/unknown/incomplete_materialization
    rerun_count = Column(Integer, nullable=False, default=0)  # 物化不完整独立重试计数（不消耗 attempts）
    incomplete_metadata = Column(Text, nullable=True)  # 物化不完整时的 obs_count 等元数据 JSON


class EvaluationRound(Base):
    """评估轮次详情表"""
    __tablename__ = "evaluation_rounds"
    
    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    result_id = Column(Integer, index=True)  # 关联 EvaluationResult.id
    round_num = Column(Integer)  # 轮次序号(1,2,3...)
    score = Column(Float)  # 本轮得分
    reason = Column(Text, nullable=True)  # 评估分析内容
    duration_ms = Column(Integer, nullable=True)  # 评估耗时(毫秒)


class EvaluationScript(Base):
    """自定义评估脚本表"""
    __tablename__ = "evaluation_scripts"

    id = Column(String, primary_key=True)  # 脚本ID
    name = Column(String, nullable=False)  # 脚本名称
    description = Column(Text, nullable=True)  # 脚本描述
    script_code = Column(Text, nullable=False)  # 脚本代码内容
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProcessCheckpointTemplate(Base):
    """Agent 关键过程点模板（v2.1 M2）"""

    __tablename__ = "process_checkpoint_templates"

    id = Column(String, primary_key=True)
    logical_agent_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    order = Column(Integer, nullable=True)
    enforce_order = Column(Boolean, default=False, nullable=False)
    required = Column(Boolean, default=True)
    match_rules = Column(Text, nullable=False)  # JSON
    score_weight = Column(Float, default=1.0)
    match_mode = Column(String, default="rule")  # rule | llm_judge
    langfuse_evaluator_id = Column(String, nullable=True)
    template_set_name = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScenarioEvalTemplate(Base):
    """场景 QA 子步骤评测模板（场景 C；绑定 scenario_key / dataset）。"""

    __tablename__ = "scenario_eval_templates"

    id = Column(String, primary_key=True)
    scenario_key = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    ground_truth_ref = Column(String, nullable=True)
    dataset_id = Column(String, nullable=True, index=True)
    dataset_item_id = Column(String, nullable=True, index=True)
    question_match = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScenarioEvalStep(Base):
    """场景专属关键子步骤定义。"""

    __tablename__ = "scenario_eval_steps"
    __table_args__ = (
        UniqueConstraint("template_id", "step_key", name="uq_scenario_step_key"),
    )

    id = Column(String, primary_key=True)
    template_id = Column(
        String,
        ForeignKey("scenario_eval_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_key = Column(String, nullable=False)
    name = Column(String, nullable=False)
    step_order = Column(Integer, nullable=False, default=0)
    weight = Column(Float, nullable=False, default=0.1)
    required = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)
    enforce_order = Column(Boolean, default=False, nullable=False)
    gt_criteria = Column(JSON, nullable=True)
    gt_expected = Column(JSON, nullable=True)
    match_rules = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ScenarioStepResult(Base):
    """单 Run 场景子步骤打分结果。"""

    __tablename__ = "scenario_step_results"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "step_id",
            name="uq_scenario_step_result_per_run",
        ),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id = Column(
        String,
        ForeignKey("scenario_eval_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    score = Column(Float, nullable=False, default=0.0)
    passed = Column(Boolean, nullable=False, default=False)
    evidence = Column(JSON, nullable=True)
    # 持久化 trace_binding（observation_id/name/type, input_preview, output_preview,
    # langfuse 链接等），供矩阵快路径直接返回，避免 Langfuse HTTP 调用
    trace_binding = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ScenarioStepSpanRef(Base):
    """物化后子步骤 ↔ observation 绑定（ST-3-3）。"""

    __tablename__ = "scenario_step_span_refs"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "step_id",
            name="uq_scenario_step_span_ref_per_run",
        ),
    )

    id = Column(String, primary_key=True)
    agent_run_id = Column(
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id = Column(
        String,
        ForeignKey("scenario_eval_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    observation_id = Column(String, nullable=True, index=True)
    span_node_id = Column(String, nullable=True)
    bind_source = Column(String, nullable=True)
    bind_meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentAssetSnapshot(Base):
    """Agent 运行态资产版本快照（A2a / §7.5.4）；由 Agent Asset Adapter 同步写入。"""

    __tablename__ = "agent_asset_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "logical_agent_id",
            "asset_type",
            "asset_hash",
            name="uq_asset_snapshot_agent_type_hash",
        ),
    )

    id = Column(String, primary_key=True)
    logical_agent_id = Column(String, nullable=False, index=True)
    agent_version = Column(String, nullable=True)
    asset_type = Column(String, nullable=False)  # ontology | skill_manifest | tool_schema | script_list
    asset_hash = Column(String, nullable=False)  # SHA-256 摘要
    asset_source = Column(String, nullable=True)  # 文件路径 / URL
    asset_payload = Column(JSON, nullable=True)  # 解析后结构化内容（有界）
    compile_status = Column(String, nullable=False, default="pending")  # pending | compiled | failed
    compile_log = Column(Text, nullable=True)
    registry_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TraceOperationRule(Base):
    """eval-owned 业务操作绑定规则（A2a / §7.5.4 / §8.8.2）；由 Trace Binding Compiler 写入。"""

    __tablename__ = "trace_operation_rules"
    __table_args__ = (
        UniqueConstraint(
            "logical_agent_id",
            "operation_id",
            "registry_version",
            name="uq_operation_rule_agent_id_version",
        ),
    )

    id = Column(String, primary_key=True)
    logical_agent_id = Column(String, nullable=False, index=True)
    operation_id = Column(String, nullable=False, index=True)  # e.g. voice.customer_feedback.parse
    display_name = Column(String, nullable=True)
    source_asset_id = Column(
        String,
        ForeignKey("agent_asset_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    match_rules = Column(JSON, nullable=False)  # JSONPath / attribute 匹配规则
    priority = Column(Integer, nullable=False, default=0)
    confidence = Column(Float, nullable=False, default=1.0)
    binding_reason_template = Column(String, nullable=True)  # e.g. "matched script_name {name}"
    registry_version = Column(Integer, nullable=False, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LLMModel(Base):
    """LLM模型配置表"""
    __tablename__ = "llm_models"
    
    id = Column(String, primary_key=True)  # 模型标识 key，如 "glm"
    name = Column(String, nullable=False)  # 显示名称，如 "智谱GLM-4.7"
    provider = Column(String, nullable=True)  # 模型厂商，如 "智谱AI"
    base_url = Column(String, nullable=False)  # API基础URL
    model = Column(String, nullable=False)  # 模型名称，如 "glm-4.7"
    api_key = Column(Text, nullable=True)  # API Key
    is_default = Column(Boolean, default=False)  # 是否为默认模型
    max_tokens = Column(Integer, nullable=True, default=4096)  # 最大输出tokens
    temperature = Column(Float, nullable=True, default=0.0)  # 温度参数
    timeout = Column(Float, nullable=True, default=180.0)  # API超时(秒)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomMetric(Base):
    """自定义评测指标表（geval/dag/code 三档）

    kind=geval：用户写自然语言评判标准，由 LLM 打分（最常用）
    kind=dag  ：用户定义决策树(DAG)，由 DeepEval DAGMetric 串行判定
    kind=code ：用户写 Python 代码，受限沙箱执行，确定性无 LLM
    """
    __tablename__ = "custom_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)          # 指标名称
    kind = Column(String, nullable=False)          # geval | dag | code
    category = Column(String, nullable=True)      # 自定义指标类别（指标集合），NULL 视为「自定义指标」
    criteria = Column(Text, nullable=True)         # geval: 自然语言评判标准
    dag = Column(JSON, nullable=True)              # dag: 决策树定义（DeepEval DAG dict）
    code = Column(Text, nullable=True)             # code: 用户 Python 代码
    multi_turn = Column(Boolean, default=False)     # 是否多轮对话指标
    params = Column(JSON, nullable=True)            # geval: LLMTestCaseParams 名列表
    threshold = Column(Float, default=0.5)         # 门控阈值
    is_active = Column(Boolean, default=True)      # 是否启用
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EvaluationReport(Base):
    """评测报告归档（xlsx 评测报告 / html 版本对比报告）。"""

    __tablename__ = "evaluation_reports"

    id = Column(String, primary_key=True)
    report_type = Column(String, nullable=False)  # evaluation | comparison
    title = Column(String, nullable=False)
    format = Column(String, nullable=False)  # xlsx | html
    task_ids = Column(JSON, nullable=True)
    baseline_task_id = Column(String, nullable=True)
    dataset_id = Column(String, nullable=True, index=True)
    dataset_name = Column(String, nullable=True)
    agent_name = Column(String, nullable=True)
    workspace_name = Column(String, nullable=True)
    content = Column(LargeBinary, nullable=False)
    file_size = Column(Integer, nullable=False, default=0)
    verdict_status = Column(String, nullable=True)
    score = Column(Float, nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class SystemConfig(Base):
    """系统级配置 KV 存储（运行时指标全局阈值等）。"""

    __tablename__ = "system_configs"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatSession(Base):
    """AI 对话配置会话。

    阶段 1：单轮 LLM 意图解析，draft_request 存 EvaluateRequest 草稿 JSON。
    阶段 2：由 agent loop 增量填充 draft_request。
    """
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)  # "chat_xxx"
    title = Column(String, nullable=True)  # 截断 intent_text 前 30 字
    status = Column(String, default="draft")  # draft | ready | consumed | error
    intent = Column(String, nullable=True)  # online | offline | unknown
    model_key = Column(String, nullable=True)  # 对话引擎用的 LLMModel.id
    intent_text = Column(Text, nullable=True)  # 用户原始意图文本
    draft_request = Column(Text, nullable=True)  # JSON: EvaluateRequest 草稿
    confidence = Column(Float, nullable=True)  # LLM 解析置信度 0-1
    missing_fields = Column(Text, nullable=True)  # JSON: 缺失字段名列表
    error_message = Column(Text, nullable=True)  # 解析失败时的错误
    pending_confirm = Column(Text, nullable=True)  # JSON: 待确认的写操作 {tool_name, tool_args}
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """对话消息。

    阶段 1：仅存 interpret 的 user/assistant 两条。
    阶段 2：扩展为流式 agent loop 消息（含 tool_name/tool_args/tool_result）。
    """
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_id_created_at", "session_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)  # 关联 chat_sessions.id（无 FK 约束，与现有表风格一致）
    role = Column(String, nullable=False)  # user | assistant | tool
    content = Column(Text, nullable=True)
    tool_name = Column(String, nullable=True)  # 阶段 2 才用
    tool_args = Column(Text, nullable=True)  # JSON
    tool_result = Column(Text, nullable=True)  # JSON
    tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


def _run_alembic_upgrade_head() -> None:
    from services.pg_bootstrap import bootstrap_postgresql_schema

    bootstrap_postgresql_schema()


async def init_db():
    log("INFO", "DATABASE", "数据库初始化开始")
    if is_postgresql_url():
        _run_alembic_upgrade_head()
        return

    async with engine.begin() as conn:
        # 检查表是否存在
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evaluation_tasks';"
        ))
        table_exists = result.fetchone() is not None
        
        if table_exists:
            log("INFO", "DATABASE", "检查现有表结构")
            # 检查列是否存在
            result = await conn.execute(text("PRAGMA table_info(evaluation_tasks);"))
            columns = {row[1] for row in result.fetchall()}

            # 添加缺失的列 - evaluation_tasks
            if 'paused_at' not in columns:
                log("INFO", "DATABASE", "添加列 paused_at")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN paused_at DATETIME;"))

            if 'resume_from' not in columns:
                log("INFO", "DATABASE", "添加列 resume_from")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN resume_from INTEGER DEFAULT 0;"))

            if 'run_count' not in columns:
                log("INFO", "DATABASE", "添加列 run_count")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN run_count INTEGER DEFAULT 1;"))

            if 'llm_model' not in columns:
                log("INFO", "DATABASE", "添加列 llm_model")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN llm_model VARCHAR DEFAULT 'glm';"))

            if 'agent_id' not in columns:
                log("INFO", "DATABASE", "添加列 agent_id")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN agent_id VARCHAR;"))

            if 'use_bm25' not in columns:
                log("INFO", "DATABASE", "添加列 use_bm25")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN use_bm25 BOOLEAN DEFAULT 0;"))

            if 'use_doc_library' not in columns:
                log("INFO", "DATABASE", "添加列 use_doc_library")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN use_doc_library BOOLEAN DEFAULT 0;"))

            if 'selected_metrics' not in columns:
                log("INFO", "DATABASE", "添加列 selected_metrics")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN selected_metrics TEXT;"))

            if 'agent_ids' not in columns:
                log("INFO", "DATABASE", "添加列 agent_ids")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN agent_ids TEXT;"))

            if 'selected_indices' not in columns:
                log("INFO", "DATABASE", "添加列 selected_indices")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN selected_indices TEXT;"))

            if 'custom_scripts' not in columns:
                log("INFO", "DATABASE", "添加列 custom_scripts")
                await conn.execute(text("ALTER TABLE evaluation_tasks ADD COLUMN custom_scripts TEXT;"))
            if 'runtime_metric_targets' not in columns:
                log("INFO", "DATABASE", "添加列 runtime_metric_targets")
                await conn.execute(
                    text("ALTER TABLE evaluation_tasks ADD COLUMN runtime_metric_targets TEXT;")
                )
            if 'verdict_thresholds' not in columns:
                log("INFO", "DATABASE", "添加列 verdict_thresholds")
                await conn.execute(
                    text("ALTER TABLE evaluation_tasks ADD COLUMN verdict_thresholds TEXT;")
                )
            if 'auto_scenario_score' not in columns:
                log("INFO", "DATABASE", "添加列 auto_scenario_score")
                await conn.execute(
                    text(
                        "ALTER TABLE evaluation_tasks ADD COLUMN auto_scenario_score BOOLEAN DEFAULT 1;"
                    )
                )
            if 'evaluation_mode' not in columns:
                log("INFO", "DATABASE", "添加列 evaluation_mode")
                await conn.execute(
                    text(
                        "ALTER TABLE evaluation_tasks ADD COLUMN evaluation_mode VARCHAR DEFAULT 'offline';"
                    )
                )
                await conn.execute(
                    text(
                        "UPDATE evaluation_tasks SET evaluation_mode = 'online' WHERE agent_id IS NOT NULL AND agent_id != '';"
                    )
                )
            if 'inference_rounds' not in columns:
                log("INFO", "DATABASE", "添加列 inference_rounds")
                await conn.execute(
                    text(
                        "ALTER TABLE evaluation_tasks ADD COLUMN inference_rounds INTEGER DEFAULT 1;"
                    )
                )
            if 'process_eval_completed' not in columns:
                log("INFO", "DATABASE", "添加列 process_eval_completed")
                await conn.execute(
                    text(
                        "ALTER TABLE evaluation_tasks ADD COLUMN process_eval_completed BOOLEAN DEFAULT 0;"
                    )
                )
            if 'langfuse_source' not in columns:
                log("INFO", "DATABASE", "添加列 langfuse_source")
                await conn.execute(
                    text("ALTER TABLE evaluation_tasks ADD COLUMN langfuse_source TEXT;")
                )

            # 检查 evaluation_results 表的新列
            result = await conn.execute(text("PRAGMA table_info(evaluation_results);"))
            result_columns = {row[1] for row in result.fetchall()}
            
            if 'question' not in result_columns:
                log("INFO", "DATABASE", "添加列 question")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN question TEXT;"))
            
            if 'ground_truth' not in result_columns:
                log("INFO", "DATABASE", "添加列 ground_truth")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN ground_truth TEXT;"))
            
            if 'agent_name' not in result_columns:
                log("INFO", "DATABASE", "添加列 agent_name")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN agent_name VARCHAR;"))
            
            if 'agent_answer' not in result_columns:
                log("INFO", "DATABASE", "添加列 agent_answer")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN agent_answer TEXT;"))
            
            if 'avg_score' not in result_columns:
                log("INFO", "DATABASE", "添加列 avg_score")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN avg_score FLOAT;"))
            
            if 'eval_model' not in result_columns:
                log("INFO", "DATABASE", "添加列 eval_model")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN eval_model VARCHAR;"))
            
            if 'eval_rounds' not in result_columns:
                log("INFO", "DATABASE", "添加列 eval_rounds")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN eval_rounds INTEGER;"))
            
            if 'inference_duration_ms' not in result_columns:
                log("INFO", "DATABASE", "添加列 inference_duration_ms")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN inference_duration_ms INTEGER;"))
            
            if 'eval_reason' not in result_columns:
                log("INFO", "DATABASE", "添加列 eval_reason")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN eval_reason TEXT;"))
            
            if 'eval_method' not in result_columns:
                log("INFO", "DATABASE", "添加列 eval_method")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN eval_method VARCHAR;"))
            
            if 'script_name' not in result_columns:
                log("INFO", "DATABASE", "添加列 script_name")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN script_name VARCHAR;"))

            if 'agent_run_id' not in result_columns:
                log("INFO", "DATABASE", "添加列 agent_run_id")
                await conn.execute(text("ALTER TABLE evaluation_results ADD COLUMN agent_run_id VARCHAR;"))

            result = await conn.execute(text("PRAGMA table_info(agent_runs);"))
            run_columns = {row[1] for row in result.fetchall()}
            if run_columns and 'trace_sync_status' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.trace_sync_status")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN trace_sync_status VARCHAR;"))
            if run_columns and 'origin' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.origin")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN origin VARCHAR DEFAULT 'evaluation';"))
            if run_columns and 'evaluation_mode' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.evaluation_mode")
                await conn.execute(text(
                    "ALTER TABLE agent_runs ADD COLUMN evaluation_mode VARCHAR DEFAULT 'online';"
                ))
            if run_columns and 'root_trace_id' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.root_trace_id")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN root_trace_id VARCHAR;"))
                await conn.execute(text(
                    "UPDATE agent_runs SET root_trace_id = trace_id WHERE root_trace_id IS NULL AND trace_id IS NOT NULL;"
                ))
            if run_columns and 'eval_context' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.eval_context")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN eval_context TEXT;"))
                await conn.execute(text(
                    """UPDATE agent_runs SET eval_context = json_object(
                        'task_id', task_id,
                        'dataset_id', dataset_id,
                        'question_id', question_id,
                        'run_index', COALESCE(run_index, 0)
                    ) WHERE eval_context IS NULL AND task_id IS NOT NULL;"""
                ))
            if run_columns and 'input_snapshot' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.input_snapshot")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN input_snapshot TEXT;"))
            if run_columns and 'output_snapshot' not in run_columns:
                log("INFO", "DATABASE", "添加列 agent_runs.output_snapshot")
                await conn.execute(text("ALTER TABLE agent_runs ADD COLUMN output_snapshot TEXT;"))
            for col in (
                "inference_model_provider",
                "inference_model_name",
                "inference_model_version",
                "inference_model_id",
            ):
                if run_columns and col not in run_columns:
                    log("INFO", "DATABASE", f"添加列 agent_runs.{col}")
                    await conn.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {col} VARCHAR;"))

            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='run_span_nodes';"
            ))
            if result.fetchone():
                result = await conn.execute(text("PRAGMA table_info(run_span_nodes);"))
                span_columns = {row[1] for row in result.fetchall()}
                if 'summary_text' not in span_columns:
                    log("INFO", "DATABASE", "添加列 run_span_nodes.summary_text")
                    await conn.execute(text("ALTER TABLE run_span_nodes ADD COLUMN summary_text TEXT;"))

            # 迁移历史数据：将 metric_name 中的"脚本:xxx"格式还原为原始指标名
            # 同时设置 eval_method 和 script_name 字段
            try:
                legacy_results = await conn.execute(text(
                    "SELECT id, metric_name FROM evaluation_results WHERE metric_name LIKE '脚本:%';"
                ))
                legacy_rows = legacy_results.fetchall()
                if legacy_rows:
                    log("INFO", "DATABASE", f"发现 {len(legacy_rows)} 条旧格式指标记录，开始迁移")
                    for row in legacy_rows:
                        old_metric = row[1]
                        # 提取脚本名称（"脚本:scene01" → "scene01"）
                        script_nm = old_metric.replace('脚本:', '')
                        await conn.execute(text(
                            "UPDATE evaluation_results SET metric_name = 'correctness', eval_method = 'script', script_name = :script_name WHERE id = :id;"
                        ), {"script_name": script_nm, "id": row[0]})
                    log("INFO", "DATABASE", f"已迁移 {len(legacy_rows)} 条旧格式指标记录")
            except Exception as e:
                log("WARNING", "DATABASE", f"迁移旧格式指标记录时出错: {e}")

            # agents 表版本字段（v2.1）
            result = await conn.execute(text("PRAGMA table_info(agents);"))
            agent_columns = {row[1] for row in result.fetchall()}
            if 'logical_agent_id' not in agent_columns:
                log("INFO", "DATABASE", "添加列 agents.logical_agent_id")
                await conn.execute(text("ALTER TABLE agents ADD COLUMN logical_agent_id VARCHAR;"))
            if 'version' not in agent_columns:
                log("INFO", "DATABASE", "添加列 agents.version")
                await conn.execute(text("ALTER TABLE agents ADD COLUMN version VARCHAR;"))
            if 'version_tag' not in agent_columns:
                log("INFO", "DATABASE", "添加列 agents.version_tag")
                await conn.execute(text("ALTER TABLE agents ADD COLUMN version_tag VARCHAR;"))
            if 'langfuse_project' not in agent_columns:
                log("INFO", "DATABASE", "添加列 agents.langfuse_project")
                await conn.execute(text("ALTER TABLE agents ADD COLUMN langfuse_project VARCHAR;"))

            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='process_checkpoint_templates';"
            ))
            if result.fetchone():
                result = await conn.execute(text("PRAGMA table_info(process_checkpoint_templates);"))
                cp_columns = {row[1] for row in result.fetchall()}
                if 'enforce_order' not in cp_columns:
                    log("INFO", "DATABASE", "添加列 process_checkpoint_templates.enforce_order")
                    await conn.execute(
                        text(
                            "ALTER TABLE process_checkpoint_templates "
                            "ADD COLUMN enforce_order BOOLEAN DEFAULT 0 NOT NULL;"
                        )
                    )

            result = await conn.execute(text("PRAGMA table_info(datasets);"))
            dataset_columns = {row[1] for row in result.fetchall()}
            if dataset_columns and 'default_scenario_key' not in dataset_columns:
                log("INFO", "DATABASE", "添加列 datasets.default_scenario_key")
                await conn.execute(
                    text("ALTER TABLE datasets ADD COLUMN default_scenario_key VARCHAR;")
                )

            result = await conn.execute(text("PRAGMA table_info(scenario_eval_templates);"))
            scen_tpl_cols = {row[1] for row in result.fetchall()}
            if scen_tpl_cols and "dataset_item_id" not in scen_tpl_cols:
                log("INFO", "DATABASE", "添加列 scenario_eval_templates.dataset_item_id")
                await conn.execute(
                    text(
                        "ALTER TABLE scenario_eval_templates ADD COLUMN dataset_item_id VARCHAR;"
                    )
                )

            result = await conn.execute(text("PRAGMA table_info(scenario_eval_steps);"))
            scen_step_cols = {row[1] for row in result.fetchall()}
            if scen_step_cols and "enabled" not in scen_step_cols:
                log("INFO", "DATABASE", "添加列 scenario_eval_steps.enabled")
                await conn.execute(
                    text(
                        "ALTER TABLE scenario_eval_steps ADD COLUMN enabled BOOLEAN DEFAULT 1;"
                    )
                )
            if scen_step_cols and "enforce_order" not in scen_step_cols:
                log("INFO", "DATABASE", "添加列 scenario_eval_steps.enforce_order")
                await conn.execute(
                    text(
                        "ALTER TABLE scenario_eval_steps "
                        "ADD COLUMN enforce_order BOOLEAN DEFAULT 0 NOT NULL;"
                    )
                )

            # llm_models 表新增 max_tokens / temperature / timeout
            result = await conn.execute(text("PRAGMA table_info(llm_models);"))
            llm_cols = {row[1] for row in result.fetchall()}
            if llm_cols and "max_tokens" not in llm_cols:
                log("INFO", "DATABASE", "添加列 llm_models.max_tokens")
                await conn.execute(
                    text("ALTER TABLE llm_models ADD COLUMN max_tokens INTEGER DEFAULT 4096;")
                )
            if llm_cols and "temperature" not in llm_cols:
                log("INFO", "DATABASE", "添加列 llm_models.temperature")
                await conn.execute(
                    text("ALTER TABLE llm_models ADD COLUMN temperature FLOAT DEFAULT 0.0;")
                )
            if llm_cols and "timeout" not in llm_cols:
                log("INFO", "DATABASE", "添加列 llm_models.timeout")
                await conn.execute(
                    text("ALTER TABLE llm_models ADD COLUMN timeout FLOAT DEFAULT 180.0;")
                )

            await conn.commit()
        else:
            log("INFO", "DATABASE", "表不存在，将创建新表")
        
        # 关闭当前事务后再创建表
        await conn.close()
    
    # 在新的连接中创建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        log("INFO", "DATABASE", "数据库初始化完成")

    # SQLite 路径不执行 alembic 迁移，需手动迁移 judging_mode 旧名→新名
    # PostgreSQL 路径由 alembic 028 处理，此处跳过
    if is_sqlite_url():
        await _migrate_judging_mode_names_sqlite()


async def _migrate_judging_mode_names_sqlite() -> None:
    """SQLite: 将 datasets.data JSON 中的 judging_mode 旧名替换为新名。

    旧名 → 新名：
      "关键词评测" → "Ground_Truth评测"
      "提示词评测" → "基于Prompt评测"
      "基于Ground_Truth评测" → "Ground_Truth评测"（中间过渡名 → 最终名）

    直接用 SQL REPLACE 替换带引号的字符串值，安全且高效。
    """
    old_new = [
        ('"关键词评测"', '"Ground_Truth评测"'),
        ('"提示词评测"', '"基于Prompt评测"'),
        ('"基于Ground_Truth评测"', '"Ground_Truth评测"'),
    ]
    async with engine.begin() as conn:
        for old, new in old_new:
            result = await conn.execute(
                text("UPDATE datasets SET data = REPLACE(data, :old, :new) WHERE data LIKE :pattern"),
                {"old": old, "new": new, "pattern": f"%{old}%"},
            )
            if result.rowcount > 0:
                log("INFO", "DATABASE", f"迁移 judging_mode: {old} → {new}，影响 {result.rowcount} 条评测集")


async def get_session():
    async with async_session() as session:
        yield session


async def get_read_session():
    """只读轻量 session：走独立连接池，避免被主池的后台任务挤占。仅供 GET 类列表接口使用。"""
    async with read_session() as session:
        yield session
