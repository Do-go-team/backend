from django.test import RequestFactory, TestCase, override_settings
from ninja_jwt.token_blacklist.models import BlacklistedToken
from ninja_jwt.tokens import RefreshToken

from auth.authentication import ACCESS_COOKIE_NAME, CookieJWTAuth
from users.models import User


class CookieJWTAuthUnitTests(TestCase):
    """CookieJWTAuth 단위 — RequestFactory 로 가짜 request 만들어 __call__ 직접 호출.
    CSRF 검증은 endpoint 통합 테스트에서 별도 커버 (단위 테스트로는 SAFE method 만 사용)."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email="cookie.auth@example.com",
            password="CookieAuth1!",
            name="쿠키",
            confirmed=True,
        )
        self.auth = CookieJWTAuth()

    def _request_with_cookie(self, value):
        """GET (SAFE method) 로 가짜 request 생성. CSRF 검증 분기 회피."""
        request = self.factory.get("/")
        request.COOKIES[ACCESS_COOKIE_NAME] = value
        return request

    def _valid_access_token(self, user=None):
        return str(RefreshToken.for_user(user or self.user).access_token)

    def test_missing_cookie_returns_none(self):
        request = self.factory.get("/")
        # 쿠키 자체를 안 박음
        self.assertIsNone(self.auth(request))

    def test_empty_cookie_returns_none(self):
        request = self._request_with_cookie("")
        self.assertIsNone(self.auth(request))

    def test_malformed_token_returns_none(self):
        request = self._request_with_cookie("not-a-jwt")
        self.assertIsNone(self.auth(request))

    def test_valid_token_returns_user(self):
        request = self._request_with_cookie(self._valid_access_token())
        user = self.auth(request)
        self.assertIsNotNone(user)
        self.assertEqual(user.id, self.user.id)

    def test_soft_deleted_user_returns_none(self):
        """access 발급 후 사용자가 soft-delete 되면 그 토큰은 무효."""
        token = self._valid_access_token()
        self.user.soft_delete()
        request = self._request_with_cookie(token)
        self.assertIsNone(self.auth(request))


class CspReportOnlyHeaderTests(TestCase):
    """CSP REPORT_ONLY 모드 — Content-Security-Policy-Report-Only 헤더가 박혀있고
    enforce (Content-Security-Policy) 헤더는 박혀있지 않음을 확인."""

    def test_report_only_header_present(self):
        response = self.client.get("/api/v1/docs")
        self.assertIn("Content-Security-Policy-Report-Only", response.headers)
        # enforce 모드는 아직 켜지 않음 — 별도 후속 티켓
        self.assertNotIn("Content-Security-Policy", response.headers)

    def test_policy_includes_default_self(self):
        response = self.client.get("/api/v1/docs")
        policy = response.headers.get("Content-Security-Policy-Report-Only", "")
        self.assertIn("default-src 'self'", policy)


class SwaggerDocsCsrfTests(TestCase):
    """Swagger UI 의 X-CSRFToken 자동 주입 활성화 검증.

    CsrfAwareSwagger 가 add_csrf=True 강제 → swagger_cdn.html 의 {% if add_csrf %}
    분기가 활성 → requestInterceptor 가 모든 요청에 X-CSRFToken 헤더 첨부.
    """

    def test_docs_html_injects_csrf_request_interceptor(self):
        response = self.client.get("/api/v1/docs")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("requestInterceptor", html)
        self.assertRegex(html, r"req\.headers\['X-CSRFToken'\] = \"[A-Za-z0-9]+\"")


class CookieJWTAuthCsrfTests(TestCase):
    """CSRF 분기 — mutating method (POST/PUT/PATCH/DELETE) 일 때만 ninja.utils.check_csrf
    호출. RequestFactory 의 POST 는 _dont_enforce_csrf_checks 자동 설정 안 하므로
    실제 CSRF 검증 흐름을 그대로 거침."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email="csrf@example.com",
            password="CsrfAuth1!",
            name="csrf",
            confirmed=True,
        )
        self.auth = CookieJWTAuth()
        self.token = str(RefreshToken.for_user(self.user).access_token)

    def test_post_without_csrf_token_returns_none(self):
        """mutating method + 정상 access 쿠키 + CSRF 토큰 없음 → None (인증 실패)."""
        request = self.factory.post("/")
        request.COOKIES[ACCESS_COOKIE_NAME] = self.token
        self.assertIsNone(self.auth(request))

    def test_get_without_csrf_token_returns_user(self):
        """SAFE method 는 CSRF 검증 면제 — 토큰 없어도 통과."""
        request = self.factory.get("/")
        request.COOKIES[ACCESS_COOKIE_NAME] = self.token
        user = self.auth(request)
        self.assertIsNotNone(user)
        self.assertEqual(user.id, self.user.id)


class TokenRefreshEndpointTests(TestCase):
    """POST /api/v1/auth/refresh — 쿠키의 refresh_token 으로 새 access + refresh 발급.

    settings 의 ROTATE_REFRESH_TOKENS=True + BLACKLIST_AFTER_ROTATION=True 정합:
    호출 시점에 기존 refresh 를 명시적으로 blacklist → 같은 토큰 재사용은 두 번째
    부터 401. 검증 실패는 모두 INVALID_REFRESH_TOKEN 401 로 묶어 토큰 존재성 노출 차단.
    """

    login_url = "/api/v1/users/login"
    refresh_url = "/api/v1/auth/refresh"
    email = "refresh@example.com"
    password = "Refresh123!"

    def setUp(self):
        self.user = User.objects.create_user(
            email=self.email,
            password=self.password,
            name="리프레시",
            confirmed=True,
        )

    def _login(self):
        """로그인 흐름 한 번 돌려 client 의 cookie jar 에 access + refresh 채움."""
        response = self.client.post(
            self.login_url,
            data={"email": self.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def _refresh(self):
        return self.client.post(self.refresh_url)

    # ── 실패 — 쿠키 부재/형식 불량 ─────────────────────────────────────

    def test_missing_refresh_cookie_returns_401(self):
        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "INVALID_REFRESH_TOKEN")

    def test_empty_refresh_cookie_returns_401(self):
        self.client.cookies["refresh_token"] = ""
        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_REFRESH_TOKEN")

    def test_malformed_refresh_cookie_returns_401(self):
        """JWT 형식 자체가 아닌 garbage."""
        self.client.cookies["refresh_token"] = "not-a-jwt"
        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_REFRESH_TOKEN")

    # ── 성공 — 새 access + refresh + rotation ─────────────────────────

    def test_success_returns_new_access_and_rotates_refresh(self):
        self._login()
        old_refresh = self.client.cookies["refresh_token"].value
        old_access = self.client.cookies["access_token"].value

        response = self._refresh()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["message"], "토큰이 성공적으로 재발급되었습니다.")

        data = body["data"]
        # body 에는 토큰 없음
        self.assertNotIn("access_token", data)
        self.assertNotIn("token_type", data)
        self.assertGreater(data["expires_in"], 0)

        # 새 access 쿠키 발급 — 값이 기존과 다름
        new_access_morsel = response.cookies.get("access_token")
        self.assertIsNotNone(new_access_morsel)
        self.assertNotEqual(new_access_morsel.value, old_access)
        self.assertNotEqual(new_access_morsel.value, "")
        self.assertTrue(new_access_morsel["httponly"])
        self.assertEqual(new_access_morsel["samesite"], "Lax")
        self.assertEqual(new_access_morsel["path"], "/")

        # 새 refresh 쿠키 발급 — 값이 기존과 다름
        new_refresh_morsel = response.cookies.get("refresh_token")
        self.assertIsNotNone(new_refresh_morsel)
        self.assertNotEqual(new_refresh_morsel.value, old_refresh)
        self.assertNotEqual(new_refresh_morsel.value, "")
        self.assertTrue(new_refresh_morsel["httponly"])
        self.assertEqual(new_refresh_morsel["samesite"], "Lax")
        self.assertEqual(new_refresh_morsel["path"], "/")

        # rotation: 기존 refresh 가 blacklist 됐는지 확인
        self.assertTrue(
            BlacklistedToken.objects.filter(token__token=old_refresh).exists()
        )

    def test_reused_refresh_returns_401(self):
        """rotation 정책: 같은 refresh 두 번째 호출은 blacklist 라 401."""
        self._login()
        first_refresh = self.client.cookies["refresh_token"].value

        first_response = self._refresh()
        self.assertEqual(first_response.status_code, 200)

        # client.cookies 는 첫 응답의 *새* refresh 로 자동 갱신됨. blacklist 검증을
        # 위해 *기존* refresh 를 다시 쿠키에 박고 호출.
        self.client.cookies["refresh_token"] = first_refresh
        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_REFRESH_TOKEN")

    def test_reuse_detection_revokes_all_outstanding_refresh(self):
        """도난 시나리오: 공격자가 *옛 refresh* 를 재사용하면 → 그 사용자의
        *모든 outstanding refresh* (정상 사용자의 새 refresh 포함) 도 일괄 blacklist.
        결과: 정상 사용자도 강제 로그아웃 (재로그인 필요). 보안 우선 원칙.

        시나리오:
          1. login → R1 발급
          2. 정상 refresh → R1 blacklist + R2 발급
          3. 공격자가 R1 재사용 → 401 + R2 일괄 무효화 트리거
          4. 정상 사용자가 R2 로 refresh → 현재는 200 (살아있음) → reuse detection 적용 후 401
        """
        self._login()
        r1 = self.client.cookies["refresh_token"].value

        # 정상 rotation
        first_response = self._refresh()
        self.assertEqual(first_response.status_code, 200)
        r2 = self.client.cookies["refresh_token"].value
        self.assertNotEqual(r1, r2)

        # 공격자 시뮬레이션: R1 (이미 blacklisted) 재사용
        self.client.cookies["refresh_token"] = r1
        attacker_response = self._refresh()
        self.assertEqual(attacker_response.status_code, 401)

        # ★ 정상 사용자 시뮬레이션: R2 로 refresh → reuse detection 후엔 401 이어야 함
        self.client.cookies["refresh_token"] = r2
        legitimate_response = self._refresh()
        self.assertEqual(
            legitimate_response.status_code,
            401,
            "reuse detection 미적용 — R1 도난 감지 후에도 R2 살아있음 (보안 결함)",
        )
        self.assertEqual(legitimate_response.json()["code"], "INVALID_REFRESH_TOKEN")

    def test_logged_out_refresh_returns_401(self):
        """logout 으로 blacklist 된 refresh 는 refresh endpoint 에서도 401."""
        self._login()
        old_refresh = self.client.cookies["refresh_token"].value

        # 로그아웃 — access 쿠키만으로 인증되어 refresh blacklist 됨
        self.client.post("/api/v1/users/logout")

        # 로그아웃 응답이 cookie 를 삭제했으니 다시 박아 넣고 refresh 시도
        self.client.cookies["refresh_token"] = old_refresh
        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_REFRESH_TOKEN")

    def test_soft_deleted_user_refresh_returns_401(self):
        """refresh 발급 받은 뒤 사용자가 soft-delete 되면 그 토큰은 더 이상 못 씀."""
        self._login()
        self.user.soft_delete()

        response = self._refresh()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_REFRESH_TOKEN")

    # ── CSRF bootstrap — refresh 응답이 csrftoken 쿠키 재발급/유지 ─────

    def test_success_sets_csrftoken_cookie(self):
        """csrftoken 쿠키 만료/유실에 대한 안전망. login 에서 bootstrap 받는 게
        주 경로지만, access 자연 갱신 (refresh) 시점에도 csrftoken 을 함께
        재발급해 FE 가 어느 시점이든 mutating 호출 가능 상태를 유지."""
        self._login()
        response = self._refresh()

        self.assertEqual(response.status_code, 200)
        csrf_cookie = response.cookies.get("csrftoken")
        self.assertIsNotNone(csrf_cookie, "refresh 응답에 csrftoken 쿠키가 없음")
        self.assertTrue(csrf_cookie.value)

    # ── 응답 envelope ─────────────────────────────────────────────────

    def test_response_envelope_shape(self):
        self._login()
        body = self._refresh().json()
        self.assertIn("success", body)
        self.assertIn("message", body)
        self.assertIn("data", body)
        keys = set(body["data"].keys())
        self.assertSetEqual(keys, {"expires_in"})

    # ── 보안 플래그 — DEBUG=False 시 secure=True ──────────────────────

    @override_settings(DEBUG=False)
    def test_secure_cookie_flag_in_production(self):
        """DEBUG=False (prod 환경) 일 때 새 refresh 쿠키가 Secure 플래그 갖는지."""
        self._login()
        response = self._refresh()
        morsel = response.cookies.get("refresh_token")
        self.assertTrue(morsel["secure"])
