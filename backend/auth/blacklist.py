"""Access JWT 즉시 무효화 (deny-list).

ninja-jwt 표준은 access 토큰을 자연 만료까지 유효 처리. 로그아웃 시점의 *즉시*
무효화가 필요한 경우 (디바이스 분실/공유 PC) 본 모듈을 거쳐 deny-list 등록.

저장: Django CACHES["blacklist"] (별도 Redis DB). TTL = 토큰 잔여 만료 시간.
즉 자연 만료 시점에 자동 정리되므로 별도 cleanup 불필요.
"""

from __future__ import annotations

import time

from django.core.cache import caches
from ninja_jwt.exceptions import TokenError
from ninja_jwt.tokens import AccessToken

BLACKLIST_CACHE_ALIAS = "blacklist"
KEY_PREFIX = "access_jti:"


def _key(jti: str) -> str:
    return f"{KEY_PREFIX}{jti}"


def add_to_blacklist(jti: str, exp: int) -> None:
    """jti 를 deny-list 에 등록. exp 는 unix timestamp.

    잔여 시간이 0 이하면 등록 X (이미 자연 만료 → 무의미한 key 누수 방지).
    """
    ttl = exp - int(time.time())
    if ttl <= 0:
        return
    caches[BLACKLIST_CACHE_ALIAS].set(_key(jti), "1", timeout=ttl)


def is_blacklisted(jti: str) -> bool:
    return caches[BLACKLIST_CACHE_ALIAS].get(_key(jti)) is not None


def blacklist_access_token(token_str: str) -> None:
    """access 토큰 문자열을 받아 jti 추출 후 deny-list 등록.

    토큰 형식 불량/만료 시 silently no-op — 어차피 인증 단계에서 거부됨.
    """
    try:
        token = AccessToken(token_str)
    except TokenError:
        return
    jti = token.payload.get("jti")
    exp = token.payload.get("exp")
    if not jti or not exp:
        return
    add_to_blacklist(jti, int(exp))
