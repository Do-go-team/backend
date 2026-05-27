from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, re_path
from ninja import NinjaAPI

from assets_3d.api import router as assets_router
from auth.api import router as auth_router
from auth.swagger import CsrfAwareSwagger
from common.handlers import api_not_found_view, register_exception_handlers
from common.response import ok
from fixtures.api import router as fixtures_router
from layouts.api import router as layouts_router, stores_layouts_router
from products.api import router as products_router
from stores.api import invitations_router, router as stores_router
from users.api import router as users_router

API_DESCRIPTION = """
DoGo VMD API. 모든 응답은 `{ success, message, data }` envelope.
검증 실패는 `INVALID_PARAMETER` 422, 비즈니스 에러는 `code` 필드로 식별.
인증은 HttpOnly 쿠키 (`access_token`, `refresh_token`) + mutating 요청 시 `X-CSRFToken` 헤더.
""".strip()

api = NinjaAPI(
    title="DoGo API",
    version="1.0.0",
    description=API_DESCRIPTION,
    docs=CsrfAwareSwagger(),
)

# OpenAPI tag-level 설명. Swagger UI 의 도메인 그룹별 헤더에 노출됨.
api.openapi_extra = {
    "tags": [
        {"name": "auth", "description": "토큰 발급/갱신. 쿠키 refresh_token 기반."},
        {
            "name": "users",
            "description": "사용자 가입·로그인·내 정보. HttpOnly 쿠키 인증.",
        },
        {"name": "stores", "description": "매장 관리. 등록자 = 점장(MANAGER) 정책."},
        {
            "name": "invitations",
            "description": "매장 워크스페이스 초대 토큰 수락. 24h TTL.",
        },
        {
            "name": "products",
            "description": "상품 카탈로그. 사진 촬영 기반 등록 흐름 + 매장 단위 공유.",
        },
        {
            "name": "fixtures",
            "description": "집기 마스터/진열 프리셋. 같은 매장 멤버끼리 공유.",
        },
        {
            "name": "layouts",
            "description": "매장 기획안. 한 매장당 활성 1개 불변식.",
        },
        {
            "name": "assets",
            "description": "3D 모델 에셋 (현재 미구현 — 추후 AI 연동).",
        },
    ]
}

register_exception_handlers(api)

api.add_router("/auth", auth_router)
api.add_router("/users", users_router)
api.add_router("/stores", stores_router)
# /invitations 는 stores 앱이 서빙 (코드 colocation, URL 은 invitee 관점이라 분리).
api.add_router("/invitations", invitations_router)
api.add_router("/products", products_router)
api.add_router("/fixtures", fixtures_router)
api.add_router("/layouts", layouts_router)
# /stores/{id}/layouts — 코드는 layouts 앱 안에 두고 prefix 만 stores 로 마운트.
api.add_router("/stores", stores_layouts_router)
api.add_router("/assets", assets_router)


@api.get("/hello", tags=["users"])
def hello(request):
    return ok(data={"message": "Hello from DoGo-BE"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns += [re_path(r"^api/", api_not_found_view)]
