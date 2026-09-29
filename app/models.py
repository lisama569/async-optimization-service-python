"""Pydantic 数据模型与状态/错误码定义（骨架）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailureReason(str, Enum):
    INVALID_INPUT = "invalid_input"
    INFEASIBLE = "infeasible"
    NO_SOLUTION = "no_solution"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class ItemInput(BaseModel):
    id: str
    demand: float
    value: float


class ResourceInput(BaseModel):
    id: str
    capacity: float


class Problem(BaseModel):
    items: list[ItemInput]
    resources: list[ResourceInput]
    seed: int = 0
    step_delay_ms: int = 0  # 求解每步耗时（假数据用），仅用于本地演示与测试


class SubmitRequest(BaseModel):
    problem: Problem
    idempotency_key: str | None = None


class Allocation(BaseModel):
    item_id: str
    resource_id: str
    amount: float
    value: float


class SolveResult(BaseModel):
    allocations: list[Allocation]
    objective: float
    used_capacity: dict[str, float]
    total_capacity: dict[str, float]
    iterations: int
    seed: int


class TaskView(BaseModel):
    task_id: str
    idempotency_key: str | None = None
    status: TaskStatus
    progress: float = 0.0
    result: SolveResult | None = None
    failure_reason: FailureReason | None = None
    message: str | None = None
    created_at: str = ""
    updated_at: str = ""


class SubmitResponse(BaseModel):
    task_id: str
    status: TaskStatus
    idempotency_key: str | None = None
    reused: bool = False


class CancelResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str | None = None


class HealthResponse(BaseModel):
    status: str
    running: int = 0
    queued: int = 0
    max_concurrency: int = 1
