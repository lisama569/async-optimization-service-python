"""FastAPI 入口：沿用 `python main.py` / `uvicorn main:app` 的启动方式。

环境变量（均有默认值，本地直接跑即可）：
- HOST / PORT：监听地址，默认 127.0.0.1:8000
- OPT_MAX_CONCURRENCY：同时运行的求解任务上限，默认 2
- OPT_STATE_FILE：本地状态文件路径，默认 ./data/tasks.json
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import InvalidInputError
from app.models import (
    HealthResponse,
    Problem,
    SubmitRequest,
    SubmitResponse,
    TaskView,
    CancelResponse,
)
from app.service import (
    AlreadyTerminalError,
    IdempotencyConflictError,
    ResultNotReadyError,
    TaskNotFoundError,
    TaskServiceError,
    validate_problem,
)
from app.solver import generate_problem
from app.store import TaskStore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("optimization.api")


def _build_store() -> TaskStore:
    return TaskStore(
        max_concurrency=int(os.environ.get("OPT_MAX_CONCURRENCY", "2")),
        state_path=os.environ.get("OPT_STATE_FILE", "data/tasks.json"),
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = _build_store()
    store.start()
    app.state.store = store
    try:
        yield
    finally:
        store.stop()


app = FastAPI(title="async-optimization-service", version="0.1.0", lifespan=lifespan)


def get_store(request: Request) -> TaskStore:
    return request.app.state.store


def _error(code: str, message: str, status_code: int, **extra: object) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(TaskServiceError)
async def handle_service_error(_: Request, exc: TaskServiceError) -> JSONResponse:
    extra: dict = {}
    task = getattr(exc, "task", None)
    if task is not None:
        extra["task"] = task.model_dump(mode="json")
    existing = getattr(exc, "existing", None)
    if existing is not None:
        extra["existing_task_id"] = existing.task_id
    logger.info("请求被拒绝 code=%s message=%s", exc.code, exc.message)
    return _error(exc.code, exc.message, exc.status_code, **extra)


@app.exception_handler(InvalidInputError)
async def handle_invalid_input(_: Request, exc: InvalidInputError) -> JSONResponse:
    logger.info("输入非法 code=invalid_input message=%s", exc.message)
    return _error("invalid_input", exc.message, 400)


@app.get("/")
def read_root() -> dict:
    return {"service": "async-optimization-service", "docs": "/docs"}


@app.get("/health", response_model=HealthResponse)
def health(store: TaskStore = Depends(get_store)) -> HealthResponse:
    stats = store.stats()
    return HealthResponse(status="ok", **stats)


@app.post("/api/demo/problem", response_model=Problem)
def demo_problem(
    seed: int = 0,
    n_items: int = 8,
    n_resources: int = 3,
    step_delay_ms: int = 0,
) -> Problem:
    """生成一份本地假算例，便于手工构造提交体。"""
    return generate_problem(
        seed=seed,
        n_items=n_items,
        n_resources=n_resources,
        step_delay_ms=step_delay_ms,
    )


@app.post("/api/tasks", response_model=SubmitResponse, status_code=201)
def submit_task(
    body: SubmitRequest,
    store: TaskStore = Depends(get_store),
) -> SubmitResponse:
    problem = body.problem
    # 语义校验：Pydantic 负责结构（422），这里区分非法输入（400 invalid_input）
    validate_problem(problem)

    # 幂等负载指纹：规范化 JSON 后取 sha256
    canonical = json.dumps(
        problem.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    view, reused = store.submit(
        problem=problem,
        payload_hash=payload_hash,
        idempotency_key=body.idempotency_key,
    )
    return SubmitResponse(
        task_id=view.task_id,
        status=view.status,
        idempotency_key=view.idempotency_key,
        reused=reused,
    )


@app.get("/api/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, store: TaskStore = Depends(get_store)) -> TaskView:
    view = store.get(task_id)
    if view is None:
        raise TaskNotFoundError()
    return view


@app.get("/api/tasks/{task_id}/result")
def get_result(
    task_id: str, store: TaskStore = Depends(get_store)
) -> dict:
    view = store.get(task_id)
    if view is None:
        raise TaskNotFoundError()
    # 对已结束任务重复取结果时，原因可区分：成功给结果；失败/取消明确拒绝
    if view.status.value == "succeeded":
        return {
            "task_id": view.task_id,
            "status": view.status,
            "result": view.result,
        }
    if view.status.value == "failed":
        raise AlreadyTerminalError(
            "result_unavailable_failed",
            f"任务 {task_id} 已失败（{view.failure_reason.value if view.failure_reason else 'unknown'}），"
            f"无结果：{view.message or ''}",
            view,
        )
    if view.status.value == "cancelled":
        raise AlreadyTerminalError(
            "result_unavailable_cancelled",
            f"任务 {task_id} 已被取消，无结果",
            view,
        )
    raise ResultNotReadyError(view)


@app.post("/api/tasks/{task_id}/cancel", response_model=CancelResponse)
def cancel_task(
    task_id: str, store: TaskStore = Depends(get_store)
) -> CancelResponse:
    view = store.cancel(task_id)
    message = None
    if view.status.value == "cancelled":
        message = "任务已取消"
    else:
        message = "取消信号已送达，任务将在最近检查点停止"
    return CancelResponse(task_id=view.task_id, status=view.status, message=message)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
