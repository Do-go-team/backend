"""쿠키 기반 access token 인증 + CSRF 검증 통합."""

from ninja.utils import check_csrf
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.exceptions import TokenError
from ninja_jwt.tokens import AccessToken

from .blacklist import is_blacklisted

ACCESS_COOKIE_NAME = "access_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CookieJWTAuth(JWTAuth):
    """access_token 을 Authorization 헤더 대신 쿠키에서 추출.

    ninja 1.6 부터 NinjaAPI(csrf=True) 옵션이 없고, ninja endpoint 의 view wrapper 가
    자동으로 csrf_exempt 라 Django 의 CsrfViewMiddleware 가 적용되지 않음. 즉 CSRF
    검증을 auth class 단에서 직접 처리해야 함. mutating method 일 때만 ninja.utils
    .check_csrf() 호출 — safe method (GET/HEAD/OPTIONS/TRACE) 는 CSRF 검증 면제.

    JWTAuth 의 토큰 검증 / user 객체 로딩 (jwt_authenticate) 은 그대로 재사용.
    추가로 soft-deleted 사용자 차단 — 표준 JWTAuth 는 deleted_at 검사를 안 해서
    soft-delete 후 만료 전 access 토큰이 계속 유효한 결함이 있음. 이 단계에서 막음.

    Access JWT blacklist (deny-list) — 로그아웃 시 jti 를 Redis 에 등록해 자연 만료
    전이라도 즉시 무효화. 토큰 형식 검증 후 jti 가 deny-list 에 있으면 거부.
    """

    def __call__(self, request):
        token = request.COOKIES.get(ACCESS_COOKIE_NAME)
        if not token:
            return None

        if request.method not in SAFE_METHODS:
            csrf_error = check_csrf(request, lambda r: None)
            if csrf_error is not None:
                return None

        try:
            jti = AccessToken(token).payload.get("jti")
        except TokenError:
            return None
        if jti and is_blacklisted(jti):
            return None

        try:
            user = self.authenticate(request, token)
        except Exception:
            return None

        if user is None:
            return None
        if getattr(user, "deleted_at", None) is not None:
            return None

        return user
