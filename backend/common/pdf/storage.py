"""PDF export 산출물 저장/URL helper.

MEDIA 임시 단계: `backend/media/exports/{file_name}` 으로 저장 + Django MEDIA_URL 기반
다운로드 URL 발급. S3 도입 시 본 모듈의 `save_pdf_and_get_url` 한 함수만 swap
하면 endpoint 응답 schema 무변경.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

EXPORTS_SUBDIR = "exports"
DOWNLOAD_TTL_SECONDS = 60 * 60  # 1h. spec 의 expires_at 정책.


def _exports_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / EXPORTS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_pdf_and_get_url(
    *, pdf_bytes: bytes, file_name: str, base_url: str | None = None
) -> tuple[str, str, datetime]:
    """PDF 바이트를 MEDIA/exports/ 에 쓰고 (file_id, download_url, expires_at) 반환.

    - file_id: 호출 식별용 UUID4 (spec 의 `file_id` 필드)
    - download_url: `{base_url 또는 MEDIA_URL}/exports/{file_name}` — FE 가 즉시 다운로드
    - expires_at: 발급 시점 + DOWNLOAD_TTL_SECONDS. MEDIA 단계엔 자동 cleanup X
      (S3 lifecycle 도입 시 자동 삭제로 대체)
    """
    out_path = _exports_dir() / file_name
    out_path.write_bytes(pdf_bytes)

    media_url = (settings.MEDIA_URL or "/media/").rstrip("/")
    if base_url:
        download_url = f"{base_url.rstrip('/')}{media_url}/{EXPORTS_SUBDIR}/{file_name}"
    else:
        download_url = f"{media_url}/{EXPORTS_SUBDIR}/{file_name}"

    file_id = f"pdf_{uuid.uuid4().hex[:16]}"
    # KST 기준 — settings.TIME_ZONE="Asia/Seoul" 정합. timezone.localtime 으로
    # UTC → KST 변환 → 응답 직렬화 시 `+09:00` offset 형태.
    expires_at = timezone.localtime(
        timezone.now() + timedelta(seconds=DOWNLOAD_TTL_SECONDS)
    )
    return file_id, download_url, expires_at


def to_buffer() -> io.BytesIO:
    """reportlab Canvas 가 쓸 in-memory 버퍼."""
    return io.BytesIO()
