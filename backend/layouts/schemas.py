from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import Field, model_serializer

from common.schemas import Asset3DOut, DimensionsOut


# ---------- Requests ----------


class LayoutCreateIn(Schema):
    name: str = Field(..., min_length=1, max_length=100, description="레이아웃 이름")
    comment: str | None = Field(None, max_length=1000, description="기획 의도 메모")
    is_active: bool = Field(
        False, description="true 시 같은 매장의 다른 활성 레이아웃 자동 비활성화"
    )


class LayoutFixtureItemIn(Schema):
    layout_fixture_id: int | None = Field(
        None, description="UPDATE 대상 ID. 누락 시 INSERT"
    )
    fixture_version_id: int | None = Field(
        None, description="alive FixtureVersion ID. INSERT 시 필수"
    )
    world_pos_x: int | None = Field(
        None, description="월드 X 좌표 (cm). INSERT 시 필수"
    )
    world_pos_y: int | None = Field(
        None, description="월드 Y 좌표 (cm). INSERT 시 필수"
    )
    world_pos_z: int | None = Field(
        None, description="월드 Z 좌표 (cm). INSERT 시 필수"
    )
    world_rot_y: int | None = Field(
        None, description="Y축 회전 (저장 시 % 360 자동 정규화)"
    )
    # ge=1 은 서비스 단의 _validate_fixture_row 가 INVALID_FIXTURE_DATA 422 로 처리.
    # Pydantic 으로 거부하면 INVALID_PARAMETER 422 가 나가서 코드 충돌.
    width: int | None = Field(
        None, description="인스턴스 가로 (cm). 누락 시 fixture_master 값 자동 복사"
    )
    height: int | None = Field(
        None, description="인스턴스 높이 (cm). 누락 시 fixture_master 값 자동 복사"
    )
    depth: int | None = Field(
        None, description="인스턴스 깊이 (cm). 누락 시 fixture_master 값 자동 복사"
    )


class LayoutUpdateIn(Schema):
    name: str | None = Field(
        None, min_length=1, max_length=100, description="변경할 이름"
    )
    comment: str | None = Field(None, max_length=1000, description="변경할 코멘트")
    is_active: bool | None = Field(None, description="활성 토글")
    fixtures: list[LayoutFixtureItemIn] | None = Field(
        None,
        description="bulk-sync. 키 부재 → 메타만 수정 / `[]` → 전체 삭제 / `[{...}]` → 3-rule sync",
    )


class LayoutFixturesCopyIn(Schema):
    layout_fixture_ids: list[int] = Field(
        ..., min_length=1, description="복사할 LayoutFixture ID 목록 (1개 이상)"
    )


class LayoutExportIn(Schema):
    """평면도 PDF 옵션. 모두 optional — 누락 시 default 사용."""

    paper_size: Literal["A4", "A3", "A2"] = Field("A4", description="용지 크기")
    orientation: Literal["portrait", "landscape"] = Field(
        "landscape", description="용지 방향"
    )
    include_labels: bool = Field(True, description="집기 라벨 표시 여부")
    show_grid: bool = Field(False, description="그리드 표시 여부")


# ---------- Responses ----------


class LayoutCreateOut(Schema):
    layout_id: int = Field(...)
    store_id: int
    name: str
    comment: str | None = None
    is_active: bool
    created_at: datetime


class LayoutItemOut(Schema):
    layout_id: int = Field(...)
    name: str
    comment: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LayoutsOut(Schema):
    layouts: list[LayoutItemOut] = Field(default_factory=list)


class LayoutFixtureInfoOut(Schema):
    name: str = Field(..., description="마스터 집기 이름")
    width: int = Field(..., description="마스터 집기 원본 가로 (cm)")
    height: int = Field(..., description="마스터 집기 원본 높이 (cm)")
    depth: int = Field(..., description="마스터 집기 원본 깊이 (cm)")
    asset_3d: Asset3DOut | None = Field(
        None, description="현재 항상 null (Asset3D 미구현)"
    )


class LayoutFixtureDetailOut(Schema):
    layout_fixture_id: int
    fixture_id: int = Field(..., description="해당 배치가 참조하는 FixtureMaster ID")
    fixture_version_id: int
    world_pos_x: int
    world_pos_y: int
    world_pos_z: int
    world_rot_y: int = Field(..., description="0~359")
    width: int = Field(..., description="인스턴스 가로 (cm)")
    height: int = Field(..., description="인스턴스 높이 (cm)")
    depth: int = Field(..., description="인스턴스 깊이 (cm)")
    fixture_info: LayoutFixtureInfoOut


class LayoutDetailOut(Schema):
    layout_id: int = Field(...)
    store_id: int
    name: str
    comment: str | None = None
    is_active: bool
    floorplan_image_url: str | None = Field(
        None, description="레이아웃 단위 도면 URL. parse 시 자동 설정."
    )
    store_dimensions: DimensionsOut
    fixtures: list[LayoutFixtureDetailOut] = Field(default_factory=list)


class LayoutUpdateOut(Schema):
    """fixtures 키가 요청에 있으면 fixtures_*_count 두 필드 포함, 없으면 응답에서 제외."""

    layout_id: int = Field(...)
    name: str | None = None
    is_active: bool | None = None
    updated_at: datetime
    fixtures_updated_count: int | None = Field(
        None, description="fixtures 요청 시만 (INSERT+UPDATE 합)"
    )
    fixtures_deleted_count: int | None = Field(
        None, description="fixtures 요청 시만 (HARD DELETE 개수)"
    )

    @model_serializer(mode="wrap")
    def _omit_none_fixture_counts(self, handler):
        data = handler(self)
        if data.get("fixtures_updated_count") is None:
            data.pop("fixtures_updated_count", None)
        if data.get("fixtures_deleted_count") is None:
            data.pop("fixtures_deleted_count", None)
        return data


class LayoutDeleteOut(Schema):
    deleted_layout_id: int


class LayoutFixturesCopyItemOut(Schema):
    source_layout_fixture_id: int
    new_layout_fixture_id: int
    new_fixture_version_id: int = Field(
        ..., description="새로 생성된 빈 진열대 version"
    )


class LayoutFixturesCopyOut(Schema):
    layout_id: int
    copied: list[LayoutFixturesCopyItemOut] = Field(default_factory=list)
    copied_count: int


class LayoutExportOut(Schema):
    file_id: str
    file_name: str
    download_url: str
    expires_at: datetime = Field(..., description="다운로드 URL 만료 시각")


class FloorplanParseOut(Schema):
    """도면 파싱 응답 — LayoutDetailOut 와 동일 형태 + parsed_at.

    한 호출로 (1) Layout.floorplan_image_url 갱신, (2) FixtureMaster N개,
    (3) FixtureVersion N개 (master 별 "진열 1"), (4) LayoutFixture N개를
    한 transaction 으로 생성하고 결과를 layout 상세 형태로 즉시 반환.
    FE 는 별도 GET /layouts/{id} 호출 없이 바로 3D 캔버스 렌더 가능.
    """

    layout_id: int = Field(...)
    store_id: int
    name: str
    comment: str | None = None
    is_active: bool
    floorplan_image_url: str | None = Field(None, description="갱신된 도면 URL")
    store_dimensions: DimensionsOut
    fixtures: list[LayoutFixtureDetailOut] = Field(default_factory=list)
    parsed_at: datetime = Field(..., description="OpenCV 분석 완료 시각")
