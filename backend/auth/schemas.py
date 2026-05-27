from ninja import Schema
from pydantic import Field


class TokenRefreshOut(Schema):
    expires_in: int = Field(
        ..., description="새 access 토큰 만료 (초)", examples=[3600]
    )
