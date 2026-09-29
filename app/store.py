"""任务持久化存储（本地 JSON 文件，原子写入）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading

from .models import TaskRecord


class TaskStore:
    """以整表 JSON 形式持久化任务记录。

    写入采用临时文件 + os.replace 原子替换，避免进程崩溃留下半截文件；
    锁保证同一进程内多协程/线程串行化落盘。
    """

    def __init__(self, path: str = "tasks.json") -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, TaskRecord]:
        with self._lock:
            if not os.path.exists(self.path):
                return {}
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            records: dict[str, TaskRecord] = {}
            for task_id, data in raw.items():
                records[task_id] = TaskRecord.model_validate(data)
            return records

    def save(self, records: dict[str, TaskRecord]) -> None:
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            payload = {
                task_id: rec.model_dump(mode="json")
                for task_id, rec in sorted(records.items())
            }
            fd, tmp_path = tempfile.mkstemp(
                dir=directory, prefix=".tasks-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
