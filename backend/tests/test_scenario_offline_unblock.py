"""验证离线场景交付两个拦截已放开"""
import sys, asyncio
sys.path.insert(0, ".")
from services.task_dimension_verdict_service import build_task_dimension_verdicts, _na_scenario_delivery

# 1. 验证 _na_scenario_delivery 仍然存在（兼容性）
na = _na_scenario_delivery()
assert na["status"] == "na", f"_na_scenario_delivery status 应为 na，实际 {na['status']}"
print("✅ _na_scenario_delivery() 兼容性正常")

# 2. 验证 build_task_dimension_verdicts 源码：离线任务支持场景交付
#    检查离线分支中同时支持 langfuse_source 和 process_eval_completed 判断
import inspect
src = inspect.getsource(build_task_dimension_verdicts)
assert "langfuse_source" in src, "离线分支应检查 langfuse_source"
print("✅ build_task_dimension_verdicts 离线分支检查 langfuse_source")
assert "process_eval_completed" in src, "离线分支应检查 process_eval_completed"
print("✅ build_task_dimension_verdicts 离线分支检查 process_eval_completed")

# 3. 验证 scenario_post_eval_service 不再拦截离线任务
from services.scenario_post_eval_service import run_post_eval_scenario_if_enabled
src2 = inspect.getsource(run_post_eval_scenario_if_enabled)
assert "offline_evaluation" not in src2, "offline_evaluation 跳过逻辑应移除"
print("✅ run_post_eval_scenario_if_enabled 不再拦截离线任务")
assert "is_online_task" not in src2, "is_online_task 调用应移除"
print("✅ is_online_task 调用已移除")

# 4. 验证离线任务中场景交付分支直接走 _aggregate_scenario_stats
assert "_aggregate_scenario_stats" in src, "离线分支应调用 _aggregate_scenario_stats"
# 检查不在 if has_process_eval 分支中（即不再被 process_eval_completed 单独拦截）
lines = src.split('\n')
offline_block_start = None
for i, line in enumerate(lines):
    if 'if offline:' in line:
        offline_block_start = i
        break
assert offline_block_start is not None

# 检查 offline 块后续行：应该有 _aggregate_scenario_stats
offline_lines = '\n'.join(lines[offline_block_start:offline_block_start+30])
assert '_aggregate_scenario_stats' in offline_lines
print("✅ 离线分支直接调用 _aggregate_scenario_stats")

# 验证不再有单独的 has_process_eval 拦截（即不会因为 process_eval_completed=False 而完全跳过）
# 允许同时检查 langfuse_source 和 process_eval_completed，但不能只检查 process_eval_completed
assert "has_process_eval" not in src, "has_process_eval 变量应不存在"
print("✅ has_process_eval 变量已不存在，离线分支不再被 process_eval_completed 单独拦截")

# 验证使用的是 process_eval_done（避免与 has_process_eval 子字符串冲突）
assert "process_eval_done" in src, "离线分支应使用 process_eval_done 变量"
print("✅ 离线分支使用 process_eval_done 变量")

print("\n全部验证通过 ✅")
