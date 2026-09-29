"""pytest 夹具：每个测试使用独立的临时文件 TaskStore，由 app 的 lifespan 启动。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as main_mod
from app.store import TaskStore

_ORIGINAL_BUILD = main_mod._build_store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[TaskStore, TestClient]]:
    s = TaskStore(max_concurrency=2, state_path=tmp_path / "tasks.json")
    # 让 lifespan 中的 _build_store 返回本测试专用 store，避免双调度器
    main_mod._build_store = lambda: s
    with TestClient(main_mod.app) as c:
        yield s, c
    main_mod._build_store = _ORIGINAL_BUILD


@pytest.fixture
def client(store: tuple[TaskStore, TestClient]) -> TestClient:
    return store[1]


@pytest.fixture
def task_store(store: tuple[TaskStore, TestClient]) -> TaskStore:
    return store[0]
