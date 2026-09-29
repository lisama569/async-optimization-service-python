"""本地假数据生成与确定性资源分配求解器。

问题模型（多背包式资源分配）：
- 每个物品有需求 demand、价值 value，必选物品 mandatory 必须全部分配；
- 每个资源有容量 capacity，单资源装载总需求不得超过容量；
- 非必选物品可丢弃，优化目标：被分配物品的价值之和最大。

三类负向结果严格区分：
- invalid_input：请求语义非法（重复 ID、instance/generate 同时给出等）；
- infeasible ：由必要条件即可判定不可行（必选物品装不下任何资源，
               或必选总需求超过总容量）；
- unsolvable ：必要条件通过但求解器在限定尝试次数内无法构造可行装载。

确定性：实例生成流与求解尝试流分别由请求 seed 派生独立随机序列，
相同输入 + 相同 seed 必然得到相同解与目标值。
"""

from __future__ import annotations

import random
from collections.abc import Callable

from .models import (
    Assignment,
    GenerateSpec,
    InstanceSpec,
    ItemSpec,
    ResourceSpec,
    SolveResult,
    SubmitRequest,
)

EPS = 1e-9


class SolverError(Exception):
    """求解失败基类。"""


class InvalidInputError(SolverError):
    """输入语义非法。"""


class InfeasibleError(SolverError):
    """算例可由必要条件判定为不可行。"""


class UnsolvableError(SolverError):
    """求解器在限定尝试次数内无法构造可行解。"""


class CancellationRequested(Exception):
    """求解过程中收到取消信号。"""


def validate_request(req: SubmitRequest) -> None:
    """校验请求语义；非法时抛出 InvalidInputError。"""
    if req.instance is not None and req.generate is not None:
        raise InvalidInputError("instance 与 generate 不能同时提供")
    if req.instance is not None:
        item_ids = [it.item_id for it in req.instance.items]
        resource_ids = [r.resource_id for r in req.instance.resources]
        if len(set(item_ids)) != len(item_ids):
            raise InvalidInputError("instance.items 中存在重复的 item_id")
        if len(set(resource_ids)) != len(resource_ids):
            raise InvalidInputError("instance.resources 中存在重复的 resource_id")


def _generate_instance(spec: GenerateSpec, seed: int) -> InstanceSpec:
    """用独立随机流确定性地生成假算例。"""
    rng = random.Random((seed ^ 0x9E3779B9) & 0xFFFFFFFF)
    lo_d, hi_d = sorted((spec.min_demand, spec.max_demand))
    lo_v, hi_v = sorted((spec.min_value, spec.max_value))
    items: list[ItemSpec] = []
    for i in range(spec.num_items):
        demand = round(rng.uniform(lo_d, hi_d), 6)
        value = round(rng.uniform(lo_v, hi_v), 6)
        mandatory = rng.random() < spec.mandatory_ratio
        items.append(
            ItemSpec(
                item_id=f"item_{i}",
                demand=demand,
                value=value,
                mandatory=mandatory,
            )
        )
    resources: list[ResourceSpec] = []
    for j in range(spec.num_resources):
        # 容量略大于平均物品需求，保证假数据通常可解
        avg_demand = (lo_d + hi_d) / 2
        capacity = round(
            rng.uniform(2 * avg_demand, 6 * avg_demand) if spec.num_items else 1.0,
            6,
        )
        resources.append(ResourceSpec(resource_id=f"resource_{j}", capacity=capacity))
    return InstanceSpec(items=items, resources=resources)


def build_instance(req: SubmitRequest) -> InstanceSpec:
    """根据请求得到算例（显式数据或本地假数据）。"""
    validate_request(req)
    if req.instance is not None:
        return req.instance.model_copy(deep=True)
    return _generate_instance(req.generate or GenerateSpec(), req.seed)


def _best_fit_pack(
    order: list[ItemSpec],
    capacities: dict[str, float],
) -> dict[str, str] | None:
    """按给定顺序对物品做 best-fit 装载；全部装下返回 {item_id: resource_id}。"""
    remaining = dict(capacities)
    placement: dict[str, str] = {}
    for item in order:
        candidates = [
            rid for rid, cap in remaining.items() if cap + EPS >= item.demand
        ]
        if not candidates:
            return None
        rid = min(candidates, key=lambda r: (remaining[r], r))
        remaining[rid] -= item.demand
        placement[item.item_id] = rid
    return placement


def solve(
    req: SubmitRequest,
    is_cancelled: Callable[[], bool] | None = None,
) -> SolveResult:
    """执行确定性求解。

    先多轮尝试把全部必选物品装入（首轮降序，后续轮次由种子派生的随机排列），
    成功后再按价值贪心装入非必选物品。
    """
    validate_request(req)
    instance = build_instance(req)

    items = list(instance.items)
    resources = list(instance.resources)
    capacities = {r.resource_id: r.capacity for r in resources}
    items_by_id = {it.item_id: it for it in items}

    mandatory = [it for it in items if it.mandatory]
    optional = [it for it in items if not it.mandatory]

    # ---- 可行性必要条件判定（infeasible） ----
    if mandatory:
        max_capacity = max(capacities.values(), default=0.0)
        too_big = [it.item_id for it in mandatory if it.demand > max_capacity + EPS]
        if too_big:
            raise InfeasibleError(
                f"必选物品 {sorted(too_big)} 的需求超过任意单一资源容量"
            )
        total_mandatory_demand = sum(it.demand for it in mandatory)
        total_capacity = sum(capacities.values())
        if total_mandatory_demand > total_capacity + EPS:
            raise InfeasibleError(
                f"必选物品总需求 {total_mandatory_demand:.3f} 超过总容量 "
                f"{total_capacity:.3f}"
            )

    # ---- 多轮尝试装载必选物品 ----
    rng = random.Random((req.seed ^ 0x5DEECE66D) & 0xFFFFFFFF)
    mandatory_placement: dict[str, str] | None = None
    orders_tried: list[tuple[str, ...]] = []

    for attempt in range(req.max_attempts):
        if is_cancelled is not None and is_cancelled():
            raise CancellationRequested("求解过程中任务被取消")
        if attempt == 0:
            order = sorted(mandatory, key=lambda it: (-it.demand, it.item_id))
        else:
            order = mandatory[:]
            rng.shuffle(order)
        order_key = tuple(it.item_id for it in order)
        if order_key in orders_tried:
            continue
        orders_tried.append(order_key)
        placement = _best_fit_pack(order, capacities)
        if placement is not None:
            mandatory_placement = placement
            break

    if mandatory_placement is None:
        raise UnsolvableError(
            f"必选物品通过了必要条件检查，但在 {req.max_attempts} 轮尝试内"
            "仍无法构造可行装载"
        )

    # ---- 非必选物品按价值贪心补装 ----
    remaining = dict(capacities)
    for item_id, rid in mandatory_placement.items():
        remaining[rid] -= items_by_id[item_id].demand

    optional_order = sorted(
        optional,
        key=lambda it: (-it.value, -(it.value / it.demand), it.item_id),
    )
    assignments_map: dict[str, str] = dict(mandatory_placement)
    for item in optional_order:
        if is_cancelled is not None and is_cancelled():
            raise CancellationRequested("求解过程中任务被取消")
        candidates = [
            rid for rid, cap in remaining.items() if cap + EPS >= item.demand
        ]
        if candidates:
            rid = min(candidates, key=lambda r: (remaining[r], r))
            remaining[rid] -= item.demand
            assignments_map[item.item_id] = rid

    assignments = [
        Assignment(item_id=item_id, resource_id=rid)
        for item_id, rid in sorted(assignments_map.items())
    ]
    assigned_ids = set(assignments_map)
    objective_value = round(
        sum(items_by_id[i].value for i in assigned_ids), 6
    )
    total_demand = round(
        sum(items_by_id[i].demand for i in assigned_ids), 6
    )
    unassigned = sorted(it.item_id for it in items if it.item_id not in assigned_ids)
    resources_used = sorted({a.resource_id for a in assignments})

    return SolveResult(
        objective_value=objective_value,
        assignments=assignments,
        total_demand=total_demand,
        unassigned_items=unassigned,
        resources_used=resources_used,
    )
