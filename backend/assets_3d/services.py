"""Sanitized submission build of the 3D task pipeline.

Core worker internals (queue locking strategy, file pipeline, and production
retry semantics) are intentionally excluded for IP protection.
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from common.exceptions import BusinessException

from .models import Asset3D, AssetGenerationTask

_MAX_ERROR_MESSAGE_LEN = 2000
_ALLOWED_TARGET_TYPES = {choice.value for choice in AssetGenerationTask.TargetType}
_ALLOWED_FILE_FORMATS = {choice.value for choice in Asset3D.FileFormat}


def _validate_target_type(target_type: str) -> str:
    if target_type not in _ALLOWED_TARGET_TYPES:
        raise BusinessException(
            "INVALID_TARGET_TYPE",
            "Unsupported target_type.",
            status=422,
        )
    return target_type


def _validate_file_format(filename: str) -> str:
    _, ext = os.path.splitext(filename or "")
    ext_clean = ext.lstrip(".").upper()
    if ext_clean not in _ALLOWED_FILE_FORMATS:
        raise BusinessException(
            "INVALID_FILE_FORMAT",
            "Unsupported 3D file extension.",
            status=422,
        )
    return ext_clean


def _build_media_url(relative_path: str) -> str:
    media_url = (settings.MEDIA_URL or "/media/").rstrip("/") + "/"
    return media_url + relative_path.replace("\\", "/").lstrip("/")


def _serialize_status(task: AssetGenerationTask) -> dict:
    return {
        "task_id": task.id,
        "target_type": task.target_type,
        "target_id": task.target_id,
        "source_image_url": task.source_image_url,
        "status": task.status,
        "result_url": task.result_url,
        "asset_3d_id": task.asset_3d_id,
        "error_message": task.error_message,
    }


def save_asset_3d_file(
    *,
    target_type: str,
    target_id: int,
    upload_file,
) -> Asset3D:
    """Sanitized stub: creates Asset3D metadata without storing raw file bytes."""
    _validate_target_type(target_type)
    file_format = _validate_file_format(getattr(upload_file, "name", "artifact.ply"))

    ext = file_format.lower()
    file_name = f"redacted_{target_type.lower()}_{target_id}_{uuid.uuid4().hex}.{ext}"
    model_url = _build_media_url(f"assets_3d/{file_name}")

    return Asset3D.objects.create(
        target_type=target_type,
        target_id=target_id,
        file_format=file_format,
        model_url=model_url,
        file_size_bytes=getattr(upload_file, "size", None),
    )


def create_task(
    *,
    target_type: str,
    target_id: int,
    source_image_url: str,
) -> AssetGenerationTask:
    _validate_target_type(target_type)
    return AssetGenerationTask.objects.create(
        target_type=target_type,
        target_id=target_id,
        source_image_url=source_image_url,
        status=AssetGenerationTask.Status.PENDING,
    )


def get_task(task_id: int) -> AssetGenerationTask:
    task = AssetGenerationTask.objects.filter(id=task_id).first()
    if task is None:
        raise BusinessException(
            "ASSET_TASK_NOT_FOUND",
            "3D generation task not found.",
            status=404,
        )
    return task


def claim_task(*, worker_id: str) -> Optional[dict]:
    """Sanitized stub: simplified claim flow without production locking internals."""
    with transaction.atomic():
        task = (
            AssetGenerationTask.objects.filter(
                status=AssetGenerationTask.Status.PENDING
            )
            .order_by("created_at")
            .first()
        )
        if task is None:
            return None

        task.status = AssetGenerationTask.Status.PROCESSING
        task.worker_id = worker_id
        task.started_at = timezone.now()
        task.attempt_count = (task.attempt_count or 0) + 1
        task.save(
            update_fields=[
                "status",
                "worker_id",
                "started_at",
                "attempt_count",
                "updated_at",
            ]
        )

        from products.detection_services import sync_detection_item_asset_status
        from products.models import ProductDetectionItem

        sync_detection_item_asset_status(
            task,
            status=ProductDetectionItem.AssetGenerationStatus.PROCESSING,
        )

    return {
        "task_id": task.id,
        "taskId": task.id,
        "target_type": task.target_type,
        "targetType": task.target_type,
        "target_id": task.target_id,
        "targetId": task.target_id,
        "source_image_url": task.source_image_url,
        "sourceImageUrl": task.source_image_url,
        "status": task.status,
    }


def complete_task(
    *,
    task_id: int,
    worker_id: str,
    upload_file,
) -> dict:
    task = get_task(task_id)

    if task.status != AssetGenerationTask.Status.PROCESSING:
        raise BusinessException(
            "ASSET_TASK_INVALID_STATE",
            "Only PROCESSING tasks can be completed.",
            status=409,
        )

    if task.worker_id and task.worker_id != worker_id:
        raise BusinessException(
            "ASSET_TASK_WORKER_MISMATCH",
            "This task is owned by another worker.",
            status=403,
        )

    with transaction.atomic():
        asset = save_asset_3d_file(
            target_type=task.target_type,
            target_id=task.target_id,
            upload_file=upload_file,
        )

        task.status = AssetGenerationTask.Status.COMPLETED
        task.asset_3d = asset
        task.result_url = asset.model_url
        task.finished_at = timezone.now()
        task.error_message = None
        task.save(
            update_fields=[
                "status",
                "asset_3d",
                "result_url",
                "finished_at",
                "error_message",
                "updated_at",
            ]
        )

        from products.detection_services import sync_detection_item_asset_status
        from products.models import ProductDetectionItem

        sync_detection_item_asset_status(
            task,
            status=ProductDetectionItem.AssetGenerationStatus.COMPLETED,
            asset_3d=asset,
        )

    return {
        "task_id": task.id,
        "status": task.status,
        "asset_3d_id": asset.id,
        "result_url": asset.model_url,
    }


def fail_task(
    *,
    task_id: int,
    worker_id: str,
    error_message: str,
) -> dict:
    task = get_task(task_id)

    if task.worker_id and task.worker_id != worker_id:
        raise BusinessException(
            "ASSET_TASK_WORKER_MISMATCH",
            "This task is owned by another worker.",
            status=403,
        )

    truncated = (error_message or "")[:_MAX_ERROR_MESSAGE_LEN]

    with transaction.atomic():
        task.status = AssetGenerationTask.Status.FAILED
        task.error_message = truncated
        task.finished_at = timezone.now()
        task.save(
            update_fields=[
                "status",
                "error_message",
                "finished_at",
                "updated_at",
            ]
        )

        from products.detection_services import sync_detection_item_asset_status
        from products.models import ProductDetectionItem

        sync_detection_item_asset_status(
            task,
            status=ProductDetectionItem.AssetGenerationStatus.FAILED,
            error_message=truncated,
        )

    return {
        "task_id": task.id,
        "status": task.status,
    }
