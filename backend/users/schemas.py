from datetime import datetime

from ninja import Schema
from pydantic import EmailStr, Field, field_validator

from common.validators import validate_http_url


# ---------- Requests ----------


class SignupIn(Schema):
    email: EmailStr = Field(
        ..., description="가입할 이메일 주소", examples=["user@dogo.local"]
    )
    password: str = Field(
        ...,
        description="8~20자 영문/숫자/특수문자 조합 필수 (서비스 단 정규식 검증)",
        examples=["Passw0rd!"],
    )
    password_confirm: str = Field(..., description="password 와 동일해야 함")
    name: str = Field(
        ..., min_length=1, max_length=20, description="사용자 이름", examples=["홍길동"]
    )
    profile_image_url: str | None = Field(
        None,
        max_length=512,
        description="프로필 이미지 URL (http/https). 빈 문자열은 null 로 정규화됨.",
        examples=["https://s3.example.com/profile.png", None],
    )
    verification_token: str = Field(
        ..., min_length=1, description="이메일 인증 확인 응답에서 받은 1회용 토큰"
    )

    @field_validator("profile_image_url")
    @classmethod
    def _validate_profile_image_url(cls, v):
        return validate_http_url(v)


class LoginIn(Schema):
    email: EmailStr = Field(..., description="가입 시 사용한 이메일")
    password: str = Field(..., description="비밀번호")


class EmailSendIn(Schema):
    email: str = Field(
        ..., description="인증 번호를 받을 이메일 주소", examples=["user@dogo.local"]
    )


class EmailVerifyIn(Schema):
    email: str = Field(..., description="인증 받을 이메일")
    code: str = Field(
        ...,
        min_length=6,
        max_length=6,
        description="발송된 6자리 인증 번호",
        examples=["123456"],
    )


# ---------- Responses ----------


class SignupOut(Schema):
    user_id: int = Field(..., description="새로 생성된 사용자 ID")
    email: EmailStr = Field(..., description="가입한 이메일")
    name: str = Field(..., description="사용자 이름")
    role: str = Field(..., description="시스템 권한", examples=["USER"])
    created_at: datetime = Field(..., description="가입 일시")


class LoginUserOut(Schema):
    id: int = Field(..., description="사용자 ID")
    name: str = Field(..., description="사용자 이름")
    role: str = Field(..., description="시스템 권한 (USER / ADMIN)")


class LoginOut(Schema):
    expires_in: int = Field(
        ..., description="access 토큰 만료 시간 (초)", examples=[3600]
    )
    user: LoginUserOut = Field(..., description="로그인한 사용자 정보")


class AccessibleStoreOut(Schema):
    store_id: int = Field(..., description="매장 ID")
    store_name: str = Field(..., description="매장 이름")
    store_role: str = Field(
        ...,
        description="해당 매장에서의 권한",
        examples=["OWNER", "MANAGER", "VICE_MANAGER", "VMD", "STAFF"],
    )


class MeOut(Schema):
    id: int = Field(..., description="사용자 ID")
    email: EmailStr = Field(..., description="이메일")
    name: str = Field(..., description="이름")
    profile_image_url: str | None = Field(
        None, description="프로필 이미지 경로 (없으면 null)"
    )
    system_role: str = Field(..., description="시스템 권한 (USER / ADMIN)")
    accessible_stores: list[AccessibleStoreOut] = Field(
        default_factory=list, description="접근 가능한 매장 목록 (활성 매장만)"
    )


class EmailSendOut(Schema):
    expires_in: int = Field(..., description="인증 번호 TTL (초)", examples=[300])


class EmailVerifyOut(Schema):
    is_verified: bool = Field(..., description="인증 성공 여부 (항상 true)")
    verification_token: str = Field(
        ..., description="회원가입 단계에서 사용할 1회용 토큰 (TTL 10분)"
    )


class LogoutOut(Schema):
    """로그아웃 응답은 data: null 이지만 Envelope 형식 유지."""

    pass
