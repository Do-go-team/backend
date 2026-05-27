from django.test import SimpleTestCase, TestCase

from common.validators import validate_http_url


class ExceptionHandlerLoggingTests(TestCase):
    """응답은 정적, 서버 로그는 진단 정보 남기는 정책 검증.

    XSS reflection 방어 (응답에 사용자 입력 노출 X) + 디버깅 가능성 (로그에 보존)
    양립 확인. assertLogs 로 logger 출력만 캡처해서 검증.
    """

    def test_validation_error_logs_errors_but_response_is_static(self):
        # invalid signup body — 인증 토큰 누락으로 422
        with self.assertLogs("common.handlers", level="WARNING") as captured:
            response = self.client.post(
                "/api/v1/users/signup",
                data={"email": "x@example.com"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        # 응답은 정적 envelope — 사용자 입력 reflection X
        self.assertEqual(body["code"], "INVALID_PARAMETER")
        self.assertEqual(body["message"], "요청 형식이 올바르지 않습니다.")
        # 로그에는 어떤 path 에서 어떤 errors 가 났는지 남음
        log_output = "\n".join(captured.output)
        self.assertIn("Validation failed", log_output)
        self.assertIn("/api/v1/users/signup", log_output)

    def test_business_exception_logs_code_but_response_is_static(self):
        # 잘못된 인증 코드로 INVALID_CODE 400 트리거
        from django.core.cache import cache

        cache.set("verify:code:e@example.com", "111111", timeout=300)
        with self.assertLogs("common.handlers", level="INFO") as captured:
            response = self.client.post(
                "/api/v1/users/email/verify",
                data={"email": "e@example.com", "code": "999999"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], "INVALID_CODE")
        log_output = "\n".join(captured.output)
        self.assertIn("Business exception", log_output)
        self.assertIn("INVALID_CODE", log_output)


class HttpUrlValidatorTests(SimpleTestCase):
    """XSS 1차 방어 — javascript: / data: 등 비-http scheme 차단."""

    def test_none_passes(self):
        self.assertIsNone(validate_http_url(None))

    def test_empty_string_normalized_to_none(self):
        # Swagger UI 가 빈 input 을 "" 로 직렬화해 보내는 케이스 — 선택 필드 미입력으로 흡수.
        self.assertIsNone(validate_http_url(""))

    def test_whitespace_only_normalized_to_none(self):
        self.assertIsNone(validate_http_url("   "))

    def test_http_passes(self):
        url = "http://example.com/image.png"
        self.assertEqual(validate_http_url(url), url)

    def test_https_passes(self):
        url = "https://cdn.example.com/img.jpg"
        self.assertEqual(validate_http_url(url), url)

    def test_javascript_scheme_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_url("javascript:alert(1)")

    def test_data_scheme_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_url("data:text/html,<script>alert(1)</script>")

    def test_no_scheme_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_url("example.com/image.png")

    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_url("/static/image.png")
