from __future__ import annotations

import posixpath
import uuid
from pathlib import Path
from urllib.parse import quote

from common.exceptions import BusinessException


def build_detection_source_key(task_id: int, filename: str) -> str:
    safe_name = Path(filename or "source.jpg").name
    return f"tmp/detections/{task_id}/source/{uuid.uuid4().hex}_{safe_name}"


def build_detection_thumbnail_key(task_id: int, slot: int) -> str:
    return f"detections/{task_id}/thumb_{slot}.png"


def upload_fileobj_to_s3(file_obj, key: str, content_type: str | None = None) -> str:
    """Sanitized submission build: returns key without real S3 upload."""
    return key


def generate_presigned_get_url(key: str, expires_in: int | None = None) -> str:
    normalized = quote(posixpath.normpath(key).lstrip("/"), safe="/")
    return f"https://submission.invalid/s3/get/{normalized}"


def generate_presigned_put_url(
    key: str, content_type: str, expires_in: int | None = None
) -> str:
    normalized = quote(posixpath.normpath(key).lstrip("/"), safe="/")
    return f"https://submission.invalid/s3/put/{normalized}"


def build_public_or_presigned_url(key: str) -> str:
    if not key:
        raise BusinessException(
            "INVALID_PARAMETER",
            "S3 object key is required.",
            status=422,
        )
    return generate_presigned_get_url(key)
