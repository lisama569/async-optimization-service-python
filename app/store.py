"""任务存储与调度：状态机、并发上限、排队、取消、幂等、本地持久化。

状态机（单向推进，不允许回退）::

    QUEUED ──► RUNNING ──► SUCCEEDED
       │          │
       │          ├────────► FAILED
       │          └────────► CANCELLED
       └───────────────────► CANCELLED

- SUCCEEDED / FAILED / CANCELLED 为终态；
- 并发：至多 max_concurrency 个任务处于 RUNNING，其余停留在 QUEUED，
  由单个调度线程在任务结束后依次拉起；
- 持久化：任务快照写入本地 JSON（原子替换），重启时把所有非终态任务
  重新置为 QUEUED，避免任务永久卡在 RUNNING。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import InfeasibleError, InvalidInputError, NoSolutionError, SolveError
from .models import FailureReason, Problem, SolveResult, TaskStatus, TaskView
from .solver import SolveCancelled, solve
from . import service as service_mod

logger = logging.getLogger("optimization.tasks")

_TERMINAL = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}
_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _TaskRecord:
    """单个任务的可变内部状态。"""

    def __init__(
        self,
        task_id: str,
        problem: Problem,
        payload_hash: str,
        idempotency_key: str | None,
    ) -> None:
        self.task_id = task_id
        self.problem = problem
        self.payload_hash = payload_hash
        self.idempotency_key = idempotency_key
        self.status: TaskStatus = TaskStatus.QUEUED
        self.progress: float = 0.0
        self.result: SolveResult | None = None
        self.failure_reason: FailureReason | None = None
        self.message: str | None = None
        self.created_at = _now()
        self.updated_at = self.created_at
        self.cancel_event = threading.Event()
        self.last_persisted_progress = -1.0

    def view(self) -> TaskView:
        return TaskView(
            task_id=self.task_id,
            idempotency_key=self.idempotency_key,
            status=self.status,
            progress=round(self.progress, 4),
            result=self.result,
            failure_reason=self.failure_reason,
            message=self.message,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def snapshot(self) -> dict:
        return {
            "task_id": self.task_id,
            "problem": self.problem.model_dump(),
            "payload_hash": self.payload_hash,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "progress": self.progress,
            "result": self.result.model_dump() if self.result else None,
            "failure_reason": (
                self.failure_reason.value if self.failure_reason else None
            ),
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskStore:
    def __init__(
        self,
        max_concurrency: int = 2,
        state_path: str | Path | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self.max_concurrency = max_concurrency
        self.state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskRecord] = {}
        self._by_idem: dict[str, str] = {}
        self._running_count = 0
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._workers: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.state_path is not None:
            self._load()
        self._stop.clear()
        self._scheduler = threading.Thread(
            target=self._schedule_loop, name="task-scheduler", daemon=True
        )
        self._scheduler.start()
        logger.info(
            "任务存储已启动 max_concurrency=%s state_path=%s",
            self.max_concurrency,
            self.state_path,
        )

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=2)
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.join(timeout=2)
        self._persist_locked()
        logger.info("任务存储已停止")

    # ------------------------------------------------------------------ 持久化
    def _persist_locked(self) -> None:
        if self.state_path is None:
            return
        data = {
            "version": 1,
            "tasks": [rec.snapshot() for rec in self._tasks.values()],
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _load(self) -> None:
        assert self.state_path is not None
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("状态文件无法解析，将以空存储启动: %s", exc)
            return
        recovered = 0
        with self._lock:
            for raw in data.get("tasks", []):
                try:
                    problem = Problem.model_validate(raw["problem"])
                except Exception as exc:  # noqa: BLE001 - 跳过坏快照
                    logger.warning(
                        "跳过无法反序列化的任务 %s: %s", raw.get("task_id"), exc
                    )
                    continue
                rec = _TaskRecord(
                    task_id=raw["task_id"],
                    problem=problem,
                    payload_hash=raw.get("payload_hash", ""),
                    idempotency_key=raw.get("idempotency_key"),
                )
                rec.result = (
                    SolveResult.model_validate(raw["result"]) if raw.get("result") else None
                )
                rec.failure_reason = (
                    FailureReason(raw["failure_reason"])
                    if raw.get("failure_reason")
                    else None
                )
                rec.message = raw.get("message")
                rec.created_at = raw.get("created_at", rec.created_at)
                rec.updated_at = raw.get("updated_at", rec.updated_at)
                status = TaskStatus(raw.get("status", "queued"))
                # 重启恢复：非终态任务一律重新排队，杜绝永久 RUNNING
                if status in _TERMINAL:
                    rec.status = status
                else:
                    rec.status = TaskStatus.QUEUED
                    rec.progress = 0.0
                    rec.message = "进程重启，任务重新进入队列"
                    rec.updated_at = _now()
                    recovered += 1
                    logger.info(
                        "task=%s 重启恢复：%s -> queued（判定依据：上次运行未落终态）",
                        rec.task_id,
                        status.value,
                    )
                self._tasks[rec.task_id] = rec
                if rec.idempotency_key:
                    self._by_idem[rec.idempotency_key] = rec.task_id
            self._persist_locked()
        if recovered:
            logger.info("共恢复 %s 个未完成任务到队列", recovered)

    # ------------------------------------------------------------------ 提交 / 查询
    def submit(
        self,
        problem: Problem,
        payload_hash: str,
        idempotency_key: str | None,
    ) -> tuple[TaskView, bool]:
        """返回 (任务视图, 是否复用已有任务)。"""
        with self._lock:
            if idempotency_key is not None:
                existing_id = self._by_idem.get(idempotency_key)
                if existing_id is not None:
                    existing = self._tasks[existing_id]
                    if existing.payload_hash != payload_hash:
                        raise service_mod.IdempotencyConflictError(existing.view())
                    logger.info(
                        "幂等命中 key=%s task=%s status=%s（判定依据：payload_hash 一致）",
                        idempotency_key,
                        existing.task_id,
                        existing.status.value,
                    )
                    return existing.view(), True

            task_id = uuid.uuid4().hex
            rec = _TaskRecord(
                task_id=task_id,
                problem=problem,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
            )
            self._tasks[task_id] = rec
            if idempotency_key is not None:
                self._by_idem[idempotency_key] = task_id
            self._persist_locked()
            logger.info(
                "task=%s 提交成功 status=queued idempotency_key=%s",
                task_id,
                idempotency_key,
            )
        self._wakeup.set()
        return rec.view(), False

    def get(self, task_id: str) -> TaskView | None:
        with self._lock:
            rec = self._tasks.get(task_id)
            return rec.view() if rec else None

    def get_by_idempotency_key(self, key: str) -> TaskView | None:
        with self._lock:
            task_id = self._by_idem.get(key)
            if task_id is None:
                return None
            return self._tasks[task_id].view()

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self._running_count,
                "queued": sum(
                    1 for r in self._tasks.values() if r.status == TaskStatus.QUEUED
                ),
                "max_concurrency": self.max_concurrency,
            }

    # ------------------------------------------------------------------ 取消
    def cancel(self, task_id: str) -> TaskView:
        """请求取消。返回取消操作后的任务视图。

        - QUEUED：直接转为 CANCELLED（终态，不会再被调度）；
        - RUNNING：设置取消信号，由工作线程在最近检查点转为 CANCELLED；
        - CANCELLED：幂等，直接返回当前视图；
        - SUCCEEDED / FAILED：抛 AlreadyTerminalError，错误码区分终态。
        """
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                raise service_mod.TaskNotFoundError()
            status = rec.status
            if status == TaskStatus.CANCELLED:
                logger.info("task=%s 重复取消，保持 cancelled（幂等）", task_id)
                return rec.view()
            if status == TaskStatus.SUCCEEDED:
                raise service_mod.AlreadyTerminalError(
                    "task_already_succeeded",
                    f"任务 {task_id} 已成功完成，不能取消",
                    rec.view(),
                )
            if status == TaskStatus.FAILED:
                raise service_mod.AlreadyTerminalError(
                    "task_already_failed",
                    f"任务 {task_id} 已失败结束，不能取消",
                    rec.view(),
                )
            if status == TaskStatus.QUEUED:
                rec.failure_reason = FailureReason.CANCELLED
                self._transition(rec, TaskStatus.CANCELLED, "用户在排队阶段取消")
                rec.cancel_event.set()
                self._persist_locked()
                logger.info("task=%s 状态变化 queued -> cancelled", task_id)
                return rec.view()
            # RUNNING：发信号，状态由工作线程推进
            rec.cancel_event.set()
            logger.info(
                "task=%s 收到取消信号，status=running（将在最近检查点流转）",
                task_id,
            )
            return rec.view()

    # ------------------------------------------------------------------ 状态机
    def _transition(
        self, rec: _TaskRecord, new_status: TaskStatus, message: str | None = None
    ) -> None:
        old = rec.status
        if new_status not in _ALLOWED_TRANSITIONS.get(old, set()):
            raise RuntimeError(
                f"非法状态流转: {old.value} -> {new_status.value} (task={rec.task_id})"
            )
        rec.status = new_status
        rec.updated_at = _now()
        if message is not None:
            rec.message = message
        logger.info(
            "task=%s 状态变化 %s -> %s%s",
            rec.task_id,
            old.value,
            new_status.value,
            f"（判定依据：{message}）" if message else "",
        )

    # ------------------------------------------------------------------ 调度
    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            launched: list[_TaskRecord] = []
            with self._lock:
                if self._running_count < self.max_concurrency:
                    for rec in self._tasks.values():
                        if self._running_count >= self.max_concurrency:
                            break
                        if rec.status == TaskStatus.QUEUED and not rec.cancel_event.is_set():
                            self._transition(rec, TaskStatus.RUNNING)
                            self._running_count += 1
                            self._persist_locked()
                            launched.append(rec)
            for rec in launched:
                t = threading.Thread(
                    target=self._run_task,
                    args=(rec.task_id,),
                    name=f"worker-{rec.task_id[:8]}",
                    daemon=True,
                )
                with self._lock:
                    self._workers[rec.task_id] = t
                t.start()
            self._wakeup.wait(timeout=0.05)
            self._wakeup.clear()

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
        if rec is None:
            return
        try:
            result = solve(
                rec.problem,
                is_cancelled=rec.cancel_event.is_set,
                report_progress=lambda p: self._on_progress(task_id, p),
            )
        except SolveCancelled as exc:
            self._finish_cancelled(task_id, str(exc))
        except (InvalidInputError, InfeasibleError, NoSolutionError) as exc:
            self._finish_failed(task_id, exc)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死亡
            logger.exception("task=%s 求解发生未预期异常", task_id)
            self._finish_failed(task_id, exc, reason=FailureReason.INTERNAL)
        else:
            self._finish_succeeded(task_id, result)
        finally:
            with self._lock:
                self._running_count = max(0, self._running_count - 1)
                self._workers.pop(task_id, None)
                self._persist_locked()
            self._wakeup.set()

    def _on_progress(self, task_id: str, progress: float) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None or rec.status != TaskStatus.RUNNING:
                return
            rec.progress = max(rec.progress, progress)
            rec.updated_at = _now()
            # 进度每推进 10% 落一次盘，降低 IO；取消信号不依赖落盘
            if progress - rec.last_persisted_progress >= 0.1 - 1e-9:
                rec.last_persisted_progress = progress
                self._persist_locked()

    def _finish_succeeded(self, task_id: str, result: SolveResult) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None or rec.status != TaskStatus.RUNNING:
                return
            if rec.cancel_event.is_set():
                # 求解结束瞬间收到取消：取消优先，结果丢弃不落终态成功
                rec.failure_reason = FailureReason.CANCELLED
                self._transition(rec, TaskStatus.CANCELLED, "结束时检测到取消信号，结果丢弃")
                rec.progress = 1.0
                return
            rec.result = result
            rec.progress = 1.0
            self._transition(
                rec,
                TaskStatus.SUCCEEDED,
                f"求解完成，目标值 {result.objective}",
            )

    def _finish_cancelled(self, task_id: str, message: str) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None or rec.status != TaskStatus.RUNNING:
                return
            rec.progress = rec.progress
            rec.failure_reason = FailureReason.CANCELLED
            self._transition(rec, TaskStatus.CANCELLED, message)

    def _finish_failed(
        self,
        task_id: str,
        exc: Exception,
        reason: FailureReason | None = None,
    ) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None or rec.status != TaskStatus.RUNNING:
                return
            if isinstance(exc, SolveError):
                mapped = exc.reason
                msg = exc.message
            else:
                mapped = reason or FailureReason.INTERNAL
                msg = f"内部错误: {exc}"
            rec.failure_reason = mapped
            self._transition(
                rec,
                TaskStatus.FAILED,
                f"{mapped.value}: {msg}",
            )

    # 仅供测试/关停时等待
    def wait_idle(self, timeout: float = 5.0) -> bool:
        """等待所有 QUEUED/RUNNING 任务进入终态。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = [
                    r
                    for r in self._tasks.values()
                    if r.status not in _TERMINAL
                ]
            if not active:
                return True
            time.sleep(0.01)
        return False
