"""异步优化求解服务：FastAPI 应用装配。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .manager import TaskManager
from .routes import router
from .store import TaskStore


def create_app() -> FastAPI:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    store_path = os.environ.get("TASK_STORE_PATH", "tasks.json")
    max_concurrency = int(os.environ.get("SOLVER_MAX_CONCURRENCY", "2"))
    manager = TaskManager(store=TaskStore(store_path), max_concurrency=max_concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.startup()
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title="Async Optimization Service", lifespan=lifespan)
    app.state.manager = manager
    app.include_router(router)

    @app.get("/")
    def read_root():
        return {"service": "async-optimization-service", "status": "ok"}

    return app


app = create_app()
