"""Router for the "layouts" domain.

두 router 로 나뉜 이유:
- ``router`` 는 ``/layouts/{id}`` 경로용 (상세/수정/삭제).
- ``stores_layouts_router`` 는 ``/stores/{id}/layouts`` 경로용 (생성/목록).
  URL 의 ``/stores/`` prefix 는 "어느 매장의 레이아웃이냐" 를 표현하는 컨텍스트일 뿐
  도메인 소속은 layouts 다 — 그래서 코드는 layouts 앱 안에 colocate.
  stores 앱의 ``invitations_router`` 와 동일한 분리 패턴.
"""

from ninja import File, Router
from ninja.files import UploadedFile

from auth.authentication import CookieJWTAuth
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut

from .schemas import (
    FloorplanParseOut,
    LayoutCreateIn,
    LayoutCreateOut,
    LayoutDeleteOut,
    LayoutDetailOut,
    LayoutExportIn,
    LayoutExportOut,
    LayoutFixturesCopyIn,
    LayoutFixturesCopyOut,
    LayoutsOut,
    LayoutUpdateIn,
    LayoutUpdateOut,
)
from .services import (
    copy_layout_fixtures,
    create_layout,
    delete_layout,
    export_layout,
    get_layout_detail,
    list_layouts,
    parse_layout_floorplan,
    update_layout,
)

router = Router(tags=["layouts"])
stores_layouts_router = Router(tags=["layouts"])


_UNAUTHORIZED = (
    401,
    "UNAUTHORIZED_USER",
    "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
)
_LAYOUT_NOT_FOUND = (
    404,
    "LAYOUT_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 레이아웃입니다.",
)
_STORE_NOT_FOUND = (
    404,
    "STORE_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 매장입니다.",
)


@stores_layouts_router.post(
    "/{store_id}/layouts",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutCreateOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN_ACCESS", "해당 매장에 레이아웃을 생성할 권한이 없습니다."),
        _STORE_NOT_FOUND,
    ),
    summary="레이아웃 생성",
    description=(
        "권한: 매장 관리자(MANAGER/VICE_MANAGER/VMD). is_active=true 토글 시 기존 활성 레이아웃 "
        "자동 비활성화 (한 매장당 활성 1개 불변식)."
    ),
)
def layout_create(request, store_id: int, payload: LayoutCreateIn):
    return ok(
        data=create_layout(request.auth, store_id, payload),
        message="레이아웃이 성공적으로 생성되었습니다.",
    )


@stores_layouts_router.get(
    "/{store_id}/layouts",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutsOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _STORE_NOT_FOUND),
    summary="레이아웃 목록 조회",
    description="매장 멤버 누구나. 정렬: is_active DESC (활성 우선) → created_at DESC.",
)
def layout_list(request, store_id: int):
    return ok(
        data=list_layouts(request.auth, store_id),
        message="레이아웃 목록 조회에 성공했습니다.",
    )


@router.get(
    "/{layout_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutDetailOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _LAYOUT_NOT_FOUND),
    summary="레이아웃 상세 조회",
    description=(
        "3D 캔버스 즉시 렌더용 aggregate 응답 (레이아웃 메타 + 매장 규격 + 배치 집기 + 집기별 3D 모델 URL). "
        "소프트 삭제된 fixture_version / fixture_master 도 스냅샷 성격으로 그대로 노출."
    ),
)
def layout_detail(request, layout_id: int):
    return ok(
        data=get_layout_detail(request.auth, layout_id),
        message="레이아웃 상세 정보 조회에 성공했습니다.",
    )


@router.delete(
    "/{layout_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutDeleteOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN", "해당 레이아웃을 삭제할 권한이 없습니다."),
        _LAYOUT_NOT_FOUND,
        (
            409,
            "ACTIVE_LAYOUT_DELETE_DENIED",
            "현재 매장에 적용 중인 활성 레이아웃은 삭제할 수 없습니다. 비활성화 후 다시 시도해 주세요.",
        ),
    ),
    summary="레이아웃 삭제",
    description=(
        "권한: 매장 관리자. 활성 레이아웃은 실수 삭제 방지로 차단 → ACTIVE_LAYOUT_DELETE_DENIED 409. "
        "소프트 삭제 (LayoutFixture 는 그대로 유지)."
    ),
)
def layout_delete(request, layout_id: int):
    return ok(
        data=delete_layout(request.auth, layout_id),
        message="레이아웃이 성공적으로 삭제되었습니다.",
    )


@router.patch(
    "/{layout_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutUpdateOut],
        401: ErrorOut,
        404: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _LAYOUT_NOT_FOUND,
        (
            422,
            "INVALID_FIXTURE_DATA",
            "유효하지 않은 집기 좌표 또는 회전 값이 포함되어 있습니다.",
        ),
        (422, "INVALID_PARAMETER", "요청 형식이 올바르지 않습니다."),
    ),
    summary="레이아웃 수정",
    description=(
        "매장 멤버 누구나 (STAFF 포함). fixtures 키 부재 → 메타만 / `[]` → 전체 삭제 / `[{...}]` → "
        "3-rule bulk-sync. INSERT 시 사이즈 누락이면 fixture_master 값 자동 복사. world_rot_y 는 "
        "저장 시 % 360 자동 정규화."
    ),
)
def layout_update(request, layout_id: int, payload: LayoutUpdateIn):
    return ok(
        data=update_layout(request.auth, layout_id, payload),
        message="레이아웃 정보가 성공적으로 수정되었습니다.",
    )


@router.post(
    "/{layout_id}/fixtures:copy",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutFixturesCopyOut],
        401: ErrorOut,
        404: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _LAYOUT_NOT_FOUND,
        (
            422,
            "INVALID_LAYOUT_FIXTURE_ID",
            "배치 목록에 유효하지 않은 layout_fixture_id 가 포함되어 있습니다.",
        ),
    ),
    summary="레이아웃 집기 다중 복사",
    description=(
        "매장 멤버 누구나. 선택된 LayoutFixture 들을 같은 layout 안에서 새 row 로 복제. "
        "FixtureVersion 도 새로 생성 (빈 진열대), placement 는 복제 X. 좌표/사이즈는 원본 그대로. "
        "cross-layout 또는 미존재 ID 포함 시 `INVALID_LAYOUT_FIXTURE_ID` 422 — DB 변화 0."
    ),
)
def layout_fixtures_copy(request, layout_id: int, payload: LayoutFixturesCopyIn):
    return ok(
        data=copy_layout_fixtures(request.auth, layout_id, payload),
        message="선택된 집기가 성공적으로 복사되었습니다.",
    )


@stores_layouts_router.post(
    "/{store_id}/layouts/{layout_id}/export",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[LayoutExportOut],
        400: ErrorOut,
        401: ErrorOut,
        404: ErrorOut,
        500: ErrorOut,
    },
    openapi_extra=error_examples(
        (
            400,
            "LAYOUT_STORE_MISMATCH",
            "요청하신 레이아웃이 해당 매장에 속해있지 않습니다.",
        ),
        _UNAUTHORIZED,
        _LAYOUT_NOT_FOUND,
        (500, "EXPORT_FAILED", "PDF 생성 중 오류가 발생했습니다."),
    ),
    summary="PDF 내보내기 (레이아웃 평면도)",
    description=(
        "매장 멤버 누구나. Top View 평면도를 PDF 로 렌더링 + MEDIA 저장 + download_url 발급. "
        "path 의 store_id 와 layout 소속 불일치 시 `LAYOUT_STORE_MISMATCH` 400. "
        "렌더링 실패 시 `EXPORT_FAILED` 500."
    ),
)
def layout_export(
    request,
    store_id: int,
    layout_id: int,
    payload: LayoutExportIn = LayoutExportIn(),
):
    return ok(
        data=export_layout(request.auth, store_id, layout_id, payload),
        message="레이아웃 PDF 파일이 성공적으로 생성되었습니다.",
    )


@router.post(
    "/{layout_id}/floorplan/parse",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FloorplanParseOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        413: ErrorOut,
        415: ErrorOut,
        500: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN_ACCESS", "해당 레이아웃의 도면을 분석할 권한이 없습니다."),
        _LAYOUT_NOT_FOUND,
        (413, "PAYLOAD_TOO_LARGE", "업로드 가능한 파일 용량(10MB)을 초과했습니다."),
        (
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "지원하지 않는 파일 형식입니다. (jpg, png만 허용)",
        ),
        (500, "PARSE_FAILED", "도면 인식에 실패했습니다."),
    ),
    summary="도면 이미지 파싱 및 자동 배치",
    description=(
        "권한: 매장 관리자 (MANAGER/VICE_MANAGER/VMD). multipart/form-data, jpg/png "
        "≤ 10MB. 한 transaction 으로 (1) Layout.floorplan_image_url 갱신, "
        "(2) FixtureMaster N개, (3) FixtureVersion N개 (master 별 '진열 1'), "
        "(4) LayoutFixture N개 생성. 분석 실패 시 PARSE_FAILED 500 + 전체 롤백."
    ),
)
def layout_floorplan_parse(
    request,
    layout_id: int,
    file: UploadedFile = File(...),
):
    return ok(
        data=parse_layout_floorplan(request.auth, layout_id, file),
        message="도면 이미지가 성공적으로 분석되었습니다.",
    )
