from __future__ import annotations

import logging
import secrets
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from assets_3d.models import AssetGenerationTask
from common.exceptions import BusinessException
from common.storage.s3_presign import (
    build_detection_source_key,
    build_public_or_presigned_url,
)
from fixtures.models import FixtureVersion
from fixtures.services import get_visible_fixture
from stores.models import StoreMember
from stores.services import get_member_store

from .detection_schemas import (
    ProductDetectionCompleteIn,
    ProductDetectionFailIn,
    ProductDetectionGenerate3DIn,
)
from .models import ProductDetectionItem, ProductDetectionTask

logger = logging.getLogger(__name__)

DETECTION_TASK_NOT_FOUND = BusinessException(
    "DETECTION_TASK_NOT_FOUND",
    "Detection task does not exist or you do not have access to it.",
    status=404,
)
INVALID_CALLBACK_TOKEN = BusinessException(
    "INVALID_CALLBACK_TOKEN",
    "Invalid callback_token.",
    status=403,
)
INVALID_DETECTION_ITEM_IDS = BusinessException(
    "INVALID_DETECTION_ITEM_IDS",
    "selected_item_ids contains one or more invalid detection item ids.",
    status=422,
)
DETECTION_ITEM_NOT_FOUND = BusinessException(
    "DETECTION_ITEM_NOT_FOUND",
    "Detection item does not exist or does not belong to this detection task.",
    status=404,
)
INVALID_DETECTION_ITEM_STATE = BusinessException(
    "INVALID_DETECTION_ITEM_STATE",
    "One or more detection items are not in a state that allows this operation.",
    status=409,
)

_MAX_ERROR_MESSAGE_LEN = 2000


def _build_thumbnail_public_url(thumbnail_key: str) -> str | None:
    if not thumbnail_key:
        return None
    return build_public_or_presigned_url(thumbnail_key)


def _serialize_detection_item(item: ProductDetectionItem) -> dict:
    return {
        "detection_item_id": item.id,
        "slot": item.slot,
        "thumbnail_key": item.thumbnail_key,
        "thumbnail_url": _build_thumbnail_public_url(item.thumbnail_key),
        "relative_position": {
            "x": float(item.relative_position_x),
            "y": float(item.relative_position_y),
        },
        "relative_size": {
            "width": float(item.relative_size_width),
            "height": float(item.relative_size_height),
        },
        "status": item.status,
        "asset_generation_status": item.asset_generation_status,
        "asset_generation_task_id": item.asset_generation_task_id,
        "asset_3d_id": item.asset_3d_id,
        "asset_3d_url": item.asset_3d.model_url if item.asset_3d_id else None,
        "confidence": float(item.confidence) if item.confidence is not None else None,
        "bbox_xyxy": item.bbox_xyxy,
    }


def _serialize_detection_task(
    task: ProductDetectionTask, *, include_rejected: bool = True
) -> dict:
    items = list(task.items.all())
    if not include_rejected:
        items = [
            item
            for item in items
            if item.status != ProductDetectionItem.Status.REJECTED
        ]

    return {
        "detection_task_id": task.id,
        "status": task.status,
        "error_message": task.error_message,
        "items": [_serialize_detection_item(item) for item in items],
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


def _get_task_queryset():
    return ProductDetectionTask.objects.select_related(
        "store",
        "fixture",
        "fixture_version",
        "requested_by",
    ).prefetch_related(
        Prefetch(
            "items",
            queryset=ProductDetectionItem.objects.select_related(
                "asset_generation_task",
                "asset_3d",
            ).order_by("slot", "id"),
        )
    )


def _assert_task_accessible_by_user(user, task: ProductDetectionTask) -> None:
    if task.requested_by_id == user.id:
        return
    if (
        task.store_id
        and StoreMember.objects.filter(
            store_id=task.store_id,
            user=user,
            store__deleted_at__isnull=True,
        ).exists()
    ):
        return
    raise DETECTION_TASK_NOT_FOUND


def _is_rejectable_detection_item(item: ProductDetectionItem) -> bool:
    if item.status == ProductDetectionItem.Status.REJECTED:
        return True
    if item.status == ProductDetectionItem.Status.REGISTERED:
        return False
    if item.asset_3d_id is not None:
        return False

    blocked_asset_states = {
        ProductDetectionItem.AssetGenerationStatus.PENDING,
        ProductDetectionItem.AssetGenerationStatus.PROCESSING,
        ProductDetectionItem.AssetGenerationStatus.COMPLETED,
    }
    if item.asset_generation_status in blocked_asset_states:
        return False

    if item.asset_generation_task_id is not None:
        task = item.asset_generation_task
        if task is None:
            task = AssetGenerationTask.objects.filter(
                id=item.asset_generation_task_id
            ).first()
        if task and task.status in {
            AssetGenerationTask.Status.PENDING,
            AssetGenerationTask.Status.PROCESSING,
            AssetGenerationTask.Status.COMPLETED,
        }:
            return False

    if (
        item.asset_generation_status
        == ProductDetectionItem.AssetGenerationStatus.FAILED
    ):
        return True

    return (
        item.status == ProductDetectionItem.Status.DETECTED
        and item.asset_generation_status
        == ProductDetectionItem.AssetGenerationStatus.NOT_REQUESTED
    )


@transaction.atomic
def create_detection_task(
    *,
    user,
    fixture_id: int,
    image_file,
    store_id: int | None = None,
    fixture_version_id: int | None = None,
    callback_base_url: str | None = None,
) -> dict:
    """Sanitized submission stub.

    The production implementation includes presigned upload slots, worker payload
    composition, and queue dispatch. Those internals are excluded here.
    """
    fixture = get_visible_fixture(user, fixture_id)

    store = None
    if store_id is not None:
        store, _ = get_member_store(user, store_id)

    fixture_version = None
    if fixture_version_id is not None:
        fixture_version = (
            FixtureVersion.objects.alive()
            .filter(
                id=fixture_version_id,
                fixture_master_id=fixture.id,
            )
            .first()
        )
        if fixture_version is None:
            raise BusinessException(
                "VERSION_NOT_FOUND",
                "Fixture version does not exist or is not accessible.",
                status=404,
            )

    callback_token = secrets.token_urlsafe(16)
    task = ProductDetectionTask.objects.create(
        store=store,
        fixture=fixture,
        fixture_version=fixture_version,
        requested_by=user,
        status=ProductDetectionTask.Status.PENDING,
        callback_token=callback_token,
        source_image_key=build_detection_source_key(
            0,
            getattr(image_file, "name", "source.jpg"),
        ),
    )

    task.source_image_key = build_detection_source_key(
        task.id,
        getattr(image_file, "name", "source.jpg"),
    )
    task.save(update_fields=["source_image_key", "updated_at"])

    return {
        "detection_task_id": task.id,
        "status": task.status,
    }


def get_detection_task_for_user(*, user, task_id: int) -> ProductDetectionTask:
    task = _get_task_queryset().filter(id=task_id).first()
    if task is None:
        raise DETECTION_TASK_NOT_FOUND
    _assert_task_accessible_by_user(user, task)
    return task


@transaction.atomic
def complete_detection_task(
    *, task_id: int, payload: ProductDetectionCompleteIn
) -> dict:
    task = ProductDetectionTask.objects.filter(id=task_id).first()
    if task is None:
        raise DETECTION_TASK_NOT_FOUND

    if task.callback_token != payload.callback_token:
        raise INVALID_CALLBACK_TOKEN

    now = timezone.now()

    for row in payload.items:
        ProductDetectionItem.objects.update_or_create(
            task=task,
            slot=row.slot,
            defaults={
                "thumbnail_key": row.thumbnail_key,
                "relative_position_x": Decimal(str(row.relative_position_x)),
                "relative_position_y": Decimal(str(row.relative_position_y)),
                "relative_size_width": Decimal(str(row.relative_size_width)),
                "relative_size_height": Decimal(str(row.relative_size_height)),
                "status": ProductDetectionItem.Status.DETECTED,
                "asset_generation_status": ProductDetectionItem.AssetGenerationStatus.NOT_REQUESTED,
                "confidence": (
                    Decimal(str(row.confidence)) if row.confidence is not None else None
                ),
                "bbox_xyxy": row.bbox_xyxy,
            },
        )

    task.status = ProductDetectionTask.Status.COMPLETED
    task.error_message = None
    task.started_at = task.started_at or now
    task.finished_at = now
    task.save(
        update_fields=[
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "updated_at",
        ]
    )

    task = _get_task_queryset().get(id=task.id)
    return _serialize_detection_task(task)


@transaction.atomic
def fail_detection_task(*, task_id: int, payload: ProductDetectionFailIn) -> dict:
    task = ProductDetectionTask.objects.filter(id=task_id).first()
    if task is None:
        raise DETECTION_TASK_NOT_FOUND

    if task.callback_token != payload.callback_token:
        raise INVALID_CALLBACK_TOKEN

    now = timezone.now()
    task.status = ProductDetectionTask.Status.FAILED
    task.error_message = payload.error_message
    task.started_at = task.started_at or now
    task.finished_at = now
    task.save(
        update_fields=[
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "updated_at",
        ]
    )

    task = _get_task_queryset().get(id=task.id)
    return _serialize_detection_task(task)


@transaction.atomic
def generate_3d_for_detection_items(
    *,
    user,
    task_id: int,
    payload: ProductDetectionGenerate3DIn,
) -> dict:
    """Sanitized submission stub.

    The production 3D worker task creation and queue internals are intentionally
    excluded. This function preserves request validation and response shape.
    """
    task = get_detection_task_for_user(user=user, task_id=task_id)

    selected_ids = sorted(set(payload.selected_item_ids))
    if not selected_ids:
        raise BusinessException(
            "INVALID_PARAMETER",
            "selected_item_ids must include at least one id.",
            status=422,
        )

    all_items = list(
        ProductDetectionItem.objects.select_related("asset_generation_task")
        .filter(task=task)
        .order_by("slot", "id")
    )
    all_item_ids = {item.id for item in all_items}
    invalid_ids = sorted(set(selected_ids) - all_item_ids)
    if invalid_ids:
        raise INVALID_DETECTION_ITEM_IDS

    selected_set = set(selected_ids)
    selected_items = [item for item in all_items if item.id in selected_set]

    blocked_items = [
        item.id
        for item in selected_items
        if item.status == ProductDetectionItem.Status.REGISTERED
    ]
    if blocked_items:
        raise INVALID_DETECTION_ITEM_STATE

    created_task_count = 0
    now = timezone.now()
    confirmed_selected_ids: list[int] = []
    asset_generation_task_ids: list[int] = []

    for item in selected_items:
        item.status = ProductDetectionItem.Status.SELECTED
        item.asset_error_message = None
        item.save(
            update_fields=[
                "status",
                "asset_error_message",
                "updated_at",
            ]
        )
        confirmed_selected_ids.append(item.id)
        if item.asset_generation_task_id is not None:
            asset_generation_task_ids.append(item.asset_generation_task_id)

    rejected_item_ids: list[int] = []
    skipped_reject_item_ids: list[int] = []
    if payload.reject_unselected:
        reject_ids: list[int] = []
        update_reject_ids: list[int] = []
        for item in all_items:
            if item.id in selected_set:
                continue
            if _is_rejectable_detection_item(item):
                reject_ids.append(item.id)
                if item.status != ProductDetectionItem.Status.REJECTED:
                    update_reject_ids.append(item.id)
            else:
                skipped_reject_item_ids.append(item.id)

        if update_reject_ids:
            ProductDetectionItem.objects.filter(id__in=update_reject_ids).update(
                status=ProductDetectionItem.Status.REJECTED,
                updated_at=now,
            )
        rejected_item_ids = reject_ids

    return {
        "detection_task_id": task.id,
        "created_task_count": created_task_count,
        "selected_item_ids": confirmed_selected_ids,
        "rejected_item_ids": rejected_item_ids,
        "skipped_reject_item_ids": skipped_reject_item_ids,
        "asset_generation_task_ids": asset_generation_task_ids,
    }


@transaction.atomic
def reject_detection_item(*, user, task_id: int, item_id: int) -> dict:
    task = get_detection_task_for_user(user=user, task_id=task_id)
    item = (
        ProductDetectionItem.objects.select_related("asset_generation_task")
        .filter(task=task, id=item_id)
        .first()
    )
    if item is None:
        raise DETECTION_ITEM_NOT_FOUND

    if (
        item.status != ProductDetectionItem.Status.REJECTED
        and not _is_rejectable_detection_item(item)
    ):
        raise INVALID_DETECTION_ITEM_STATE

    if item.status != ProductDetectionItem.Status.REJECTED:
        item.status = ProductDetectionItem.Status.REJECTED
        item.save(update_fields=["status", "updated_at"])

    return {
        "detection_task_id": task.id,
        "detection_item_id": item.id,
        "status": ProductDetectionItem.Status.REJECTED,
    }


def get_detection_task_detail(
    *, user, task_id: int, include_rejected: bool = True
) -> dict:
    task = get_detection_task_for_user(user=user, task_id=task_id)
    return _serialize_detection_task(task, include_rejected=include_rejected)


def sync_detection_item_asset_status(
    task,
    *,
    status: str,
    asset_3d=None,
    error_message: str | None = None,
) -> None:
    if task.target_type != AssetGenerationTask.TargetType.DETECTION_ITEM:
        return

    item = ProductDetectionItem.objects.filter(id=task.target_id).first()
    if item is None:
        logger.warning(
            "ProductDetectionItem missing for asset task sync",
            extra={
                "asset_task_id": task.id,
                "detection_item_id": task.target_id,
                "status": status,
            },
        )
        return

    item.asset_generation_status = status
    update_fields = ["asset_generation_status", "updated_at"]

    if status == ProductDetectionItem.AssetGenerationStatus.PROCESSING:
        item.asset_error_message = None
        update_fields.append("asset_error_message")
    elif status == ProductDetectionItem.AssetGenerationStatus.COMPLETED:
        if asset_3d is not None:
            item.asset_3d = asset_3d
            update_fields.append("asset_3d")
        item.asset_error_message = None
        update_fields.append("asset_error_message")
    elif status == ProductDetectionItem.AssetGenerationStatus.FAILED:
        item.asset_error_message = (error_message or "")[:_MAX_ERROR_MESSAGE_LEN]
        update_fields.append("asset_error_message")

    item.save(update_fields=update_fields)
