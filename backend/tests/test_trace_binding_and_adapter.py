"""
A1 + A4 单元测试：Trace Binding Compiler + Trace Adapter。
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import Base
from services.agent_asset_adapter import AssetCatalog, OperationCandidate, build_asset_catalog
from services.trace_binding_compiler import (
    compile_match_rules,
    compile_binding_reason,
    compile_and_persist_rules,
)
from services.trace_adapter import (
    inject_eval_operation_ids,
    _evaluate_match_rules,
    _resolve_path,
    _apply_op,
)


@pytest.fixture
async def mem_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ── compile_match_rules ────────────────────────────────────────


class TestCompileMatchRules:
    def test_basic_script_candidate(self):
        candidate = OperationCandidate(
            operation_id="voice.parse",
            display_name="Voice Parse",
            source_type="script",
            match_hints={"script_name": "parse.py", "agent_step_kind": "tool"},
        )
        rules = compile_match_rules(candidate)
        assert rules["mode"] == "any"
        assert rules["version"] == "2.0"
        # 应有 eval.operation.id 主规则 + script_name + step_kind 辅助规则
        assert len(rules["rules"]) >= 3
        # 第一条是 eval.operation.id
        assert rules["rules"][0]["path"] == "$.attributes.eval.operation.id"
        assert rules["rules"][0]["value"] == "voice.parse"

    def test_tool_candidate(self):
        candidate = OperationCandidate(
            operation_id="web_search",
            display_name="Web Search",
            source_type="tool",
            match_hints={"agent_tool_name": "web_search", "agent_step_kind": "tool"},
        )
        rules = compile_match_rules(candidate)
        rule_paths = [r["path"] for r in rules["rules"]]
        assert "$.attributes.agent.tool.name" in rule_paths

    def test_action_with_step_id(self):
        candidate = OperationCandidate(
            operation_id="plan",
            display_name="Planner",
            source_type="action",
            match_hints={"agent_step_id": "planner"},
        )
        rules = compile_match_rules(candidate)
        rule_paths = [r["path"] for r in rules["rules"]]
        assert "$.attributes.agent.step.id" in rule_paths

    def test_no_hints(self):
        candidate = OperationCandidate(
            operation_id="unknown_op",
            display_name="Unknown",
            source_type="script",
            match_hints={},
        )
        rules = compile_match_rules(candidate)
        # 仅有 eval.operation.id 主规则
        assert len(rules["rules"]) == 1


# ── compile_binding_reason ─────────────────────────────────────


class TestCompileBindingReason:
    def test_script_hint(self):
        c = OperationCandidate("op1", "Op1", "script", match_hints={"script_name": "parse.py"})
        assert "parse.py" in compile_binding_reason(c)

    def test_tool_hint(self):
        c = OperationCandidate("op1", "Op1", "tool", match_hints={"agent_tool_name": "search"})
        assert "search" in compile_binding_reason(c)

    def test_no_hint(self):
        c = OperationCandidate("my_op", "My Op", "script", match_hints={})
        assert "my_op" in compile_binding_reason(c)


# ── compile_and_persist_rules ──────────────────────────────────


class TestCompileAndPersistRules:
    @pytest.mark.asyncio
    async def test_persist_creates_rules(self, mem_session):
        from services.operation_registry_service import get_active_rules_for_agent

        catalog = await build_asset_catalog(
            "test_agent",
            skill_manifest={
                "scripts": [{"name": "parse", "path": "/scripts/parse.py"}],
            },
        )
        count = await compile_and_persist_rules(mem_session, catalog)
        assert count == 1

        rules = await get_active_rules_for_agent(mem_session, "test_agent")
        assert len(rules) == 1
        assert rules[0].operation_id == "parse"

    @pytest.mark.asyncio
    async def test_version_increment(self, mem_session):
        from services.operation_registry_service import get_latest_registry_version

        catalog1 = await build_asset_catalog(
            "ver_agent",
            skill_manifest={"scripts": [{"name": "op_a"}]},
        )
        await compile_and_persist_rules(mem_session, catalog1)
        v1 = await get_latest_registry_version(mem_session, "ver_agent")
        assert v1 == 1

        catalog2 = await build_asset_catalog(
            "ver_agent",
            skill_manifest={"scripts": [{"name": "op_a"}, {"name": "op_b"}]},
        )
        await compile_and_persist_rules(mem_session, catalog2)
        v2 = await get_latest_registry_version(mem_session, "ver_agent")
        assert v2 == 2


# ── _resolve_path ──────────────────────────────────────────────


class TestResolvePath:
    def test_simple(self):
        attrs = {"eval": {"operation": {"id": "voice.parse"}}}
        assert _resolve_path(attrs, "$.attributes.eval.operation.id") == "voice.parse"

    def test_nested(self):
        attrs = {"agent": {"step": {"id": "planner"}}}
        assert _resolve_path(attrs, "$.attributes.agent.step.id") == "planner"

    def test_missing_key(self):
        attrs = {"foo": "bar"}
        assert _resolve_path(attrs, "$.attributes.baz") is None

    def test_strip_prefix(self):
        attrs = {"eval": {"operation": {"id": "test"}}}
        assert _resolve_path(attrs, "eval.operation.id") == "test"


# ── _apply_op ──────────────────────────────────────────────────


class TestApplyOp:
    def test_eq(self):
        assert _apply_op("voice.parse", "eq", "voice.parse") is True
        assert _apply_op("voice.parse", "eq", "other") is False

    def test_contains(self):
        assert _apply_op("客户声音结构化解析.py", "contains", "客户声音") is True

    def test_gte(self):
        assert _apply_op(0.96, "gte", 0.8) is True
        assert _apply_op(0.5, "gte", 0.8) is False


# ── _evaluate_match_rules ─────────────────────────────────────


class TestEvaluateMatchRules:
    def test_any_mode_hit(self):
        attrs = {"eval": {"operation": {"id": "voice.parse"}}}
        rules = {
            "mode": "any",
            "rules": [
                {"type": "jsonpath", "path": "$.attributes.eval.operation.id", "op": "eq", "value": "voice.parse"},
            ],
        }
        assert _evaluate_match_rules(attrs, rules) is True

    def test_any_mode_miss(self):
        attrs = {"agent": {"tool": {"name": "search"}}}
        rules = {
            "mode": "any",
            "rules": [
                {"type": "jsonpath", "path": "$.attributes.eval.operation.id", "op": "eq", "value": "voice.parse"},
            ],
        }
        assert _evaluate_match_rules(attrs, rules) is False

    def test_all_mode(self):
        attrs = {
            "eval": {"operation": {"id": "voice.parse"}},
            "agent": {"step": {"kind": "tool"}},
        }
        rules = {
            "mode": "all",
            "rules": [
                {"type": "jsonpath", "path": "$.attributes.eval.operation.id", "op": "eq", "value": "voice.parse"},
                {"type": "jsonpath", "path": "$.attributes.agent.step.kind", "op": "eq", "value": "tool"},
            ],
        }
        assert _evaluate_match_rules(attrs, rules) is True

    def test_empty_rules(self):
        assert _evaluate_match_rules({}, {"mode": "any", "rules": []}) is False


# ── inject_eval_operation_ids ──────────────────────────────────


class TestInjectEvalOperationIds:
    @pytest.mark.asyncio
    async def test_inject_with_rules(self, mem_session):
        from database import AgentRun, RunSpanNode
        from services.operation_registry_service import upsert_operation_rule

        # 准备 AgentRun
        run = AgentRun(
            id="run_adapter_test",
            agent_id="agent_1",
            logical_agent_id="adapter_agent",
            agent_snapshot="{}",
            status="synced",
            started_at=__import__("datetime").datetime.utcnow(),
        )
        mem_session.add(run)

        # 准备 SpanNode
        span = RunSpanNode(
            id="span_adapter_1",
            agent_run_id="run_adapter_test",
            sibling_order=0,
            observation_id="obs_1",
            name="ParseScript",
            node_type="tool",
            attributes={"agent": {"script": {"path": "parse.py"}}},
        )
        mem_session.add(span)

        # 写入规则
        await upsert_operation_rule(
            mem_session,
            logical_agent_id="adapter_agent",
            operation_id="parse",
            match_rules={
                "mode": "any",
                "rules": [
                    {"type": "jsonpath", "path": "$.attributes.agent.script.path", "op": "contains", "value": "parse.py"},
                ],
            },
        )
        await mem_session.flush()

        # 执行注入
        injected = await inject_eval_operation_ids(mem_session, run, [span])
        assert injected == 1

        # 验证属性已注入：flush 让 flag_modified 标记的改动落 DB，
        # 再 refresh 重新加载（refresh 本身不 autoflush）
        await mem_session.flush()
        await mem_session.refresh(span)
        attrs = span.attributes
        assert attrs.get("eval.operation.id") == "parse"
        assert "eval.binding.rule_id" in attrs
        assert "eval.binding.confidence" in attrs

    @pytest.mark.asyncio
    async def test_no_rules_no_inject(self, mem_session):
        from database import AgentRun, RunSpanNode

        run = AgentRun(
            id="run_no_rules",
            agent_id="agent_2",
            logical_agent_id="no_rules_agent",
            agent_snapshot="{}",
            status="synced",
            started_at=__import__("datetime").datetime.utcnow(),
        )
        mem_session.add(run)
        span = RunSpanNode(
            id="span_no_rules",
            agent_run_id="run_no_rules",
            sibling_order=0,
            attributes={"foo": "bar"},
        )
        mem_session.add(span)
        await mem_session.flush()

        injected = await inject_eval_operation_ids(mem_session, run, [span])
        assert injected == 0

    @pytest.mark.asyncio
    async def test_no_logical_agent_id(self, mem_session):
        from database import AgentRun

        run = AgentRun(
            id="run_no_laid",
            agent_id="agent_3",
            logical_agent_id=None,
            agent_snapshot="{}",
            status="synced",
            started_at=__import__("datetime").datetime.utcnow(),
        )
        mem_session.add(run)
        await mem_session.flush()

        injected = await inject_eval_operation_ids(mem_session, run, [])
        assert injected == 0
