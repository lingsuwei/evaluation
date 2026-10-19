"""验证离线场景交付区分：未配置 Langfuse vs 已配置但无数据"""
import sys, asyncio
sys.path.insert(0, ".")

from services.task_dimension_verdict_service import build_task_dimension_verdicts

async def main():
    from database import async_session
    from sqlalchemy import select
    from database import EvaluationTask

    # 查找最近的离线任务
    async with async_session() as session:
        result = await session.execute(
            select(EvaluationTask)
            .where(EvaluationTask.evaluation_mode == 'offline')
            .order_by(EvaluationTask.created_at.desc())
            .limit(3)
        )
        tasks = result.scalars().all()

        print(f"找到 {len(tasks)} 个离线任务\n")

        for task in tasks:
            print(f"任务ID: {task.id}")
            print(f"  状态: {task.status}")
            print(f"  有Langfuse配置: {bool(task.langfuse_source)}")

            verdicts = await build_task_dimension_verdicts(session, task.id, task=task)
            if verdicts:
                scenario = verdicts.get("scenario_delivery", {})
                print(f"  场景交付状态: {scenario.get('status')}")
                print(f"  场景交付标题: {scenario.get('headline')}")
                reason = scenario.get('metrics', {}).get('reason')
                if reason:
                    print(f"  原因: {reason}")
            print()

if __name__ == "__main__":
    asyncio.run(main())
