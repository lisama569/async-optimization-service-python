"""进程重启后：RUNNING 不允许残留，QUEUED 重新排队，终态与幂等映射保留。"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app import create_app
from app.models import (
    Assignment,
    FailureCode,
    GenerateSpec,
    SolveResult,
    SubmitRequest,
    TaskRecord,
    TaskStatus,
)
from app.store import TaskStore
from conftest import poll


def _fresh_client(store_path):
    os.environ["TASK_STORE_PATH"] = str(store_path)
    os.environ["SOLVER_MAX_CONCURRENCY"] = "2"
    application = create_app()
    return TestClient(application)


def test_restart_marks_running_interrupted_and_replays_queued(tmp_path):
    store_path = tmp_path / "tasks.json"
    req = SubmitRequest(generate=GenerateSpec(num_items=4, num_resources=1), seed=8)

    stale_running = TaskRecord(
        task_id="run-1", status=TaskStatus.RUNNING, request=req
    )
    stale_queued = TaskRecord(
        task_id="que-1", status=TaskStatus.QUEUED, request=req
    )
    stale_done = TaskRecord(
        task_id="done-1",
        status=TaskStatus.SUCCEEDED,
        request=req,
        result=SolveResult(
            objective_value=1.5,
            assignments=[Assignment(item_id="a", resource_id="r")],
            total_demand=1.0,
            unassigned_items=[],
            resources_used=["r"],
        ),
    )
    TaskStore(str(store_path)).save(
        {"run-1": stale_running, "que-1": stale_queued, "done-1": stale_done}
    )

    with _fresh_client(store_path) as c:
        # 1) 残留 RUNNING 立即被判定为 interrupted 失败
        r = c.get("/api/tasks/run-1/result")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert body["failure_code"] == FailureCode.INTERRUPTED.value
        assert "重启" in body["failure_reason"]

        # 2) 终态保持
        done = c.get("/api/tasks/done-1/result").json()
        assert done["status"] == "succeeded"
        assert done["result"]["objective_value"] == 1.5

        # 3) QUEUED 被重新执行
        final = poll(c, "que-1")
        assert final["status"] == "succeeded"


def test_idempotency_map_survives_restart(tmp_path):
    store_path = tmp_path / "tasks.json"

    # 第一次进程：用幂等键提交并完成
    with _fresh_client(store_path) as c:
        payload = {
            "idempotency_key": "durable-key",
            "generate": {"num_items": 5, "num_resources": 1},
            "seed": 2,
        }
        task_id = c.post("/api/tasks", json=payload).json()["task_id"]
        poll(c, task_id)

    # 第二次进程：同键提交必须回到原任务
    with _fresh_client(store_path) as c:
        payload = {
            "idempotency_key": "durable-key",
            "generate": {"num_items": 5, "num_resources": 1},
            "seed": 2,
        }
        again = c.post("/api/tasks", json=payload).json()
        assert again["task_id"] == task_id
        assert again["idempotent_replayed"] is True
        assert again["status"] == "succeeded"
