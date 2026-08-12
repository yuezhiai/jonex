"""TaskRepository — JSON file-based task persistence."""

import json
import logging
import os
from pathlib import Path
from raganything.service.models import TaskInfo

logger = logging.getLogger(__name__)


class TaskRepository:
    """Persist TaskInfo objects as JSON files under base_dir/{tenant}/{kb}/tasks/."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    def _task_path(self, tenant_id: str, kb_id: str, task_id: str) -> Path:
        kb = kb_id or "default_kb"
        return Path(self.base_dir) / tenant_id / kb / "tasks" / f"{task_id}.json"

    def save(self, task: TaskInfo) -> None:
        path = self._task_path(task.tenant_id, task.kb_id, task.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                task.model_dump(mode="json"), f, ensure_ascii=False, default=str,
            )
        os.replace(tmp, path)

    def load_all(self) -> dict[str, TaskInfo]:
        """扫描并反序列化全部任务 JSON。

        [jonex] P0-C：原实现对反序列化失败静默 ``continue``，任务文件在磁盘上
        仍是 created，但进程内 ``_tasks`` 里没有它 —— 查询 API 直接 404，排障时
        看不出任何线索。此处改为逐文件告警并统计跳过数量。
        """
        tasks: dict[str, TaskInfo] = {}
        base = Path(self.base_dir)
        if not base.is_dir():
            return tasks
        scanned = 0
        skipped = 0
        for task_file in base.rglob("*/tasks/*.json"):
            scanned += 1
            try:
                data = json.loads(task_file.read_text(encoding="utf-8"))
                task = TaskInfo(**data)
                tasks[task.task_id] = task
            except Exception as e:
                skipped += 1
                logger.warning("任务文件加载失败，已跳过: %s: %s", task_file, e)
        if skipped:
            logger.error(
                "任务恢复不完整: 扫描 %d 个文件，成功 %d，跳过 %d（跳过的任务将永不执行）",
                scanned, len(tasks), skipped,
            )
        else:
            logger.info("任务恢复: 扫描 %d 个文件，成功加载 %d", scanned, len(tasks))
        return tasks

    def delete(self, task_id: str, tenant_id: str, kb_id: str) -> None:
        path = self._task_path(tenant_id, kb_id, task_id)
        if path.exists():
            path.unlink()
