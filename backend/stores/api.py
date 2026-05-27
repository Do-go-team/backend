"""Router for the "stores" domain. Endpoints to be added per API spec."""

from ninja import File, Router
from ninja.files import UploadedFile

from auth.authentication import CookieJWTAuth
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut

from .schemas import (
    FloorplanUploadOut,
    InvitationAcceptIn,
    InvitationAcceptOut,
    InvitationCreateIn,
    InvitationCreateOut,
    MemberRoleUpdateIn,
    MemberRoleUpdateOut,
    MembersOut,
    StoreDetailOut,
    StoreProductAssignIn,
    StoreProductAssignOut,
    StoreProductDeleteOut,
    StoreProductsOut,
    StoreProductUpdateIn,
    StoreProductUpdateOut,
    StoreUpdateIn,
    StoreUpdateOut,
)
from .services import (
    accept_store_invitation,
    assign_products_to_store,
    create_store_invitation,
    delete_store,
    delete_store_product,
    get_store_detail,
    list_store_members,
    list_store_products,
    update_store,
    update_store_member_role,
    update_store_product,
    upload_store_floorplan,
)

router = Router(tags=["stores"])
# /invitations 경로용 별도 라우터. 코드는 stores 앱 안에 모음 (StoreInvitation
# 모델·서비스·테스트 colocate). config/urls.py 에서 별도 prefix 로 마운트.
invitations_router = Router(tags=["invitations"])


_UNAUTHORIZED = (
    401,
    "UNAUTHORIZED_USER",
    "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
)
_STORE_NOT_FOUND = (
    404,
    "STORE_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 매장입니다.",
)


@router.get(
    "/{store_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreDetailOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _STORE_NOT_FOUND),
    summary="특정 매장 상세 정보 조회",
    description="매장 멤버 누구나 조회 가능. 비회원/미존재/삭제 매장 모두 STORE_NOT_FOUND 404 통합.",
)
def store_detail(request, store_id: int):
    return ok(
        data=get_store_detail(request.auth, store_id),
        message="매장 상세 정보 조회에 성공했습니다.",
    )


@router.patch(
    "/{store_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreUpdateOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN_ACCESS", "매장 정보를 수정할 권한이 없습니다."),
        _STORE_NOT_FOUND,
    ),
    summary="매장 정보 수정",
    description=(
        "권한: 매장 관리자(MANAGER/VICE_MANAGER/VMD). PATCH 부분 수정 — 보낸 키만 변경. "
        "이미지 필드는 URL 시 upsert, 명시적 null 시 row 삭제."
    ),
)
def store_update(request, store_id: int, payload: StoreUpdateIn):
    return ok(
        data=update_store(request.auth, store_id, payload),
        message="매장 정보가 성공적으로 수정되었습니다.",
    )


@router.delete(
    "/{store_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[None],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN_ACCESS", "매장을 삭제할 권한이 없습니다. (점장 권한 필요)"),
        _STORE_NOT_FOUND,
    ),
    summary="매장 삭제",
    description="권한: 점장(MANAGER) 만. 매장 + 활성 레이아웃 cascade 소프트 삭제.",
)
def store_delete(request, store_id: int):
    delete_store(request.auth, store_id)
    return ok(data=None, message="매장이 성공적으로 삭제되었습니다.")


@router.post(
    "/{store_id}/floorplan",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[FloorplanUploadOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        413: ErrorOut,
        415: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_ACCESS",
            "해당 매장의 도면 이미지를 업로드할 권한이 없습니다.",
        ),
        _STORE_NOT_FOUND,
        (413, "PAYLOAD_TOO_LARGE", "업로드 가능한 파일 용량(10MB)을 초과했습니다."),
        (
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "지원하지 않는 파일 형식입니다. (jpg, png만 허용)",
        ),
    ),
    summary="2D 도면 이미지 업로드",
    description=(
        "multipart/form-data. jpg/png, ≤ 10MB. 같은 매장 FLOORPLAN row 있으면 덮어쓰기 (upsert). "
        "권한: 매장 관리자."
    ),
)
def store_floorplan_upload(
    request,
    store_id: int,
    file: UploadedFile = File(...),
):
    return ok(
        data=upload_store_floorplan(request.auth, store_id, file),
        message="도면 이미지가 성공적으로 업로드되었습니다.",
    )


@router.post(
    "/{store_id}/products",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreProductAssignOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_ACCESS",
            "해당 매장에 상품을 할당할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
        ),
        _STORE_NOT_FOUND,
        (
            422,
            "INVALID_PRODUCT_IDS",
            "유효하지 않은 상품 ID 가 포함되어 있습니다: [99, 100]",
        ),
    ),
    summary="매장 상품 등록",
    description=(
        "기존 master 들을 매장에 일괄 매핑. 유효하지 않은 ID 하나라도 있으면 전체 거부 (strict). "
        "PAUSED → ACTIVE reactivate. 매핑된 master 의 모든 variant 에 StoreInventory(0) 자동 생성."
    ),
)
def store_products_assign(request, store_id: int, payload: StoreProductAssignIn):
    return ok(
        data=assign_products_to_store(request.auth, store_id, payload.product_ids),
        message="선택한 상품들이 매장에 성공적으로 할당되었습니다.",
    )


@router.get(
    "/{store_id}/products",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreProductsOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _STORE_NOT_FOUND),
    summary="매장 상품 조회",
    description=(
        "매장 멤버 누구나. status 3종 ACTIVE/PAUSED/DISCONTINUED — master 단종 시 합성. "
        "soft-deleted variant 도 is_discontinued: true 로 포함."
    ),
)
def store_products_list(request, store_id: int):
    return ok(
        data=list_store_products(request.auth, store_id),
        message="매장 취급 상품 목록 조회에 성공했습니다.",
    )


@router.patch(
    "/{store_id}/products/{product_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreProductUpdateOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (403, "FORBIDDEN_ACCESS", "해당 매장의 상품 정보를 수정할 권한이 없습니다."),
        _STORE_NOT_FOUND,
        (404, "PRODUCT_NOT_ASSIGNED", "해당 매장에 할당되어 있지 않은 상품입니다."),
    ),
    summary="매장 상품 수정",
    description="status 만 변경 가능 (ACTIVE/PAUSED). DISCONTINUED 는 합성 값이라 입력 거부.",
)
def store_product_update(
    request,
    store_id: int,
    product_id: int,
    payload: StoreProductUpdateIn,
):
    return ok(
        data=update_store_product(request.auth, store_id, product_id, payload),
        message="매장 상품의 취급 상태가 성공적으로 수정되었습니다.",
    )


@router.delete(
    "/{store_id}/products/{product_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[StoreProductDeleteOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_ACCESS",
            "해당 매장의 정보를 수정할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
        ),
        _STORE_NOT_FOUND,
        (404, "PRODUCT_NOT_ASSIGNED", "해당 매장에 할당되어 있지 않은 상품입니다."),
    ),
    summary="매장 상품 삭제",
    description=(
        "store_products + 같은 매장 variant 재고 row 모두 하드 삭제. master 자체는 본사 자산이라 손대지 않음."
    ),
)
def store_product_delete(request, store_id: int, product_id: int):
    return ok(
        data=delete_store_product(request.auth, store_id, product_id),
        message="매장의 취급 상품 목록에서 성공적으로 삭제되었습니다.",
    )


@router.get(
    "/{store_id}/members",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[MembersOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _STORE_NOT_FOUND),
    summary="직원 목록 및 관리자 TO 조회",
    description="매장 멤버 누구나 (read-only). admin_quota.current = MANAGER+VICE_MANAGER+VMD 합.",
)
def store_members_list(request, store_id: int):
    return ok(
        data=list_store_members(request.auth, store_id),
        message="매장 직원 목록 조회에 성공했습니다.",
    )


@router.patch(
    "/{store_id}/members/{user_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[MemberRoleUpdateOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_ACTION",
            "직원의 권한을 변경할 수 있는 관리자 권한이 없습니다.",
        ),
        _STORE_NOT_FOUND,
        (404, "MEMBER_NOT_FOUND", "해당 매장의 직원이 아닙니다."),
        (
            409,
            "ADMIN_QUOTA_EXCEEDED",
            "해당 매장의 관리자 계정 생성 한도를 초과하여 승급할 수 없습니다.",
        ),
    ),
    summary="직원 권한 수정",
    description=(
        "권한: 관리자. allowed roles: MANAGER/VICE_MANAGER/VMD/STAFF (OWNER 임명 거부). "
        "STAFF→관리자급 승급 시 정원 초과면 ADMIN_QUOTA_EXCEEDED 409."
    ),
)
def store_member_role_update(
    request,
    store_id: int,
    user_id: int,
    payload: MemberRoleUpdateIn,
):
    return ok(
        data=update_store_member_role(request.auth, store_id, user_id, payload),
        message="직원 권한이 성공적으로 변경되었습니다.",
    )


@router.post(
    "/{store_id}/invitations",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[InvitationCreateOut],
        401: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (
            403,
            "FORBIDDEN_ACCESS",
            "매장에 인원을 초대할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
        ),
        _STORE_NOT_FOUND,
        (409, "ALREADY_MEMBER", "해당 이메일 사용자는 이미 이 매장의 멤버입니다."),
        (409, "ALREADY_INVITED", "해당 이메일로 이미 대기 중인 초대장이 있습니다."),
        (
            409,
            "EDITOR_QUOTA_EXCEEDED",
            "현재 에디터(MANAGER/VICE_MANAGER/VMD) 정원이 꽉 찼거나, 대기 중인 초대장이 있어 더 이상 해당 직급으로 초대할 수 없습니다.",
        ),
    ),
    summary="매장 워크스페이스 초대 링크 생성",
    description=(
        "권한: 관리자. 토큰 TTL 24h. 중복 초대 ALREADY_INVITED, 정원 초과 EDITOR_QUOTA_EXCEEDED, "
        "이미 멤버 ALREADY_MEMBER. transaction.on_commit 으로 Celery 메일 발송."
    ),
)
def store_invitation_create(
    request,
    store_id: int,
    payload: InvitationCreateIn,
):
    return ok(
        data=create_store_invitation(request.auth, store_id, payload),
        message="초대장이 성공적으로 발송되었습니다.",
    )


# ── /invitations 마운트용 (config/urls.py 에서 별도 prefix 등록) ──


@invitations_router.post(
    "/accept",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[InvitationAcceptOut],
        401: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
        410: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (404, "INVALID_TOKEN", "유효하지 않거나 이미 사용된 초대 토큰입니다."),
        (409, "ALREADY_MEMBER", "이미 이 매장의 멤버입니다."),
        (
            410,
            "INVITATION_EXPIRED",
            "초대 유효 기간이 만료되었습니다. 관리자에게 재발송을 요청하세요.",
        ),
    ),
    summary="매장 초대 수락",
    description=(
        "토큰 미존재/사용됨/이메일 미일치/매장 삭제 모두 INVALID_TOKEN 404 통합 — 토큰 존재성 노출 차단. "
        "만료 INVITATION_EXPIRED 410, 이미 멤버 ALREADY_MEMBER 409."
    ),
)
def invitation_accept(request, payload: InvitationAcceptIn):
    return ok(
        data=accept_store_invitation(request.auth, payload),
        message="매장 워크스페이스에 성공적으로 합류했습니다.",
    )
