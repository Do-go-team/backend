"""Swagger UI 의 X-CSRFToken 자동 주입을 강제하는 커스텀 docs 핸들러.

ninja 기본 Swagger 는 `_csrf_needed(api)` 가 NinjaAPI 의 global `auth` 인자만 봄.
우리는 endpoint 별 auth 데코레이터 (`auth=CookieJWTAuth()`) 사용 → global auth 는
None → 기본 swagger UI 가 csrftoken 을 자동 첨부하지 않음 → "Try it out" 의
mutating 요청이 401 (CSRF 검증 실패).

CookieJWTAuth 가 mutating method 에서 항상 CSRF 검증을 하므로 docs 페이지에서
add_csrf=True 를 강제. swagger.html 템플릿이 body 에 csrftoken 을 주입하면
swagger-ui-init.js 가 모든 요청에 X-CSRFToken 헤더 첨부.
"""

import json
from typing import Any

from django.http import HttpRequest, HttpResponse
from ninja.openapi.docs import Swagger as NinjaSwagger
from ninja.openapi.docs import render_template


class CsrfAwareSwagger(NinjaSwagger):
    def render_page(
        self, request: HttpRequest, api: Any, **kwargs: Any
    ) -> HttpResponse:
        self.settings["url"] = self.get_openapi_url(api, kwargs)
        context = {
            "swagger_settings": json.dumps(self.settings, indent=1),
            "api": api,
            "add_csrf": True,
        }
        return render_template(request, self.template, self.template_cdn, context)
