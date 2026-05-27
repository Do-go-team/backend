from __future__ import annotations

from dataclasses import dataclass

AI_CELERY_QUEUE = "ai"


@dataclass
class _StubAsyncResult:
    task_name: str

    @property
    def id(self) -> str:
        return "submission-stub-task"

    def get(self, timeout: int | None = None):
        if self.task_name == "ai_app.tasks.parse_floorplan":
            return {
                "image_width": 1000,
                "image_height": 1000,
                "fixtures": [],
            }
        return {
            "status": "QUEUED",
            "message": "Sanitized submission build: worker logic excluded.",
        }


class _StubCeleryApp:
    def send_task(self, name: str, args=None, kwargs=None, queue: str | None = None):
        return _StubAsyncResult(task_name=name)


ai_celery_app = _StubCeleryApp()
