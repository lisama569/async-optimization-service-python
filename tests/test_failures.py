"""不可行 / 无解 / 非法输入三类负向结果必须分别识别。"""

from __future__ import annotations

from conftest import poll


def _submit_instance(client, items, resources, max_attempts=200, seed=0):
    return client.post(
        "/api/tasks",
        json={
            "instance": {"items": items, "resources": resources},
            "max_attempts": max_attempts,
            "seed": seed,
        },
    ).json()["task_id"]


def test_infeasible_single_item_too_big(client):
    tid = _submit_instance(
        client,
        items=[{"item_id": "big", "demand": 9, "value": 1, "mandatory": True}],
        resources=[{"resource_id": "r", "capacity": 8}],
    )
    poll(client, tid)
    data = client.get(f"/api/tasks/{tid}/result").json()
    assert data["status"] == "failed"
    assert data["failure_code"] == "infeasible"
    assert "超过任意单一资源容量" in data["failure_reason"]


def test_infeasible_total_demand_exceeds_total_capacity(client):
    tid = _submit_instance(
        client,
        items=[
            {"item_id": "a", "demand": 5, "value": 1, "mandatory": True},
            {"item_id": "b", "demand": 5, "value": 1, "mandatory": True},
        ],
        resources=[
            {"resource_id": "r1", "capacity": 8},
            {"resource_id": "r2", "capacity": 1},
        ],
    )
    poll(client, tid)
    data = client.get(f"/api/tasks/{tid}/result").json()
    assert data["failure_code"] == "infeasible"
    assert "总需求" in data["failure_reason"]


def test_unsolvable_distinct_from_infeasible(client):
    # 6,5,5 与容量 8,8：单件可装下、总量也够，但无解
    tid = _submit_instance(
        client,
        items=[
            {"item_id": "a", "demand": 6, "value": 1, "mandatory": True},
            {"item_id": "b", "demand": 5, "value": 1, "mandatory": True},
            {"item_id": "c", "demand": 5, "value": 1, "mandatory": True},
        ],
        resources=[
            {"resource_id": "r1", "capacity": 8},
            {"resource_id": "r2", "capacity": 8},
        ],
        max_attempts=50,
    )
    poll(client, tid)
    data = client.get(f"/api/tasks/{tid}/result").json()
    assert data["status"] == "failed"
    assert data["failure_code"] == "unsolvable", data
    assert "无法构造可行装载" in data["failure_reason"]


def test_invalid_input_instance_and_generate_together(client):
    resp = client.post(
        "/api/tasks",
        json={
            "instance": {"items": [], "resources": []},
            "generate": {"num_items": 3},
        },
    )
    assert resp.status_code == 400
    assert "不能同时提供" in resp.json()["detail"]


def test_invalid_input_duplicate_item_id(client):
    resp = client.post(
        "/api/tasks",
        json={
            "instance": {
                "items": [
                    {"item_id": "x", "demand": 1, "value": 1},
                    {"item_id": "x", "demand": 2, "value": 2},
                ],
                "resources": [{"resource_id": "r", "capacity": 9}],
            }
        },
    )
    assert resp.status_code == 400


def test_invalid_input_duplicate_resource_id(client):
    resp = client.post(
        "/api/tasks",
        json={
            "instance": {
                "items": [],
                "resources": [
                    {"resource_id": "r", "capacity": 1},
                    {"resource_id": "r", "capacity": 2},
                ],
            }
        },
    )
    assert resp.status_code == 400


def test_pydantic_field_validation_rejected(client):
    # demand 必须 > 0
    resp = client.post(
        "/api/tasks",
        json={
            "instance": {
                "items": [{"item_id": "x", "demand": -1, "value": 1}],
                "resources": [{"resource_id": "r", "capacity": 1}],
            }
        },
    )
    assert resp.status_code == 422


def test_optional_items_can_be_dropped_and_still_succeed(client):
    # 非必选物品太多装不下时丢弃，任务仍成功，unassigned_items 明确列出
    tid = _submit_instance(
        client,
        items=[
            {"item_id": "a", "demand": 2, "value": 10, "mandatory": True},
            {"item_id": "b", "demand": 9, "value": 1, "mandatory": False},
        ],
        resources=[{"resource_id": "r", "capacity": 5}],
    )
    poll(client, tid)
    data = client.get(f"/api/tasks/{tid}/result").json()
    assert data["status"] == "succeeded"
    assert data["result"]["unassigned_items"] == ["b"]
