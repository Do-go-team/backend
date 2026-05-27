from datetime import datetime

from ninja import Schema
from pydantic import Field


class ProductDetectionTaskCreateOut(Schema):
    detection_task_id: int
    status: str


class RelativePositionOut(Schema):
    x: float
    y: float


class RelativeSizeOut(Schema):
    width: float
    height: float


class ProductDetectionItemOut(Schema):
    detection_item_id: int
    slot: int
    thumbnail_key: str
    thumbnail_url: str | None = None
    relative_position: RelativePositionOut
    relative_size: RelativeSizeOut
    status: str
    asset_generation_status: str
    asset_generation_task_id: int | None = None
    asset_3d_id: int | None = None
    asset_3d_url: str | None = None
    confidence: float | None = None
    bbox_xyxy: list[float] | None = None


class ProductDetectionTaskDetailOut(Schema):
    detection_task_id: int
    status: str
    error_message: str | None = None
    items: list[ProductDetectionItemOut] = []
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ProductDetectionCompleteItemIn(Schema):
    slot: int = Field(ge=0, le=49)
    thumbnail_key: str = Field(min_length=1, max_length=512)
    relative_position_x: float
    relative_position_y: float
    relative_size_width: float
    relative_size_height: float
    confidence: float | None = None
    bbox_xyxy: list[float] | None = None


class ProductDetectionCompleteIn(Schema):
    callback_token: str = Field(min_length=1, max_length=128)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    items: list[ProductDetectionCompleteItemIn] = Field(default_factory=list)


class ProductDetectionFailIn(Schema):
    callback_token: str = Field(min_length=1, max_length=128)
    error_message: str = Field(min_length=1)


class ProductDetectionGenerate3DIn(Schema):
    selected_item_ids: list[int] = Field(min_length=1)
    reject_unselected: bool = True


class ProductDetectionGenerate3DOut(Schema):
    detection_task_id: int
    created_task_count: int
    selected_item_ids: list[int] = []
    rejected_item_ids: list[int] = []
    skipped_reject_item_ids: list[int] = []
    asset_generation_task_ids: list[int] = []


class ProductDetectionItemRejectOut(Schema):
    detection_task_id: int
    detection_item_id: int
    status: str
