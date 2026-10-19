from typing import Any, Dict, Type
from .base_metric import BaseMetric
from .faithfulness import FaithfulnessMetric, AnswerRelevancyMetric, CorrectnessMetric


# Metric registry - add new metrics here
# 值可以是类（原生指标，通过 get_metric 实例化）或工厂函数（DeepEval 指标，注册时绑定 model_config）
METRICS_REGISTRY: Dict[str, Any] = {
    "faithfulness": FaithfulnessMetric,
    "answer_relevancy": AnswerRelevancyMetric,
    "correctness": CorrectnessMetric,
}

# DeepEval 指标 key 集合：register_deepeval_metrics() 填充，供 pipeline 判断指标来源
DEEPEVAL_METRIC_KEYS: set = set()

# 已配置的指标参数缓存（metric_key -> {param_name: value}）
# 启动时从 system_configs 表加载，PUT /metrics/{key}/params 写入后刷新
_metric_params_cache: Dict[str, Dict[str, Any]] = {}


async def load_metric_params_cache(session) -> None:
    """启动时从 system_configs 表加载所有已配置的指标参数到进程缓存。"""
    global _metric_params_cache
    from services.system_config_service import get_all_metric_params
    _metric_params_cache = await get_all_metric_params(session)


async def refresh_metric_params(session, metric_key: str) -> None:
    """单个指标参数写入后刷新进程缓存。"""
    global _metric_params_cache
    from services.system_config_service import get_metric_params
    _metric_params_cache[metric_key] = await get_metric_params(session, metric_key)


from config import get_default_model_key

def get_metric(
    name: str,
    model_key: str = None,
    use_bm25: bool = False,
    use_doc_library: bool = False,
    ontology_package: str = None,
    judging_mode: str = "",
    eval_prompt: str = "",
) -> BaseMetric:
    """Get metric instance by name.

    Args:
        name: 指标名称
        model_key: 模型标识 (glm)
        use_bm25: 是否使用BM25算法(仅对faithfulness有效)
        use_doc_library: 是否使用文档库作为参考(仅对faithfulness且use_bm25=True时有效)
        ontology_package: 本体包名称(使用指定目录的md文档进行BM25评估)
        judging_mode: 评测模式（Ground_Truth评测/基于Prompt评测），仅对 correctness 有效
        eval_prompt: 基于Prompt评测模式下使用的评测提示词
    """
    if name not in METRICS_REGISTRY:
        raise ValueError(f"Unknown metric: {name}. Available: {list(METRICS_REGISTRY.keys())}")

    # DeepEval 指标：通过工厂函数实例化（model_config 在 register_deepeval_metrics 时绑定）
    if name in DEEPEVAL_METRIC_KEYS:
        factory = METRICS_REGISTRY[name]
        return factory()

    # 对于faithfulness指标，支持BM25和文档库选项
    if name == "faithfulness":
        effective_model_key = model_key or get_default_model_key()
        return METRICS_REGISTRY[name](model_key=effective_model_key, use_bm25=use_bm25, use_doc_library=use_doc_library, ontology_package=ontology_package)

    # 其他指标强制使用LLM评估，确保model_key有效
    effective_model_key = model_key or get_default_model_key()
    if name == "correctness":
        return METRICS_REGISTRY[name](
            model_key=effective_model_key,
            judging_mode=judging_mode,
            eval_prompt=eval_prompt,
        )
    return METRICS_REGISTRY[name](model_key=effective_model_key)


def register_metric(name: str, metric_class: Type[BaseMetric]):
    """Register a custom metric."""
    METRICS_REGISTRY[name] = metric_class


def register_deepeval_metrics(model_config: dict) -> int:
    """启动时注册所有 DeepEval 声明式指标（deepeval 已安装时）。

    遍历 deepeval_metrics.deepeval_suite.METRIC_SPECS，跳过与原生指标同名的 key
    （faithfulness / answer_relevancy 保留 onto-eval 原生实现，含 BM25/文档库能力），
    其余 25 项注册为 DeepEvalMetricAdapter 工厂函数。

    Args:
        model_config: 评测模型配置
            {model, base_url, api_key, temperature, provider}

    Returns:
        已注册的 DeepEval 指标数量
    """
    try:
        from .deepeval_metrics.deepeval_suite import METRIC_SPECS
    except ImportError:
        return 0  # deepeval 未装，静默跳过

    count = 0
    for spec in METRIC_SPECS:
        # 不覆盖已有指标（faithfulness / answer_relevancy 保留原生实现）
        if spec.key in METRICS_REGISTRY:
            continue

        # 用默认参数捕获 spec，避免闭包延迟绑定问题
        def _make_adapter(s=spec, mc=model_config):
            from .deepeval_adapter import DeepEvalMetricAdapter
            return DeepEvalMetricAdapter(
                spec={
                    "key": s.key,
                    "cls": s.cls,
                    "label": s.label,
                    "category": s.category,
                    "kind": s.kind,
                    "requires": s.requires,
                    "extra": dict(s.extra),
                    "params": [
                        {
                            "name": p.name,
                            "label": p.label,
                            "type": p.type,
                            "required": p.required,
                            "placeholder": p.placeholder,
                        }
                        for p in s.params
                    ],
                    "note": s.note,
                    "lower_is_better": s.lower_is_better,
                    "gate_threshold": s.gate_threshold,
                },
                model_config=mc,
                metric_params=_metric_params_cache.get(s.key, {}),
            )

        METRICS_REGISTRY[spec.key] = _make_adapter
        DEEPEVAL_METRIC_KEYS.add(spec.key)
        count += 1

    return count
