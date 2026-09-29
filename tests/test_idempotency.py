"""幂等键：同一键落到同一任务，不重复计算；不同内容冲突可区分。"""

from __future__ import annotations

import threading
import time

from conftest import poll


def test_duplicate_submit_same_task_and_result(client):
    payload = {
        "idempotency_key": "key-100",
        "generate": {"num_items": 15, "num_resources": 3},
        "seed": 42,
    }
    first = client.post("/api/tasks", json=payload)
    second = client.post("/api/tasks", json=payload)
    assert first.status_code == 202 and second.status_code == 202
    f, s = first.json(), second.json()
    assert f["task_id"] == s["task_id"]
    assert f["idempotent_replayed"] is False
    assert s["idempotent_replayed"] is True

    task_id = f["task_id"]
    poll(client, task_id)

    # 终态后再次用同一键提交，仍返回同一终态任务
    third = client.post("/api/tasks", json=payload).json()
    assert third["task_id"] == task_id
    assert third["idempotent_replayed"] is True
    assert third["status"] == "succeeded"

    # 结果只有一份且稳定
    r1 = client.get(f"/api/tasks/{task_id}/result").json()
    r2 = client.get(f"/api/tasks/{task_id}/result").json()
    assert r1 == r2
    assert r1["result"]["objective_value"] > 0


def test_same_key_different_body_conflicts(client):
    base = {"idempotency_key": "k-conflict", "generate": {"num_items": 5}}
    client.post("/api/tasks", json=base)
    changed = {"idempotency_key": "k-conflict", "generate": {"num_items": 6}}
    resp = client.post("/api/tasks", json=changed)
    assert resp.status_code == 409
    assert "不一致" in resp.json()["detail"]


def test_concurrent_submits_with_same_key_share_one_task(client):
    """并发提交同一幂等键，只允许创建一个任务。"""
    payload = {
        "idempotency_key": "race-key",
        "generate": {"num_items": 6, "num_resources": 2},
        "seed": 5,
    }
    results: list[dict] = []
    errors = []

    def submit():
        try:
            r = client.post("/api/tasks", json=payload)
            results.append(r.json())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    task_ids = {r["task_id"] for r in results}
    assert len(task_ids) == 1, f"同一幂等键产生了多个任务: {task_ids}"
    replays = sum(1 for r in results if r["idempotent_replayed"])
    assert replays == 7

    task_id = next(iter(task_ids))
    final = poll(client, task_id)
    assert final["status"] == "succeeded"
