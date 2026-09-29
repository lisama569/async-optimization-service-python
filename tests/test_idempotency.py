"""幂等提交与结果可复现测试。"""

from __future__ import annotations

from tests.helpers import feasible_problem, poll


def _submit(client, problem, key):
    body = {"idempotency_key": key, "problem": problem.model_dump(mode="json")}
    return client.post("/api/tasks", json=body)


def test_repeated_submit_same_key_same_payload_reuses_task(client):
    problem = feasible_problem(seed=42, n=4)
    r1 = _submit(client, problem, "dup-key")
    assert r1.status_code == 201 and r1.json()["reused"] is False
    first = r1.json()["task_id"]

    # 任务结束后再次提交：仍落同一任务，不重复计算
    poll(client, first)
    r2 = _submit(client, problem, "dup-key")
    assert r2.status_code == 201
    assert r2.json()["reused"] is True
    assert r2.json()["task_id"] == first


def test_same_key_different_payload_conflicts(client):
    p1 = feasible_problem(seed=1, n=3)
    p2 = feasible_problem(seed=2, n=3)
    _submit(client, p1, "shared-key")
    r = _submit(client, p2, "shared-key")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["existing_task_id"]


def test_same_seed_same_input_deterministic_result(client):
    """相同输入 + 相同 seed，两次独立任务结果一致。"""
    p = feasible_problem(seed=123, n=6)
    r1 = _submit(client, p, "det-1")
    r2 = _submit(client, p.model_copy(update={"seed": 123}), "det-2")  # 不同键 => 独立任务
    f1 = poll(client, r1.json()["task_id"])
    f2 = poll(client, r2.json()["task_id"])
    assert f1["task_id"] != f2["task_id"]
    assert f1["result"] == f2["result"]
    assert f1["result"]["objective"] == f2["result"]["objective"]


def test_different_seed_may_differ_but_inputs_equal_seed_equal_identical(client):
    p = feasible_problem(seed=0, n=6)
    ra = client.post(
        "/api/tasks",
        json={"idempotency_key": "a", "problem": p.model_dump(mode="json")},
    )
    fb = poll(client, ra.json()["task_id"])
    # 同任务重复取结果一致（不产生第二份结果）
    rr1 = client.get(f"/api/tasks/{ra.json()['task_id']}/result").json()
    rr2 = client.get(f"/api/tasks/{ra.json()['task_id']}/result").json()
    assert rr1 == rr2
    assert fb["status"] == "succeeded"
