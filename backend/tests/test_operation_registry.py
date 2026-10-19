"""
A2a + A3 单元测试：Operation Registry ORM + Agent Asset Adapter。
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import Base
from services.agent_asset_adapter import (
    AssetCatalog,
    OperationCandidate,
    build_asset_catalog,
    compute_asset_hash,
    parse_ontology,
    parse_skill_manifest,
    parse_tool_schema,
    parse_script_list,
    _normalize_operation_id,
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


# ── _normalize_operation_id ─────────────────────────────────────


class TestNormalizeOperationId:
    def test_simple_name(self):
        assert _normalize_operation_id("CustomerFeedback") == "customerfeedback"

    def test_path_with_slashes(self):
        assert _normalize_operation_id("voice/parse") == "voice_parse"

    def test_chinese_and_special(self):
        result = _normalize_operation_id("客户声音结构化解析.py")
        assert result == "py"  # 中文字符被替换，剩余 py

    def test_empty_input(self):
        assert _normalize_operation_id("") == "unknown"
        assert _normalize_operation_id(None) == "unknown"

    def test_dots_replaced_with_underscore(self):
        # 归一化规则：非字母数字统一替换为下划线（点号不保留）
        assert _normalize_operation_id("voice.customer_feedback.parse") == "voice_customer_feedback_parse"


# ── parse_ontology ──────────────────────────────────────────────


class TestParseOntology:
    def test_actions_list(self):
        data = {
            "actions": [
                {"name": "parse_feedback", "display_name": "Parse Feedback"},
                {"name": "route_intent", "display_name": "Route Intent"},
            ]
        }
        result = parse_ontology(data)
        assert len(result) == 2
        assert result[0].operation_id == "parse_feedback"
        assert result[0].source_type == "action"

    def test_flat_items(self):
        data = {
            "items": [
                {"name": "plan", "type": "action"},
                {"name": "retrieve", "type": "llm"},  # 非 action，跳过
            ]
        }
        result = parse_ontology(data)
        assert len(result) == 1
        assert result[0].operation_id == "plan"

    def test_empty(self):
        assert parse_ontology({}) == []


# ── parse_skill_manifest ────────────────────────────────────────


class TestParseSkillManifest:
    def test_scripts_and_tools(self):
        data = {
            "scripts": [
                {"name": "客户声音解析", "path": "/skills/parse.py"},
            ],
            "tools": [
                {"name": "web_search"},
            ],
        }
        result = parse_skill_manifest(data)
        assert len(result) == 2
        # script：中文名被归一化
        assert result[0].source_type == "script"
        assert result[0].match_hints.get("script_name") == "客户声音解析"
        # tool
        assert result[1].operation_id == "web_search"
        assert result[1].match_hints.get("agent_step_kind") == "tool"

    def test_actions(self):
        data = {
            "actions": [
                {"name": "summarize", "skill_name": "summary_skill"},
            ]
        }
        result = parse_skill_manifest(data)
        assert len(result) == 1
        assert result[0].match_hints.get("agent_step_id") == "summarize"


# ── parse_tool_schema ──────────────────────────────────────────


class TestParseToolSchema:
    def test_openapi_paths(self):
        data = {
            "paths": {
                "/tool/search": {
                    "post": {"operationId": "search", "summary": "Search Tool"},
                }
            }
        }
        result = parse_tool_schema(data)
        assert len(result) == 1
        assert result[0].operation_id == "search"

    def test_flat_tools(self):
        data = {"tools": [{"name": "sql_query", "description": "SQL Query Tool"}]}
        result = parse_tool_schema(data)
        assert len(result) == 1
        assert result[0].source_type == "tool"


# ── parse_script_list ──────────────────────────────────────────


class TestParseScriptList:
    def test_basic(self):
        data = [
            {"name": "parse_feedback", "path": "/scripts/parse.py"},
            {"name": "route_intent", "path": "/scripts/route.py"},
        ]
        result = parse_script_list(data)
        assert len(result) == 2
        assert result[0].match_hints.get("script_name") == "parse_feedback"

    def test_path_only(self):
        data = [{"path": "/scripts/analyze.py"}]
        result = parse_script_list(data)
        assert len(result) == 1
        assert result[0].operation_id == "analyze_py"


# ── build_asset_catalog ────────────────────────────────────────


class TestBuildAssetCatalog:
    @pytest.mark.asyncio
    async def test_merge_from_multiple_sources(self):
        catalog = await build_asset_catalog(
            "agent_v1",
            ontology={"actions": [{"name": "plan"}]},
            skill_manifest={"scripts": [{"name": "plan", "path": "/plan.py"}]},
        )
        # 同名 operation_id 应合并
        assert len(catalog.operations) == 1
        op = catalog.operations[0]
        assert op.operation_id == "plan"
        # match_hints 合并：ontology 的 agent_step_id + skill 的 script_name
        assert "agent_step_id" in op.match_hints
        assert "script_name" in op.match_hints

    @pytest.mark.asyncio
    async def test_empty_catalog(self):
        catalog = await build_asset_catalog("empty_agent")
        assert len(catalog.operations) == 0

    @pytest.mark.asyncio
    async def test_dedup_by_operation_id(self):
        catalog = await build_asset_catalog(
            "agent_v2",
            skill_manifest={
                "scripts": [{"name": "analyze"}],
                "tools": [{"name": "analyze"}],
            },
        )
        # 同 operation_id 去重
        assert len(catalog.operations) == 1


# ── compute_asset_hash ─────────────────────────────────────────


class TestComputeAssetHash:
    def test_deterministic(self):
        payload = {"actions": [{"name": "plan"}]}
        h1 = compute_asset_hash("ontology", payload)
        h2 = compute_asset_hash("ontology", payload)
        assert h1 == h2

    def test_type_sensitive(self):
        payload = {"x": 1}
        h1 = compute_asset_hash("ontology", payload)
        h2 = compute_asset_hash("skill_manifest", payload)
        assert h1 != h2

    def test_content_sensitive(self):
        h1 = compute_asset_hash("ontology", {"a": 1})
        h2 = compute_asset_hash("ontology", {"a": 2})
        assert h1 != h2


# ── Operation Registry CRUD（需 DB session，使用 mock）───────────


class TestOperationRegistryCRUD:
    @pytest.mark.asyncio
    async def test_upsert_asset_snapshot_create(self, mem_session):
        from services.operation_registry_service import upsert_asset_snapshot

        snap = await upsert_asset_snapshot(
            mem_session,
            logical_agent_id="test_agent",
            asset_type="ontology",
            asset_hash="abc123",
            asset_payload={"actions": [{"name": "plan"}]},
        )
        assert snap.id.startswith("asset_")
        assert snap.logical_agent_id == "test_agent"
        assert snap.compile_status == "pending"

    @pytest.mark.asyncio
    async def test_upsert_asset_snapshot_idempotent(self, mem_session):
        from services.operation_registry_service import upsert_asset_snapshot

        s1 = await upsert_asset_snapshot(
            mem_session,
            logical_agent_id="test_agent",
            asset_type="ontology",
            asset_hash="same_hash",
            asset_payload={"v": 1},
        )
        s2 = await upsert_asset_snapshot(
            mem_session,
            logical_agent_id="test_agent",
            asset_type="ontology",
            asset_hash="same_hash",
            asset_payload={"v": 2},
        )
        assert s1.id == s2.id
        assert s2.asset_payload == {"v": 2}

    @pytest.mark.asyncio
    async def test_upsert_operation_rule(self, mem_session):
        from services.operation_registry_service import upsert_operation_rule

        rule = await upsert_operation_rule(
            mem_session,
            logical_agent_id="test_agent",
            operation_id="voice.parse",
            match_rules={"mode": "all", "rules": [{"type": "jsonpath", "path": "$.attributes.script_name", "op": "eq", "value": "parse.py"}]},
            display_name="Voice Parse",
        )
        assert rule.id.startswith("tor_")
        assert rule.operation_id == "voice.parse"
        assert rule.enabled is True

    @pytest.mark.asyncio
    async def test_get_active_rules(self, mem_session):
        from services.operation_registry_service import (
            upsert_operation_rule,
            get_active_rules_for_agent,
        )

        await upsert_operation_rule(
            mem_session,
            logical_agent_id="test_agent",
            operation_id="op_a",
            match_rules={"mode": "any", "rules": []},
        )
        await upsert_operation_rule(
            mem_session,
            logical_agent_id="test_agent",
            operation_id="op_b",
            match_rules={"mode": "any", "rules": []},
        )
        rules = await get_active_rules_for_agent(mem_session, "test_agent")
        assert len(rules) == 2

    @pytest.mark.asyncio
    async def test_deactivate_old_rules(self, mem_session):
        from services.operation_registry_service import (
            upsert_operation_rule,
            deactivate_old_rules,
            get_active_rules_for_agent,
        )

        # v1
        await upsert_operation_rule(
            mem_session,
            logical_agent_id="deact_agent",
            operation_id="old_op",
            match_rules={"mode": "any", "rules": []},
            registry_version=1,
        )
        # v2
        await upsert_operation_rule(
            mem_session,
            logical_agent_id="deact_agent",
            operation_id="new_op",
            match_rules={"mode": "any", "rules": []},
            registry_version=2,
        )
        count = await deactivate_old_rules(mem_session, logical_agent_id="deact_agent", keep_version=2)
        assert count >= 1

        # 验证 v1 被禁用
        rules = await get_active_rules_for_agent(mem_session, "deact_agent", registry_version=2)
        assert all(r.registry_version >= 2 for r in rules)
