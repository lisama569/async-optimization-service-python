"""HTTP API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from .manager import TaskConflictError
from .models import (
    SubmitRequest,
    SubmitResponse,
    TaskResultResponse,
    TaskStatusResponse,
)
from .solver import InvalidInputError

router = APIRouter(prefix="/api")


def _manager(request: Request):
    return request.app.state.manager


@router.post("/tasks", response_model=SubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_task(payload: SubmitRequest, request: Request) -> SubmitResponse:
    """提交优化算例。

    - 202：任务已受理（新任务或幂等重放都返回同一 task_id）；
    - 400：请求语义非法（instance/generate 同给、ID 重复等）；
    - 409：同一幂等键已绑定内容不同的请求。
    """
    manager = _manager(request)
    try:
        record, replayed = await manager.submit(payload)
    except InvalidInputError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except TaskConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return SubmitResponse(
        task_id=record.task_id,
        status=record.status,
        idempotent_replayed=replayed,
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str, request: Request) -> TaskStatusResponse:
    """查询任务状态与关键时间戳。"""
    record = _manager(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return TaskStatusResponse(
        task_id=record.task_id,
        status=record.status,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


@router.get("/tasks/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(task_id: str, request: Request) -> TaskResultResponse:
    """获取任务结果。

    - 200 + result          ：成功；
    - 200 + failure_code    ：失败/取消（不同 failure_code 可区分原因）；
    - 404 not_found         ：任务不存在；
    - 409 not_finished      ：仍在排队/运行，尚无结果；
    - 409 result_discarded  ：已取消，取消后不会产生结果。
    """
    record = _manager(request).get(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if record.status.value in ("queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "not_finished",
                "status": record.status.value,
                "message": "任务尚未结束，请稍后再查询",
            },
        )
    if record.status.value == "cancelled" and record.result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "result_discarded",
                "status": record.status.value,
                "message": record.failure_reason or "任务已取消，结果不再产生",
            },
        )
    return TaskResultResponse(
        task_id=record.task_id,
        status=record.status,
        result=record.result,
        failure_code=record.failure_code,
        failure_reason=record.failure_reason,
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict:
    """取消排队中或运行中的任务。

    - 200 cancelled          ：本次取消成功；
    - 200 already_cancelled  ：此前已取消（重复取消）；
    - 404 not_found          ：任务不存在；
    - 409 already_succeeded / already_failed：任务已结束，无法取消。
    """
    record, why = await _manager(request).cancel(task_id)
    if why == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if why in ("already_succeeded", "already_failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": why,
                "status": record.status.value,
                "message": f"任务已结束（{record.status.value}），不能取消",
            },
        )
    return {
        "task_id": task_id,
        "status": record.status.value,
        "reason": why,
    }
