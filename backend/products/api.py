"""Router for the "products" domain. Endpoints to be added per API spec."""

from ninja import File, Form, Router
from ninja.files import UploadedFile
from pydantic import ValidationError as PydanticValidationError

from auth.authentication import CookieJWTAuth
from common.exceptions import BusinessException
from common.openapi_helpers import error_examples
from common.response import ok
from common.schemas import Envelope, ErrorOut

from .detection_api import router as detection_router
from .schemas import (
    ProductCreateIn,
    ProductCreateOut,
    ProductDeleteOut,
    ProductDetailOut,
    ProductListOut,
    ProductUpdateIn,
    ProductUpdateOut,
)
from .services import (
    create_products,
    delete_product,
    get_product_detail,
    list_products,
    update_product,
)

router = Router(tags=["products"])
router.add_router("/detection-tasks", detection_router)


_UNAUTHORIZED = (
    401,
    "UNAUTHORIZED_USER",
    "인증 정보가 유효하지 않거나 로그인이 필요합니다.",
)
_PRODUCT_NOT_FOUND = (
    404,
    "PRODUCT_NOT_FOUND",
    "존재하지 않거나 접근 권한이 없는 상품입니다.",
)


@router.post(
    "",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductCreateOut],
        401: ErrorOut,
        404: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (404, "STORE_NOT_FOUND", "존재하지 않거나 접근 권한이 없는 매장입니다."),
        (422, "INVALID_PARAMETER", "요청 형식이 올바르지 않습니다."),
    ),
    summary="상품 등록 (사진 촬영 흐름)",
    description=(
        "AI 가 인식한 객체별 image_url/width/height 만 받아 일괄 등록. master + variant + "
        "store_products 한 트랜잭션. master 의 name/price/depth + variant 옵션 필드는 모두 "
        "null 로 시작 — 사용자가 추후 PATCH 로 채움."
    ),
)
def product_create(request, payload: ProductCreateIn):
    return ok(
        data=create_products(request.auth, payload),
        message="상품이 성공적으로 등록되었습니다.",
    )


@router.get(
    "",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductListOut],
        401: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED),
    summary="전체 상품 목록 조회",
    description=(
        "매장 단위 공유 카탈로그 — store_products bridge 로 가시성 결정. 사용자가 멤버인 매장에 "
        "등록된 master 만 노출. 정렬 created_at DESC."
    ),
)
def product_list(request):
    return ok(
        data=list_products(request.auth),
        message="상품 목록 조회에 성공했습니다.",
    )


# 스펙 path: GET /products/{product_id}/variants — master+variants 묶음 응답.
@router.get(
    "/{product_id}/variants",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductDetailOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(_UNAUTHORIZED, _PRODUCT_NOT_FOUND),
    summary="상품 상세 조회",
    description="가시성 정책은 목록 조회와 동일. 비가시 → PRODUCT_NOT_FOUND 404 통합.",
)
def product_detail(request, product_id: int):
    return ok(
        data=get_product_detail(request.auth, product_id),
        message="상품 상세 조회에 성공했습니다.",
    )


# 165 — 상품 부분 수정 (multipart). 167 (POST /products/{id}/variants) 의 책임을
# 흡수: 카메라 스캔 흐름 (sku/size/color + barcode image 일괄 등록) 도 본 endpoint
# 한 번에 처리. multipart form fields:
#   - data: JSON-encoded ProductUpdateIn (메타 + variants bulk-sync 항목)
#   - images: optional file array. variants[].image_index 로 매칭.
@router.patch(
    "/{product_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductUpdateOut],
        401: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
        422: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        _PRODUCT_NOT_FOUND,
        (409, "SKU_DUPLICATED", "이미 등록된 SKU 코드가 포함되어 있습니다."),
        (422, "INVALID_PARAMETER", "요청 형식이 올바르지 않습니다."),
        (
            422,
            "INVALID_VARIANT_ID",
            "해당 상품에 속하지 않는 옵션 ID가 포함되어 있습니다.",
        ),
        (
            422,
            "INVALID_IMAGE_INDEX",
            "전송된 이미지 파일과 매칭되지 않는 image_index 가 포함되어 있습니다.",
        ),
    ),
    summary="상품 수정 (multipart)",
    description=(
        "Content-Type: multipart/form-data. `data` field 에 JSON 문자열, `images` field 에 파일 배열. "
        "variants bulk-sync: id 있음 UPDATE / 없음 INSERT / 누락 SOFT DELETE / `[]` 전체 삭제. "
        "기존 POST /products/{id}/variants (바코드 업로드) 기능 흡수."
    ),
)
def product_update(
    request,
    product_id: int,
    data: str = Form(...),
    images: list[UploadedFile] = File(None),
):
    try:
        payload = ProductUpdateIn.model_validate_json(data)
    except PydanticValidationError as exc:
        raise BusinessException(
            "INVALID_PARAMETER",
            "요청 형식이 올바르지 않습니다.",
            status=422,
        ) from exc
    return ok(
        data=update_product(request.auth, product_id, payload, images or []),
        message="상품 정보가 성공적으로 수정되었습니다.",
    )


@router.delete(
    "/{product_id}",
    auth=CookieJWTAuth(),
    response={
        200: Envelope[ProductDeleteOut],
        401: ErrorOut,
        404: ErrorOut,
    },
    openapi_extra=error_examples(
        _UNAUTHORIZED,
        (404, "PRODUCT_NOT_FOUND", "존재하지 않거나 이미 삭제된 상품입니다."),
    ),
    summary="상품 삭제",
    description=(
        "4단계 트랜잭션: variants soft + store_inventories hard + store_products hard + master soft. "
        "매장 멤버 누구나 (가시 범위 내)."
    ),
)
def product_delete(request, product_id: int):
    return ok(
        data=delete_product(request.auth, product_id),
        message="상품이 성공적으로 삭제 처리되었습니다.",
    )
