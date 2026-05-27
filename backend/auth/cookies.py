"""인증 쿠키 set/clear 헬퍼 + 상수 일원화.

set 시점과 clear 시점의 path/samesite 가 다르면 브라우저가 다른 쿠키로 인식해서
삭제가 안 됨. 모든 호출처가 같은 헬퍼를 통하도록 강제해서 path/samesite drift 방지.
"""

from django.conf import settings
from django.http import HttpResponse

ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"
COOKIE_PATH = "/"
COOKIE_SAMESITE = "Lax"


def _common_kwargs():
    return {
        "httponly": True,
        "secure": not settings.DEBUG,
        "samesite": COOKIE_SAMESITE,
        "path": COOKIE_PATH,
    }


def set_auth_cookies(
    response: HttpResponse,
    access_token: str,
    refresh_token: str,
    access_max_age: int,
    refresh_max_age: int,
) -> None:
    common = _common_kwargs()
    response.set_cookie(
        ACCESS_COOKIE_NAME, access_token, max_age=access_max_age, **common
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME, refresh_token, max_age=refresh_max_age, **common
    )


def clear_auth_cookies(response: HttpResponse) -> None:
    """delete_cookie() 가 path/samesite 인자를 받지만 httponly/secure 는 안 받음.
    Django 5 의 delete_cookie 헬퍼로는 max-age=0 + path/samesite 일치까지만 가능.
    값 비우고 max_age=0 으로 직접 set 해서 set_auth_cookies 와 같은 attribute 일관성 유지."""
    common = _common_kwargs()
    response.set_cookie(ACCESS_COOKIE_NAME, "", max_age=0, **common)
    response.set_cookie(REFRESH_COOKIE_NAME, "", max_age=0, **common)
