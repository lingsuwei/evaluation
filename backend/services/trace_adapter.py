"""
Trace Adapter（A4 / §7.5.5 / §8.8.2）：Builder 物化时查询 Operation Registry → 注入 eval.operation.id。

核心职责：
- 查询 Agent 当前生效的 TraceOperationRule
- 对每个 RunSpanNode 的 attributes 做规则匹配
- 命中时注入 eval.operation.id / eval.binding.rule_id / eval.binding.confidence / eval.binding.reason
- 未命中时不注入（退回 agent.step.id 或 name 降级）

设计原则：
- 查 Registry → 有则注入，无则跳过（不阻塞）
- 注入是幂等的（重复执行结果一致）
- 不修改 match_rules 逻辑，仅做属性注入
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from database import AgentRun, RunSpanNode, TraceOperationRule
from logger import log


# ── 属性注入 ────────────────────────────────────────────────────


async def inject_eval_operation_ids(
    session: AsyncSession,
    run: AgentRun,
    span_nodes: Sequence[RunSpanNode],
) -> int:
    """对 AgentRun 下的 Span 节点注入 eval.operation.id 等属性。

    流程：
    1. 查询 run.logical_agent_id 对应的生效规则
    2. 遍历 span_nodes，对每个节点尝试匹配
    3. 命中时更新 attributes 并标记 dirty

    返回命中注入的节点数。
    """
    logical_agent_id = run.logical_agent_id
    if not logical_agent_id:
        return 0

    # 1. 查询生效规则
    rules = await _load_active_rules(session, logical_agent_id)
    if not rules:
        log(
            "DEBUG",
            "TRACE_ADAPTER",
            "无生效规则，跳过注入",
            logical_agent_id=logical_agent_id,
            agent_run_id=run.id,
        )
        return 0

    # 2. 遍历匹配
    injected = 0
    for node in span_nodes:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        matched_rule = _match_node(attrs, rules)
        if matched_rule is not None:
            _inject_binding_attributes(attrs, matched_rule)
            # RunSpanNode.attributes 是原生 Column(JSON)，无 MutableDict 包装。
            # attrs 取自 node.attributes 的内部引用，_inject_binding_attributes
            # 原地改 dict 内容，SQLAlchemy 不会感知变化（history 仍为 unchanged）。
            # 重新赋值 node.attributes = attrs / dict(attrs) 也无效：新旧值 == 相等。
            # 必须用 flag_modified 强制标记 dirty，flush 时才生成 UPDATE。
            flag_modified(node, "attributes")
            injected += 1

    log(
        "INFO",
        "TRACE_ADAPTER",
        "eval.operation.id 注入完成",
        agent_run_id=run.id,
        logical_agent_id=logical_agent_id,
        total_nodes=len(span_nodes),
        injected=injected,
        rule_count=len(rules),
    )
    return injected


# ── 规则加载 ────────────────────────────────────────────────────


async def _load_active_rules(
    session: AsyncSession,
    logical_agent_id: str,
) -> Sequence[TraceOperationRule]:
    """加载 Agent 当前生效的操作规则，按 priority 降序。"""
    stmt = (
        select(TraceOperationRule)
        .where(
            TraceOperationRule.logical_agent_id == logical_agent_id,
            TraceOperationRule.enabled.is_(True),
        )
        .order_by(TraceOperationRule.priority.desc(), TraceOperationRule.operation_id)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ── 规则匹配 ────────────────────────────────────────────────────


def _match_node(
    attrs: Dict[str, Any],
    rules: Sequence[TraceOperationRule],
) -> Optional[TraceOperationRule]:
    """对单个节点的 attributes 尝试匹配规则，返回首个命中的规则。

    匹配逻辑遵循 match_rules JSON 中的 mode 和 rules：
    - mode=all: 所有规则必须命中
    - mode=any: 任一规则命中即可

    每条规则支持 jsonpath 类型（$.attributes.xxx）。
    """
    for rule in rules:
        match_rules = rule.match_rules
        if not isinstance(match_rules, dict):
            continue
        if _evaluate_match_rules(attrs, match_rules):
            return rule
    return None


def _evaluate_match_rules(
    attrs: Dict[str, Any],
    match_rules: Dict[str, Any],
) -> bool:
    """评估 match_rules JSON 对 attributes 是否命中。"""
    mode = match_rules.get("mode", "any")
    rules_list = match_rules.get("rules", [])
    if not rules_list:
        return False

    results = [_evaluate_single_rule(attrs, r) for r in rules_list if isinstance(r, dict)]

    if mode == "all":
        return all(results) if results else False
    else:  # "any"
        return any(results) if results else False


def _evaluate_single_rule(
    attrs: Dict[str, Any],
    rule: Dict[str, Any],
) -> bool:
    """评估单条匹配规则。

    支持 type=jsonpath：对 $.attributes.xxx 做点分路径取值。
    支持 op: eq / contains / regex / gte / in
    """
    rule_type = rule.get("type", "")
    if rule_type != "jsonpath":
        # 其他类型暂不支持，降级为不命中
        return False

    path = rule.get("path", "")
    op = rule.get("op", "eq")
    expected = rule.get("value")

    actual = _resolve_path(attrs, path)
    if actual is None:
        return False

    return _apply_op(actual, op, expected)


def _resolve_path(attrs: Dict[str, Any], path: str) -> Any:
    """轻量 JSONPath 解析：仅支持 $.attributes.a.b.c 点分路径。

    会自动剥离 $.attributes. 前缀（因为 attrs 已经是 attributes 字典）。
    """
    if not path:
        return None

    # 剥离 $.attributes. 前缀
    normalized = path
    if normalized.startswith("$.attributes."):
        normalized = normalized[len("$.attributes."):]
    elif normalized.startswith("$."):
        normalized = normalized[2:]

    parts = normalized.split(".")
    current: Any = attrs
    for part in parts:
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _apply_op(actual: Any, op: str, expected: Any) -> bool:
    """应用比较操作符。"""
    import re

    try:
        if op == "eq":
            return str(actual) == str(expected)
        elif op == "contains":
            return str(expected) in str(actual)
        elif op == "regex":
            return bool(re.search(str(expected), str(actual)))
        elif op == "gte":
            return float(actual) >= float(expected)
        elif op == "in":
            if isinstance(expected, list):
                return str(actual) in [str(e) for e in expected]
            return str(actual) == str(expected)
        else:
            return False
    except (TypeError, ValueError):
        return False


# ── 属性注入 ────────────────────────────────────────────────────


def _inject_binding_attributes(
    attrs: Dict[str, Any],
    rule: TraceOperationRule,
) -> None:
    """将匹配结果注入 attributes 字典。

    注入字段（§8.8.2）：
    - eval.operation.id: string
    - eval.binding.rule_id: string
    - eval.binding.confidence: number
    - eval.binding.reason: string
    """
    attrs["eval.operation.id"] = rule.operation_id
    attrs["eval.binding.rule_id"] = rule.id
    attrs["eval.binding.confidence"] = rule.confidence

    # 生成命中原因
    reason_template = rule.binding_reason_template or f"matched operation {rule.operation_id}"
    attrs["eval.binding.reason"] = reason_template
