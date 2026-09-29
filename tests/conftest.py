"""公共 fixtures：每个测试使用独立的临时任务存储与 TestClient。"""

from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

# 保证无论以何种工作目录启动 pytest，项目根目录都在 sys.path 上
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def client(tmp_path, monkeypatch):
    store_path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASK_STORE_PATH", str(store_path))
    monkeypatch.setenv("SOLVER_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    # 环境变量在 create_app() 时读取；每次构造独立应用，不做模块级 reload，
    # 这样测试中对 app.manager.solve 的 monkeypatch 与运行时是同一模块对象
    from app import create_app

    application = create_app()

    with TestClient(application) as c:
        c.store_path = store_path
        yield c


def poll(client, task_id, expect_terminal=True, timeout=10.0):
    """轮询任务直到进入终态（或显式指定的状态），返回最终状态响应 JSON。"""
    import time

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/tasks/{task_id}").json()
        if not expect_terminal or last["status"] in (
            "succeeded",
            "failed",
            "cancelled",
        ):
            return last
        time.sleep(0.02)
    raise AssertionError(f"任务未在超时内结束，最后状态={last}")
