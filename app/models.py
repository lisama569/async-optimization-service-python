"""数据模型与任务状态机定义。

任务状态单向流转：

    QUEUED -> RUNNING -> SUCCEEDED
                       \\-> FAILED
    QUEUED             \\-> CANCELLED
    RUNNING            \\-> CANCELLED
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class FailureCode(str, Enum):
    """失败原因分类，便于调用方区分处理。"""

    INVALID_INPUT = "invalid_input"        # 输入非法（结构/字段不合法）
    INFEASIBLE = "infeasible"              # 可判定为不可行
    UNSOLVABLE = "unsolvable"              # 无可行解（求解器无法构造解）
    CANCELLED = "cancelled"                # 被用户取消
    INTERRUPTED = "interrupted"            # 进程重启导致中断


class ItemSpec(BaseModel):
    item_id: str
    demand: float = Field(gt=0)
    value: float = Field(ge=0)
    mandatory: bool = False


class ResourceSpec(BaseModel):
    resource_id: str
    capacity: float = Field(gt=0)


class InstanceSpec(BaseModel):
    """显式算例数据。留空时由本地假数据生成器生成。"""

    items: list[ItemSpec] = Field(default_factory=list)
    resources: list[ResourceSpec] = Field(default_factory=list)


class GenerateSpec(BaseModel):
    """本地假数据生成参数。"""

    num_items: int = Field(default=10, ge=0, le=10_000)
    num_resources: int = Field(default=2, ge=0, le=1000)
    min_demand: float = Field(default=1.0, gt=0)
    max_demand: float = Field(default=5.0, gt=0)
    min_value: float = Field(default=1.0, ge=0)
    max_value: float = Field(default=10.0, ge=0)
    mandatory_ratio: float = Field(default=0.2, ge=0.0, le=1.0)


class SubmitRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=200)
    instance: InstanceSpec | None = None
    generate: GenerateSpec | None = None
    # 求解随机种子：相同输入 + 相同种子 => 相同结果
    seed: int = Field(default=0, ge=0)
    # 求解器尝试轮数上限（可用于构造 unsolvable 场景）
    max_attempts: int = Field(default=200, ge=1, le=100_000)


class Assignment(BaseModel):
    item_id: str
    resource_id: str


class SolveResult(BaseModel):
    objective_value: float
    assignments: list[Assignment]
    total_demand: float
    unassigned_items: list[str]
    resources_used: list[str]


class TaskRecord(BaseModel):
    """内部持久化的任务记录。"""

    task_id: str
    idempotency_key: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    request: SubmitRequest
    result: SolveResult | None = None
    failure_code: FailureCode | None = None
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    attempt: int = 0


class TaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskResultResponse(BaseModel):
    task_id: str
    status: TaskStatus
    result: SolveResult | None = None
    failure_code: FailureCode | None = None
    failure_reason: str | None = None


class SubmitResponse(BaseModel):
    task_id: str
    status: TaskStatus
    idempotent_replayed: bool = False
