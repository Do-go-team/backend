from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import EmailStr, Field, field_validator

from common.validators import validate_http_url


# ---------- Requests ----------


class StoreCreateIn(Schema):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="매장 이름",
        examples=["DO-GO 성수 팝업"],
    )
    address: str = Field(..., min_length=1, max_length=255, description="매장 주소")
    width: int = Field(..., ge=1, description="가로 (cm)")
    height: int = Field(..., ge=1, description="천장 높이 (cm)")
    depth: int = Field(..., ge=1, description="세로 (cm)")
    floorplan_image_url: str | None = Field(
        None,
        max_length=512,
        description="평면도 이미지 URL (http/https). 빈 문자열은 null 로 정규화됨.",
        examples=["https://s3.example.com/floorplan.png", None],
    )
    actual_photo_url: str | None = Field(
        None,
        max_length=512,
        description="실제 현장 사진 URL (http/https). 빈 문자열은 null 로 정규화됨.",
    )

    @field_validator("floorplan_image_url", "actual_photo_url")
    @classmethod
    def _validate_image_urls(cls, v):
        return validate_http_url(v)


class StoreUpdateIn(Schema):
    name: str | None = Field(
        None, min_length=1, max_length=100, description="변경할 매장 이름"
    )
    address: str | None = Field(
        None, min_length=1, max_length=255, description="변경할 주소"
    )
    width: int | None = Field(None, ge=1, description="변경할 가로 (cm)")
    height: int | None = Field(None, ge=1, description="변경할 높이 (cm)")
    depth: int | None = Field(None, ge=1, description="변경할 세로 (cm)")
    floorplan_image_url: str | None = Field(
        None,
        max_length=512,
        description="평면도 이미지 URL. URL 시 upsert, 명시적 null 시 row 삭제.",
    )
    actual_photo_url: str | None = Field(
        None,
        max_length=512,
        description="실제 사진 URL. URL 시 upsert, 명시적 null 시 row 삭제.",
    )

    @field_validator("floorplan_image_url", "actual_photo_url")
    @classmethod
    def _validate_image_urls(cls, v):
        return validate_http_url(v)


class StoreProductAssignIn(Schema):
    product_ids: list[int] = Field(
        ..., description="매장에 매핑할 product_masters.id 목록", examples=[[1, 2, 3]]
    )


class StoreProductUpdateIn(Schema):
    # DISCONTINUED 는 master.deleted_at 합성 산출물이므로 입력 불가.
    # DB choices(StoreProduct.Status) 와 정합.
    status: Literal["ACTIVE", "PAUSED"] = Field(
        ..., description="매장 취급 상태. DISCONTINUED 는 응답 시점 합성이라 입력 거부."
    )


class InvitationCreateIn(Schema):
    invite_email: EmailStr = Field(..., description="초대할 이메일 (비회원도 가능)")
    # OWNER 임명 차단 (소유권 이관은 별도 advanced).
    target_role: Literal["MANAGER", "VICE_MANAGER", "VMD", "STAFF"] = Field(
        ..., description="수락 시 부여할 매장 권한. OWNER 임명 불가."
    )


class MemberRoleUpdateIn(Schema):
    # OWNER 는 임명 불가 (MVP 범위 — 소유권 이관은 별도 advanced 엔드포인트).
    role: Literal["MANAGER", "VICE_MANAGER", "VMD", "STAFF"] = Field(
        ..., description="변경할 매장 권한. OWNER 임명 불가."
    )


class InvitationAcceptIn(Schema):
    invite_token: str = Field(..., min_length=1, description="초대 메일에 포함된 토큰")


# ---------- Responses ----------


class MyStoreOut(Schema):
    store_id: int = Field(..., description="매장 ID")
    name: str = Field(..., description="매장 이름")
    width: int = Field(..., description="가로 (cm)")
    height: int = Field(..., description="높이 (cm)")
    depth: int = Field(..., description="세로 (cm)")
    my_role: str = Field(..., description="해당 매장에서의 권한")
    floorplan_image_url: str | None = Field(None, description="평면도 이미지 URL")
    actual_photo_url: str | None = Field(None, description="실제 사진 URL")
    created_at: datetime = Field(..., description="매장 생성 일시")


class MyStoresOut(Schema):
    stores: list[MyStoreOut] = Field(
        default_factory=list, description="내가 멤버인 매장 목록"
    )


class StoreDetailOut(Schema):
    store_id: int = Field(...)
    name: str
    width: int
    height: int
    depth: int
    my_role: str = Field(
        ..., description="OWNER / MANAGER / VICE_MANAGER / VMD / STAFF"
    )
    floorplan_image_url: str | None = None
    actual_photo_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StoreCreateOut(Schema):
    store_id: int
    name: str
    address: str
    width: int
    height: int
    depth: int
    created_at: datetime


class StoreUpdateOut(Schema):
    store_id: int = Field(...)
    name: str
    address: str
    width: int
    height: int
    depth: int
    updated_at: datetime


class FloorplanUploadOut(Schema):
    store_id: int
    floorplan_image_url: str = Field(..., description="저장된 MEDIA path")
    updated_at: datetime


class StoreProductVariantOut(Schema):
    id: int
    size: str | None = None
    color: str | None = None
    sku_code: str | None = None
    barcode_image_url: str | None = None
    stock_quantity: int = Field(0, description="매장 재고 (없으면 0 fallback)")
    is_discontinued: bool = Field(
        False, description="variant 가 소프트 삭제 됐는지 여부"
    )


class StoreProductItemOut(Schema):
    id: int
    name: str | None = Field(None, description="placeholder 시 null")
    price: int | None = Field(None, description="placeholder 시 null")
    status: str = Field(
        ..., description="ACTIVE / PAUSED / DISCONTINUED (master 단종 시 합성)"
    )
    width: int
    height: int
    depth: int | None = None
    image_url: str | None = None
    model_url: str | None = Field(None, description="현재 항상 null (Asset3D 미구현)")
    variants: list[StoreProductVariantOut] = Field(default_factory=list)


class StoreProductsOut(Schema):
    products: list[StoreProductItemOut] = Field(default_factory=list)


class StoreProductAssignOut(Schema):
    assigned_count: int = Field(..., description="신규 매핑 + PAUSED→ACTIVE 합")
    total_count: int = Field(..., description="매장의 전체 store_products row 수")


class StoreProductUpdateOut(Schema):
    product_id: int
    status: str
    updated_at: datetime


class StoreProductDeleteOut(Schema):
    deleted_product_id: int
    total_count: int


class InvitationCreateOut(Schema):
    invitation_id: int = Field(...)
    invitee_email: EmailStr
    target_role: str
    invite_link: str = Field(..., description="FE 절대 URL + token 쿼리")
    expires_at: datetime = Field(..., description="now + 24h")


class InvitationAcceptOut(Schema):
    store_id: int
    store_name: str
    granted_role: str
    joined_at: datetime


class AdminQuotaOut(Schema):
    current: int = Field(
        ..., description="현재 관리자급 인원 (MANAGER + VICE_MANAGER + VMD)"
    )
    max: int = Field(..., description="매장의 최대 관리자 정원")


class MemberOut(Schema):
    user_id: int
    name: str
    email: EmailStr
    profile_image_url: str | None = None
    role: str
    joined_at: datetime


class MembersOut(Schema):
    admin_quota: AdminQuotaOut
    members: list[MemberOut] = Field(default_factory=list)


class MemberRoleUpdateOut(Schema):
    store_id: int
    user_id: int
    role: str
    updated_at: datetime
