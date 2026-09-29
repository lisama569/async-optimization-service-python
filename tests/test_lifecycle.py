"""任务生命周期、并发上限与队列、取消语义测试。"""

from __future__ import annotations

import time

from app.models import TaskStatus
from app.service import TaskServiceError
from tests.helpers import poll, slow_problem


def _submit(client, problem, key=None):
    body = {"problem": problem.model_dump(mode="json")}
    if key is not None:
        body["idempotency_key"] = key
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_full_lifecycle_succeeds(client):
    t = _submit(client, slow_problem(seed=1, n=3, step_delay_ms=10))
    assert t["status"] == TaskStatus.QUEUED.value

    view = client.get(f"/api/tasks/{t['task_id']}").json()
    assert view["status"] in ("queued", "running")

    final = poll(client, t["task_id"])
    assert final["status"] == "succeeded"
    assert final["progress"] == 1.0
    assert final["result"] is not None
    assert final["result"]["objective"] > 0

    rr = client.get(f"/api/tasks/{t['task_id']}/result")
    assert rr.status_code == 200
    assert rr.json()["result"]["objective"] == final["result"]["objective"]


def test_concurrency_limit_and_queue(client, task_store):
    """并发上限=2：第 3 个任务排队，前两个完成后自动拉起。"""
    t1 = _submit(client, slow_problem(1, n=30, step_delay_ms=20))
    t2 = _submit(client, slow_problem(2, n=30, step_delay_ms=20))
    time.sleep(0.15)
    t3 = _submit(client, slow_problem(3, n=10, step_delay_ms=10))

    # 上限内两个 running，第三个必须 queued 而不是失败
    time.sleep(0.1)
    s1 = client.get(f"/api/tasks/{t1['task_id']}").json()["status"]
    s2 = client.get(f"/api/tasks/{t2['task_id']}").json()["status"]
    s3 = client.get(f"/api/tasks/{t3['task_id']}").json()["status"]
    running = [s for s in (s1, s2, s3) if s == "running"]
    assert len(running) <= 2
    assert s3 == "queued", f"超额任务应排队，实际 {s3}"

    # 队列自动推进：最终三个都成功
    for t in (t1, t2, t3):
        final = poll(client, t["task_id"], timeout=15)
        assert final["status"] == "succeeded"


def test_concurrent_submissions_same_idempotency_key_single_task(client):
    """并发提交同一幂等键：只落一个任务。"""
    import concurrent.futures

    problem = slow_problem(7, n=5, step_delay_ms=5)
    body = {"idempotency_key": "concurrent-key", "problem": problem.model_dump(mode="json")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post("/api/tasks", json=body), range(8)))

    task_ids = {r.json()["task_id"] for r in responses}
    assert len(task_ids) == 1, f"同一幂等键产生了多个任务: {task_ids}"
    assert all(r.status_code == 201 for r in responses)

    final = poll(client, next(iter(task_ids)))
    assert final["status"] == "succeeded"


def test_cancel_queued_task_produces_no_result(client):
    # 用两个慢任务占满并发槽
    _submit(client, slow_problem(1, n=40, step_delay_ms=20))
    _submit(client, slow_problem(2, n=40, step_delay_ms=20))
    time.sleep(0.15)
    q = _submit(client, slow_problem(3, n=10))
    assert client.get(f"/api/tasks/{q['task_id']}").json()["status"] == "queued"

    r = client.post(f"/api/tasks/{q['task_id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    time.sleep(0.3)
    v = client.get(f"/api/tasks/{q['task_id']}").json()
    assert v["status"] == "cancelled"
    assert v["result"] is None
    assert v["failure_reason"] == "cancelled"

    # 对已结束任务取消：取消动作本身幂等
    r2 = client.post(f"/api/tasks/{q['task_id']}/cancel")
    assert r2.status_code == 200 and r2.json()["status"] == "cancelled"

    # 取消后取结果：明确原因，可与 failed 区分
    rr = client.get(f"/api/tasks/{q['task_id']}/result")
    assert rr.status_code == 409
    assert rr.json()["error"]["code"] == "result_unavailable_cancelled"


def test_cancel_running_task_produces_no_result(client):
    t = _submit(client, slow_problem(1, n=40, step_delay_ms=20))
    # 等到 running
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if client.get(f"/api/tasks/{t['task_id']}").json()["status"] == "running":
            break
        time.sleep(0.01)

    r = client.post(f"/api/tasks/{t['task_id']}/cancel")
    assert r.status_code == 200
    # 发信号瞬间可能仍是 running，由 worker 在检查点推进
    assert r.json()["status"] in ("running", "cancelled")

    final = poll(client, t["task_id"], timeout=10)
    assert final["status"] == "cancelled"
    assert final["result"] is None

    rr = client.get(f"/api/tasks/{t['task_id']}/result")
    assert rr.json()["error"]["code"] == "result_unavailable_cancelled"


def test_cancel_terminal_succeeded_failed_is_distinguishable(client):
    from tests.helpers import feasible_problem, oversized_problem

    ok = _submit(client, feasible_problem(1, n=2))
    poll(client, ok["task_id"])
    r = client.post(f"/api/tasks/{ok['task_id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_already_succeeded"

    bad = _submit(client, oversized_problem())
    poll(client, bad["task_id"])
    r = client.post(f"/api/tasks/{bad['task_id']}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_already_failed"


def test_result_not_ready_while_pending(client):
    t = _submit(client, slow_problem(1, n=30, step_delay_ms=20))
    time.sleep(0.1)
    rr = client.get(f"/api/tasks/{t['task_id']}/result")
    assert rr.status_code == 409
    assert rr.json()["error"]["code"] == "result_not_ready"


def test_unknown_task_404(client):
    assert client.get("/api/tasks/does-not-exist").status_code == 404
    assert client.post("/api/tasks/does-not-exist/cancel").status_code == 404
    assert client.get("/api/tasks/does-not-exist/result").status_code == 404
