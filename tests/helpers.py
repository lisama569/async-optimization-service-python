"""测试用算例构造与轮询工具。"""

from __future__ import annotations

import time

from app.models import ItemInput, Problem, ResourceInput


def feasible_problem(seed: int = 1, n: int = 5, step_delay_ms: int = 0) -> Problem:
    """保证可行：每件需求 1，资源容量充足。"""
    return Problem(
        items=[ItemInput(id=f"i{i}", demand=1.0, value=float(i + 1)) for i in range(n)],
        resources=[
            ResourceInput(id="r0", capacity=float(n)),
            ResourceInput(id="r1", capacity=float(n)),
        ],
        seed=seed,
        step_delay_ms=step_delay_ms,
    )


def slow_problem(seed: int = 1, n: int = 20, step_delay_ms: int = 20) -> Problem:
    return feasible_problem(seed=seed, n=n, step_delay_ms=step_delay_ms)


def infeasible_total_problem() -> Problem:
    """总需求 > 总容量（单件均放得下）。"""
    return Problem(
        items=[
            ItemInput(id="a", demand=4.0, value=1.0),
            ItemInput(id="b", demand=4.0, value=1.0),
        ],
        resources=[ResourceInput(id="r", capacity=5.0)],
    )


def oversized_problem() -> Problem:
    """单件需求超过最大资源容量。"""
    return Problem(
        items=[ItemInput(id="a", demand=99.0, value=1.0)],
        resources=[ResourceInput(id="r", capacity=1.0)],
    )


def no_solution_problem() -> Problem:
    """总容量恰好等于总需求，但任何资源都无法装箱全部（贪心失败）。"""
    return Problem(
        items=[ItemInput(id=f"i{i}", demand=4.0, value=1.0) for i in range(3)],
        resources=[
            ResourceInput(id="r0", capacity=6.0),
            ResourceInput(id="r1", capacity=6.0),
        ],
    )


def poll(client, task_id: str, timeout: float = 10.0, interval: float = 0.01) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/tasks/{task_id}").json()
        if last["status"] in ("succeeded", "failed", "cancelled"):
            return last
        time.sleep(interval)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束: {last}")
