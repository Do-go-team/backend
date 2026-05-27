import os

from celery import Celery


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


broker_url = os.getenv("AI_CELERY_BROKER_URL", "redis://redis:6379/0").strip()
result_backend = os.getenv("AI_CELERY_RESULT_BACKEND", broker_url).strip()
default_queue = os.getenv("CELERY_TASK_DEFAULT_QUEUE", "default").strip()
ai_queue = os.getenv("CELERY_AI_QUEUE", "ai").strip()
task_time_limit = int(os.getenv("CELERY_TASK_TIME_LIMIT", "300").strip())
task_soft_time_limit = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "270").strip())
task_acks_late = _env_bool("CELERY_TASK_ACKS_LATE", True)
task_reject_on_worker_lost = _env_bool("CELERY_TASK_REJECT_ON_WORKER_LOST", True)

app = Celery(
    "ai_app",
    broker=broker_url,
    backend=result_backend,
)

app.conf.update(
    task_default_queue=default_queue,
    task_acks_late=task_acks_late,
    task_reject_on_worker_lost=task_reject_on_worker_lost,
    task_time_limit=task_time_limit,
    task_soft_time_limit=task_soft_time_limit,
)

app.conf.task_routes = {
    "ai_app.tasks.segment_product_image": {"queue": ai_queue},
    "ai_app.tasks.parse_floorplan": {"queue": ai_queue},
}

app.autodiscover_tasks(["ai_app"])
