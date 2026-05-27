import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError, ValidationError
from ninja_jwt.exceptions import AuthenticationFailed as JWTAuthenticationFailed

from .exceptions import BusinessException

logger = logging.getLogger(__name__)


@csrf_exempt
def api_not_found_view(request, *args, **kwargs):
    """Catch-all for unmatched /api/* URLs so they return the failure envelope
    instead of Django's default HTML 404 page (which fires before ninja).

    csrf_exempt: This is a plain Django view (not Ninja-wrapped), so without
    explicit exemption a POST to a non-existent /api/* path would be rejected
    by CsrfViewMiddleware with a 403 "CSRF verification failed" before our 404
    envelope ever runs — confusing for server-to-server callers hitting a
    typo'd worker endpoint.
    """
    return JsonResponse(
        {
            "success": False,
            "code": "NOT_FOUND",
            "message": "요청하신 리소스를 찾을 수 없습니다.",
        },
        status=404,
    )


def register_exception_handlers(api):
    """Attach standard error envelope handlers to a NinjaAPI instance."""

    @api.exception_handler(BusinessException)
    def on_business(request, exc: BusinessException):
        # 응답에는 정적 envelope, 서버 로그에는 진단 정보 (code/status/method/path)
        # 보존 — XSS reflection 방어 + 운영 디버깅 양립.
        logger.info(
            "Business exception: code=%s status=%s method=%s path=%s",
            exc.code,
            exc.status,
            request.method,
            request.path,
        )
        return api.create_response(
            request,
            {"success": False, "code": exc.code, "message": exc.message},
            status=exc.status,
        )

    @api.exception_handler(ValidationError)
    def on_validation(request, exc: ValidationError):
        # pydantic 의 errors 에 어느 필드가 어떤 입력으로 실패했는지 들어있음.
        # 응답 본문에는 노출하지 않고 서버 로그에만 남겨 디버깅 가능 + XSS 차단.
        logger.warning(
            "Validation failed: method=%s path=%s errors=%r",
            request.method,
            request.path,
            getattr(exc, "errors", None),
        )
        return api.create_response(
            request,
            {
                "success": False,
                "code": "INVALID_PARAMETER",
                "message": "요청 형식이 올바르지 않습니다.",
            },
            status=422,
        )

    @api.exception_handler(AuthenticationError)
    def on_authentication(request, exc: AuthenticationError):
        return api.create_response(
            request,
            {
                "success": False,
                "code": "UNAUTHORIZED_USER",
                "message": "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
            },
            status=401,
        )

    @api.exception_handler(JWTAuthenticationFailed)
    def on_jwt_authentication(request, exc: JWTAuthenticationFailed):
        # ninja_jwt's JWTAuth raises InvalidToken/AuthenticationFailed (DRF-style,
        # detail=dict) which bypasses ninja.errors.AuthenticationError. Remap to
        # the project envelope so every JWTAuth endpoint fails uniformly.
        return api.create_response(
            request,
            {
                "success": False,
                "code": "UNAUTHORIZED_USER",
                "message": "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
            },
            status=401,
        )

    @api.exception_handler(PermissionDenied)
    def on_permission(request, exc: PermissionDenied):
        return api.create_response(
            request,
            {
                "success": False,
                "code": "FORBIDDEN",
                "message": "해당 리소스에 대한 권한이 없습니다.",
            },
            status=403,
        )

    @api.exception_handler(Http404)
    def on_not_found(request, exc: Http404):
        return api.create_response(
            request,
            {
                "success": False,
                "code": "NOT_FOUND",
                "message": "요청하신 리소스를 찾을 수 없습니다.",
            },
            status=404,
        )

    @api.exception_handler(Exception)
    def on_unhandled(request, exc: Exception):
        logger.exception("Unhandled exception in API handler")
        return api.create_response(
            request,
            {
                "success": False,
                "code": "INTERNAL_ERROR",
                "message": "서버 내부 오류가 발생했습니다.",
            },
            status=500,
        )
