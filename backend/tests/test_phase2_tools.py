"""Phase 2 工具函数冒烟测试（独立内存 SQLite，不走 database.py 全局引擎）。"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from database import Base, Dataset, Agent, LLMModel, CustomMetric, ChatSession, ChatMessage
from chat.tools import (
    list_datasets, list_agents, list_llm_models, list_metrics,
    get_dataset_preview, recommend_metrics, build_eval_request,
)


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as s:
        # Seed
        s.add(Dataset(id="ds_test", name="测试数据集",
                      data=json.dumps([{"question": "q1", "ground_truth": "g1"}]),
                      item_count=1))
        s.add(Dataset(id="ds_empty", name="空集", data="[]", item_count=0))
        s.add(Agent(id="ag_test", name="测试Agent", is_active=True, agent_kind="single"))
        s.add(Agent(id="ag_inactive", name="已停用", is_active=False, agent_kind="single"))
        s.add(LLMModel(id="glm", name="智谱GLM", base_url="http://x", model="glm-4", is_default=True))
        s.add(CustomMetric(id=1, name="自定义指标1", kind="geval", is_active=True))
        s.add(ChatSession(id="chat_test", title="测试会话", status="draft"))
        await s.commit()

    async with factory() as s:
        # 1. list_datasets
        ds = await list_datasets(s)
        assert ds["total"] == 2, f"list_datasets: {ds}"
        print(f"  [PASS] list_datasets: {ds['total']} items")

        # 2. list_agents (only active)
        ag = await list_agents(s)
        assert ag["total"] == 1, f"list_agents: {ag}"
        assert ag["items"][0]["id"] == "ag_test"
        print(f"  [PASS] list_agents: {ag['total']} active (excluded inactive)")

        # 3. list_llm_models (no api_key leaked)
        llm = await list_llm_models(s)
        assert llm["total"] == 1 and llm["items"][0]["key"] == "glm"
        assert "api_key" not in llm["items"][0]
        print(f"  [PASS] list_llm_models: {llm['total']} model, no api_key leaked")

        # 4. list_metrics (flatten builtin + custom)
        metrics = await list_metrics(s)
        keys = [m["key"] for m in metrics["items"]]
        assert "faithfulness" in keys
        assert "custom_1" in keys
        print(f"  [PASS] list_metrics: {metrics['total']} metrics (builtin+custom)")

        # 4b. list_metrics offline filter excludes runtime
        metrics_offline = await list_metrics(s, evaluation_mode="offline")
        offline_keys = [m["key"] for m in metrics_offline["items"]]
        assert "inference_latency" not in offline_keys
        print(f"  [PASS] list_metrics(offline): runtime excluded, {metrics_offline['total']} items")

        # 5. get_dataset_preview
        prev = await get_dataset_preview(s, dataset_id="ds_test")
        assert prev["found"] and prev["item_count"] == 1
        assert "question" in prev["fields"]
        print(f"  [PASS] get_dataset_preview: fields={prev['fields']}")

        # 5b. get_dataset_preview 404
        prev404 = await get_dataset_preview(s, dataset_id="ds_missing")
        assert not prev404["found"]
        print(f"  [PASS] get_dataset_preview(404): not found")

        # 6. recommend_metrics online + focus
        rec = await recommend_metrics(s, online_mode=True, focus="忠实度")
        assert "faithfulness" in rec["recommended"]
        assert "inference_latency" in rec["recommended"]
        print(f"  [PASS] recommend_metrics(online,忠实): {rec['recommended']}")

        # 6b. recommend_metrics offline, no focus → 3 quality only
        rec2 = await recommend_metrics(s, online_mode=False, focus=None)
        assert len(rec2["recommended"]) == 3
        assert "inference_latency" not in rec2["recommended"]
        print(f"  [PASS] recommend_metrics(offline,no focus): {rec2['recommended']}")

        # 7. build_eval_request (incremental update)
        br = await build_eval_request(
            s, session_id="chat_test",
            dataset_id="ds_test", online_mode=True, agent_id="ag_test",
        )
        assert "dataset_id" in br["updated_fields"]
        assert br["draft"]["dataset_id"] == "ds_test"
        assert "dataset_id" not in br["missing_fields"]
        print(f"  [PASS] build_eval_request: updated={br['updated_fields']} missing={br['missing_fields']}")

        # 7b. build_eval_request incremental (add llm_model)
        br2 = await build_eval_request(s, session_id="chat_test", llm_model="glm")
        assert br2["draft"]["dataset_id"] == "ds_test"  # preserved
        assert br2["draft"]["llm_model"] == "glm"  # new
        print(f"  [PASS] build_eval_request(incremental): draft preserved + new field")

    await engine.dispose()
    print()
    print("=== All Phase 2 tool smoke tests PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
