import jwt
from django.contrib.auth import get_user_model
from ninja_jwt.exceptions import TokenError
from ninja_jwt.settings import api_settings as jwt_settings
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.tokens import RefreshToken

from common.exceptions import BusinessException

User = get_user_model()


def _detect_reuse_and_revoke_all(refresh_token: str) -> None:
    """blacklisted refresh 의 재사용 감지 시 사용자의 모든 outstanding refresh 일괄 무효화.

    rotation 만으로는 도난 시 공격자가 가진 *다른* outstanding refresh (rotation 으로
    발급된 새 토큰) 가 살아있음. 본 함수가 그 갭을 메움 — 시그니처는 검증하되
    만료/blacklist 검사는 skip 하고 payload 의 jti 가 BlacklistedToken 에 있으면
    도난으로 간주, 사용자의 모든 outstanding 토큰을 일괄 blacklist 로 등록.

    결과: 정상 사용자도 강제 로그아웃 (재로그인하면 새 토큰 발급) — 보안 우선 원칙.
    """
    try:
        payload = jwt.decode(
            refresh_token,
            jwt_settings.SIGNING_KEY,
            algorithms=[jwt_settings.ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.InvalidTokenError:
        return

    jti = payload.get("jti")
    user_id = payload.get("user_id")
    if not jti or not user_id:
        return

    if not BlacklistedToken.objects.filter(token__jti=jti).exists():
        return

    for outstanding in OutstandingToken.objects.filter(user_id=user_id):
        BlacklistedToken.objects.get_or_create(token=outstanding)


def refresh_access_token(refresh_token: str | None) -> tuple[str, str, int]:
    """쿠키의 refresh_token 으로 새 access + refresh 한 쌍 발급.

    settings.NINJA_JWT 의 ROTATE_REFRESH_TOKENS=True + BLACKLIST_AFTER_ROTATION=True
    조합과 정합 — 호출 시점에 기존 refresh 를 명시적으로 blacklist 한 뒤 새 refresh
    를 발급. 즉 같은 refresh 의 재사용은 두 번째 호출부터 차단됨 (rotation).

    Reuse detection: TokenError 발생 시 _detect_reuse_and_revoke_all 호출 — 이미
    blacklist 된 토큰의 재사용이면 사용자의 모든 outstanding refresh 일괄 무효화.

    검증 단계:
      1. 쿠키 부재 / 빈 문자열 → INVALID_REFRESH_TOKEN
      2. 토큰 형식 불량 / 만료 / 이미 blacklist → INVALID_REFRESH_TOKEN (TokenError)
      3. user_id payload 의 사용자가 deleted_at IS NOT NULL → INVALID_REFRESH_TOKEN
         (soft-deleted 사용자가 만료 전 refresh 로 access 받는 것 차단)

    모든 실패는 같은 코드/401 로 묶음 — 토큰 존재성 노출 차단 (logout 의
    blacklist 정책과 같은 결).

    Returns: (new_access_str, new_refresh_str, access_expires_in_seconds)
    """
    invalid = BusinessException(
        "INVALID_REFRESH_TOKEN",
        "유효하지 않거나 만료된 refresh token 입니다.",
        status=401,
    )

    if not refresh_token:
        raise invalid

    try:
        old_refresh = RefreshToken(refresh_token)
    except TokenError as exc:
        _detect_reuse_and_revoke_all(refresh_token)
        raise invalid from exc

    user_id = old_refresh.payload.get("user_id")
    user = User.objects.filter(id=user_id, deleted_at__isnull=True).first()
    if user is None:
        raise invalid

    # rotation: 기존 refresh 무효화 → 같은 토큰 재사용 시 다음 호출부터 401
    old_refresh.blacklist()

    new_refresh = RefreshToken.for_user(user)
    new_access = new_refresh.access_token
    return (
        str(new_access),
        str(new_refresh),
        int(new_access.lifetime.total_seconds()),
    )
