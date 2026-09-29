"""非法输入、不可行、无解三类失败的区分测试。"""

from __future__ import annotations

from tests.helpers import (
    infeasible_total_problem,
    no_solution_problem,
    oversized_problem,
    poll,
)


def _submit_raw(client, problem_dict):
    return client.post("/api/tasks", json={"problem": problem_dict})


def test_invalid_input_empty_items_400(client):
    r = _submit_raw(client, {"items": [], "resources": [{"id": "r", "capacity": 1.0}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_input"
    assert "items" in r.json()["error"]["message"]


def test_invalid_input_negative_and_duplicate_ids_400(client):
    cases = [
        ({"items": [{"id": "a", "demand": -1, "value": 1}],
          "resources": [{"id": "r", "capacity": 1}]}, "负数"),
        ({"items": [{"id": "a", "demand": 1, "value": 0}],
          "resources": [{"id": "r", "capacity": 1}]}, "零价值"),
        ({"items": [{"id": "a", "demand": 1, "value": 1},
                    {"id": "a", "demand": 1, "value": 1}],
          "resources": [{"id": "r", "capacity": 5}]}, "重复 id"),
        ({"items": [{"id": "a", "demand": 1, "value": 1}],
          "resources": []}, "空资源"),
    ]
    for payload, label in cases:
        r = _submit_raw(client, payload)
        assert r.status_code == 400, f"{label} 应判 400: {r.text}"
        assert r.json()["error"]["code"] == "invalid_input", label


def test_malformed_structural_input_422(client):
    r = _submit_raw(client, {"items": [{"id": "a", "demand": "abc", "value": 1}],
                             "resources": [{"id": "r", "capacity": 1}]})
    assert r.status_code == 422


def test_infeasible_total_capacity(client):
    r = _submit_raw(client, infeasible_total_problem().model_dump(mode="json"))
    tid = r.json()["task_id"]
    final = poll(client, tid)
    assert final["status"] == "failed"
    assert final["failure_reason"] == "infeasible"
    assert "总需求" in final["message"]

    rr = client.get(f"/api/tasks/{tid}/result")
    assert rr.status_code == 409
    assert rr.json()["error"]["code"] == "result_unavailable_failed"
    assert rr.json()["error"]["task"]["failure_reason"] == "infeasible"


def test_infeasible_oversized_item(client):
    r = _submit_raw(client, oversized_problem().model_dump(mode="json"))
    final = poll(client, r.json()["task_id"])
    assert final["status"] == "failed"
    assert final["failure_reason"] == "infeasible"
    assert "容量" in final["message"]


def test_no_solution_distinct_from_infeasible(client):
    r = _submit_raw(client, no_solution_problem().model_dump(mode="json"))
    final = poll(client, r.json()["task_id"])
    assert final["status"] == "failed"
    assert final["failure_reason"] == "no_solution", final
    assert "未能放置" in final["message"]

    rr = client.get(f"/api/tasks/{r.json()['task_id']}/result")
    assert rr.json()["error"]["code"] == "result_unavailable_failed"


def test_terminal_state_never_reverts(client):
    """失败任务状态不会因重复查询/取消而变化。"""
    r = _submit_raw(client, oversized_problem().model_dump(mode="json"))
    tid = r.json()["task_id"]
    first = poll(client, tid)
    for _ in range(3):
        client.get(f"/api/tasks/{tid}")
        client.post(f"/api/tasks/{tid}/cancel")
    again = client.get(f"/api/tasks/{tid}").json()
    assert again["status"] == first["status"] == "failed"
    assert again["failure_reason"] == "infeasible"
