"""4xx 응답을 Sentry 로 명시 캡쳐하는 Django middleware.

5xx 는 DjangoIntegration 이 traceback 포함 자동 캡쳐. 4xx 는 정상 응답이라
자동 캡쳐 X — 사용자 요구사항 (4xx 알림) 충족을 위해 본 middleware 가
`capture_message` 로 Sentry 에 전송. status/method/path 별로 자동 grouping.

캡쳐 대상에서 401/404/429 는 의도된 routine 응답으로 제외 (운영 노이즈 컷).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sentry_sdk
from django.conf import settings

from .sanitize import sanitize

logger = logging.getLogger(__name__)


class FourXxSentryMiddleware:
    """4xx 응답을 Sentry warning event 로 캡쳐.

    동작:
        1. get_response 통과 → final HttpResponse 확보
        2. status_code 가 캡쳐 대상이면 push_scope 로 tag/context 첨부
        3. sentry_sdk.capture_message(level="warning")
        4. 응답은 그대로 반환 — 클라이언트 영향 0
    """

    # 의도된 routine 응답 — 캡쳐 시 노이즈 증가, dedup 도 위치당 1건씩 쌓여 quota
    # 부담. 디버깅 가치 < 비용 → 제외.
    EXCLUDED_STATUSES = frozenset({401, 404, 429})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not self._should_capture(response):
            return response

        try:
            self._capture(request, response)
        except Exception:
            # Sentry 캡쳐 자체가 실패해도 응답은 정상 진행 (가시성 위한 부가 기능).
            logger.exception("FourXxSentryMiddleware: capture failed silently")

        return response

    # ────────────────────── helpers ──────────────────────

    def _should_capture(self, response) -> bool:
        if not getattr(settings, "SENTRY_FOURXX_CAPTURE_ENABLED", False):
            return False
        status = response.status_code
        if status in self.EXCLUDED_STATUSES:
            return False
        return 400 <= status < 500

    def _capture(self, request, response) -> None:
        status = response.status_code
        method = request.method
        path = request.path

        # sentry-sdk 2.x: push_scope 는 deprecated → new_scope 사용 (격리된 임시 scope).
        with sentry_sdk.new_scope() as scope:
            # tags — Sentry UI 의 group/filter 단위. 검색·dedup 모두 tag 기준.
            scope.set_tag("http.status", str(status))
            scope.set_tag("http.method", method)
            scope.set_tag("http.path", path)
            # fingerprint 는 2.x 에서 set_fingerprint() 메서드 없음 → 속성 직접 할당.
            scope.fingerprint = ["fourxx", str(status), method, path]

            scope.set_context(
                "request_info",
                {
                    "method": method,
                    "path": path,
                    "user": self._user_repr(request),
                    "ip": self._client_ip(request),
                    "query": sanitize(dict(request.GET.lists())),
                    "body": sanitize(self._parse_request_body(request)),
                },
            )
            scope.set_context(
                "response_info",
                {
                    "status": status,
                    "detail": self._parse_response_body(response),
                },
            )

            sentry_sdk.capture_message(
                f"HTTP {status} {method} {path}",
                level="warning",
            )

    @staticmethod
    def _user_repr(request) -> str:
        user = getattr(request, "user", None)
        if user is None:
            return "anonymous"
        # 익명 사용자는 id 가 None — username/email 도 보통 비어 있어 그대로 표시.
        is_auth = getattr(user, "is_authenticated", False)
        if not is_auth:
            return "anonymous"
        return f"id={getattr(user, 'id', None)}"

    @staticmethod
    def _client_ip(request) -> str:
        # nginx 가 X-Forwarded-For 로 원래 IP 전달. 가장 왼쪽이 client.
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")

    @staticmethod
    def _parse_request_body(request) -> Any:
        # Content-Type 이 json 인 경우만 본문 첨부. multipart (파일 업로드) 는
        # 바이트 크기와 PII 위험 모두 커서 메타 (key 목록) 만 첨부.
        content_type = request.META.get("CONTENT_TYPE", "")
        if "multipart/form-data" in content_type:
            return {"_multipart_keys": list(request.POST.keys())}
        if "application/json" not in content_type:
            return None
        try:
            raw = request.body
            if not raw:
                return None
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return {"_parse_error": "non-json body"}

    @staticmethod
    def _parse_response_body(response) -> Any:
        # 표준 envelope ({success, code, message}) JSON 만 첨부. streaming/binary
        # 는 skip.
        try:
            content_type = response.get("Content-Type", "")
            if "application/json" not in content_type:
                return None
            raw = getattr(response, "content", b"")
            if not raw:
                return None
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
