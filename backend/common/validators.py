"""사용자 입력 검증 유틸. XSS 방어 1차 — URL 입력의 scheme 한정.

`profile_image_url`, `floorplan_image_url`, `image_url` 등 사용자가 보내는 URL 이
이후 FE 의 `<img src={url}>` 같은 attribute 컨텍스트에 렌더링됨. `javascript:` /
`data:` 등 비-http scheme 이 통과하면 일부 렌더링 컨텍스트에서 코드 실행으로
이어질 수 있음. http(s) 만 허용해서 boundary 에서 차단.
"""


def validate_http_url(value: str | None) -> str | None:
    """None / 빈 문자열 / 공백만 있는 문자열은 모두 None 으로 정규화 (선택 필드 미입력 의미).
    그 외에는 http:// 또는 https:// 로 시작하지 않으면 거부.
    """
    if value is None:
        return None
    # Swagger UI 등이 비어있는 입력을 "" 로 직렬화해 보내는 케이스를 미입력으로 흡수.
    if value.strip() == "":
        return None
    if not value.startswith(("http://", "https://")):
        raise ValueError("URL은 http 또는 https scheme 이어야 합니다.")
    return value
