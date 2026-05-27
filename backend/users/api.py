"""Router for the "users" domain. Endpoints to be added per API spec."""

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import get_token
from ninja import Router

from auth.authentication import ACCESS_COOKIE_NAME, CookieJWTAuth
from auth.cookies import REFRESH_COOKIE_NAME, clear_auth_cookies, set_auth_cookies
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut
from stores.schemas import MyStoresOut, StoreCreateIn, StoreCreateOut
from stores.services import create_store, list_my_stores

from .schemas import (
    EmailSendIn,
    EmailSendOut,
    EmailVerifyIn,
    EmailVerifyOut,
    LoginIn,
    LoginOut,
    MeOut,
    SignupIn,
    SignupOut,
)
from .services import (
    authenticate_user,
    list_accessible_stores,
    logout_user,
    register_user,
    request_verification_code,
    verify_email_code,
)

router = Router(tags=["users"])


@router.get(
    "/me/stores",
    auth=CookieJWTAuth(),
    tags=["stores"],
    response={
        200: Envelope[MyStoresOut],
        401: ErrorOut,  # UNAUTHORIZED_USER
    },
    openapi_extra=error_examples(
        (401, "UNAUTHORIZED_USER", "인증 정보가 유효하지 않거나 로그인이 필요합니다."),
    ),
    summary="내 매장 목록 조회",
    description="로그인한 사용자가 멤버로 속한 모든 매장 목록. 소프트 삭제된 매장 제외, created_at ASC 정렬.",
)
def my_stores(request):
    stores = list_my_stores(request.auth)
    return ok(
        data={"stores": stores},
        message="내 매장 목록 조회에 성공했습니다.",
    )


@router.post(
    "/me/stores",
    auth=CookieJWTAuth(),
    tags=["stores"],
    response={
        200: Envelope[StoreCreateOut],
        401: ErrorOut,  # UNAUTHORIZED_USER
        422: ErrorOut,  # INVALID_PARAMETER
    },
    openapi_extra=error_examples(
        (401, "UNAUTHORIZED_USER", "인증 정보가 유효하지 않거나 로그인이 필요합니다."),
        (422, "INVALID_PARAMETER", "요청 형식이 올바르지 않습니다."),
    ),
    summary="매장 등록 및 가상 공간 생성",
    description=(
        "Store + StoreMember(MANAGER, 등록자 자동) + StoreImage(floorplan/actual_photo 있는 경우) "
        "한 트랜잭션 생성. 등록자가 자동으로 점장 권한 획득."
    ),
)
def my_store_create(request, payload: StoreCreateIn):
    return ok(
        data=create_store(request.auth, payload),
        message="신규 매장 가상 공간이 성공적으로 생성되었습니다.",
    )


@router.get(
    "/me",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[MeOut],
        401: ErrorOut,  # UNAUTHORIZED_USER
    },
    openapi_extra=error_examples(
        (401, "UNAUTHORIZED_USER", "인증 정보가 유효하지 않거나 로그인이 필요합니다."),
    ),
    summary="내 정보 조회",
    description="로그인한 사용자 정보 + 접근 가능 매장의 최소 projection (id/name/role).",
)
def me(request):
    user = request.auth
    # ImageField exposes a FieldFile whose .name is the raw stored string —
    # accessing .url would prepend MEDIA_URL and mangle absolute URLs.
    profile_url = user.profile_image_url.name if user.profile_image_url else None
    return ok(
        data={
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "profile_image_url": profile_url or None,
            "system_role": user.role,
            "accessible_stores": list_accessible_stores(user),
        },
        message="사용자 정보 조회에 성공했습니다.",
    )


@router.post(
    "/email/send",
    response={
        200: Envelope[EmailSendOut],
        400: ErrorOut,  # INVALID_EMAIL_FORMAT
        429: ErrorOut,  # TOO_MANY_REQUESTS (쿨다운 / 1일 한도)
    },
    openapi_extra=error_examples(
        (400, "INVALID_EMAIL_FORMAT", "올바른 이메일 형식이 아닙니다."),
        (
            429,
            "TOO_MANY_REQUESTS",
            "요청 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
        ),
    ),
    summary="이메일 인증 번호 발송",
    description=(
        "6자리 인증 번호를 Redis 캐시에 저장 + Celery 워커로 메일 발송. TTL 5분. "
        "Rate limit: 재발송 쿨다운 60초 + 1일 10회."
    ),
)
def email_send(request, payload: EmailSendIn):
    expires_in = request_verification_code(payload.email)
    return ok(
        data={"expires_in": expires_in},
        message="인증 번호가 발송되었습니다. 메일함을 확인해 주세요.",
    )


@router.post(
    "/email/verify",
    response={
        200: Envelope[EmailVerifyOut],
        400: ErrorOut,  # CODE_EXPIRED / INVALID_CODE
    },
    openapi_extra=error_examples(
        (
            400,
            "CODE_EXPIRED",
            "인증 번호 입력 시간이 초과되었습니다. 다시 시도해 주세요.",
        ),
        (400, "INVALID_CODE", "인증 번호가 일치하지 않습니다."),
    ),
    summary="이메일 인증 번호 확인",
    description="6자리 코드 검증. 일치 시 1회용 verification_token (TTL 10분) 발급 — 회원가입 단계에서 소비.",
)
def email_verify(request, payload: EmailVerifyIn):
    token = verify_email_code(payload.email, payload.code)
    return ok(
        data={"is_verified": True, "verification_token": token},
        message="이메일 인증이 완료되었습니다.",
    )


@router.post(
    "/login",
    response={
        200: Envelope[LoginOut],
        401: ErrorOut,  # USER_NOT_FOUND / INVALID_CREDENTIALS
        403: ErrorOut,  # DELETED_ACCOUNT
        429: ErrorOut,  # TOO_MANY_REQUESTS
    },
    openapi_extra=error_examples(
        (401, "USER_NOT_FOUND", "가입되지 않은 이메일입니다."),
        (401, "INVALID_CREDENTIALS", "비밀번호가 올바르지 않습니다."),
        (403, "DELETED_ACCOUNT", "탈퇴된 계정입니다."),
        (
            429,
            "TOO_MANY_REQUESTS",
            "로그인 시도 횟수를 초과했습니다. 5분 후 다시 시도해 주세요.",
        ),
    ),
    summary="로그인",
    description=(
        "access/refresh 모두 HttpOnly 쿠키로 응답 — body 에 토큰 값 없음. "
        "비활성·미인증 계정은 INVALID_CREDENTIALS 401 로 silent 통일 (OWASP 권고). "
        "이메일별 rate limit(5회/5분) 초과는 TOO_MANY_REQUESTS 429 로 명시 — FE 가 "
        "대기 안내. 모든 실패 (미가입 포함) 카운트, 성공 시 reset."
    ),
)
def login(request, payload: LoginIn, response: HttpResponse):
    user, access_token, refresh_token, expires_in = authenticate_user(
        payload.email, payload.password
    )
    set_auth_cookies(
        response,
        access_token=access_token,
        refresh_token=refresh_token,
        access_max_age=expires_in,
        refresh_max_age=int(
            settings.NINJA_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()
        ),
    )
    # ninja endpoint 가 자동 csrf_exempt 라 Django 의 자동 csrftoken 부트스트랩
    # 경로가 차단된 상태. get_token 호출이 CsrfViewMiddleware.process_response
    # 단계에서 Set-Cookie: csrftoken=... 을 박도록 유도 — FE 가 이후 mutating
    # 요청에 X-CSRFToken 헤더를 첨부할 수 있게 됨. (CookieJWTAuth 의 CSRF
    # 검증 전제 조건. CSRF_COOKIE_AGE 기본 1년이라 login 시점 한 번이면 충분.)
    get_token(request)
    return ok(
        data={
            "expires_in": expires_in,
            "user": {
                "id": user.id,
                "name": user.name,
                "role": user.role,
            },
        },
        message="로그인에 성공했습니다.",
    )


@router.post(
    "/logout",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[None],
        401: ErrorOut,  # UNAUTHORIZED_USER
    },
    openapi_extra=error_examples(
        (401, "UNAUTHORIZED_USER", "인증 정보가 유효하지 않거나 로그인이 필요합니다."),
    ),
    summary="로그아웃",
    description=(
        "쿠키의 refresh_token blacklist + access_token jti deny-list 등록. "
        "두 쿠키 모두 Max-Age=0 으로 삭제. 멱등성 보장."
    ),
)
def logout(request, response: HttpResponse):
    logout_user(
        refresh_token=request.COOKIES.get(REFRESH_COOKIE_NAME),
        access_token=request.COOKIES.get(ACCESS_COOKIE_NAME),
    )
    clear_auth_cookies(response)
    return ok(data=None, message="안전하게 로그아웃 되었습니다.")


@router.post(
    "/signup",
    response={
        200: Envelope[SignupOut],
        # 400 코드: INVALID_PASSWORD_FORMAT / PASSWORD_MISMATCH / INVALID_VERIFICATION
        #          / EMAIL_MISMATCH / DUPLICATE_EMAIL
        400: ErrorOut,
    },
    openapi_extra=error_examples(
        (
            400,
            "INVALID_PASSWORD_FORMAT",
            "비밀번호는 8~20자의 영문/숫자/특수문자 조합이어야 합니다.",
        ),
        (400, "PASSWORD_MISMATCH", "비밀번호와 비밀번호 확인이 일치하지 않습니다."),
        (
            400,
            "INVALID_VERIFICATION",
            "이메일 인증 정보가 유효하지 않거나 만료되었습니다. 다시 인증해 주세요.",
        ),
        (
            400,
            "EMAIL_MISMATCH",
            "인증된 이메일 주소와 가입하려는 이메일 주소가 일치하지 않습니다.",
        ),
        (400, "DUPLICATE_EMAIL", "이미 가입된 이메일입니다."),
    ),
    summary="회원가입",
    description=(
        "이메일 인증 후 발급받은 verification_token 으로 가입. 비밀번호 8~20자 영문/숫자/특수문자 조합. "
        "verification_token 유효성 + 이메일 일치 + 중복 가입 사전 검증."
    ),
)
def signup(request, payload: SignupIn):
    user = register_user(
        email=payload.email,
        password=payload.password,
        password_confirm=payload.password_confirm,
        name=payload.name,
        profile_image_url=payload.profile_image_url,
        verification_token=payload.verification_token,
    )
    return ok(
        data={
            "user_id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "created_at": user.created_at,
        },
        message="회원가입이 성공적으로 완료되었습니다.",
    )
