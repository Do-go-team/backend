from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from users.models import User

EMAIL_VERIFICATION_TEST = {
    "CODE_TTL": 300,
    "RESEND_COOLDOWN": 60,
    "DAILY_LIMIT": 3,
}


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_VERIFICATION=EMAIL_VERIFICATION_TEST,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class EmailSendEndpointTests(TestCase):
    url = "/api/v1/users/email/send"

    def setUp(self):
        cache.clear()

    def _post(self, email):
        return self.client.post(
            self.url, data={"email": email}, content_type="application/json"
        )

    @patch("users.services.send_verification_email.delay")
    def test_success_returns_expires_in_and_dispatches_task(self, mock_delay):
        response = self._post("new.user@example.com")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["expires_in"], 300)
        mock_delay.assert_called_once()
        called_email, called_code = mock_delay.call_args.args
        self.assertEqual(called_email, "new.user@example.com")
        self.assertRegex(called_code, r"^\d{6}$")

    def test_invalid_email_returns_invalid_email_format(self):
        response = self._post("not-an-email")

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "INVALID_EMAIL_FORMAT")

    @patch("users.services.send_verification_email.delay")
    def test_cooldown_blocks_immediate_resend(self, mock_delay):
        first = self._post("cooldown@example.com")
        self.assertEqual(first.status_code, 200)

        second = self._post("cooldown@example.com")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "TOO_MANY_REQUESTS")
        self.assertEqual(mock_delay.call_count, 1)

    @patch("users.services.send_verification_email.delay")
    def test_daily_limit_blocks_after_threshold(self, mock_delay):
        email = "daily@example.com"
        for _ in range(EMAIL_VERIFICATION_TEST["DAILY_LIMIT"]):
            self.assertEqual(self._post(email).status_code, 200)
            cache.delete(f"verify:resend:{email}")

        over_limit = self._post(email)
        self.assertEqual(over_limit.status_code, 429)
        self.assertEqual(over_limit.json()["code"], "TOO_MANY_REQUESTS")


class SendVerificationEmailTaskRetryTests(TestCase):
    """send_verification_email Celery task 의 자동 retry 옵션 검증.

    Gmail 일시 오류 (SMTPException) 발생 시 task 가 즉시 실패하지 않고 지수 backoff
    로 재시도하도록 데코레이터에 옵션이 박혀 있어야 함. retry 미설정 시 사용자가
    "코드는 Redis 에 있는데 메일 못 받음" 상태로 cooldown 까지 갇히는 결함 방지.
    """

    def test_autoretry_for_includes_smtp_exception(self):
        from smtplib import SMTPException

        from users.tasks import send_verification_email

        self.assertIn(SMTPException, send_verification_email.autoretry_for)

    def test_max_retries_is_3(self):
        from users.tasks import send_verification_email

        self.assertEqual(send_verification_email.max_retries, 3)

    def test_retry_backoff_is_2(self):
        from users.tasks import send_verification_email

        self.assertEqual(send_verification_email.retry_backoff, 2)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    EMAIL_VERIFICATION=EMAIL_VERIFICATION_TEST,
)
class EmailVerifyEndpointTests(TestCase):
    url = "/api/v1/users/email/verify"
    email = "verify@example.com"
    code = "123456"

    def setUp(self):
        cache.clear()
        cache.set(f"verify:code:{self.email}", self.code, timeout=300)

    def _post(self, email, code):
        return self.client.post(
            self.url,
            data={"email": email, "code": code},
            content_type="application/json",
        )

    def test_success_returns_token_and_consumes_code(self):
        response = self._post(self.email, self.code)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["data"]["is_verified"])
        token = body["data"]["verification_token"]
        self.assertTrue(token)

        self.assertIsNone(cache.get(f"verify:code:{self.email}"))
        self.assertEqual(cache.get(f"verify:token:{token}"), self.email)

    def test_wrong_code_returns_invalid_code(self):
        response = self._post(self.email, "000000")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_CODE")
        self.assertEqual(cache.get(f"verify:code:{self.email}"), self.code)

    def test_missing_code_returns_code_expired(self):
        cache.delete(f"verify:code:{self.email}")

        response = self._post(self.email, self.code)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CODE_EXPIRED")

    def test_malformed_email_returns_code_expired(self):
        response = self._post("not-an-email", self.code)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "CODE_EXPIRED")

    def test_second_verify_after_success_returns_code_expired(self):
        first = self._post(self.email, self.code)
        self.assertEqual(first.status_code, 200)

        second = self._post(self.email, self.code)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["code"], "CODE_EXPIRED")


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class SignupEndpointTests(TestCase):
    url = "/api/v1/users/signup"
    email = "signup@example.com"
    token = "valid-token"
    password = "ValidPass1!"

    def setUp(self):
        cache.clear()
        cache.set(f"verify:token:{self.token}", self.email, timeout=600)

    def _post(self, **overrides):
        body = {
            "email": self.email,
            "password": self.password,
            "password_confirm": self.password,
            "name": "김철수",
            "verification_token": self.token,
            **overrides,
        }
        return self.client.post(self.url, data=body, content_type="application/json")

    def test_success_creates_user_and_consumes_token(self):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])

        data = body["data"]
        self.assertEqual(data["email"], self.email)
        self.assertEqual(data["name"], "김철수")
        self.assertEqual(data["role"], User.Role.USER)
        self.assertIn("user_id", data)
        self.assertIn("created_at", data)

        user = User.objects.get(email=self.email)
        self.assertTrue(user.check_password(self.password))
        self.assertTrue(user.confirmed)
        self.assertIsNone(cache.get(f"verify:token:{self.token}"))

    def test_invalid_verification_when_token_missing(self):
        cache.delete(f"verify:token:{self.token}")

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_VERIFICATION")

    def test_email_mismatch_when_token_email_differs(self):
        cache.set(f"verify:token:{self.token}", "other@example.com", timeout=600)

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "EMAIL_MISMATCH")

    def test_duplicate_email_when_user_exists(self):
        User.objects.create_user(email=self.email, password="Existing1!", name="기존")

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "DUPLICATE_EMAIL")

    def test_invalid_password_format_when_missing_special(self):
        response = self._post(password="NoSpecial1")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PASSWORD_FORMAT")
        self.assertFalse(User.objects.filter(email=self.email).exists())

    def test_invalid_password_format_when_too_short(self):
        response = self._post(password="Ab1!")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PASSWORD_FORMAT")

    def test_invalid_password_format_when_too_long(self):
        response = self._post(password="Abcdefghij12345!@#$%Z")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PASSWORD_FORMAT")

    def test_invalid_password_format_when_special_is_non_ascii(self):
        # 한글이 특수문자 자리를 채우는 케이스는 거부 (ASCII 기호 한정 정책)
        response = self._post(password="abcdef12한")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PASSWORD_FORMAT")

    def test_invalid_password_format_when_special_is_whitespace(self):
        # 공백 / 탭은 ASCII 이지만 기호 범위 밖이라 거부
        response = self._post(password="abcdef12 ")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_PASSWORD_FORMAT")

    def test_javascript_scheme_url_rejected(self):
        # XSS 1차 방어 — profile_image_url 의 javascript: scheme 거부 (Pydantic 422)
        response = self._post(profile_image_url="javascript:alert(document.cookie)")

        self.assertEqual(response.status_code, 422)
        self.assertFalse(User.objects.filter(email=self.email).exists())

    def test_token_is_single_use(self):
        first = self._post()
        self.assertEqual(first.status_code, 200)

        second = self._post(email="another@example.com")
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["code"], "INVALID_VERIFICATION")

    def test_password_mismatch_when_confirm_differs(self):
        response = self._post(password_confirm="Different1!")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "PASSWORD_MISMATCH")
        self.assertFalse(User.objects.filter(email=self.email).exists())

    def test_password_confirm_required(self):
        body = {
            "email": self.email,
            "password": self.password,
            "name": "김철수",
            "verification_token": self.token,
        }
        response = self.client.post(
            self.url, data=body, content_type="application/json"
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(User.objects.filter(email=self.email).exists())


LOGIN_RATE_LIMIT_TEST = {"EMAIL_WINDOW": 300, "EMAIL_MAX": 5}


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    LOGIN_RATE_LIMIT=LOGIN_RATE_LIMIT_TEST,
)
class LoginEndpointTests(TestCase):
    url = "/api/v1/users/login"
    email = "login@example.com"
    password = "Login123!"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email=self.email,
            password=self.password,
            name="로그인",
            confirmed=True,
        )

    def _post(self, **overrides):
        body = {"email": self.email, "password": self.password, **overrides}
        return self.client.post(self.url, data=body, content_type="application/json")

    def test_success_returns_user_and_sets_auth_cookies(self):
        from ninja_jwt.tokens import AccessToken

        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])

        data = body["data"]
        # body 에는 토큰 없음 — access/refresh 모두 쿠키
        self.assertNotIn("access_token", data)
        self.assertNotIn("token_type", data)
        self.assertNotIn("refresh_token", data)
        self.assertEqual(data["expires_in"], 3600)
        self.assertEqual(data["user"]["id"], self.user.id)
        self.assertEqual(data["user"]["name"], "로그인")
        self.assertEqual(data["user"]["role"], User.Role.USER)

        # access 쿠키 — HttpOnly + 1h max-age + 토큰 디코드 검증
        access_cookie = response.cookies.get("access_token")
        self.assertIsNotNone(access_cookie)
        self.assertTrue(access_cookie.value)
        self.assertTrue(access_cookie["httponly"])
        self.assertEqual(access_cookie["samesite"].lower(), "lax")
        self.assertEqual(access_cookie["path"], "/")
        self.assertEqual(access_cookie["max-age"], 3600)
        decoded = AccessToken(access_cookie.value)
        self.assertEqual(decoded["user_id"], self.user.id)

        # refresh 쿠키 — HttpOnly
        refresh_cookie = response.cookies.get("refresh_token")
        self.assertIsNotNone(refresh_cookie)
        self.assertTrue(refresh_cookie.value)
        self.assertTrue(refresh_cookie["httponly"])
        self.assertEqual(refresh_cookie["samesite"].lower(), "lax")
        self.assertEqual(refresh_cookie["path"], "/")

    def test_success_sets_csrftoken_cookie(self):
        """FE 가 후속 mutating 요청에서 X-CSRFToken 헤더를 붙일 수 있도록
        login 응답이 csrftoken 쿠키를 bootstrap. ninja endpoint 가 자동
        csrf_exempt 라 Django 의 자동 발급 경로가 없어, 핸들러가 명시적으로
        get_token(request) 을 호출해 CsrfViewMiddleware.process_response 가
        Set-Cookie 를 박도록 유도. CookieJWTAuth 의 CSRF 검증 전제 조건."""
        response = self._post()

        self.assertEqual(response.status_code, 200)
        csrf_cookie = response.cookies.get("csrftoken")
        self.assertIsNotNone(csrf_cookie, "login 응답에 csrftoken 쿠키가 없음")
        self.assertTrue(csrf_cookie.value)

    def test_invalid_credentials_when_password_wrong(self):
        response = self._post(password="WrongPass1!")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

    def test_user_not_found_when_email_not_registered(self):
        response = self._post(email="ghost@example.com")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "USER_NOT_FOUND")

    def test_deleted_account_when_soft_deleted(self):
        self.user.soft_delete()

        response = self._post()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "DELETED_ACCOUNT")

    def test_unconfirmed_account_silent_invalid_credentials(self):
        """미인증 계정 — 내부 차단은 유지하되 외부 응답은 401 INVALID_CREDENTIALS 통일.

        OWASP 응답 통일 권고 — 계정 상태 enumeration 공격면 축소.
        """
        self.user.confirmed = False
        self.user.save(update_fields=["confirmed"])

        response = self._post()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

    def test_inactive_account_silent_invalid_credentials(self):
        """admin 정지(`is_active=False`) — 내부 차단 유지 + 외부 silent 401."""
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self._post()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

    def test_password_checked_before_state_branches(self):
        """탈퇴 계정 + 잘못된 비밀번호 → INVALID_CREDENTIALS (탈퇴 신호 노출 X)."""
        self.user.soft_delete()

        response = self._post(password="WrongPass1!")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

    def test_rate_limit_blocks_after_max_failures(self):
        """비밀번호 5회 실패 후 6번째 시도는 정답이어도 429 TOO_MANY_REQUESTS."""
        for _ in range(LOGIN_RATE_LIMIT_TEST["EMAIL_MAX"]):
            response = self._post(password="WrongPass1!")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

        # 한도 초과 — 올바른 비밀번호여도 429 TOO_MANY_REQUESTS 명시
        response = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "TOO_MANY_REQUESTS")

    def test_rate_limit_resets_on_successful_login(self):
        """성공 로그인 시 실패 카운터 reset — 다음 시도부터 한도 새로 시작."""
        for _ in range(LOGIN_RATE_LIMIT_TEST["EMAIL_MAX"] - 1):
            self._post(password="WrongPass1!")

        # 한도 도달 직전 성공
        success = self._post()
        self.assertEqual(success.status_code, 200)

        # 카운터 reset 확인 — 다시 한도 -1 회까지 실패 응답 정상
        for _ in range(LOGIN_RATE_LIMIT_TEST["EMAIL_MAX"] - 1):
            response = self._post(password="WrongPass1!")
            self.assertEqual(response.json()["code"], "INVALID_CREDENTIALS")

        # 정답으로 다시 로그인 가능
        again = self._post()
        self.assertEqual(again.status_code, 200)

    def test_user_not_found_counts_toward_rate_limit(self):
        """미가입 이메일도 카운트 — 5회 USER_NOT_FOUND 후 6번째는 429 TOO_MANY_REQUESTS.

        근거: 한도 초과 응답이 429 로 통일돼 있어 USER_NOT_FOUND ↔ INVALID_CREDENTIALS
        flip 으로 인한 계정 존재 leak 이 발생하지 않음. 단일 이메일에 대한 무한 probing
        차단 + DB lookup 부하 절감이 이득.
        """
        ghost = "ghost@example.com"
        for _ in range(LOGIN_RATE_LIMIT_TEST["EMAIL_MAX"]):
            response = self._post(email=ghost)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "USER_NOT_FOUND")

        response = self._post(email=ghost)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "TOO_MANY_REQUESTS")


class LogoutEndpointTests(TestCase):
    login_url = "/api/v1/users/login"
    logout_url = "/api/v1/users/logout"
    email = "logout@example.com"
    password = "Logout123!"

    def setUp(self):
        self.user = User.objects.create_user(
            email=self.email,
            password=self.password,
            name="로그아웃",
            confirmed=True,
        )

    def _login(self):
        """Drive the login flow so the client holds both access and refresh
        cookies, exactly like a browser would."""
        response = self.client.post(
            self.login_url,
            data={"email": self.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def _logout(self):
        return self.client.post(self.logout_url)

    def test_success_blacklists_refresh_and_clears_cookies(self):
        from ninja_jwt.token_blacklist.models import BlacklistedToken

        self._login()
        refresh_before = self.client.cookies["refresh_token"].value
        self.assertFalse(
            BlacklistedToken.objects.filter(token__token=refresh_before).exists()
        )

        response = self._logout()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIsNone(body["data"])

        # Refresh token is now blacklisted (server-side revocation)
        self.assertTrue(
            BlacklistedToken.objects.filter(token__token=refresh_before).exists()
        )

        # Response instructs the browser to expire BOTH cookies
        for name in ("access_token", "refresh_token"):
            cleared = response.cookies.get(name)
            self.assertIsNotNone(cleared, f"{name} 쿠키 삭제 헤더 누락")
            self.assertEqual(cleared.value, "")
            self.assertEqual(cleared["max-age"], 0)
            # set 시점과 동일한 path/samesite 로 삭제 (미일치 시 브라우저가 못 지움)
            self.assertEqual(cleared["path"], "/")
            self.assertEqual(cleared["samesite"].lower(), "lax")

    def test_idempotent_without_refresh_cookie(self):
        self._login()
        self.client.cookies.pop("refresh_token", None)

        response = self._logout()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_idempotent_when_refresh_already_blacklisted(self):
        self._login()
        self._logout()  # first call blacklists + cookies cleared from test client

        # 두 번째 호출 — 이제 쿠키가 비어있어 401 예상 (refresh blacklist 상관없이
        # access 쿠키 자체가 빈 값이라 인증 불가). 이게 신규 동작.
        second = self.client.post(self.logout_url)
        self.assertEqual(second.status_code, 401)

    def test_unauthorized_without_access_cookie(self):
        response = self._logout()

        self.assertEqual(response.status_code, 401)

    def test_access_token_blacklisted_after_logout(self):
        """로그아웃 후 *동일 access 토큰* 으로 다시 인증 시도하면 401.

        클라이언트는 logout 응답으로 쿠키가 삭제되지만, 공격자가 logout 직전에
        복사해 둔 access 토큰을 그대로 다시 보내는 시나리오. blacklist 등록 전엔
        access 토큰이 자연 만료까지 유효해 통과되는 결함이 있음 — 이 테스트로 막음.
        """
        self._login()
        access_before_logout = self.client.cookies["access_token"].value
        self.assertNotEqual(access_before_logout, "")

        # 로그아웃 — 응답이 client cookie jar 의 access/refresh 를 비움
        logout_response = self._logout()
        self.assertEqual(logout_response.status_code, 200)

        # 공격자 시뮬레이션: 로그아웃 직전 access 를 manual 복원해 인증 시도
        self.client.cookies["access_token"] = access_before_logout
        me_response = self.client.get("/api/v1/users/me")

        self.assertEqual(
            me_response.status_code,
            401,
            "blacklist 미적용 — logout 후에도 access 토큰이 유효함 (보안 결함)",
        )

    def test_unauthorized_with_malformed_access_cookie(self):
        self.client.cookies["access_token"] = "not-a-real-token"
        response = self._logout()

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")

    def test_blacklisted_refresh_cannot_rotate_afterwards(self):
        """Proves the blacklist has teeth — a refresh token that was revoked
        during logout can no longer be used to mint new tokens."""
        from ninja_jwt.exceptions import TokenError
        from ninja_jwt.tokens import RefreshToken

        self._login()
        refresh_str = self.client.cookies["refresh_token"].value
        self._logout()

        with self.assertRaises(TokenError):
            RefreshToken(refresh_str).check_blacklist()


@override_settings(CORS_ALLOWED_ORIGINS=["http://localhost:5173"])
class CorsCredentialsTests(TestCase):
    """Guards against the refresh_token cookie flow being silently broken by
    missing CORS_ALLOW_CREDENTIALS. Without the Allow-Credentials response
    header, browsers drop the cookie on cross-origin XHR even when the client
    opts in with `credentials: "include"`."""

    origin = "http://localhost:5173"

    def test_cross_origin_response_includes_allow_credentials(self):
        response = self.client.get("/api/v1/hello", HTTP_ORIGIN=self.origin)

        self.assertEqual(
            response.headers.get("Access-Control-Allow-Credentials"), "true"
        )
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"), self.origin
        )


class MeEndpointTests(TestCase):
    url = "/api/v1/users/me"
    email = "me@example.com"
    password = "MeTest123!"

    def setUp(self):
        from ninja_jwt.tokens import AccessToken

        self.user = User.objects.create_user(
            email=self.email,
            password=self.password,
            name="나자신",
            confirmed=True,
        )
        self.access = str(AccessToken.for_user(self.user))

    def _get(self, access_token=None):
        headers = {}
        if access_token is not None:
            self.client.cookies["access_token"] = access_token
        return self.client.get(self.url, **headers)

    def test_success_returns_user_info_and_empty_stores_when_no_memberships(self):
        response = self._get(self.access)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])

        data = body["data"]
        self.assertEqual(data["id"], self.user.id)
        self.assertEqual(data["email"], self.email)
        self.assertEqual(data["name"], "나자신")
        self.assertIsNone(data["profile_image_url"])
        self.assertEqual(data["system_role"], User.Role.USER)
        self.assertEqual(data["accessible_stores"], [])

    def test_accessible_stores_lists_memberships_excluding_soft_deleted(self):
        from django.utils import timezone

        from stores.models import Store, StoreMember

        s1 = Store.objects.create(
            user=self.user,
            name="Store One",
            address="A",
            max_admin_count=5,
            width=1,
            height=1,
            depth=1,
        )
        s2 = Store.objects.create(
            user=self.user,
            name="Store Two",
            address="B",
            max_admin_count=5,
            width=1,
            height=1,
            depth=1,
        )
        s_deleted = Store.objects.create(
            user=self.user,
            name="Store Gone",
            address="X",
            max_admin_count=5,
            width=1,
            height=1,
            depth=1,
        )
        StoreMember.objects.create(store=s1, user=self.user, role="OWNER")
        StoreMember.objects.create(store=s2, user=self.user, role="VMD")
        StoreMember.objects.create(store=s_deleted, user=self.user, role="STAFF")
        s_deleted.deleted_at = timezone.now()
        s_deleted.save(update_fields=["deleted_at"])

        response = self._get(self.access)

        self.assertEqual(response.status_code, 200)
        stores = response.json()["data"]["accessible_stores"]

        self.assertEqual(len(stores), 2)
        names = [s["store_name"] for s in stores]
        self.assertIn("Store One", names)
        self.assertIn("Store Two", names)
        self.assertNotIn("Store Gone", names)

        # Minimal projection — must not leak fields beyond the spec
        self.assertEqual(
            set(stores[0].keys()), {"store_id", "store_name", "store_role"}
        )

    def test_unauthorized_without_authorization_header(self):
        response = self._get()

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")

    def test_unauthorized_with_malformed_token(self):
        response = self._get("not-a-real-token")

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "UNAUTHORIZED_USER")
