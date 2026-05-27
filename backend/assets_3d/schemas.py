from typing import Optional

from ninja import Schema
from pydantic import Field


# ---------- Requests ----------


class Asset3DTaskCreateRequest(Schema):
    # target_type: PRODUCT/FIXTURE/STORE — 모델 choices 와 동일하나 schema 단계에서는
    # 자유 문자열로 받고 service 에서 검증 (타 도메인이 typo 로 쉽게 깨지지 않게).
    target_type: str = Field(min_length=1, max_length=20)
    target_id: int
    source_image_url: str = Field(min_length=1, max_length=512)


class Asset3DTaskClaimRequest(Schema):
    worker_id: str = Field(min_length=1, max_length=100)


class Asset3DTaskFailRequest(Schema):
    worker_id: str = Field(min_length=1, max_length=100)
    error_message: str = Field(min_length=1)


# ---------- Responses ----------


class Asset3DTaskCreateResponse(Schema):
    task_id: int
    status: str


class Asset3DTaskStatusResponse(Schema):
    task_id: int
    target_type: str
    target_id: int
    source_image_url: str
    status: str
    result_url: Optional[str] = None
    asset_3d_id: Optional[int] = None
    error_message: Optional[str] = None


class Asset3DTaskCompleteResponse(Schema):
    task_id: int
    status: str
    asset_3d_id: int
    result_url: str
