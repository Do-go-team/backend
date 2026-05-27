"""Sentry 등 외부 시스템에 첨부할 dict/list 의 PII 를 마스킹.

middleware 가 request body / error detail 을 Sentry context 에 넣기 전에 거치는
필터. 키 이름 기반 (대소문자 무시, 부분 일치 X) — 보수적으로 정확 매칭만.
"""

from typing import Any

MASK = "***"

# 마스킹 대상 키 (정확 매칭, 소문자 비교). 부분 일치는 일반 식별자 ("user_id" 의
# id, "token_count" 의 token) 과 충돌 위험이 있어 채택하지 않음.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "secret",
        "client_secret",
        "authorization",
        "credit_card",
        "card_number",
        "cvv",
    }
)


def sanitize(value: Any) -> Any:
    """dict/list/scalar 를 받아 PII 키 값을 MASK 로 치환한 사본 반환.

    원본을 mutate 하지 않음 — 호출처가 raw body 를 그대로 로깅/Sentry 첨부 둘 다
    가능하도록 보장.
    """
    if isinstance(value, dict):
        return {
            k: (MASK if str(k).lower() in SENSITIVE_KEYS else sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    return value
