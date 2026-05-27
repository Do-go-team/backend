"""Router for the "authentication" domain."""

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import get_token
from ninja import Router

from auth.cookies import REFRESH_COOKIE_NAME, set_auth_cookies
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut

from .schemas import TokenRefreshOut
from .services import refresh_access_token

router = Router(tags=["auth"])


@router.post(
    "/refresh",
    response={
        200: Envelope[TokenRefreshOut],
        401: ErrorOut,  # INVALID_REFRESH_TOKEN
    },
    openapi_extra=error_examples(
        (401, "INVALID_REFRESH_TOKEN", "유효하지 않거나 만료된 refresh token 입니다."),
    ),
    summary="토큰 재발급",
    description=(
        "쿠키의 `refresh_token` 으로 새 access + refresh 한 쌍 발급. 응답은 쿠키로 전달되며 "
        "body 에 토큰 값 없음. 모든 검증 실패 (쿠키 부재 / 형식 불량 / 만료 / blacklist / "
        "삭제된 사용자) 는 `INVALID_REFRESH_TOKEN` 401 통합 — 토큰 존재성 노출 차단. "
        "FE 의 401→refresh→retry 인터셉터는 single-inflight dedup 필수."
    ),
)
def token_refresh(request, response: HttpResponse):
    """Access 만료 시 FE 가 호출. 쿠키의 refresh_token 으로 새 access 발급 +
    rotation 으로 새 refresh 도 같이 발급해 쿠키로 응답.

    인증 미들웨어 X — refresh_token 쿠키 자체가 인증 수단.
    """
    access_token, new_refresh_token, expires_in = refresh_access_token(
        request.COOKIES.get(REFRESH_COOKIE_NAME),
    )
    set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=new_refresh_token,
        access_max_age=expires_in,
        refresh_max_age=int(
            settings.NINJA_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()
        ),
    )
    # csrftoken 쿠키 안전망 — 주 부트스트랩은 login 이지만 csrftoken 만료/유실
    # 가능성 대비. access 자연 갱신 시점에 함께 재발급해 FE 의 mutating 호출
    # 가능 상태를 어느 시점이든 유지. 자세한 메커니즘은 users.api.login 참조.
    get_token(request)
    return ok(
        data={"expires_in": expires_in},
        message="토큰이 성공적으로 재발급되었습니다.",
    )
