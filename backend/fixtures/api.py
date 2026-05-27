"""Router for the "fixtures" domain."""

from ninja import File, Form, Router
from ninja.files import UploadedFile

from auth.authentication import CookieJWTAuth
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut
from products.detection_schemas import ProductDetectionTaskCreateOut
from products.detection_services import create_detection_task

from .schemas import (
    FixtureCreateIn,
    FixtureCreateOut,
    FixtureDetailOut,
    FixturesOut,
    FixtureUpdateIn,
    FixtureUpdateOut,
    PlacementsOut,
    PlacementsUpdateIn,
    PlacementsUpdateOut,
    VersionCreateIn,
    VersionCreateOut,
    VersionsOut,
)
from .services import (
    create_fixture,
    create_fixture_version,
    delete_fixture,
    delete_fixture_version,
    get_fixture_detail,
    list_fixture_versions,
    list_fixtures,
    list_version_placements,
    update_fixture,
    update_version_placements,
)

router = Router(tags=["fixtures"])


_UNAUTHORIZED = (
    401,
    "UNAUTHORIZED_USER",
    "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
)
_FIXTURE_NOT_FOUND = (
    404,
    "FIXTURE_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 집기 정보입니다.",
)
_VERSION_NOT_FOUND = (
    404,
    "VERSION_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 프리셋(진열 버전)입니다.",
)


@router.get(
    "",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FixturesOut],
        401: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED),
    summary="전체 집기 조회",
    description=(
        "매장 단위 공유 — 본인 또는 같은 매장 멤버가 등록한 alive fixture 노출. "
        "매장 멤버십 0건이면 빈 배열. 정렬 created_at ASC."
    ),
)
def fixtures_list(request):
    return ok(
        data=list_fixtures(request.auth),
        message="전체 집기 목록 조회에 성공했습니다.",
    )


@router.post(
    "",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FixtureCreateOut],
        401: ErrorOut,
        403: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_NO_STORE",
            "fixture 를 등록하려면 1개 이상의 매장에 멤버로 속해 있어야 합니다.",
        ),
        (422, "INVALID_PARAMETER", "요청 형식이 올바르지 않습니다."),
    ),
    summary="집기 추가",
    description="새 마스터 집기 등록. 매장 멤버 1개 이상 필수 (0건 사용자 거부 FORBIDDEN_NO_STORE 403).",
)
def fixture_create(request, payload: FixtureCreateIn):
    return ok(
        data=create_fixture(request.auth, payload),
        message="새로운 집기가 성공적으로 등록되었습니다.",
    )


@router.get(
    "/{fixture_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FixtureDetailOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _FIXTURE_NOT_FOUND),
    summary="특정 집기 조회",
    description="매장 단위 공유 가시 범위만. 비가시/미존재/삭제 모두 FIXTURE_NOT_FOUND 404 통합.",
)
def fixture_detail(request, fixture_id: int):
    return ok(
        data=get_fixture_detail(request.auth, fixture_id),
        message="집기 상세 정보 조회에 성공했습니다.",
    )


@router.patch(
    "/{fixture_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FixtureUpdateOut],
        401: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _FIXTURE_NOT_FOUND,
        (
            409,
            "FIXTURE_IS_NOT_EMPTY",
            "집기 크기를 수정하시려면 진열된 제품을 모두 제거해주세요.",
        ),
    ),
    summary="특정 집기 이름 및 크기 수정",
    description=(
        "권한: 매장 관리자(MANAGER/VICE_MANAGER/VMD). 크기 변경 시 진열된 상품 있으면 "
        "FIXTURE_IS_NOT_EMPTY 409. admin 권한 없음도 NOT_FOUND 통합."
    ),
)
def fixture_update(request, fixture_id: int, payload: FixtureUpdateIn):
    return ok(
        data=update_fixture(request.auth, fixture_id, payload),
        message="집기 정보가 성공적으로 수정되었습니다.",
    )


@router.delete(
    "/{fixture_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[None],
        401: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _FIXTURE_NOT_FOUND,
        (
            409,
            "FIXTURE_IN_USE",
            "현재 레이아웃에 배치되어 사용 중인 집기는 삭제할 수 없습니다. 배치를 먼저 해제해주세요.",
        ),
    ),
    summary="특정 집기 삭제",
    description=(
        "권한: 매장 관리자. 활성 레이아웃에 배치돼 있으면 FIXTURE_IN_USE 409. "
        "소프트 삭제 + 활성 진열 버전 cascade."
    ),
)
def fixture_delete(request, fixture_id: int):
    delete_fixture(request.auth, fixture_id)
    return ok(message="원형 집기가 성공적으로 삭제되었습니다.")


@router.get(
    "/{fixture_id}/versions",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[VersionsOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _FIXTURE_NOT_FOUND),
    summary="특정 집기의 진열 버전 조회",
    description="매장 멤버 누구나. 활성 version 만. 정렬 updated_at DESC, id DESC.",
)
def fixture_versions_list(request, fixture_id: int):
    return ok(
        data=list_fixture_versions(request.auth, fixture_id),
        message="집기의 진열 버전 목록 조회에 성공했습니다.",
    )


@router.post(
    "/{fixture_id}/versions",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[VersionCreateOut],
        400: ErrorOut,
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        (400, "INVALID_PARAMETER", "진열 버전 이름(version_name)은 필수 입력값입니다."),
        _UNAUTHORIZED,
        _FIXTURE_NOT_FOUND,
    ),
    summary="특정 집기의 진열 버전 생성",
    description="매장 멤버 누구나. version_name strip 후 빈 문자열이면 INVALID_PARAMETER 400. 빈 진열대 시작.",
)
def fixture_version_create(request, fixture_id: int, payload: VersionCreateIn):
    return ok(
        data=create_fixture_version(request.auth, fixture_id, payload),
        message="집기의 새로운 진열 버전이 성공적으로 생성되었습니다.",
    )


@router.delete(
    "/{fixture_id}/versions/{version_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[None],
        401: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _VERSION_NOT_FOUND,
        (
            409,
            "VERSION_IN_USE",
            "현재 매장 레이아웃에 배치되어 사용 중인 진열 버전은 삭제할 수 없습니다. 배치를 먼저 해제해주세요.",
        ),
    ),
    summary="특정 집기 진열 버전 삭제",
    description="권한: 매장 관리자. 활성 레이아웃에 배치돼 있으면 VERSION_IN_USE 409. 소프트 삭제.",
)
def fixture_version_delete(request, fixture_id: int, version_id: int):
    delete_fixture_version(request.auth, fixture_id, version_id)
    return ok(message="집기 진열 버전이 성공적으로 삭제되었습니다.")


@router.get(
    "/{fixture_id}/versions/{version_id}/placements",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[PlacementsOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _VERSION_NOT_FOUND),
    summary="특정 집기 내부의 배치된 상품 목록 및 로컬 좌표 조회",
    description="매장 멤버 누구나. variant 정보는 id + sku_code 만 노출. 정렬 placement_id ASC.",
)
def fixture_version_placements_list(request, fixture_id: int, version_id: int):
    return ok(
        data=list_version_placements(request.auth, fixture_id, version_id),
        message="진열 버전 내부 상품 배치 목록 조회에 성공했습니다.",
    )


@router.patch(
    "/{fixture_id}/versions/{version_id}/placements",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[PlacementsUpdateOut],
        401: ErrorOut,
        404: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _VERSION_NOT_FOUND,
        (
            422,
            "INVALID_PLACEMENT_ID",
            "배치 목록에 유효하지 않은 placement_id 가 포함되어 있습니다.",
        ),
        (
            422,
            "INVALID_VARIANT_ID",
            "배치 목록 중 유효하지 않은 상품 옵션(variant)이 포함되어 있습니다.",
        ),
    ),
    summary="특정 집기 내부의 상품 로컬 좌표/메모/상태 수정 및 저장",
    description=(
        "매장 멤버 누구나. bulk-sync: placement_id 있음 UPDATE / 없음 INSERT / 누락 HARD DELETE. "
        "빈 배열 → 전체 삭제. 사전 검증 실패 시 전체 롤백."
    ),
)
def fixture_version_placements_update(
    request, fixture_id: int, version_id: int, payload: PlacementsUpdateIn
):
    return ok(
        data=update_version_placements(request.auth, fixture_id, version_id, payload),
        message="집기 내 상품 배치 정보가 성공적으로 저장되었습니다.",
    )


@router.post(
    "/{fixture_id}/detect-products",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductDetectionTaskCreateOut],
        401: ErrorOut,  # UNAUTHORIZED_USER
        404: ErrorOut,  # FIXTURE_NOT_FOUND
        422: ErrorOut,  # INVALID_PARAMETER
        500: ErrorOut,  # INVALID_BACKEND_BASE_URL / S3_NOT_CONFIGURED
    },
    summary="집기 사진에서 상품 후보 탐지 작업 생성",
    description=(
        "multipart/form-data 로 진열대 사진을 업로드하면 ProductDetectionTask 를 PENDING 으로 "
        "생성하고 ai-worker Celery queue 에 enqueue. ai-worker 가 YOLO segmentation → presigned "
        "PUT 으로 thumbnail 업로드 → /detection-tasks/{id}/complete 콜백을 비동기 처리. "
        "사용자는 GET /api/v1/products/detection-tasks/{id} 로 진행 상태와 후보 목록을 조회. "
        "store_id, fixture_version_id 는 선택. callback_base_url 은 settings.AI_CALLBACK_BASE_URL "
        "이 우선이며 미설정 시 요청 호스트가 fallback."
    ),
)
def fixture_detect_products(
    request,
    fixture_id: int,
    image: UploadedFile = File(...),
    store_id: int | None = Form(None),
    fixture_version_id: int | None = Form(None),
):
    return ok(
        data=create_detection_task(
            user=request.auth,
            fixture_id=fixture_id,
            image_file=image,
            store_id=store_id,
            fixture_version_id=fixture_version_id,
            callback_base_url=request.build_absolute_uri("/").rstrip("/"),
        ),
        message="상품 탐지 작업이 생성되었습니다.",
    )
