"""进程重启恢复测试：非终态任务重新排队，不残留永久 running。"""

from __future__ import annotations

import json
import time

from app.store import TaskStore
from tests.helpers import feasible_problem, poll, slow_problem


def test_stale_running_requeued_on_restart(tmp_path):
    state = tmp_path / "tasks.json"
    p = feasible_problem(seed=5, n=10).model_copy(update={"step_delay_ms": 30})
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "task_id": "zombie-1",
                        "problem": p.model_dump(mode="json"),
                        "payload_hash": "h",
                        "idempotency_key": "k1",
                        "status": "running",
                        "progress": 0.42,
                        "result": None,
                        "failure_reason": None,
                        "message": None,
                        "created_at": "t0",
                        "updated_at": "t1",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = TaskStore(max_concurrency=1, state_path=state)
    # 先只加载不启动调度器，验证崩溃快照被严格重置为 queued/progress=0
    store._load()
    pre = store.get("zombie-1")
    assert pre.status.value == "queued", "崩溃遗留的 running 必须重置为 queued"
    assert pre.progress == 0.0
    store.start()
    try:
        v = store.get("zombie-1")
        assert v.status.value in ("queued", "running"), v.status
        # 关键不变量：不会永久卡在 running；可推进到终态
        assert store.wait_idle(timeout=10)
        assert store.get("zombie-1").status.value == "succeeded"
        # 幂等索引也恢复
        assert store.get_by_idempotency_key("k1").task_id == "zombie-1"
    finally:
        store.stop()


def test_terminal_tasks_survive_restart(tmp_path):
    state = tmp_path / "state.json"
    s1 = TaskStore(max_concurrency=2, state_path=state)
    s1.start()
    p_ok = feasible_problem(seed=9, n=2)
    from app.models import ItemInput, Problem, ResourceInput

    p_bad = Problem(
        items=[ItemInput(id="x", demand=99, value=1)],
        resources=[ResourceInput(id="r", capacity=1)],
    )
    v_ok, _ = s1.submit(p_ok, "h-ok", "k-ok")
    v_bad, _ = s1.submit(p_bad, "h-bad", "k-bad")
    assert s1.wait_idle(timeout=5)
    obj = s1.get(v_ok.task_id).result.objective
    s1.stop()

    s2 = TaskStore(max_concurrency=2, state_path=state)
    s2.start()
    try:
        assert s2.get(v_ok.task_id).status.value == "succeeded"
        assert s2.get(v_ok.task_id).result.objective == obj
        assert s2.get(v_bad.task_id).status.value == "failed"
        assert s2.get(v_bad.task_id).failure_reason.value == "infeasible"
    finally:
        s2.stop()


def test_api_recovers_after_restart(client, task_store):
    """通过 API 提交、等待、关停后重开：终态结果仍可查。"""
    body = {"problem": feasible_problem(seed=3, n=3).model_dump(mode="json")}
    r = client.post("/api/tasks", json=body)
    tid = r.json()["task_id"]
    poll(client, tid)
    obj = client.get(f"/api/tasks/{tid}/result").json()["result"]["objective"]

    path = task_store.state_path
    task_store.stop()

    from fastapi.testclient import TestClient
    import main as main_mod

    s2 = TaskStore(max_concurrency=2, state_path=path)
    main_mod._build_store = lambda: s2
    with TestClient(main_mod.app) as c2:
        v = c2.get(f"/api/tasks/{tid}").json()
        assert v["status"] == "succeeded"
        assert v["result"]["objective"] == obj
    main_mod._build_store = lambda: task_store  # 还原供外层 fixture 关停
