"""诊断脚本：检查离线任务的 process_eval_completed 状态"""
import asyncio
import sys
sys.path.insert(0, '/data/code/onto-eval/backend')

from database import async_session, AgentRun, EvaluationTask
from sqlalchemy import select, func, text


async def main():
    task_id = "task_de429aca842f4e68b5b975b2801a071b"

    async with async_session() as session:
        task = await session.get(EvaluationTask, task_id)

        if not task:
            print(f"❌ 任务 {task_id} 不存在")
            return

        print("=" * 60)
        print(f"任务 ID: {task.id}")
        print(f"状态: {task.status}")
        print(f"数据集: {task.dataset_id}")
        print(f"Agent ID: {task.agent_id}")
        print(f"evaluation_mode: {getattr(task, 'evaluation_mode', 'N/A')}")
        print(f"process_eval_completed: {getattr(task, 'process_eval_completed', 'N/A')}")
        print(f"langfuse_source: {getattr(task, 'langfuse_source', 'N/A')}")
        print("=" * 60)

        runs_result = await session.execute(
            select(func.count(AgentRun.id)).where(AgentRun.task_id == task_id)
        )
        run_count = runs_result.scalar()
        print(f"AgentRun 数量: {run_count}")

        if run_count > 0:
            runs = await session.execute(
                select(AgentRun).where(AgentRun.task_id == task_id)
            )
            runs_list = runs.scalars().all()
            for r in runs_list[:5]:
                print(f"  Run: {r.id}, mode={r.evaluation_mode}, question_id={r.question_id}")

        from database import ScenarioStepResult
        ssr_result = await session.execute(
            select(func.count(ScenarioStepResult.id))
            .join(AgentRun, ScenarioStepResult.agent_run_id == AgentRun.id)
            .where(AgentRun.task_id == task_id)
        )
        ssr_count = ssr_result.scalar()
        print(f"ScenarioStepResult 数量: {ssr_count}")

        print("=" * 60)

        cap_list = []
        from services.evaluation_mode import get_task_capabilities
        cap_list = get_task_capabilities(task)
        print(f"get_task_capabilities: {cap_list}")

        if "scenario" in cap_list:
            print("✅ capabilities 包含 'scenario'，前端应显示场景子步骤 Tab")
        else:
            print("❌ capabilities 不包含 'scenario'，前端会灰化场景子步骤 Tab")

        print("=" * 60)
        pe_completed = getattr(task, 'process_eval_completed', False)
        if pe_completed:
            print("✅ process_eval_completed = True")
        else:
            print("❌ process_eval_completed = False - 这就是问题根源！")
            lfs = getattr(task, 'langfuse_source', None)
            print(f"   langfuse_source: {lfs}")
            if not lfs:
                print("   → 原因：创建任务时未配置 langfuse_source，过程评估从未执行")

asyncio.run(main())
