import re
import secrets
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from email_validator import EmailNotValidError, validate_email
from ninja_jwt.exceptions import TokenError
from ninja_jwt.tokens import RefreshToken

from common.exceptions import BusinessException
from stores.models import StoreMember

from .models import User
from .tasks import send_verification_email

CODE_KEY = "verify:code:{email}"
COOLDOWN_KEY = "verify:resend:{email}"
DAILY_KEY = "verify:daily:{email}:{date}"
TOKEN_KEY = "verify:token:{token}"
DAILY_TTL_SECONDS = 24 * 60 * 60
VERIFICATION_TOKEN_TTL = 10 * 60

LOGIN_FAIL_KEY = "login:fail:{email}"

PASSWORD_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[!-/:-@\[-`{-~]).{8,20}$")


def _canonical_email(email: str) -> str | None:
    try:
        return validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _normalize_email(email: str) -> str:
    normalized = _canonical_email(email)
    if normalized is None:
        raise BusinessException(
            "INVALID_EMAIL_FORMAT", "올바른 이메일 형식이 아닙니다.", status=400
        )
    return normalized


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _enforce_rate_limit(email: str) -> None:
    cfg = settings.EMAIL_VERIFICATION
    cooldown_key = COOLDOWN_KEY.format(email=email)
    if not cache.add(cooldown_key, "1", timeout=cfg["RESEND_COOLDOWN"]):
        raise BusinessException(
            "TOO_MANY_REQUESTS",
            "요청 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
            status=429,
        )

    daily_key = DAILY_KEY.format(email=email, date=datetime.utcnow().strftime("%Y%m%d"))
    try:
        count = cache.incr(daily_key)
    except ValueError:
        cache.set(daily_key, 1, timeout=DAILY_TTL_SECONDS)
        count = 1
    if count > cfg["DAILY_LIMIT"]:
        raise BusinessException(
            "TOO_MANY_REQUESTS",
            "요청 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
            status=429,
        )


def request_verification_code(email: str) -> int:
    """Validate the email, enforce rate limits, persist a fresh code to cache,
    dispatch the delivery task, and return the code TTL (seconds)."""
    normalized = _normalize_email(email)
    _enforce_rate_limit(normalized)

    ttl = settings.EMAIL_VERIFICATION["CODE_TTL"]
    code = _generate_code()
    cache.set(CODE_KEY.format(email=normalized), code, timeout=ttl)

    send_verification_email.delay(normalized, code)
    return ttl


def verify_email_code(email: str, code: str) -> str:
    """Compare the submitted code against the cached one; on success, consume
    the code and issue a short-lived verification token for the signup step."""
    normalized = _canonical_email(email) or ""
    stored = cache.get(CODE_KEY.format(email=normalized)) if normalized else None
    if stored is None:
        raise BusinessException(
            "CODE_EXPIRED",
            "인증 번호 입력 시간이 초과되었습니다. 다시 시도해 주세요.",
            status=400,
        )
    if stored != code:
        raise BusinessException(
            "INVALID_CODE", "인증 번호가 일치하지 않습니다.", status=400
        )

    cache.delete(CODE_KEY.format(email=normalized))
    token = secrets.token_urlsafe(32)
    cache.set(TOKEN_KEY.format(token=token), normalized, timeout=VERIFICATION_TOKEN_TTL)
    return token


def _validate_password_format(password: str) -> None:
    if not PASSWORD_PATTERN.match(password or ""):
        raise BusinessException(
            "INVALID_PASSWORD_FORMAT",
            "비밀번호는 8~20자의 영문/숫자/특수문자 조합이어야 합니다.",
            status=400,
        )


def register_user(
    *,
    email: str,
    password: str,
    password_confirm: str,
    name: str,
    profile_image_url: str | None,
    verification_token: str,
) -> User:
    """Consume a verification token, enforce the signup invariants from the
    spec, and create a brand-new USER-role account with a hashed password."""
    _validate_password_format(password)
    if password != password_confirm:
        raise BusinessException(
            "PASSWORD_MISMATCH",
            "비밀번호와 비밀번호 확인이 일치하지 않습니다.",
            status=400,
        )

    normalized = _canonical_email(email) or email.lower()

    token_key = TOKEN_KEY.format(token=verification_token)
    stored_email = cache.get(token_key)
    if stored_email is None:
        raise BusinessException(
            "INVALID_VERIFICATION",
            "이메일 인증 정보가 유효하지 않거나 만료되었습니다. 다시 인증해 주세요.",
            status=400,
        )

    if stored_email != normalized:
        raise BusinessException(
            "EMAIL_MISMATCH",
            "인증된 이메일 주소와 가입하려는 이메일 주소가 일치하지 않습니다.",
            status=400,
        )

    if User.objects.alive().filter(email=normalized).exists():
        raise BusinessException(
            "DUPLICATE_EMAIL", "이미 가입된 이메일입니다.", status=400
        )

    user = User.objects.create_user(
        email=normalized,
        password=password,
        name=name,
        profile_image_url=profile_image_url or None,
        confirmed=True,
    )
    cache.delete(token_key)
    return user


def _invalid_credentials() -> BusinessException:
    return BusinessException(
        "INVALID_CREDENTIALS",
        "비밀번호가 올바르지 않습니다.",
        status=401,
    )


def _check_login_rate_limit(email: str) -> None:
    """이메일별 실패 카운트가 한도 초과면 429 TOO_MANY_REQUESTS 발생.

    Retry-After 헤더는 BusinessException 이 헤더를 지원하지 않아 미부착 —
    필요해지면 별도 티켓에서 BusinessException + handler 확장으로 추가.
    """
    cap = settings.LOGIN_RATE_LIMIT["EMAIL_MAX"]
    if cache.get(LOGIN_FAIL_KEY.format(email=email), 0) >= cap:
        window_minutes = max(1, settings.LOGIN_RATE_LIMIT["EMAIL_WINDOW"] // 60)
        raise BusinessException(
            "TOO_MANY_REQUESTS",
            f"로그인 시도 횟수를 초과했습니다. {window_minutes}분 후 다시 시도해 주세요.",
            status=429,
        )


def _record_login_failure(email: str) -> None:
    """로그인 실패 시 카운터 +1 (fixed-window — 첫 실패에서만 TTL 설정).

    미가입 (USER_NOT_FOUND) / 비밀번호 틀림 / 비활성·미인증 모두 카운트 대상.
    한도 초과 시 응답이 429 TOO_MANY_REQUESTS 로 통일되므로 이전의 silent
    flip(``USER_NOT_FOUND → INVALID_CREDENTIALS``) enum leak 문제는 없음.
    """
    key = LOGIN_FAIL_KEY.format(email=email)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=settings.LOGIN_RATE_LIMIT["EMAIL_WINDOW"])


def _reset_login_failures(email: str) -> None:
    cache.delete(LOGIN_FAIL_KEY.format(email=email))


def authenticate_user(email: str, password: str) -> tuple[User, str, str, int]:
    """Verify credentials and return (user, access_str, refresh_str, access_expires_in_seconds).

    외부 응답:
      - 401 USER_NOT_FOUND: 이메일 미가입 (FE UX 분기를 위해 분리 유지)
      - 401 INVALID_CREDENTIALS: 비밀번호 틀림 / 비활성 / 미인증
      - 403 DELETED_ACCOUNT: 탈퇴한 계정 (UX 안내 위해 분리 유지)
      - 429 TOO_MANY_REQUESTS: 이메일당 EMAIL_MAX 회/EMAIL_WINDOW 초 초과 (FE 가 대기 안내)

    내부 검증은 모두 유지 — 비활성/미인증 계정은 여전히 로그인 차단되고
    응답은 INVALID_CREDENTIALS 로 OWASP 권고에 맞춰 통일 (계정 상태 enum 방어).
    Rate limit: 모든 실패 케이스 (미가입 / 비밀번호 / 비활성 / 미인증) 카운트,
    성공 시 reset. 한도 초과 시 응답이 429 로 통일되므로 카운트 대상 확대가
    USER_NOT_FOUND ↔ INVALID_CREDENTIALS 응답 flip enum leak 을 만들지 않음.
    """
    normalized = _canonical_email(email) or email.lower()

    _check_login_rate_limit(normalized)

    user = User.objects.filter(email=normalized).first()
    if user is None:
        _record_login_failure(normalized)
        raise BusinessException(
            "USER_NOT_FOUND",
            "가입되지 않은 이메일입니다.",
            status=401,
        )

    if not user.check_password(password):
        _record_login_failure(normalized)
        raise _invalid_credentials()

    if user.deleted_at is not None:
        raise BusinessException("DELETED_ACCOUNT", "탈퇴된 계정입니다.", status=403)

    if not user.is_active or not user.confirmed:
        _record_login_failure(normalized)
        raise _invalid_credentials()

    _reset_login_failures(normalized)

    refresh = RefreshToken.for_user(user)
    access = refresh.access_token
    return user, str(access), str(refresh), int(access.lifetime.total_seconds())


def logout_user(refresh_token: str | None, access_token: str | None = None) -> None:
    """Blacklist the caller's refresh token + access jti so neither can be reused.

    Refresh: ninja-jwt 의 token_blacklist 테이블 — rotation 차단.
    Access: Redis deny-list — 자연 만료 전 즉시 무효화 (디바이스 분실 방어).

    Idempotent: missing / malformed / already-blacklisted tokens 모두 no-op.
    """
    if refresh_token:
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError:
            pass
    if access_token:
        from auth.blacklist import blacklist_access_token

        blacklist_access_token(access_token)


def list_accessible_stores(user) -> list[dict]:
    """Minimal projection for /users/me — just {id, name, role} per store the
    user has a membership on. Soft-deleted stores are excluded."""
    memberships = (
        StoreMember.objects.filter(user=user, store__deleted_at__isnull=True)
        .select_related("store")
        .order_by("store__created_at")
    )
    return [
        {
            "store_id": m.store.id,
            "store_name": m.store.name,
            "store_role": m.role,
        }
        for m in memberships
    ]
