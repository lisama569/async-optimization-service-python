"""任务生命周期、状态单向流转与结果复现。"""

from __future__ import annotations

from conftest import poll


def test_full_lifecycle_success_with_generated_instance(client):
    resp = client.post(
        "/api/tasks",
        json={"generate": {"num_items": 12, "num_resources": 3}, "seed": 21},
    )
    assert resp.status_code == 202
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] == "queued"
    assert body["idempotent_replayed"] is False

    final = poll(client, task_id)
    assert final["status"] == "succeeded"
    assert final["started_at"] is not None
    assert final["finished_at"] is not None

    result = client.get(f"/api/tasks/{task_id}/result")
    assert result.status_code == 200
    data = result.json()
    assert data["status"] == "succeeded"
    assert data["failure_code"] is None
    assert data["result"]["objective_value"] > 0
    assert data["result"]["assignments"], "应至少分配部分物品"


def test_status_never_goes_backwards_and_finished_remains(client):
    resp = client.post(
        "/api/tasks",
        json={
            "instance": {
                "items": [
                    {"item_id": "a", "demand": 2, "value": 5, "mandatory": True}
                ],
                "resources": [{"resource_id": "r1", "capacity": 8}],
            },
            "seed": 1,
        },
    )
    task_id = resp.json()["task_id"]

    seen = set()
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        s = client.get(f"/api/tasks/{task_id}").json()["status"]
        seen.add(s)
        if s in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.005)

    # 终态后重复查询结果保持一致
    r1 = client.get(f"/api/tasks/{task_id}/result").json()
    r2 = client.get(f"/api/tasks/{task_id}/result").json()
    assert r1 == r2
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "succeeded"
    assert seen <= {"queued", "running", "succeeded"}


def test_same_input_same_seed_same_result_different_tasks(client):
    payload = {"generate": {"num_items": 20, "num_resources": 3}, "seed": 99}
    ids = []
    objectives = []
    for _ in range(2):
        r = client.post("/api/tasks", json=payload)
        assert r.status_code == 202
        ids.append(r.json()["task_id"])
    assert ids[0] != ids[1], "无幂等键时应创建不同任务"

    for tid in ids:
        final = poll(client, tid)
        assert final["status"] == "succeeded"
        objectives.append(
            client.get(f"/api/tasks/{tid}/result").json()["result"][
                "objective_value"
            ]
        )
    assert objectives[0] == objectives[1], "相同输入+相同种子目标值必须一致"


def test_different_seed_may_differ(client):
    payloads = [
        {"generate": {"num_items": 20, "num_resources": 3}, "seed": 1},
        {"generate": {"num_items": 20, "num_resources": 3}, "seed": 2},
    ]
    results = []
    for p in payloads:
        tid = client.post("/api/tasks", json=p).json()["task_id"]
        poll(client, tid)
        results.append(
            client.get(f"/api/tasks/{tid}/result").json()["result"][
                "objective_value"
            ]
        )
    # 不同种子不要求必然不同，但生成器+求解器都由种子决定，这里记录行为
    assert all(isinstance(v, float) for v in results)
