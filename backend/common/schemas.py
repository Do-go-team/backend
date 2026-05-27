from typing import Any, Generic, Optional, TypeVar

from ninja import Schema
from pydantic import Field, model_serializer

T = TypeVar("T")


class ErrorOut(Schema):
    success: bool = Field(False, description="실패 여부 (항상 false)")
    code: str = Field(
        ..., description="에러 코드 (예: STORE_NOT_FOUND)", examples=["STORE_NOT_FOUND"]
    )
    message: str = Field(..., description="사용자에게 노출할 메시지")


class SuccessOut(Schema):
    success: bool = Field(True, description="성공 여부 (항상 true)")
    message: str = Field("", description="응답 메시지")
    data: Optional[Any] = Field(None, description="endpoint 별 응답 데이터")


class Envelope(Schema, Generic[T]):
    """공통 응답 envelope. 모든 endpoint 응답은 이 구조로 직렬화됨.

    Generic 파라미터 T 에 endpoint 별 응답 schema 를 넣어 사용:
        response=Envelope[StoreDetailOut]
    """

    success: bool = Field(True, description="성공 여부")
    message: str = Field("", description="응답 메시지")
    data: T | None = Field(None, description="endpoint 별 응답 데이터")


class DimensionsOut(Schema):
    width: int = Field(..., description="가로 (cm)")
    height: int = Field(..., description="높이 (cm)")
    depth: int = Field(..., description="세로 (cm)")


class Asset3DOut(Schema):
    """3D 모델 에셋 정보. `file_size` 가 None 이면 응답에서 제외됨."""

    file_format: str = Field(
        ..., description="3D 모델 파일 포맷", examples=["GLB", "GLTF", "OBJ"]
    )
    model_url: str = Field(..., description="3D 모델 다운로드 URL")
    file_size: int | None = Field(None, description="파일 용량 (bytes). 없을 수도 있음")

    @model_serializer(mode="wrap")
    def _omit_none_file_size(self, handler):
        data = handler(self)
        if data.get("file_size") is None:
            data.pop("file_size", None)
        return data
