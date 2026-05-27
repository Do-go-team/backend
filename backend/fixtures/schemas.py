from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import Field

from common.schemas import Asset3DOut, DimensionsOut


# ---------- Requests ----------


class FixtureCreateIn(Schema):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="집기 이름",
        examples=["중앙 매대 A형"],
    )
    width: int = Field(..., ge=1, description="가로 (cm)")
    height: int = Field(..., ge=1, description="높이 (cm)")
    depth: int = Field(..., ge=1, description="깊이 (cm)")


class FixtureUpdateIn(Schema):
    name: str | None = Field(
        None, min_length=1, max_length=100, description="변경할 이름"
    )
    width: int | None = Field(None, ge=1, description="변경할 가로 (cm)")
    height: int | None = Field(None, ge=1, description="변경할 높이 (cm)")
    depth: int | None = Field(None, ge=1, description="변경할 깊이 (cm)")


class VersionCreateIn(Schema):
    # 빈 문자열 / 공백만 입력은 서비스 단에서 strip 후 INVALID_PARAMETER 400 으로 거부.
    # Pydantic min_length 두면 422 로 먼저 거부돼 비즈니스 코드와 충돌.
    version_name: str = Field(
        ..., max_length=100, description="진열 프리셋 이름. strip 후 빈 문자열 거부"
    )


class PlacementItemIn(Schema):
    placement_id: int | None = Field(None, description="UPDATE 대상 ID. 누락 시 INSERT")
    variant_id: int = Field(..., description="진열할 variant ID (가시 범위 내)")
    local_pos_x: int = Field(..., description="로컬 X 좌표 (cm)")
    local_pos_y: int = Field(..., description="로컬 Y 좌표 (cm)")
    local_pos_z: int = Field(..., description="로컬 Z 좌표 (cm)")
    # spec line 61 — 현재 DISPLAY 만 허용. enum 확장 시 model migration + 본 줄 갱신.
    status: Literal["DISPLAY"] | None = Field(None, description="현재 DISPLAY 만 허용")
    memo: str | None = Field(None, max_length=500, description="VMD 메모")


class PlacementsUpdateIn(Schema):
    placements: list[PlacementItemIn] = Field(
        ..., description="bulk-sync 대상. `[]` 시 모든 placement HARD DELETE"
    )


# ---------- Responses ----------


class FixtureListItemOut(Schema):
    fixture_id: int = Field(...)
    name: str
    width: int
    height: int
    depth: int
    created_at: datetime


class FixturesOut(Schema):
    fixtures: list[FixtureListItemOut] = Field(default_factory=list)


class FixtureCreateOut(Schema):
    fixture_id: int = Field(...)
    name: str
    width: int
    height: int
    depth: int
    created_at: datetime


class FixtureDetailOut(Schema):
    fixture_id: int = Field(...)
    name: str
    dimensions: DimensionsOut
    asset_3d: Asset3DOut | None = Field(
        None, description="현재 항상 null (Asset3D 미구현)"
    )
    created_at: datetime
    updated_at: datetime


class FixtureUpdateOut(Schema):
    fixture_id: int = Field(...)
    name: str
    width: int
    height: int
    depth: int
    updated_at: datetime


class VersionItemOut(Schema):
    version_id: int = Field(...)
    version_name: str
    created_at: datetime
    updated_at: datetime


class VersionsOut(Schema):
    fixture_id: int
    versions: list[VersionItemOut] = Field(default_factory=list)


class VersionCreateOut(Schema):
    version_id: int = Field(...)
    fixture_id: int
    version_name: str
    created_at: datetime


class PlacementVariantOut(Schema):
    variant_id: int
    sku_code: str | None = None


class PlacementItemOut(Schema):
    placement_id: int = Field(...)
    local_pos_x: int
    local_pos_y: int
    local_pos_z: int
    status: str
    memo: str | None = None
    variant: PlacementVariantOut


class PlacementsOut(Schema):
    version_id: int
    placements: list[PlacementItemOut] = Field(default_factory=list)


class PlacementsUpdateOut(Schema):
    version_id: int
    updated_count: int = Field(..., description="INSERT + UPDATE 합")
    deleted_count: int | None = Field(None, description="HARD DELETE 개수")
    updated_at: datetime
