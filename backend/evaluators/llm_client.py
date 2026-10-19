import asyncio
import time
import openai
from openai import AsyncOpenAI
from config import get_llm_config, get_available_models, get_llm_call_override
from logger import log

# 全局client缓存,避免重复创建
_client_cache = {}
_client_lock = asyncio.Lock()

# LLM 调用耗时统计（内存累积，重启清零）
_call_stats: dict = {}  # {model: {"count":int, "total_ms":float, "min_ms":float, "max_ms":float, "slow_count":int, "durations":list}}

_SLOW_THRESHOLD_MS = 30_000  # 超过 30s 视为慢请求


def _record_call_duration(model: str, duration_ms: float):
    """记录单次调用耗时到内存统计器。"""
    s = _call_stats.setdefault(model, {
        "count": 0, "total_ms": 0.0, "min_ms": float("inf"),
        "max_ms": 0.0, "slow_count": 0, "durations": [],
    })
    s["count"] += 1
    s["total_ms"] += duration_ms
    s["min_ms"] = min(s["min_ms"], duration_ms)
    s["max_ms"] = max(s["max_ms"], duration_ms)
    s["durations"].append(duration_ms)
    if duration_ms > _SLOW_THRESHOLD_MS:
        s["slow_count"] += 1


def get_call_stats_summary() -> dict:
    """返回各模型的调用耗时统计摘要（含 P50/P95/P99）。"""
    summary = {}
    for model, s in _call_stats.items():
        ds = sorted(s["durations"])
        n = len(ds)
        avg = s["total_ms"] / n if n else 0
        def _pct(p):
            if not ds:
                return 0
            idx = min(int(n * p / 100), n - 1)
            return round(ds[idx], 1)
        summary[model] = {
            "count": n,
            "avg_ms": round(avg, 1),
            "min_ms": round(s["min_ms"], 1) if n else 0,
            "max_ms": round(s["max_ms"], 1) if n else 0,
            "p50_ms": _pct(50),
            "p95_ms": _pct(95),
            "p99_ms": _pct(99),
            "slow_count": s["slow_count"],
            "slow_threshold_ms": _SLOW_THRESHOLD_MS,
        }
    return summary


def reset_call_stats():
    """清零统计。"""
    _call_stats.clear()

# 全局信号量：限制并发 LLM 调用数（替代原有 5 秒全局限速锁）
# 上限 8：约为消费者 agent 30 并发容量的 27%，留余量给 429 重试/总结生成等
_LLM_CALL_SEMAPHORE = asyncio.Semaphore(8)

# 429 速率限制重试配置
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 10.0  # 首次重试等待 10 秒，之后递增（10/20/30/40/50）
# Auto 模式：全部模型 429 后的重试轮数
_AUTO_POOL_MAX_ROUNDS = 2
_AUTO_POOL_RETRY_DELAY = 15.0  # 全部模型失败后，等待秒数再重试一轮

# Auto 模型池 429 计数（运行时内存统计，重启清零）
# 用于排序模型池：429 次数少的模型优先选择
_model_429_counts: dict[str, int] = {}


def _sort_pool_by_429_counts(pool: list) -> list:
    """按 429 错误次数升序排列模型池，次数少的排前面。"""
    return sorted(pool, key=lambda item: _model_429_counts.get(item["key"], 0))


def get_llm_client(model_key: str = None) -> AsyncOpenAI:
    """Get configured OpenAI client for specified model."""
    config = get_llm_config(model_key)
    return AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"]
    )


async def evaluate_with_llm(
    prompt: str,
    system_prompt: str = "你是一个专业的评估助手，请用中文回答。",
    model_key: str = None,
    override_max_tokens: int = None,
    override_temperature: float = None
) -> str:
    """Call LLM for evaluation.

    支持 Auto 模型池模式：当 model_key="auto" 时，从已配置的模型池中
    按顺序尝试，遇到 429 限流自动切换到下一个模型。

    Args:
        prompt: 用户提示词
        system_prompt: 系统提示词
        model_key: 模型标识 (glm / auto)
        override_max_tokens: 临时覆盖最大输出tokens（None则用配置值）
        override_temperature: 临时覆盖温度参数（None则用配置值）
    """
    from config import get_model_pool

    call_override = get_llm_call_override()

    # ── Auto 模型池模式 ──
    if model_key and model_key.strip().lower() == "auto":
        pool = get_model_pool()
        if not pool:
            raise RuntimeError("Auto 模型池为空：请先在系统设置中配置至少一个 LLM API Key")

        # 按 429 历史次数排序：次数少的优先
        pool = _sort_pool_by_429_counts(pool)

        last_error = None
        all_429_in_round = True  # 跟踪本轮是否全部 429
        # 第一轮：429 优先换模型，不重试
        for pool_idx, pool_item in enumerate(pool):
            pk = pool_item["key"]
            cfg = pool_item["config"]
            try:
                result = await _call_single_model(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    config=cfg,
                    model_key=pk,
                    call_override=call_override,
                    override_max_tokens=override_max_tokens,
                    override_temperature=override_temperature,
                    skip_429_retry=True,
                )
                log("INFO", "LLM_CLIENT", "Auto 模型池：调用成功",
                    model_key=pk, model=cfg["model"], pool_idx=pool_idx)
                return result
            except Exception as e:
                error_str = str(e)
                is_429 = isinstance(e, openai.RateLimitError)
                if is_429:
                    _model_429_counts[pk] = _model_429_counts.get(pk, 0) + 1
                else:
                    all_429_in_round = False
                log("WARNING", "LLM_CLIENT", "Auto 模型池：模型失败",
                    model_key=pk, model=cfg["model"], pool_idx=pool_idx,
                    total=len(pool), is_429=is_429, error=error_str[:120])
                last_error = e
                # 429 立即换模型，不等待；非 429 错误短暂等待后换模型
                if not is_429 and pool_idx < len(pool) - 1:
                    await asyncio.sleep(2.0)
                continue

        # 第一轮全部失败，检查是否全因 429
        if all_429_in_round and _AUTO_POOL_MAX_ROUNDS > 1:
            # 第二轮及之后：所有模型都 429，等待后重新轮询全部模型
            for round_idx in range(1, _AUTO_POOL_MAX_ROUNDS):
                wait = _AUTO_POOL_RETRY_DELAY * round_idx
                all_429_in_round = True  # 重置本轮标志
                # 重试前按最新 429 计数重新排序
                pool = _sort_pool_by_429_counts(pool)
                log("WARNING", "LLM_CLIENT", "Auto 模型池：全部模型 429，等待后重试",
                    round=round_idx + 1, max_rounds=_AUTO_POOL_MAX_ROUNDS,
                    wait_seconds=wait, pool_size=len(pool),
                    counts_429={pk: _model_429_counts.get(pk, 0) for pk in [p["key"] for p in pool]})
                await asyncio.sleep(wait)

                for pool_idx, pool_item in enumerate(pool):
                    pk = pool_item["key"]
                    cfg = pool_item["config"]
                    try:
                        result = await _call_single_model(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            config=cfg,
                            model_key=pk,
                            call_override=call_override,
                            override_max_tokens=override_max_tokens,
                            override_temperature=override_temperature,
                            skip_429_retry=True,
                        )
                        log("INFO", "LLM_CLIENT", "Auto 模型池：重试后调用成功",
                            model_key=pk, model=cfg["model"],
                            pool_idx=pool_idx, round=round_idx + 1)
                        return result
                    except Exception as e:
                        error_str = str(e)
                        is_429 = isinstance(e, openai.RateLimitError)
                        if is_429:
                            _model_429_counts[pk] = _model_429_counts.get(pk, 0) + 1
                        else:
                            all_429_in_round = False
                        log("WARNING", "LLM_CLIENT", "Auto 模型池：重试仍失败",
                            model_key=pk, model=cfg["model"], pool_idx=pool_idx,
                            round=round_idx + 1, is_429=is_429, error=error_str[:120])
                        last_error = e
                        if not is_429 and pool_idx < len(pool) - 1:
                            await asyncio.sleep(2.0)
                        continue

                # 本轮有非 429 错误则不再重试
                if not all_429_in_round:
                    break

        # 所有模型 + 所有重试轮次都失败了
        raise RuntimeError(
            f"Auto 模型池所有 {len(pool)} 个模型均调用失败"
            f"（已重试 {_AUTO_POOL_MAX_ROUNDS} 轮）。"
            f"最后一个错误: {last_error}"
        )

    # ── 单模型模式 ──
    config = get_llm_config(model_key)
    return await _call_single_model(
        prompt=prompt,
        system_prompt=system_prompt,
        config=config,
        model_key=model_key,
        call_override=call_override,
        override_max_tokens=override_max_tokens,
        override_temperature=override_temperature,
    )


async def _call_single_model(
    prompt: str,
    system_prompt: str,
    config: dict,
    model_key: str = None,
    call_override: dict = None,
    override_max_tokens: int = None,
    override_temperature: float = None,
    *,
    skip_429_retry: bool = False,
) -> str:
    """对单个模型执行一次 LLM 调用（含 429 重试）。

    Args:
        skip_429_retry: 为 True 时不在本模型内重试 429，直接抛出由调用方换模型。
    """
    call_override = call_override or {}
    cache_key = model_key or "default"

    effective_max_tokens = override_max_tokens if override_max_tokens is not None else call_override.get("max_tokens") if call_override.get("max_tokens") is not None else config.get("max_tokens", 4096)
    effective_temperature = override_temperature if override_temperature is not None else call_override.get("temperature") if call_override.get("temperature") is not None else config.get("temperature", 0.0)

    log("INFO", "LLM_CLIENT", "LLM 调用开始",
        model=config["model"],
        prompt_length=len(prompt),
        system_prompt_length=len(system_prompt),
        model_key=model_key)

    # 使用client缓存,避免重复创建
    async with _client_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = AsyncOpenAI(
                api_key=config["api_key"],
                base_url=config["base_url"]
            )

    client = _client_cache[cache_key]

    # 全局信号量：限制并发 LLM 调用数（替代原有 5 秒全局限速）
    async with _LLM_CALL_SEMAPHORE:
        try:
            for _attempt in range(_MAX_RETRIES + 1):
                try:
                    _api_t0 = time.monotonic()
                    response = await client.chat.completions.create(
                        model=config["model"],
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=effective_temperature,
                        max_tokens=effective_max_tokens,
                        timeout=config.get("timeout", 180.0)
                    )
                    _api_ms = round((time.monotonic() - _api_t0) * 1000, 1)

                    result = response.choices[0].message.content or ""
                    _record_call_duration(config["model"], _api_ms)

                    _is_slow = _api_ms > _SLOW_THRESHOLD_MS
                    log("INFO", "LLM_CLIENT", "LLM 调用完成",
                        model=config["model"],
                        response_length=len(result),
                        response_preview=result[:200] if result else "空响应",
                        api_duration_ms=_api_ms,
                        is_slow=_is_slow)
                    if _is_slow:
                        log("WARNING", "LLM_CLIENT", "LLM 调用耗时过长",
                            model=config["model"],
                            api_duration_ms=_api_ms,
                            threshold_ms=_SLOW_THRESHOLD_MS,
                            prompt_length=len(prompt),
                            response_length=len(result))

                    return result
                except Exception as e:
                    error_str = str(e)
                    # 429 速率限制错误时自动重试（Auto 模式下 skip_429_retry=True 跳过）
                    is_429 = isinstance(e, openai.RateLimitError)
                    if is_429 and not skip_429_retry and _attempt < _MAX_RETRIES:
                        wait = _RETRY_BASE_DELAY * (_attempt + 1)
                        log("WARNING", "LLM_CLIENT", "触发速率限制，等待后重试",
                            model=config["model"],
                            attempt=_attempt + 1,
                            max_retries=_MAX_RETRIES,
                            wait_seconds=wait,
                            error=error_str)
                        await asyncio.sleep(wait)
                        continue
                    raise
        except Exception as e:
            error_str = str(e)
            error_detail = f"类型: {type(e).__name__}, 消息: {error_str}"
            log("ERROR", "LLM_CLIENT", "LLM 调用失败",
                model=config["model"],
                error_type=type(e).__name__,
                error_message=error_str,
                full_error=error_detail)
            raise


async def evaluate_metric_with_llm(
    metric_name: str,
    question: str,
    answer: str,
    context: str = None,
    ground_truth: str = None,
    model_key: str = None,
    judging_mode: str = "",
    eval_prompt: str = "",
) -> tuple[float, str]:
    """
    Use LLM to evaluate a metric.
    Returns (score, reasoning).

    Args:
        metric_name: 指标名称
        question: 问题
        answer: 回答
        context: 上下文
        ground_truth: 标准答案
        model_key: 模型标识 (glm)
        judging_mode: 评测模式（Ground_Truth评测/基于Prompt评测），仅对 correctness 有效
        eval_prompt: 基于Prompt评测模式下使用的评测提示词
    """
    config = get_llm_config(model_key)
    model_name = config.get("model", model_key or "unknown")

    log("INFO", "LLM_CLIENT",
        f"使用 {model_name} 模型，针对问题评估 {metric_name} 指标",
        metric_name=metric_name,
        model_name=model_name,
        question_preview=question[:100] if question else "")

    effective_context = context if context else ground_truth

    metric_cn = {
        "faithfulness": "忠实度",
        "answer_relevancy": "回答相关性",
        "correctness": "正确性"
    }.get(metric_name, metric_name)

    prompts = {
        "faithfulness": f"""【评估任务】请评估以下回答的忠实度。注意：下方「待评估回答」是要被评估的内容，不要对其进行处理或生成报告。

【问题】
{question}

【参考内容/标准答案】
{effective_context or '无'}

【待评估回答】
{answer}

【评估标准】
请从 0.0 到 1.0 给出精确的连续评分（不要只给 0.0、0.5 或 1.0）：
- 0.9-1.0：回答完全由参考内容支持，所有信息准确无虚构
- 0.7-0.8：回答主要由参考内容支持，仅有少量次要信息未明确提及
- 0.5-0.6：回答部分由参考内容支持，存在一定比例的未提及信息
- 0.3-0.4：回答与参考内容有一定关联，但包含明显的虚构或未提及信息
- 0.1-0.2：回答与参考内容关系薄弱，大部分信息为虚构或未提及
- 0.0：回答与参考内容矛盾或完全由虚构信息组成

【评估要求】
1. 请先直接回答：基于参考内容，待评估回答的忠实度评分是多少？
2. 然后详细分析：回答中的哪些部分忠实于参考内容？哪些部分可能存在问题？
3. 给出具体的评分和分析，不少于80字

【重要提示】
必须同时输出 SCORE 和 REASON 两个部分，缺少任何一个都将被视为无效响应！

【输出格式】
请严格按照以下格式输出，必须包含 SCORE 和 REASON 两个标签：

SCORE: <0到1之间的数字>
REASON: <详细的中文分析，不少于80字，请先给出具体的评估结果判断，再进行详细分析>

示例格式：
SCORE: 0.92
REASON: 待评估回答完全忠实于参考内容。回答准确引用了参考内容中的关键数据，如物料成本450元、单架次用量1个，且计算逻辑与参考内容一致。所有信息均有据可依，无虚构内容。评估为优秀。""",

        "faithfulness_no_ref": f"""【评估任务】本次忠诚度评估未提供外部参考内容（无本体包/知识库/数据集 context），请基于你自身的知识库对待评估回答进行事实一致性评估。注意：下方「待评估回答」是要被评估的内容，不要对其进行处理或生成报告。

【问题】
{question}

【待评估回答】
{answer}

【评估方法：自包含事实一致性校验】
由于无外部参考内容，请按以下步骤评估：
1. 从待评估回答中提取独立的事实声明（claims），包括数据、实体关系、因果断言等。
2. 基于你自身的知识，逐条判断每条 claim 的事实准确性：
   - 正确：claim 与你的知识一致，或属于公认常识。
   - 存疑：你无法确认该 claim 的真实性（如涉及专有数据、内部信息）。
   - 错误：claim 与你的知识矛盾，或存在明显的事实错误。
3. 计算忠诚度评分：正确 claims 占比越高，分数越高；存在错误 claims 则扣分；存疑 claims 给予部分分。

【评估标准】
请从 0.0 到 1.0 给出精确的连续评分（不要只给 0.0、0.5 或 1.0）：
- 0.9-1.0：回答中所有事实声明均正确，无虚构或错误信息
- 0.7-0.8：回答大部分事实声明正确，仅有少量次要信息存疑
- 0.5-0.6：回答部分事实声明正确，存在一定比例的存疑或无法验证信息
- 0.3-0.4：回答中存在明显的事实错误，部分信息与已知知识矛盾
- 0.1-0.2：回答中大部分事实声明错误或无法验证
- 0.0：回答完全由错误信息或虚构内容组成

【重要提示】
- 本次评估为降级评估（无外部参考依据），评分基于模型自身知识，对专有领域数据可能不够准确。
- 必须同时输出 SCORE 和 REASON 两个部分，缺少任何一个都将被视为无效响应！

【输出格式】
请严格按照以下格式输出，必须包含 SCORE 和 REASON 两个标签：

SCORE: <0到1之间的数字>
REASON: <详细的中文分析，不少于80字，请先列出提取的关键事实声明及判断，再给出综合评分>

示例格式：
SCORE: 0.75
REASON: 从回答中提取3条事实声明：1)物料A的单价为450元——正确；2)单架次用量为1个——正确；3)供应商为XX公司——存疑，无法从公开知识确认。2条正确、1条存疑，大部分信息可靠但存在不确定内容，综合评分为0.75。""",

        "answer_relevancy": f"""【评估任务】请评估以下回答与问题的相关性。注意：下方「待评估回答」是要被评估的内容，不要对其进行处理或生成报告。

【问题】
{question}

【待评估回答】
{answer}

【评估标准】
请从 0.0 到 1.0 给出精确的连续评分（不要只给 0.0、0.5 或 1.0）：
- 0.9-1.0：回答直接且完整地回应了问题，无任何无关内容
- 0.7-0.8：回答主要回应了问题，仅包含少量无关或冗余内容
- 0.5-0.6：回答部分回应了问题，包含一定比例的无关或冗余内容
- 0.3-0.4：回答与问题有一定关联，但大部分内容无关或冗余
- 0.1-0.2：回答与问题关系薄弱，仅有极少部分内容相关
- 0.0：回答完全与问题无关

【评估要求】
1. 请先直接回答：回答是否与问题相关？相关性评分是多少？
2. 然后详细分析：回答是如何回应问题的？是否存在无关或冗余内容？
3. 给出具体的评分和分析，不少于80字

【重要提示】
必须同时输出 SCORE 和 REASON 两个部分，缺少任何一个都将被视为无效响应！

【输出格式】
请严格按照以下格式输出，必须包含 SCORE 和 REASON 两个标签：

SCORE: <0到1之间的数字>
REASON: <详细的中文分析，不少于80字，请先给出具体的评估结果判断，再进行详细分析>

示例格式：
SCORE: 0.88
REASON: 回答与问题高度相关，直接回应了问题的核心要点。回答准确提供了问题所需的信息，内容精炼且切中要害，无无关或冗余内容。评估为优秀。""",

        "correctness": f"""【评估任务】请评估以下回答相对于标准答案的正确性。注意：下方「待评估回答」是要被评估的内容，不要对其进行处理或生成报告。

【问题】
{question}

【标准答案】
{ground_truth or '无'}

【待评估回答】
{answer}

【评估标准】
请从 0.0 到 1.0 给出精确的连续评分（不要只给 0.0、0.5 或 1.0）：
- 0.9-1.0：回答与标准答案完全一致，语义等价，包含所有关键信息
- 0.7-0.8：回答基本正确，包含大部分关键信息，仅有少量次要信息缺失或表述略有不同
- 0.5-0.6：回答部分正确，包含一些关键信息，但有明显的信息缺失或表述不准确
- 0.3-0.4：回答存在一定相关性，但关键信息大部分错误或缺失
- 0.1-0.2：回答与标准答案几乎无关，仅存在极少相关信息
- 0.0：回答完全错误或与问题无关

【评估要求】
1. 请先直接回答：待评估回答相对于标准答案的正确性评分是多少？
2. 然后详细分析：回答中的哪些内容是正确的？哪些内容存在错误或缺失？
3. 给出具体的评分和分析，不少于80字

【评分边界约束】
- 本指标只评估语义层面的正确性、完整性和一致性，即回答中的事实、数据、逻辑推导、结论等是否与标准答案在语义上等价。
- 以下格式类问题不在本指标扣分范围内，请勿因这些问题降低语义评分：
  * 回答在 JSON 或其他结构化内容前后附加了额外说明文本（如摘要、注释）
  * 分隔符、标点符号的风格差异（如 "/" 与 "、"、逗号与句号）
  * 使用了 Markdown 代码块包裹、额外换行等排版差异
  * 表述风格差异（如自然语言 vs 伪代码风格），只要其传达的语义相同
  * 回答包含标准答案中没有的额外合理推导/计算内容（如基于已知数据计算出的派生指标），只要核心结论与标准答案一致且不矛盾，不应因此扣分
  * 回答比标准答案更详细或包含额外分析步骤，只要核心结论正确，额外内容不应扣分
  * 中文量词（如"个""发""枚""架""挺""门""辆"等）的差异，只要数值相同且核心含义一致，不同量词应视为语义等价，不视为事实错误
- 格式、结构、排版类问题由独立的「JSON 正确性」指标评估，不在本指标重复扣分。

【重要提示】
必须同时输出 SCORE 和 REASON 两个部分，缺少任何一个都将被视为无效响应！

【输出格式】
请严格按照以下格式输出，必须包含 SCORE 和 REASON 两个标签：

SCORE: <0到1之间的数字>
REASON: <详细的中文分析，不少于80字，请先给出具体的评估结果判断，再进行详细分析>

示例格式：
SCORE: 0.85
REASON: 待评估回答基本正确，包含了标准答案中的大部分关键信息。回答准确指出了A001和B001两个物料的变更，以及变更前后的MPart编码。但未提及物料变更类型和在制品处理意见，因此略有信息缺失。整体评估为良好。"""
    }

    # 基于Prompt评测模式：使用数据项中的 prompt 作为评测标准
    # 兼容新旧名称：旧名"提示词评测"，新名"基于Prompt评测"
    is_prompt_mode = (
        metric_name == "correctness"
        and ("Prompt" in (judging_mode or "") or "提示词" in (judging_mode or ""))
        and eval_prompt
    )
    if is_prompt_mode:
        gt_section = ""
        if ground_truth and ground_truth.strip():
            gt_section = f"""
【参考答案（可选）】
{ground_truth}
"""
        prompts["correctness_prompt"] = f"""【评估任务】请根据以下评测提示词，对「待评估回答」进行评分。注意：下方「待评估回答」是要被评估的内容，不要对其进行处理或生成报告。

【问题】
{question}

【评测提示词】
{eval_prompt}
{gt_section}
【待评估回答】
{answer}

【评估标准】
请从 0.0 到 1.0 给出精确的连续评分（不要只给 0.0、0.5 或 1.0）：
- 0.9-1.0：回答完全满足评测提示词的所有要求，内容准确完整
- 0.7-0.8：回答基本满足评测提示词的要求，仅有少量次要内容缺失或表述略有不同
- 0.5-0.6：回答部分满足评测提示词的要求，存在明显的内容缺失或表述不准确
- 0.3-0.4：回答与评测提示词要求有一定关联，但关键内容大部分错误或缺失
- 0.1-0.2：回答与评测提示词要求几乎无关，仅存在极少相关内容
- 0.0：回答完全错误或与问题无关

【评估要求】
1. 请先直接回答：待评估回答相对于评测提示词的评分是多少？
2. 然后详细分析：回答中的哪些内容满足了评测提示词的要求？哪些内容存在错误或缺失？
3. 给出具体的评分和分析，不少于80字

【评分边界约束】
- 本指标只评估语义层面的正确性、完整性和一致性，即回答中的事实、数据、逻辑推导、结论等是否在语义上满足评测提示词的要求。
- 以下格式类问题不在本指标扣分范围内，请勿因这些问题降低语义评分：
  * 回答在 JSON 或其他结构化内容前后附加了额外说明文本（如摘要、注释）
  * 分隔符、标点符号的风格差异（如 "/" 与 "、"、逗号与句号）
  * 使用了 Markdown 代码块包裹、额外换行等排版差异
  * 表述风格差异（如自然语言 vs 伪代码风格），只要其传达的语义相同
  * 回答包含标准答案中没有的额外合理推导/计算内容（如基于已知数据计算出的派生指标），只要核心结论与标准答案一致且不矛盾，不应因此扣分
  * 回答比标准答案更详细或包含额外分析步骤，只要核心结论正确，额外内容不应扣分
  * 中文量词（如"个""发""枚""架""挺""门""辆"等）的差异，只要数值相同且核心含义一致，不同量词应视为语义等价，不视为事实错误
- 格式、结构、排版类问题由独立的「JSON 正确性」指标评估，不在本指标重复扣分。

【重要提示】
必须同时输出 SCORE 和 REASON 两个部分，缺少任何一个都将被视为无效响应！

【输出格式】
请严格按照以下格式输出，必须包含 SCORE 和 REASON 两个标签：

SCORE: <0到1之间的数字>
REASON: <详细的中文分析，不少于80字，请先给出具体的评估结果判断，再进行详细分析>

示例格式：
SCORE: 0.82
REASON: 待评估回答基本满足评测提示词的要求。回答准确涵盖了提示词中要求的关键要素，逻辑清晰且内容完整。但部分细节表述与要求略有偏差，整体评估为良好。"""

    prompt = prompts.get(metric_name, prompts["answer_relevancy"])
    # 忠诚度降级评估：无外部参考内容时切换为自包含事实一致性评估
    if metric_name == "faithfulness" and not effective_context:
        prompt = prompts["faithfulness_no_ref"]
    if is_prompt_mode:
        prompt = prompts["correctness_prompt"]

    try:
        response = await evaluate_with_llm(
            prompt,
            system_prompt="你是一个专业的评估助手。你的唯一任务是评估「待评估回答」的质量，不是生成报告或总结。无论回答内容是什么格式（JSON、表格、纯文本），都将其视为待评估的内容。请严格按照要求的格式回复，必须同时包含SCORE和REASON两个标签，使用中文进行详细分析。",
            model_key=model_key
        )

        # 记录 LLM 的原始响应（INFO 级别，确保能看到）
        log("INFO", "LLM_CLIENT",
            f"LLM 原始响应内容",
            metric_name=metric_name,
            model_name=model_name,
            response_length=len(response) if response else 0,
            raw_response=response if response else "[空响应]")

        # 检查响应是否为空
        if not response or not response.strip():
            error_msg = "LLM 返回空响应，可能原因：1) API调用超时 2) 输入内容过长被截断 3) 模型拒绝回答 4) Token 限制"
            log("ERROR", "LLM_CLIENT",
                f"LLM 评估失败：空响应",
                metric_name=metric_name,
                model_name=model_name,
                error_detail=error_msg)
            return 0.0, f"LLM 评估失败：{error_msg}"

        lines = response.strip().split('\n')
        score = 0.5
        reason = response  # 默认使用完整响应

        has_score = False
        has_reason_tag = False
        extracted_reason_parts = []

        for i, line in enumerate(lines):
            if line.startswith("SCORE:"):
                try:
                    score = float(line.replace("SCORE:", "").strip())
                    score = max(0.0, min(1.0, score))
                    has_score = True
                    log("DEBUG", "LLM_CLIENT",
                        f"提取到 SCORE",
                        metric_name=metric_name,
                        score=score,
                        score_line=line)
                except Exception as e:
                    log("WARNING", "LLM_CLIENT",
                        f"解析 SCORE 失败",
                        metric_name=metric_name,
                        error=str(e),
                        score_line=line)
            elif line.startswith("REASON:"):
                has_reason_tag = True
                # 提取 REASON: 后的内容，包括后续行
                reason_content = line.replace("REASON:", "").strip()
                if reason_content:
                    extracted_reason_parts.append(reason_content)
                # 继续收集后续行（如果 REASON 内容跨多行）
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith("SCORE:"):
                        extracted_reason_parts.append(next_line)
                    else:
                        break

        # 如果提取到了 REASON 内容，使用提取的内容
        if extracted_reason_parts:
            reason = '\n'.join(extracted_reason_parts)
            log("DEBUG", "LLM_CLIENT",
                f"提取到 REASON 内容",
                metric_name=metric_name,
                reason_parts_count=len(extracted_reason_parts),
                reason_length=len(reason))
        elif has_score and not has_reason_tag:
            # 有 SCORE 但没有 REASON 标签，使用完整响应
            log("WARNING", "LLM_CLIENT",
                f"LLM 未返回 REASON 标签，使用完整响应",
                metric_name=metric_name,
                response_length=len(response))
            reason = response

        # 如果 SCORE 标签未找到，尝试从响应中提取分数
        if not has_score:
            import re
            # 尝试匹配各种分数格式：0.85, 0.8, 85%, 85分
            score_patterns = [
                r'(?:score|得分|分数|评分)["\']?\s*[:：]\s*([01]?\.\d+)',
                r'(?:score|得分|分数|评分)["\']?\s*[:：]\s*([01]\.\d+)',
                r'\b([01]\.\d{1,2})\b\s*(?:分|points?)',
                r'(\d{1,3})\s*%',
                r'\b([01]\.\d{1,2})\b',
            ]
            for pattern in score_patterns:
                m = re.search(pattern, response, re.IGNORECASE)
                if m:
                    try:
                        extracted = float(m.group(1))
                        if extracted > 1.0:
                            extracted = extracted / 100.0
                        score = max(0.0, min(1.0, extracted))
                        has_score = True
                        log("WARNING", "LLM_CLIENT",
                            f"SCORE 标签未找到，从响应中提取分数",
                            metric_name=metric_name,
                            model_name=model_name,
                            extracted_score=score,
                            matched_text=m.group(0))
                        break
                    except (ValueError, IndexError):
                        continue

        if not has_score:
            # 仍然无法提取分数，使用默认值而非 0 分
            score = 0.5
            log("WARNING", "LLM_CLIENT",
                f"LLM 未返回标准 SCORE 格式，使用默认分 0.5",
                metric_name=metric_name,
                model_name=model_name,
                response_preview=response[:200])
            reason = f"LLM 响应未包含标准 SCORE 标签，使用默认评分 0.5。\n原始响应：\n{response[:500]}"

        log("INFO", "LLM_CLIENT",
            f"使用 {model_name} 模型评估 {metric_name} 指标完成",
            metric_name=metric_name,
            model_name=model_name,
            score=score,
            reason_length=len(reason),
            reason_preview=reason[:100] if reason else "")

        return score, reason

    except Exception as e:
        error_reason = f"LLM 评估失败: {str(e)}"
        log("ERROR", "LLM_CLIENT",
            f"使用 {model_name} 模型评估 {metric_name} 指标失败",
            metric_name=metric_name,
            model_name=model_name,
            error=str(e))
        return 0.0, error_reason


async def generate_evaluation_summary(
    task_id: str,
    dataset_id: str,
    model_name: str,
    results: list,
    model_key: str = None,
    dimension_verdicts: dict | None = None,
    agent_name: str = "",
    *,
    fail_examples: list | None = None,
    scenario_fails: list | None = None,
    stability_fails: list | None = None,
) -> str:
    """
    Generate evaluation summary using LLM.

    Args:
        task_id: Task ID
        dataset_id: Dataset ID
        model_name: 评估模型（用于 Judge 的 LLM）显示名
        results: List of evaluation results
        model_key: 模型标识 (glm)
        dimension_verdicts: 四维结论读模型（过程/场景/稳定性），可选
        agent_name: 被测 Agent 名称（prompt 中"被测 Agent"字段）
        fail_examples: top-N fail 题目摘要（含 eval_reason），可选
        scenario_fails: 场景子步骤 fail 明细，可选
        stability_fails: 运行稳定性 fail 明细，可选

    Returns:
        Generated summary text
    """
    from services.quality_score_service import RUNTIME_METRICS

    total_questions = len(set(r['question_id'] for r in results))
    metrics = {}

    for result in results:
        metric_name = result['metric_name']
        if metric_name in RUNTIME_METRICS:
            continue
        if metric_name not in metrics:
            metrics[metric_name] = {'scores': [], 'passed': 0, 'total': 0}

        metrics[metric_name]['scores'].append(result['score'])
        metrics[metric_name]['total'] += 1
        if result['passed']:
            metrics[metric_name]['passed'] += 1

    summary_stats = []
    for metric_name, data in metrics.items():
        valid_scores = [s for s in data['scores'] if s is not None]
        avg_score = sum(valid_scores) / len(valid_scores) if valid_scores else 0
        pass_rate = (data['passed'] / data['total'] * 100) if data['total'] > 0 else 0

        metric_cn = {
            'faithfulness': '忠实度',
            'answer_relevancy': '回答相关性',
            'correctness': '正确性'
        }.get(metric_name, metric_name)

        summary_stats.append(f"{metric_cn}: 平均分 {avg_score:.3f}, 通过率 {pass_rate:.1f}%")

    stats_text = '\n'.join(summary_stats) if summary_stats else '（无质量类指标）'

    dimension_text = ""
    if dimension_verdicts:
        from services.task_dimension_verdict_service import format_dimension_verdicts_for_summary
        dimension_text = format_dimension_verdicts_for_summary(dimension_verdicts)

    dimension_block = ""
    if dimension_text:
        dimension_block = f"""
四维评测结论（规则聚合，供总结引用）：
{dimension_text}
"""

    fail_detail_block = _build_fail_detail_block(fail_examples, scenario_fails, stability_fails)

    prompt = f"""请为以下评测结果生成一份专业的中文总结报告。

任务信息：
- 任务ID: {task_id}
- 数据集ID: {dataset_id}
- 被测 Agent: {agent_name or "未指定"}
- 评估模型: {model_name}
- 评测问题总数: {total_questions}

质量指标统计（LLM/脚本 Judge）：
{stats_text}
{dimension_block}{fail_detail_block}
请生成一份结构化的总结报告，包括：
1. 整体表现概述（含质量总分/达标情况）
2. 质量指标详细分析
3. 过程一致性、场景子步骤交付、运行稳定性（若上文有数据则必须分别简述，稳定性仅描述结构/性能波动，勿与质量 pass/fail 混淆）
4. 优点总结
5. 改进建议

要求：
- 使用专业、客观的语言
- 突出关键发现，与四维结论保持一致
- 在"质量指标详细分析"和"改进建议"中，针对上述失败题目明细给出具体归因和可执行建议
- 引用具体题号、步骤名、超阈值数据，避免空泛描述
- 提供具体可行的建议
- 字数控制在500-900字之间"""

    try:
        summary = await evaluate_with_llm(
            prompt,
            system_prompt="你是一个专业的AI评测分析师，擅长分析 Agent 评测结果并提供专业建议。",
            model_key=model_key
        )
        return summary
    except Exception as e:
        return f"生成总结失败: {str(e)}"


def _build_fail_detail_block(
    fail_examples: list | None,
    scenario_fails: list | None,
    stability_fails: list | None,
) -> str:
    """构造 fail 明细 prompt block，供 LLM 引用具体题号和原因。"""
    if not (fail_examples or scenario_fails or stability_fails):
        return ""
    parts = ["\n失败题目与维度明细（用于深入分析，请针对性引用具体题号和原因）："]
    if fail_examples:
        parts.append("\n【质量指标 fail 题目 Top-10】")
        for fq in fail_examples[:10]:
            score = fq.get('score')
            score_str = f"{float(score):.2f}" if score is not None else "—"
            parts.append(
                f"- 题目#{fq.get('question_id')} 得分 {score_str} "
                f"指标 {fq.get('metric_name')} 通过={fq.get('passed')}"
            )
            reason = fq.get('eval_reason') or ""
            if reason:
                parts.append(f"  点评: {reason[:200]}")
    if scenario_fails:
        parts.append(f"\n【场景子步骤 fail 明细 共{len(scenario_fails)}条，列前10】")
        for sf in scenario_fails[:10]:
            score = sf.get('score')
            score_str = f"{float(score):.2f}" if score is not None else "—"
            parts.append(
                f"- 题目#{sf.get('qid')} 步骤「{sf.get('step_name')}」"
                f"得分 {score_str} 必做={sf.get('required')}"
            )
            reason = sf.get('reason') or ""
            if reason:
                parts.append(f"  原因: {reason[:200]}")
    if stability_fails:
        parts.append(f"\n【运行稳定性 fail 明细 共{len(stability_fails)}条，列前10】")
        for st in stability_fails[:10]:
            parts.append(
                f"- 题目#{st.get('qid')} {st.get('sub')} {st.get('val')} (目标{st.get('thr')})"
            )
            reason = st.get('reason') or ""
            if reason:
                parts.append(f"  原因: {reason[:200]}")
    return "\n".join(parts) + "\n"
