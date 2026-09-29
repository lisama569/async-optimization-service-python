"""并发上限、排队等待与取消语义。"""

from __future__ import annotations

import time

import pytest

import app.manager as manager_mod
from conftest import poll

SLOW_PAYLOAD = {
    "generate": {"num_items": 4, "num_resources": 1, "mandatory_ratio": 0.0},
    "seed": 3,
    "max_attempts": 2,
}


@pytest.fixture
def slow_solver(monkeypatch):
    original = manager_mod.solve

    def slow_solve(req, is_cancelled=None):
        time.sleep(0.4)
        return original(req, is_cancelled)

    monkeypatch.setattr(manager_mod, "solve", slow_solve)
    yield


def _post(client):
    return client.post("/api/tasks", json=SLOW_PAYLOAD).json()["task_id"]


def _wait_status(client, task_id, expected, timeout=3.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/tasks/{task_id}").json()["status"]
        if last == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"{task_id} 期望 {expected}，实际 {last}")


def test_concurrency_limit_and_queue(client, slow_solver):
    # 上限 SOLVER_MAX_CONCURRENCY=2：前两个 running，第三个 queued
    t1, t2, t3 = _post(client), _post(client), _post(client)
    _wait_status(client, t1, "running")
    _wait_status(client, t2, "running")
    time.sleep(0.05)
    assert client.get(f"/api/tasks/{t3}").json()["status"] == "queued"

    for tid in (t1, t2, t3):
        final = poll(client, tid)
        assert final["status"] == "succeeded"


def test_cancel_queued_task(client, slow_solver):
    t1 = _post(client)
    t2 = _post(client)
    t3 = _post(client)  # 排队中
    _wait_status(client, t1, "running")
    _wait_status(client, t2, "running")
    _wait_status(client, t3, "queued")

    resp = client.post(f"/api/tasks/{t3}/cancel")
    assert resp.status_code == 200
    assert resp.json()["reason"] == "cancelled"
    assert client.get(f"/api/tasks/{t3}").json()["status"] == "cancelled"

    # 排队任务取消后不得产生结果
    result = client.get(f"/api/tasks/{t3}/result")
    assert result.status_code == 409
    assert result.json()["detail"]["reason"] == "result_discarded"

    # 重复取消：行为明确，原因可区分
    again = client.post(f"/api/tasks/{t3}/cancel")
    assert again.status_code == 200
    assert again.json()["reason"] == "already_cancelled"

    poll(client, t1)
    poll(client, t2)


def test_cancel_running_task_discards_result(client, slow_solver):
    t1 = _post(client)
    _wait_status(client, t1, "running")

    resp = client.post(f"/api/tasks/{t1}/cancel")
    assert resp.status_code == 200
    assert resp.json()["reason"] == "cancelled"

    final = poll(client, t1)
    assert final["status"] == "cancelled"

    # 即使求解线程稍后返回，也不允许写入结果
    time.sleep(0.6)
    status = client.get(f"/api/tasks/{t1}").json()["status"]
    result = client.get(f"/api/tasks/{t1}/result")
    assert status == "cancelled"
    assert result.status_code == 409
    assert result.json()["detail"]["reason"] == "result_discarded"


def test_cancel_terminal_task_is_distinguishable(client):
    # 成功任务
    ok = _post(client)  # 无 slow_solver 作用，走快速求解
    # 注意：本测试未请求 slow_solver fixture，求解很快
    poll(client, ok)
    r = client.post(f"/api/tasks/{ok}/cancel")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "already_succeeded"

    # 失败任务
    bad = client.post(
        "/api/tasks",
        json={
            "instance": {
                "items": [
                    {"item_id": "x", "demand": 99, "value": 1, "mandatory": True}
                ],
                "resources": [{"resource_id": "r", "capacity": 2}],
            }
        },
    ).json()["task_id"]
    poll(client, bad)
    r = client.post(f"/api/tasks/{bad}/cancel")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "already_failed"

    # 不存在
    assert client.post("/api/tasks/ghost/cancel").status_code == 404


def test_result_before_finish_conflicts(client, slow_solver):
    t1 = _post(client)
    _wait_status(client, t1, "running")
    r = client.get(f"/api/tasks/{t1}/result")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_finished"
    client.post(f"/api/tasks/{t1}/cancel")
    poll(client, t1)
