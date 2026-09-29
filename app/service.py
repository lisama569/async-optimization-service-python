"""任务领域服务：请求校验与服务级错误定义。"""

from __future__ import annotations

from .models import Problem, TaskView
from .solver import is_finite_number


class TaskServiceError(Exception):
    """接口层业务错误，携带稳定错误码与 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TaskNotFoundError(TaskServiceError):
    def __init__(self, message: str = "任务不存在") -> None:
        super().__init__("task_not_found", message, 404)


class IdempotencyConflictError(TaskServiceError):
    def __init__(self, existing: TaskView) -> None:
        super().__init__(
            "idempotency_conflict",
            f"幂等键已绑定任务 {existing.task_id}，但本次请求体与其不一致",
            409,
        )
        self.existing = existing


class AlreadyTerminalError(TaskServiceError):
    """对已结束任务执行不允许的操作；code 区分具体终态。"""

    def __init__(self, code: str, message: str, task: TaskView) -> None:
        super().__init__(code, message, 409)
        self.task = task


class ResultNotReadyError(TaskServiceError):
    def __init__(self, task: TaskView) -> None:
        super().__init__(
            "result_not_ready",
            f"任务当前状态为 {task.status.value}，结果尚未产生",
            409,
        )
        self.task = task


def validate_problem(problem: Problem) -> None:
    """校验求解输入；非法输入抛 InvalidInputError（来自 errors 模块）。

    注意：结构性问题（字段缺失、类型错误）由 Pydantic 在进入本函数前拦截，
    这里负责语义校验。
    """
    from .errors import InvalidInputError  # 局部导入避免循环依赖

    if not problem.items:
        raise InvalidInputError("items 不能为空：至少需要一个物品")
    if not problem.resources:
        raise InvalidInputError("resources 不能为空：至少需要一个资源")

    item_ids: set[str] = set()
    for it in problem.items:
        if not it.id or not it.id.strip():
            raise InvalidInputError("存在 id 为空的物品")
        if it.id in item_ids:
            raise InvalidInputError(f"物品 id 重复: {it.id}")
        item_ids.add(it.id)
        if not is_finite_number(it.demand) or it.demand <= 0:
            raise InvalidInputError(f"物品 {it.id} 的 demand 必须为正数")
        if not is_finite_number(it.value) or it.value <= 0:
            raise InvalidInputError(f"物品 {it.id} 的 value 必须为正数")

    res_ids: set[str] = set()
    for r in problem.resources:
        if not r.id or not r.id.strip():
            raise InvalidInputError("存在 id 为空的资源")
        if r.id in res_ids:
            raise InvalidInputError(f"资源 id 重复: {r.id}")
        res_ids.add(r.id)
        if not is_finite_number(r.capacity) or r.capacity <= 0:
            raise InvalidInputError(f"资源 {r.id} 的 capacity 必须为正数")

    if not is_finite_number(problem.seed):
        raise InvalidInputError("seed 必须为有限整数")
    if problem.step_delay_ms < 0:
        raise InvalidInputError("step_delay_ms 不能为负数")
