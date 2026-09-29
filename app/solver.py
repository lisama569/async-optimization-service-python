"""本地假数据生成与确定性贪心求解。

问题模型（广义分配 / 装箱）：
- 每个物品有需求 demand 与价值 value，必须整体放入某个资源；
- 每个资源有容量 capacity，放入物品的需求之和不得超过容量；
- 目标：把全部物品放入，最大化已放置物品的总价值（成功时即全部物品价值之和）。

失败语义区分：
- InvalidInputError：输入结构非法（空列表、重复 id、负数、NaN 等）；
- InfeasibleError：可证明不可行（总需求 > 总容量，或单个物品超过所有资源容量）；
- NoSolutionError：理论上可能可行，但本地贪心启发式没能找到可行放置；
- SolveCancelled：求解过程中被取消。

确定性：物品按价值密度降序排列（id 作为并列次序），资源在候选集合中按
剩余容量加权、由 random.Random(seed) 决定选择；相同输入 + 相同 seed
必然得到相同的分配与目标值。
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from .errors import InfeasibleError, NoSolutionError
from .models import Allocation, ItemInput, Problem, ResourceInput, SolveResult

# 取消检查回调：返回 True 表示任务已被请求取消
CancelChecker = Callable[[], bool]
# 进度回调：参数为 0~1 的进度
ProgressReporter = Callable[[float], None]

_EPS = 1e-9


class SolveCancelled(Exception):
    """求解过程中收到取消信号。"""


def generate_problem(
    seed: int = 0,
    n_items: int = 8,
    n_resources: int = 3,
    step_delay_ms: int = 0,
) -> Problem:
    """用本地随机数生成确定性的假算例（同 seed 同数据）。"""
    rng = random.Random(seed)
    items = [
        ItemInput(
            id=f"item-{i}",
            demand=round(rng.uniform(1.0, 10.0), 4),
            value=round(rng.uniform(1.0, 20.0), 4),
        )
        for i in range(n_items)
    ]
    resources = [
        ResourceInput(
            id=f"res-{j}",
            capacity=round(rng.uniform(8.0, 20.0), 4),
        )
        for j in range(n_resources)
    ]
    return Problem(
        items=items,
        resources=resources,
        seed=seed,
        step_delay_ms=step_delay_ms,
    )


def solve(
    problem: Problem,
    is_cancelled: CancelChecker,
    report_progress: ProgressReporter,
) -> SolveResult:
    """执行确定性贪心求解。"""
    items = problem.items
    resources = problem.resources
    n = len(items)

    total_demand = sum(it.demand for it in items)
    capacities = {r.id: r.capacity for r in resources}
    total_capacity = sum(capacities.values())
    max_capacity = max(capacities.values())

    # —— 可证明不可行的判定 ——
    oversized = [it.id for it in items if it.demand > max_capacity + _EPS]
    if oversized:
        raise InfeasibleError(
            f"物品 {oversized} 的需求超过最大资源容量 {max_capacity}"
        )
    if total_demand > total_capacity + _EPS:
        raise InfeasibleError(
            f"总需求 {total_demand:.4f} 超过总容量 {total_capacity:.4f}"
        )

    # —— 确定性贪心：按价值密度降序放置 ——
    order = sorted(items, key=lambda it: (-it.value / it.demand, it.id))
    remaining = dict(capacities)
    used = {rid: 0.0 for rid in capacities}
    allocations: list[Allocation] = []
    placed_ids: set[str] = set()
    rng = random.Random(problem.seed)
    delay = max(0, problem.step_delay_ms) / 1000.0

    for idx, item in enumerate(order):
        if is_cancelled():
            raise SolveCancelled(f"任务在第 {idx + 1}/{n} 步被取消")
        if delay:
            time.sleep(delay)

        candidates = sorted(
            (rid for rid, cap in remaining.items() if cap + _EPS >= item.demand),
            key=lambda rid: (-remaining[rid], rid),
        )
        if candidates:
            # seed 决定在多个可放资源之间的加权选择，保证可复现
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                weights = [max(remaining[rid], _EPS) for rid in candidates]
                chosen = rng.choices(candidates, weights=weights, k=1)[0]
            remaining[chosen] -= item.demand
            used[chosen] += item.demand
            allocations.append(
                Allocation(
                    item_id=item.id,
                    resource_id=chosen,
                    amount=item.demand,
                    value=item.value,
                )
            )
            placed_ids.add(item.id)

        report_progress((idx + 1) / n)

    unplaced = [it.id for it in order if it.id not in placed_ids]
    if unplaced:
        raise NoSolutionError(
            f"贪心启发式未能放置 {len(unplaced)} 个物品: {unplaced}；"
            "总容量虽充足，但不存在满足单资源容量的贪心放置"
        )

    objective = round(sum(a.value for a in allocations), 9)
    return SolveResult(
        allocations=sorted(allocations, key=lambda a: a.item_id),
        objective=objective,
        used_capacity={rid: round(v, 9) for rid, v in used.items()},
        total_capacity=capacities,
        iterations=n,
        seed=problem.seed,
    )


def is_finite_number(x: float) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
