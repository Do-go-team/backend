"""FourXxSentryMiddleware 단위 테스트.

sentry_sdk.capture_message 를 mock 으로 가로채서 호출 여부 / level / tag /
context 를 검증. 실제 Sentry 전송은 일어나지 않음 (DSN 미설정 + mock).
"""

from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from common.middleware import FourXxSentryMiddleware
from common.sanitize import MASK


def _make_response(status, body=None):
    from django.http import JsonResponse, HttpResponse

    if body is not None:
        return JsonResponse(body, status=status)
    return HttpResponse(status=status)


@override_settings(SENTRY_FOURXX_CAPTURE_ENABLED=True)
class FourXxSentryMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _run(self, request, status, body=None):
        def get_response(_req):
            return _make_response(status, body)

        mw = FourXxSentryMiddleware(get_response)
        return mw(request)

    # ── _should_capture 정책 ───────────────────────────────────────

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_captures_422(self, mock_capture):
        request = self.factory.post(
            "/api/v1/users/signup",
            data='{"email": "x"}',
            content_type="application/json",
        )
        response = self._run(request, 422, {"code": "INVALID_PARAMETER"})
        self.assertEqual(response.status_code, 422)
        mock_capture.assert_called_once()
        (msg,) = mock_capture.call_args.args
        self.assertIn("HTTP 422", msg)
        self.assertIn("POST", msg)
        self.assertIn("/api/v1/users/signup", msg)
        self.assertEqual(mock_capture.call_args.kwargs.get("level"), "warning")

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_skips_401(self, mock_capture):
        request = self.factory.get("/api/v1/users/me")
        self._run(request, 401)
        mock_capture.assert_not_called()

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_skips_404(self, mock_capture):
        request = self.factory.get("/api/v1/nonexistent")
        self._run(request, 404)
        mock_capture.assert_not_called()

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_skips_429(self, mock_capture):
        request = self.factory.get("/api/v1/users/email/send")
        self._run(request, 429)
        mock_capture.assert_not_called()

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_skips_2xx(self, mock_capture):
        request = self.factory.get("/api/v1/hello")
        self._run(request, 200, {"success": True})
        mock_capture.assert_not_called()

    @patch("common.middleware.sentry_sdk.capture_message")
    def test_skips_5xx(self, mock_capture):
        # 5xx 는 DjangoIntegration 자동 캡쳐 영역 — middleware 는 손대지 않음.
        request = self.factory.get("/api/v1/hello")
        self._run(request, 500, {"code": "INTERNAL_ERROR"})
        mock_capture.assert_not_called()

    @override_settings(SENTRY_FOURXX_CAPTURE_ENABLED=False)
    @patch("common.middleware.sentry_sdk.capture_message")
    def test_disabled_via_settings(self, mock_capture):
        request = self.factory.post(
            "/api/v1/users/signup",
            data="{}",
            content_type="application/json",
        )
        self._run(request, 422, {"code": "INVALID_PARAMETER"})
        mock_capture.assert_not_called()

    # ── scope 첨부 내용 ───────────────────────────────────────────

    @patch("common.middleware.sentry_sdk.new_scope")
    @patch("common.middleware.sentry_sdk.capture_message")
    def test_attaches_tags_and_fingerprint(self, mock_capture, mock_scope):
        scope = mock_scope.return_value.__enter__.return_value
        request = self.factory.post(
            "/api/v1/users/signup",
            data="{}",
            content_type="application/json",
        )
        self._run(request, 422, {"code": "INVALID_PARAMETER"})

        tag_calls = {c.args[0]: c.args[1] for c in scope.set_tag.call_args_list}
        self.assertEqual(tag_calls.get("http.status"), "422")
        self.assertEqual(tag_calls.get("http.method"), "POST")
        self.assertEqual(tag_calls.get("http.path"), "/api/v1/users/signup")
        # sentry-sdk 2.x: fingerprint 는 method 가 아니라 속성 — 직접 할당된 값 검증.
        self.assertEqual(
            scope.fingerprint,
            ["fourxx", "422", "POST", "/api/v1/users/signup"],
        )

    @patch("common.middleware.sentry_sdk.new_scope")
    @patch("common.middleware.sentry_sdk.capture_message")
    def test_masks_password_in_attached_body(self, mock_capture, mock_scope):
        scope = mock_scope.return_value.__enter__.return_value
        request = self.factory.post(
            "/api/v1/users/signup",
            data='{"email": "x@example.com", "password": "secret123"}',
            content_type="application/json",
        )
        self._run(request, 422, {"code": "INVALID_PARAMETER"})

        contexts = {c.args[0]: c.args[1] for c in scope.set_context.call_args_list}
        body = contexts["request_info"]["body"]
        self.assertEqual(body["email"], "x@example.com")
        self.assertEqual(body["password"], MASK)

    @patch("common.middleware.sentry_sdk.new_scope")
    @patch("common.middleware.sentry_sdk.capture_message")
    def test_handles_non_json_body(self, mock_capture, mock_scope):
        scope = mock_scope.return_value.__enter__.return_value
        request = self.factory.get("/api/v1/products/9999")
        self._run(request, 400, {"code": "BAD_REQUEST"})

        contexts = {c.args[0]: c.args[1] for c in scope.set_context.call_args_list}
        # GET 요청 + form content-type 아님 → body=None
        self.assertIsNone(contexts["request_info"]["body"])

    @patch("common.middleware.sentry_sdk.new_scope")
    @patch("common.middleware.sentry_sdk.capture_message")
    def test_attaches_response_detail(self, mock_capture, mock_scope):
        scope = mock_scope.return_value.__enter__.return_value
        request = self.factory.post(
            "/api/v1/stores/9999/products",
            content_type="application/json",
            data="{}",
        )
        self._run(
            request,
            403,
            {"success": False, "code": "FORBIDDEN", "message": "권한 없음"},
        )

        contexts = {c.args[0]: c.args[1] for c in scope.set_context.call_args_list}
        detail = contexts["response_info"]["detail"]
        self.assertEqual(detail["code"], "FORBIDDEN")
        self.assertEqual(contexts["response_info"]["status"], 403)

    # ── 안전성 ────────────────────────────────────────────────────

    @patch(
        "common.middleware.sentry_sdk.capture_message",
        side_effect=RuntimeError("sdk down"),
    )
    def test_capture_failure_does_not_break_response(self, mock_capture):
        request = self.factory.post(
            "/api/v1/users/signup",
            data="{}",
            content_type="application/json",
        )
        # capture 실패해도 응답은 그대로 사용자에게 전달
        response = self._run(request, 422, {"code": "INVALID_PARAMETER"})
        self.assertEqual(response.status_code, 422)
