"""OpenAPI 응답 example 헬퍼.

각 endpoint 의 `openapi_extra` 에 status 별 + 비즈니스 코드별 example 을
구체적으로 넣기 위한 빌더. 전역 ErrorOut.code 의 example 이 모든 endpoint 에
일괄 적용되는 문제를 endpoint-specific 으로 override.

ninja 가 `response={401: ErrorOut}` 으로 자동 생성하는 응답 entry 는 **int** 키.
helper 도 동일하게 int 키 + ErrorOut schema ref 를 함께 명시해서, ninja 가
`openapi_extra["responses"]` 를 dict.update 로 덮어쓸 때 schema 정보가 손실
되지 않도록 함. (str 키 사용 시 JSON 직렬화 후 같은 key 가 중복돼 Swagger UI
파서가 schema 없는 entry 로 덮어써 화면이 깨짐.)

사용:
    from common.openapi_helpers import error_examples

    @router.post(
        "/login",
        response={200: Envelope[LoginOut], 401: ErrorOut, 403: ErrorOut},
        openapi_extra=error_examples(
            (401, "USER_NOT_FOUND", "가입되지 않은 이메일입니다."),
            (401, "INVALID_CREDENTIALS", "비밀번호가 올바르지 않습니다."),
            (403, "DELETED_ACCOUNT", "탈퇴된 계정입니다."),
        ),
    )
"""

from __future__ import annotations

# OpenAPI 표준 status reason. examples 만 override 시 description 누락되면
# Swagger UI 가 "Description: ..." 자리에 빈 값 노출. 표준 reason phrase 채워둠.
_STATUS_DESC = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    410: "Gone",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
}


def error_examples(*errors: tuple[int, str, str]) -> dict:
    """(status, code, message) 튜플들을 OpenAPI 응답 examples dict 로 변환.

    같은 status 의 여러 비즈니스 코드는 dropdown 형태로 Swagger UI 에 노출.
    각 status 별로 `schema` ($ref ErrorOut) + `description` 도 함께 채워서
    ninja 자동 생성 정보 손실 없이 덮어쓰기 가능하게 함.
    """
    grouped: dict[int, dict] = {}
    for status, code, message in errors:
        # int 키 사용 — ninja 의 response= 자동 생성과 같은 키 타입.
        # str 키는 JSON 직렬화 후 같은 키가 중복돼 Swagger UI 가 깨짐.
        bucket = grouped.setdefault(
            status,
            {
                "description": _STATUS_DESC.get(status, ""),
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorOut"},
                        "examples": {},
                    }
                },
            },
        )
        bucket["content"]["application/json"]["examples"][code] = {
            "summary": code,
            "value": {"success": False, "code": code, "message": message},
        }
    return {"responses": grouped}
