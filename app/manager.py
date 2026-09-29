"""任务生命周期管理、并发上限、排队调度与幂等处理。

设计要点：
- 固定数量的 worker 协程通过 asyncio.Semaphore 限制同时求解的任务数，
  超出上限的任务留在内存队列中排队，不会失败也不会被丢弃；
- 状态只能由 QUEUED -> RUNNING -> SUCCEEDED/FAILED/CANCELLED 单向推进，
  所有流转在 asyncio.Lock 下判定后落盘；
- 幂等键 -> 任务 ID 的映射持久化；同一键的重复提交返回同一任务；
- worker 执行求解前/落盘前都要二次确认取消标记，取消后不会产生结果；
- 进程重启时，残留 RUNNING 标记为 interrupted 失败，QUEUED 重新入队，
  不会留下永远运行中的任务。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from .models import (
    FailureCode,
    SubmitRequest,
    TaskRecord,
    TaskStatus,
    utcnow,
)
from .solver import (
    CancellationRequested,
    InfeasibleError,
    InvalidInputError,
    UnsolvableError,
    solve,
)
from .store import TaskStore

logger = logging.getLogger("opt_service.manager")


class TaskConflictError(Exception):
    """同一幂等键携带了不同的请求体。"""


class TaskManager:
    def __init__(
        self,
        store: TaskStore | None = None,
        max_concurrency: int = 2,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self.store = store or TaskStore()
        self.max_concurrency = max_concurrency
        self.records: dict[str, TaskRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task] = []

    # ---------------- 启动 / 关闭 / 恢复 ----------------

    async def startup(self) -> None:
        self.records = await asyncio.to_thread(self.store.load)
        self._idempotency = {
            rec.idempotency_key: rec.task_id
            for rec in self.records.values()
            if rec.idempotency_key is not None
        }
        for task_id, rec in list(self.records.items()):
            self._cancel_events[task_id] = asyncio.Event()
            if rec.status == TaskStatus.RUNNING:
                # 进程重启：运行中的任务不可能再完成，标记为中断失败
                async with self._lock:
                    await self._mark_failed_locked(
                        rec,
                        FailureCode.INTERRUPTED,
                        "进程重启时任务仍在运行，已被标记为中断失败，请重新提交",
                    )
                logger.warning(
                    "task=%s 状态恢复：RUNNING -> FAILED(interrupted)", task_id
                )
            elif rec.status == TaskStatus.QUEUED:
                await self._queue.put(task_id)
                logger.info("task=%s 状态恢复：重新进入队列", task_id)

        self._workers = [
            asyncio.create_task(self._worker(i), name=f"solver-worker-{i}")
            for i in range(self.max_concurrency)
        ]
        logger.info(
            "TaskManager 已启动：并发上限=%d，恢复任务数=%d，待排队任务=%d",
            self.max_concurrency,
            len(self.records),
            self._queue.qsize(),
        )

    async def shutdown(self) -> None:
        # 排空队列：排队任务保持 QUEUED 落盘，下次启动继续；worker 随后退出
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.store.save, self.records)
        logger.info("TaskManager 已关闭，任务记录已落盘")

    # ---------------- 提交 / 查询 / 取消 ----------------

    async def submit(self, req: SubmitRequest) -> tuple[TaskRecord, bool]:
        # 语义非法的请求在入队前直接抛出，由路由转成 400
        from .solver import validate_request

        validate_request(req)

        async with self._lock:
            if req.idempotency_key is not None:
                existing_id = self._idempotency.get(req.idempotency_key)
                if existing_id is not None:
                    existing = self.records[existing_id]
                    if existing.request != req:
                        raise TaskConflictError(
                            f"幂等键 {req.idempotency_key!r} 已绑定任务 "
                            f"{existing_id}，但提交内容与首次不一致"
                        )
                    logger.info(
                        "task=%s 幂等命中 key=%s status=%s（不重复计算）",
                        existing_id,
                        req.idempotency_key,
                        existing.status.value,
                    )
                    return existing, True

            task_id = uuid.uuid4().hex
            record = TaskRecord(
                task_id=task_id,
                idempotency_key=req.idempotency_key,
                request=req,
                status=TaskStatus.QUEUED,
            )
            self.records[task_id] = record
            self._cancel_events[task_id] = asyncio.Event()
            if req.idempotency_key is not None:
                self._idempotency[req.idempotency_key] = task_id
            await self._persist()

        await self._queue.put(task_id)
        logger.info("task=%s 提交成功 status=QUEUED key=%s", task_id, req.idempotency_key)
        return record, False

    def get(self, task_id: str) -> TaskRecord | None:
        return self.records.get(task_id)

    async def cancel(self, task_id: str) -> tuple[TaskRecord, str]:
        """取消任务。返回 (最新记录, 原因码)。

        原因码：
        - cancelled          ：本次成功取消（QUEUED/RUNNING）
        - already_cancelled  ：任务此前已被取消
        - already_succeeded  ：任务已成功，无法取消
        - already_failed     ：任务已失败，无法取消
        - not_found          ：任务不存在
        """
        async with self._lock:
            rec = self.records.get(task_id)
            if rec is None:
                return None, "not_found"
            if rec.status == TaskStatus.CANCELLED:
                return rec, "already_cancelled"
            if rec.status == TaskStatus.SUCCEEDED:
                return rec, "already_succeeded"
            if rec.status == TaskStatus.FAILED:
                return rec, "already_failed"

            event = self._cancel_events.get(task_id)
            if event is not None:
                event.set()
            await self._mark_cancelled_locked(rec)
        logger.info("task=%s 用户请求取消 -> CANCELLED", task_id)
        return rec, "cancelled"

    # ---------------- worker 与状态流转 ----------------

    async def _worker(self, index: int) -> None:
        logger.info("worker=%d 开始等待任务", index)
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            finally:
                self._queue.task_done()

    async def _run_one(self, task_id: str) -> None:
        rec = self.records.get(task_id)
        if rec is None:
            return
        async with self._lock:
            # 出队后二次确认：可能在排队期间已被取消
            if rec.status != TaskStatus.QUEUED:
                logger.info(
                    "task=%s 出队时状态已为 %s，跳过执行",
                    task_id,
                    rec.status.value,
                )
                return
            if self._cancel_events[task_id].is_set():
                await self._mark_cancelled_locked(rec)
                return
            rec.status = TaskStatus.RUNNING
            rec.started_at = utcnow()
            rec.updated_at = utcnow()
            await self._persist()
            logger.info("task=%s 状态流转 QUEUED -> RUNNING", task_id)

        event = self._cancel_events[task_id]
        try:
            result = await asyncio.to_thread(
                solve, rec.request, event.is_set
            )
        except CancellationRequested:
            async with self._lock:
                if rec.status == TaskStatus.RUNNING:
                    await self._mark_cancelled_locked(rec)
            logger.info("task=%s 求解过程中确认取消 -> CANCELLED", task_id)
            return
        except InvalidInputError as exc:
            await self._finish_failed(rec, FailureCode.INVALID_INPUT, str(exc))
            return
        except InfeasibleError as exc:
            await self._finish_failed(rec, FailureCode.INFEASIBLE, str(exc))
            return
        except UnsolvableError as exc:
            await self._finish_failed(rec, FailureCode.UNSOLVABLE, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 求解器未知异常也要落为 FAILED
            await self._finish_failed(
                rec, FailureCode.INVALID_INPUT, f"求解发生未预期错误: {exc!r}"
            )
            logger.exception("task=%s 求解抛出未预期异常", task_id)
            return

        async with self._lock:
            # 落盘成功结果前最后一次确认取消，保证取消后绝不产生结果
            if event.is_set() or rec.status != TaskStatus.RUNNING:
                if rec.status == TaskStatus.RUNNING:
                    await self._mark_cancelled_locked(rec)
                logger.info(
                    "task=%s 求解结束但取消已生效（status=%s），丢弃结果",
                    task_id,
                    rec.status.value,
                )
                return
            rec.result = result
            rec.status = TaskStatus.SUCCEEDED
            rec.finished_at = utcnow()
            rec.updated_at = utcnow()
            await self._persist()
        logger.info(
            "task=%s 状态流转 RUNNING -> SUCCEEDED objective=%s 依据=%d 个分配",
            task_id,
            result.objective_value,
            len(result.assignments),
        )

    async def _finish_failed(
        self, rec: TaskRecord, code: FailureCode, reason: str
    ) -> None:
        async with self._lock:
            if rec.status != TaskStatus.RUNNING:
                # 竞态：运行中已被取消，则以取消为准，不再写失败
                logger.info(
                    "task=%s 失败结论到达时状态为 %s，以既有状态为准",
                    rec.task_id,
                    rec.status.value,
                )
                return
            await self._mark_failed_locked(rec, code, reason)
        logger.info(
            "task=%s 状态流转 RUNNING -> FAILED code=%s 依据=%s",
            rec.task_id,
            code.value,
            reason,
        )

    async def _mark_failed_locked(
        self, rec: TaskRecord, code: FailureCode, reason: str
    ) -> None:
        """标记失败；调用方必须持有 self._lock。"""
        rec.status = TaskStatus.FAILED
        rec.failure_code = code
        rec.failure_reason = reason
        rec.finished_at = utcnow()
        rec.updated_at = utcnow()
        await self._persist()

    async def _mark_cancelled_locked(self, rec: TaskRecord) -> None:
        rec.status = TaskStatus.CANCELLED
        rec.failure_code = FailureCode.CANCELLED
        rec.failure_reason = "任务被用户取消"
        rec.finished_at = utcnow()
        rec.updated_at = utcnow()
        await self._persist()

    async def _persist(self) -> None:
        await asyncio.to_thread(self.store.save, self.records)
