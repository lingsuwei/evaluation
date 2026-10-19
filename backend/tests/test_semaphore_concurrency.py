"""
测试 _LLM_CALL_SEMAPHORE(8) 是否正确限制并发 LLM 调用数。

预期行为：
  - 10 个并发任务，峰值并发数 = 8（而非 10）
  - 前 8 个任务立即获得信号量，后 2 个等待
  - 总耗时 ≈ 2s（8 个 1s + 2 个 1s 分两批）

运行方式：
  cd backend
  python -m tests.test_semaphore_concurrency
"""

import asyncio
import time


# 尝试导入 llm_client 中真实的信号量；导入失败则创建等效的
try:
    from evaluators.llm_client import _LLM_CALL_SEMAPHORE
    _SOURCE = "llm_client._LLM_CALL_SEMAPHORE（真实模块）"
except Exception as e:
    _LLM_CALL_SEMAPHORE = asyncio.Semaphore(8)
    _SOURCE = f"本地 fallback（导入失败: {e}）"


class ConcurrencyTracker:
    """跟踪当前/峰值并发数，记录每个任务的时间线。"""

    def __init__(self):
        self.current = 0
        self.peak = 0
        self.timeline = []  # [(timestamp, event, call_id, current)]

    def record(self, event: str, call_id: int):
        self.timeline.append((time.monotonic(), event, call_id, self.current))

    def acquire(self, call_id: int):
        self.current += 1
        if self.current > self.peak:
            self.peak = self.current
        self.record("ACQUIRE", call_id)

    def release(self, call_id: int):
        self.current -= 1
        self.record("RELEASE", call_id)


async def simulate_llm_call(call_id: int, tracker: ConcurrencyTracker, work_duration: float = 1.0):
    """模拟一次 LLM 调用：获取信号量 → 等待（模拟 API 响应）→ 释放。"""
    t0 = time.monotonic()
    tracker.record("REQUEST", call_id)  # 发起请求（可能在信号量前排队）

    async with _LLM_CALL_SEMAPHORE:
        tracker.acquire(call_id)
        elapsed_wait = time.monotonic() - t0
        print(f"  [Task {call_id:2d}] 获取信号量（等待 {elapsed_wait:.3f}s）"
              f"  当前并发={tracker.current}")

        await asyncio.sleep(work_duration)  # 模拟 API 响应耗时

        tracker.release(call_id)
        print(f"  [Task {call_id:2d}] 释放信号量  当前并发={tracker.current}")


async def main():
    NUM_TASKS = 10
    WORK_DURATION = 1.0  # 每个任务模拟 1 秒的 API 响应

    print("=" * 60)
    print(f"信号量来源: {_SOURCE}")
    print(f"任务数: {NUM_TASKS}  每任务耗时: {WORK_DURATION}s  信号量上限: 8")
    print("=" * 60)

    tracker = ConcurrencyTracker()
    t_start = time.monotonic()

    tasks = [
        simulate_llm_call(i, tracker, WORK_DURATION)
        for i in range(NUM_TASKS)
    ]
    await asyncio.gather(*tasks)

    t_total = time.monotonic() - t_start

    print("=" * 60)
    print("结果汇总")
    print("=" * 60)
    print(f"  峰值并发数: {tracker.peak}  （预期: 8）")
    print(f"  总耗时:     {t_total:.3f}s  （预期: ~{2 * WORK_DURATION}s）")

    if tracker.peak == 8:
        print("  [PASS] 信号量正确限制并发为 8")
    elif tracker.peak < 8:
        print(f"  [WARN] 峰值并发 {tracker.peak} < 8，可能信号量配置有误")
    else:
        print(f"  [FAIL] 峰值并发 {tracker.peak} > 8，信号量未生效！")

    # 打印时间线
    print()
    print("时间线（前 20 条）:")
    print(f"  {'时间':>8}  {'事件':>8}  {'TaskID':>6}  {'并发数':>4}")
    for ts, event, call_id, concurrent in tracker.timeline[:20]:
        print(f"  {ts - t_start:8.3f}  {event:>8}  {call_id:>6}  {concurrent:>4}")


if __name__ == "__main__":
    asyncio.run(main())
