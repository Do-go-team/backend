from datetime import datetime

from ninja import Schema
from pydantic import Field, field_validator

from common.schemas import Asset3DOut
from common.validators import validate_http_url


# ---------- Requests ----------


class ProductCaptureItemIn(Schema):
    # AI 가 사진 분석 후 객체별로 주는 3개 필드만 받음.
    # name/price/depth/options 등은 사용자가 추후 채울 placeholder.
    image_url: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="AI 가 외부 storage 에 올린 crop 이미지 URL (http/https)",
        examples=["https://s3.example.com/crop_1.jpg"],
    )
    width: int = Field(..., ge=1, description="객체 가로 (cm)")
    height: int = Field(..., ge=1, description="객체 세로 (cm)")

    @field_validator("image_url")
    @classmethod
    def _validate_image_url(cls, v):
        return validate_http_url(v)


class ProductCreateIn(Schema):
    # 사진 촬영 시점에 사용자가 어느 매장의 fixture 위에 있었는지 — FE 가
    # context 알고 있으니 body 로 전달. master 와 함께 store_products row 도
    # 같은 트랜잭션에 INSERT → 매장 단위 가시성 (162) bridge 채움.
    store_id: int = Field(..., description="사진 촬영한 매장 ID")
    # 사진 1번 촬영 = AI 인식 객체 N개 일괄 등록. 빈 배열은 의미 없으므로
    # min_length=1 강제.
    products: list[ProductCaptureItemIn] = Field(
        ..., min_length=1, description="AI 가 인식한 객체 목록 (1개 이상)"
    )
    # 상품 등록과 동시에 GPU Worker 큐에 3D 생성 task 를 PENDING 으로 예약.
    # /products 자체가 3D 파일을 만들지 않음 — assets_3d 도메인의 task 1건만 생성.
    # auto_create_3d_task 는 동의어 — FE 측 키 이름 변동 흡수.
    create_3d_task: bool = Field(
        False, description="true 시 등록과 동시에 3D 생성 task 예약"
    )
    auto_create_3d_task: bool = Field(
        False, description="create_3d_task 동의어 (FE 키명 변동 흡수)"
    )


class ProductVariantUpdateIn(Schema):
    # bulk-sync 규칙: id 있음 = UPDATE, id 없음 = INSERT, 누락 = SOFT DELETE.
    # image_index: multipart 로 함께 전송된 images 파일 array 의 index.
    id: int | None = Field(None, description="UPDATE 대상 variant ID. 누락 시 INSERT")
    size: str | None = Field(None, max_length=20, description="사이즈")
    color: str | None = Field(None, max_length=30, description="색상")
    sku_code: str | None = Field(
        None, max_length=50, description="고유 SKU 바코드. alive 끼리 unique"
    )
    image_index: int | None = Field(
        None,
        ge=0,
        description="multipart images 배열의 0-based index. 매칭된 파일이 바코드 이미지로 저장",
    )


class ProductUpdateIn(Schema):
    name: str | None = Field(None, max_length=100, description="상품명")
    price: int | None = Field(None, ge=0, description="가격")
    image_url: str | None = Field(
        None, max_length=512, description="상품 대표 이미지 URL (http/https)"
    )
    width: int | None = Field(None, ge=1, description="가로 (cm)")
    height: int | None = Field(None, ge=1, description="높이 (cm)")
    depth: int | None = Field(None, ge=1, description="세로 (cm)")
    variants: list[ProductVariantUpdateIn] | None = Field(
        None,
        description="bulk-sync 대상. 키 부재 → 미수정 / `[]` → 전체 삭제 / `[{...}]` → 3-rule sync",
    )

    @field_validator("image_url")
    @classmethod
    def _validate_image_url(cls, v):
        return validate_http_url(v)


# ---------- Responses ----------


class ProductVariantOut(Schema):
    id: int
    size: str | None = None
    color: str | None = None
    sku_code: str | None = None
    barcode_image_url: str | None = None


class ProductItemOut(Schema):
    id: int
    name: str | None = Field(None, description="placeholder 시 null")
    price: int | None = Field(None, description="placeholder 시 null")
    image_url: str | None = None
    width: int
    height: int
    depth: int | None = None
    asset_3d: Asset3DOut | None = Field(
        None, description="현재 항상 null (Asset3D 미구현)"
    )
    variants: list[ProductVariantOut] = Field(default_factory=list)


class ProductListOut(Schema):
    products: list[ProductItemOut] = Field(default_factory=list)


class ProductVariantDetailOut(Schema):
    variant_id: int = Field(...)
    size: str | None = None
    color: str | None = None
    sku_code: str | None = None
    barcode_image_url: str | None = None


class ProductDetailOut(Schema):
    product_id: int = Field(...)
    name: str | None = None
    price: int | None = None
    image_url: str | None = None
    width: int
    height: int
    depth: int | None = None
    asset_3d: Asset3DOut | None = None
    variants: list[ProductVariantDetailOut] = Field(default_factory=list)


class ProductCreatedItemOut(Schema):
    master_id: int = Field(..., description="등록된 product_masters.id")
    variant_id: int = Field(..., description="등록된 placeholder variant ID")
    image_url: str | None = None
    width: int
    height: int
    created_at: datetime


class ProductCreateOut(Schema):
    products: list[ProductCreatedItemOut] = Field(default_factory=list)


class UpdatedVariantOut(Schema):
    variant_id: int = Field(...)
    size: str | None = None
    color: str | None = None
    sku_code: str | None = None
    barcode_image_url: str | None = None


class ProductUpdateOut(Schema):
    product_id: int = Field(...)
    name: str | None = None
    synced_variants_count: int = Field(..., description="INSERT + UPDATE 합")
    deleted_variants_count: int = Field(..., description="SOFT DELETE 개수")
    updated_at: datetime
    variants: list[UpdatedVariantOut] = Field(
        default_factory=list, description="INSERT/UPDATE 한 variant 의 현재 상태"
    )


class ProductDeleteOut(Schema):
    deleted_product_id: int
    deleted_variants_count: int
