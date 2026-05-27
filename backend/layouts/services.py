from django.db import transaction

from assets_3d.models import Asset3D
from common.exceptions import BusinessException
from fixtures.models import FixtureMaster, FixtureVersion
from stores.models import StoreMember
from stores.services import (
    ADMIN_ROLES,
    ALLOWED_FLOORPLAN_CONTENT_TYPES,
    MAX_FLOORPLAN_BYTES,
    get_member_store,
    get_member_store_for_admin,
)

from .models import Layout, LayoutFixture
from .schemas import (
    LayoutCreateIn,
    LayoutExportIn,
    LayoutFixtureItemIn,
    LayoutFixturesCopyIn,
    LayoutUpdateIn,
)


def get_member_layout(user, layout_id: int) -> tuple[Layout, str]:
    """레이아웃 단건 + 매장 멤버십을 함께 검증.

    비회원/미존재 레이아웃/소프트딜리트(레이아웃 또는 매장) 모두
    LAYOUT_NOT_FOUND 404 로 묶어 ID 프로빙 차단 (stores.get_member_store 와 동일 정책).

    Returns (layout, member_role) — 호출 측이 권한 분기 시 role 활용.
    """
    layout = (
        Layout.objects.alive()
        .select_related("store")
        .filter(id=layout_id, store__deleted_at__isnull=True)
        .first()
    )
    not_found = BusinessException(
        "LAYOUT_NOT_FOUND",
        "존재하지 않거나 접근 권한이 없는 레이아웃입니다.",
        status=404,
    )
    if layout is None:
        raise not_found

    membership = StoreMember.objects.filter(
        store_id=layout.store_id,
        user=user,
    ).first()
    if membership is None:
        raise not_found

    return layout, membership.role


def get_member_layout_for_admin(
    user,
    layout_id: int,
    *,
    error_code: str = "FORBIDDEN_ACCESS",
    error_message: str | None = None,
) -> tuple[Layout, str]:
    """Admin variant of get_member_layout — STAFF 거부.

    stores.get_member_store_for_admin 와 같은 결의 helper. 비회원/미존재 등
    LAYOUT_NOT_FOUND 처리는 그대로 유지하고, 그 위에 ADMIN_ROLES 게이트만 얹음.

    error_code / error_message 는 endpoint 별 spec 차이(예: 삭제 endpoint 의
    'FORBIDDEN' 코드)를 흡수하기 위한 override.
    """
    layout, role = get_member_layout(user, layout_id)
    if role not in ADMIN_ROLES:
        raise BusinessException(
            error_code,
            error_message or "해당 레이아웃에 대한 권한이 없습니다.",
            status=403,
        )
    return layout, role


@transaction.atomic
def create_layout(user, store_id: int, payload: LayoutCreateIn) -> dict:
    """매장 안에 새 레이아웃 시안 생성.

    권한: ADMIN_ROLES (스펙 "매장의 소유권" 표현 → 매장 단위 write 게이트와 동일).

    is_active=True 요청 시 같은 매장의 기존 활성 레이아웃을 모두 비활성화.
    "한 매장당 active 1개" 불변식을 application-level + transaction.atomic 으로
    유지 (DB 레벨 partial unique 는 도입하지 않음 — race 는 트랜잭션으로 충분).
    소프트딜리트된 레이아웃은 deactivate 대상에서 제외 (이미 invisible).
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="해당 매장에 레이아웃을 생성할 권한이 없습니다.",
    )

    if payload.is_active:
        Layout.objects.alive().filter(store=store, is_active=True).update(
            is_active=False,
        )

    layout = Layout.objects.create(
        store=store,
        name=payload.name,
        comment=payload.comment,
        is_active=payload.is_active,
    )

    return {
        "layout_id": layout.id,
        "store_id": store.id,
        "name": layout.name,
        "comment": layout.comment,
        "is_active": layout.is_active,
        "created_at": layout.created_at,
    }


def list_layouts(user, store_id: int) -> dict:
    """매장에 등록된 레이아웃 시안 목록 조회.

    권한: any membership (STAFF 포함). 시안 목록은 정보 공유 성격이라
    write 권한과 분리. 비회원/미존재 매장은 STORE_NOT_FOUND 404 (ID 프로빙 방지).

    정렬: is_active=True 인 항목이 항상 최상단 (현재 매장에 적용 중인 시안을
    먼저 보여주는 UX), 나머지는 created_at desc (최신 시안 위로). PostgreSQL
    BOOLEAN 정렬은 False(0) < True(1) 라서 -is_active 로 내림차순 → True 우선.

    소프트딜리트 레이아웃은 alive() 로 제외.
    """
    store, _ = get_member_store(user, store_id)

    layouts = (
        Layout.objects.alive().filter(store=store).order_by("-is_active", "-created_at")
    )

    return {
        "layouts": [
            {
                "layout_id": layout.id,
                "name": layout.name,
                "comment": layout.comment,
                "is_active": layout.is_active,
                "created_at": layout.created_at,
                "updated_at": layout.updated_at,
            }
            for layout in layouts
        ],
    }


def get_layout_detail(user, layout_id: int) -> dict:
    """3D 캔버스 즉시 렌더용 Aggregate 응답.

    한 번의 요청으로 (1) 레이아웃 메타 (2) 매장 물리 규격 (3) 배치 집기들의 좌표
    + master 정보 + 3D 모델 URL 까지 묶어 반환 — 프론트가 추가 round-trip 없이
    캔버스 그릴 수 있도록.

    쿼리 계획 (집기 개수와 무관하게 ~3 round-trips):
      1. Layout + store (get_member_layout 의 select_related 결과 재사용)
      2. LayoutFixture + fixture_version + fixture_master (chained select_related)
      3. Asset3D 일괄 조회 (target_type=FIXTURE, target_id__in=fixture_master_ids)

    소프트딜리트된 fixture_version / fixture_master 도 그대로 노출 — 레이아웃은
    *작성 시점* 의 스냅샷 성격이라 본사 차원의 단종이 표시 자체를 지우지 않음.
    (products 156 의 DISCONTINUED 표면화 정책과 같은 결.)

    같은 fixture_master 에 Asset3D 가 여러 건 등록돼 있으면 created_at 최신 1개만
    채택 (target_id 그룹 내 desc).
    """
    layout, _ = get_member_layout(user, layout_id)
    store = layout.store

    fixtures = list(
        LayoutFixture.objects.filter(layout=layout)
        .select_related("fixture_version__fixture_master")
        .order_by("id")
    )

    asset_map: dict[int, dict | None] = {}
    if fixtures:
        fixture_master_ids = list(
            {fx.fixture_version.fixture_master_id for fx in fixtures}
        )
        for asset in Asset3D.objects.filter(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id__in=fixture_master_ids,
        ).order_by("target_id", "-created_at"):
            if asset.target_id not in asset_map:
                asset_map[asset.target_id] = {
                    "file_format": asset.file_format,
                    "model_url": asset.model_url or None,
                }

    fixtures_payload: list[dict] = []
    for fx in fixtures:
        fm = fx.fixture_version.fixture_master
        fixtures_payload.append(
            {
                "layout_fixture_id": fx.id,
                "fixture_id": fm.id,
                "fixture_version_id": fx.fixture_version_id,
                "world_pos_x": fx.world_pos_x,
                "world_pos_y": fx.world_pos_y,
                "world_pos_z": fx.world_pos_z,
                "world_rot_y": fx.world_rot_y,
                "width": fx.width,
                "height": fx.height,
                "depth": fx.depth,
                "fixture_info": {
                    "name": fm.name,
                    "width": fm.width,
                    "height": fm.height,
                    "depth": fm.depth,
                    "asset_3d": asset_map.get(fm.id),
                },
            }
        )

    return {
        "layout_id": layout.id,
        "store_id": store.id,
        "name": layout.name,
        "comment": layout.comment,
        "is_active": layout.is_active,
        "floorplan_image_url": layout.floorplan_image_url,
        "store_dimensions": {
            "width": store.width,
            "height": store.height,
            "depth": store.depth,
        },
        "fixtures": fixtures_payload,
    }


def delete_layout(user, layout_id: int) -> dict:
    """레이아웃 시안 소프트 삭제 — `deleted_at` 만 세팅, row 보존.

    권한: ADMIN_ROLES (스펙 'FORBIDDEN' 코드 사용 — helper 의 default
    'FORBIDDEN_ACCESS' 와 다름).

    안전장치: 현재 매장에 적용 중인 레이아웃(is_active=True)은 실수 삭제 방지를
    위해 ACTIVE_LAYOUT_DELETE_DENIED 로 차단. status 409(Conflict) — 리소스의
    현재 상태가 요청을 막는 케이스. 사용자는 다른 레이아웃을 활성화한 뒤
    이 레이아웃을 비활성으로 만들고 다시 시도해야 함.

    LayoutFixture row 는 그대로 두고 Layout 만 deleted_at 마킹 — 복원 가능성
    고려 (스펙 "과거 데이터 보호"). 153 의 매장 삭제 cascade 와 다른 흐름:
    매장 cascade 는 소속 layouts 일괄 비가시화 목적이라 active 차단 안 함.
    """
    layout, _ = get_member_layout_for_admin(
        user,
        layout_id,
        error_code="FORBIDDEN",
        error_message="해당 레이아웃을 삭제할 권한이 없습니다.",
    )

    if layout.is_active:
        raise BusinessException(
            "ACTIVE_LAYOUT_DELETE_DENIED",
            "현재 매장에 적용 중인 활성 레이아웃은 삭제할 수 없습니다. "
            "비활성화 후 다시 시도해 주세요.",
            status=409,
        )

    layout.soft_delete()
    return {"deleted_layout_id": layout.id}


# ── 레이아웃 수정 (메타 + fixtures bulk-sync) ─────────────────────────

INVALID_FIXTURE = BusinessException(
    "INVALID_FIXTURE_DATA",
    "유효하지 않은 집기 좌표 또는 회전 값이 포함되어 있습니다.",
    status=422,
)


def _validate_fixture_row(
    row: LayoutFixtureItemIn,
    existing_lf_ids: set[int],
    valid_fv_ids: set[int],
) -> None:
    """옵션 b: fixtures 배열은 optional 이지만 row 가 *보내지면* 모든 필드 required
    + 좌표/회전 범위 + fixture_version 존재성을 검증.

    스펙 표는 sub-field 들을 X(optional) 로 표기했으나, 실제 의미는 'fixtures
    배열 자체가 optional' 이라는 뜻 (배열 안 row 는 캔버스 동기화 컨텍스트라
    항상 완전체로 보내짐). 검증 실패는 모두 INVALID_FIXTURE_DATA 422 로 통합.
    """
    if row.fixture_version_id is None:
        raise INVALID_FIXTURE
    if row.world_pos_x is None or row.world_pos_y is None or row.world_pos_z is None:
        raise INVALID_FIXTURE
    if row.world_rot_y is None:
        raise INVALID_FIXTURE
    # world_rot_y 범위는 강제하지 않음 — 저장 시 % 360 정규화. 3D 캔버스에서
    # 마우스 회전 누적값(예: 720, -90)을 백엔드가 책임지고 0~359 로 wrap.
    # fixture_version 존재성 + alive — soft-deleted FV 새 참조는 차단
    # (detail 의 스냅샷 정책과 다름: update 는 *현재 상태* 를 만드는 작업이라 alive 강제)
    if row.fixture_version_id not in valid_fv_ids:
        raise INVALID_FIXTURE
    # UPDATE 케이스: layout_fixture_id 가 *이 레이아웃의* 기존 row 인지
    # cross-layout fixture id 도 INVALID_FIXTURE_DATA 로 묶음 (보안 + 데이터 무결성)
    if (
        row.layout_fixture_id is not None
        and row.layout_fixture_id not in existing_lf_ids
    ):
        raise INVALID_FIXTURE
    # 사이즈는 명시된 경우 양수 강제. INSERT 시 누락이면 sync 단계에서 master 값 복사.
    for size in (row.width, row.height, row.depth):
        if size is not None and size < 1:
            raise INVALID_FIXTURE


def _sync_fixtures(
    layout: Layout,
    items: list[LayoutFixtureItemIn],
) -> tuple[int, int]:
    """3-rule bulk sync. 사전 검증 통과한 items 만 들어옴.

    - layout_fixture_id 있음  → UPDATE (보낸 사이즈 필드만 갱신, 좌표/회전은 항상 갱신)
    - layout_fixture_id 없음  → INSERT (사이즈 누락 시 fixture_master 값 자동 복사)
    - DB 에 있으나 items 에 누락 → DELETE

    INSERT 시 사이즈 자동 복사 — FE 가 신규 집기 배치할 때 사이즈 매번 보내는
    어색함 제거. 사이즈 명시 row 면 그 값 사용.

    Returns (updated_count, deleted_count). 스펙의 fixtures_updated_count 는
    INSERT + UPDATE 합산.
    """
    existing_ids = set(
        LayoutFixture.objects.filter(layout=layout).values_list("id", flat=True)
    )

    insert_fv_ids = [r.fixture_version_id for r in items if r.layout_fixture_id is None]
    master_size_map: dict[int, tuple[int, int, int]] = {}
    if insert_fv_ids:
        for fv in FixtureVersion.objects.select_related("fixture_master").filter(
            id__in=insert_fv_ids
        ):
            fm = fv.fixture_master
            master_size_map[fv.id] = (fm.width, fm.height, fm.depth)

    request_ids: set[int] = set()
    inserted = 0
    updated = 0

    for row in items:
        # world_rot_y 정규화 — 한 바퀴 = 360도 라서 361 ≡ 1, -90 ≡ 270.
        rot_y = row.world_rot_y % 360
        if row.layout_fixture_id is None:
            default_w, default_h, default_d = master_size_map[row.fixture_version_id]
            LayoutFixture.objects.create(
                layout=layout,
                fixture_version_id=row.fixture_version_id,
                world_pos_x=row.world_pos_x,
                world_pos_y=row.world_pos_y,
                world_pos_z=row.world_pos_z,
                world_rot_y=rot_y,
                width=row.width if row.width is not None else default_w,
                height=row.height if row.height is not None else default_h,
                depth=row.depth if row.depth is not None else default_d,
            )
            inserted += 1
        else:
            update_fields = {
                "fixture_version_id": row.fixture_version_id,
                "world_pos_x": row.world_pos_x,
                "world_pos_y": row.world_pos_y,
                "world_pos_z": row.world_pos_z,
                "world_rot_y": rot_y,
            }
            # 사이즈는 명시된 것만 갱신 (UPDATE 시 미터치 의미론)
            if row.width is not None:
                update_fields["width"] = row.width
            if row.height is not None:
                update_fields["height"] = row.height
            if row.depth is not None:
                update_fields["depth"] = row.depth
            LayoutFixture.objects.filter(id=row.layout_fixture_id).update(
                **update_fields
            )
            updated += 1
            request_ids.add(row.layout_fixture_id)

    to_delete = existing_ids - request_ids
    if to_delete:
        LayoutFixture.objects.filter(id__in=to_delete).delete()

    return inserted + updated, len(to_delete)


@transaction.atomic
def update_layout(user, layout_id: int, payload: LayoutUpdateIn) -> dict:
    """레이아웃 메타 + fixtures 동시 수정.

    권한: ADMIN_ROLES (생성/삭제와 동일한 매장 단위 write 게이트).

    PATCH 의미론:
      - exclude_unset 으로 'fixtures' 키 자체 부재(메타만 수정) 와 빈 배열(전체
        삭제) 을 구분. payload.fixtures is None 도 *키 자체 부재* 로 취급
        (frontend 가 null 보내는 경우 방어).
      - is_active=True 토글 시 같은 매장의 다른 활성 레이아웃 자동 비활성화
        (생성과 동일 로직 — 한 매장당 active 1개 불변식).

    Fixtures 검증 → DB 쓰기 분리:
      - 검증 단계가 모든 row 를 사전에 통과해야 DB 변경 시작.
      - 한 row 라도 INVALID_FIXTURE_DATA 면 *어떤 변경도 일어나지 않음*
        (transaction.atomic + 사전 fail-fast 이중 보장).

    응답:
      - fixtures 키가 요청에 있었으면 fixtures_updated_count / fixtures_deleted_count
        모두 포함, 아니면 둘 다 미포함 (스펙 example 두 모드 일치).
      - updated_at 은 항상 최신 — 메타 변경 없고 fixtures 만 동기화한 경우에도
        updated_at 명시 bump (스펙 example 일치).
    """
    layout, _ = get_member_layout(user, layout_id)

    changes = payload.model_dump(exclude_unset=True)
    fixtures_in_request = "fixtures" in changes and payload.fixtures is not None
    items = payload.fixtures if fixtures_in_request else []

    # 1. 사전 검증 (DB 쓰기 전에 모든 row 검증)
    if fixtures_in_request and items:
        existing_lf_ids = set(
            LayoutFixture.objects.filter(layout=layout).values_list("id", flat=True)
        )
        requested_fv_ids = [
            r.fixture_version_id for r in items if r.fixture_version_id is not None
        ]
        valid_fv_ids = set(
            FixtureVersion.objects.alive()
            .filter(id__in=requested_fv_ids)
            .values_list("id", flat=True)
        )
        for row in items:
            _validate_fixture_row(row, existing_lf_ids, valid_fv_ids)

    # 2. is_active=True 토글 — 다른 활성 자동 비활성화 (생성 endpoint 와 동일)
    if changes.get("is_active") is True:
        Layout.objects.alive().filter(
            store=layout.store,
            is_active=True,
        ).exclude(id=layout.id).update(is_active=False)

    # 3. 메타 저장
    meta_keys = [k for k in ("name", "comment", "is_active") if k in changes]
    if meta_keys:
        for key in meta_keys:
            setattr(layout, key, changes[key])
        layout.save(update_fields=meta_keys + ["updated_at"])

    # 4. Fixtures 동기화
    fixtures_updated_count: int | None = None
    fixtures_deleted_count: int | None = None
    if fixtures_in_request:
        u, d = _sync_fixtures(layout, items)
        fixtures_updated_count = u
        fixtures_deleted_count = d
        # 메타 변경 없이 fixtures 만 동기화한 경우에도 updated_at bump
        if not meta_keys:
            layout.save(update_fields=["updated_at"])

    response = {
        "layout_id": layout.id,
        "name": layout.name,
        "is_active": layout.is_active,
        "updated_at": layout.updated_at,
    }
    if fixtures_in_request:
        response["fixtures_updated_count"] = fixtures_updated_count
        response["fixtures_deleted_count"] = fixtures_deleted_count
    return response


INVALID_LAYOUT_FIXTURE_ID = BusinessException(
    "INVALID_LAYOUT_FIXTURE_ID",
    "배치 목록에 유효하지 않은 layout_fixture_id 가 포함되어 있습니다.",
    status=422,
)


@transaction.atomic
def copy_layout_fixtures(user, layout_id: int, payload: LayoutFixturesCopyIn) -> dict:
    """선택된 LayoutFixture 들을 같은 layout 안에서 복제 — 매장 멤버 누구나 (STAFF 포함).

    복제 정책 (b-2 "빈 진열대로 복사"):
      - FixtureVersion 도 **새로 생성** (master 는 원본 FK 그대로 참조, 복제 X)
        → 원본·복사본 진열이 완전 독립. 한쪽 진열 수정해도 다른 쪽 영향 X.
      - placement(FixtureVersionProduct) 는 **복제 안 함** → 새 version 은
        빈 진열대 상태로 시작. 사용자가 직접 채움.
      - 좌표·회전·사이즈는 원본 그대로 (FE 가 이후 옮기는 책임).
      - version_name 은 원본 + " (사본)" 접미사로 자동 명명.

    검증:
      - get_member_layout — 가시성, 실패 시 LAYOUT_NOT_FOUND 404
      - layout_fixture_ids 가 *이 layout* 의 row 인가 (cross-layout/미존재 차단)
        실패 시 INVALID_LAYOUT_FIXTURE_ID 422, DB 변화 0
    """
    layout, _ = get_member_layout(user, layout_id)

    requested_ids = list(payload.layout_fixture_ids)
    sources = list(
        LayoutFixture.objects.filter(
            id__in=requested_ids, layout=layout
        ).select_related("fixture_version__fixture_master")
    )
    found_ids = {s.id for s in sources}
    if set(requested_ids) - found_ids:
        raise INVALID_LAYOUT_FIXTURE_ID

    sources_by_id = {s.id: s for s in sources}
    copied: list[dict] = []
    for src_id in requested_ids:
        src = sources_by_id[src_id]
        new_version = FixtureVersion.objects.create(
            fixture_master=src.fixture_version.fixture_master,
            version_name=f"{src.fixture_version.version_name} (사본)",
        )
        new_row = LayoutFixture.objects.create(
            layout=layout,
            fixture_version=new_version,
            world_pos_x=src.world_pos_x,
            world_pos_y=src.world_pos_y,
            world_pos_z=src.world_pos_z,
            world_rot_y=src.world_rot_y,
            width=src.width,
            height=src.height,
            depth=src.depth,
        )
        copied.append(
            {
                "source_layout_fixture_id": src.id,
                "new_layout_fixture_id": new_row.id,
                "new_fixture_version_id": new_version.id,
            }
        )

    return {
        "layout_id": layout.id,
        "copied": copied,
        "copied_count": len(copied),
    }


def export_layout(user, store_id: int, layout_id: int, payload: LayoutExportIn) -> dict:
    """레이아웃 평면도 (Top View) PDF 생성 + MEDIA 저장 + download_url 발급.

    권한: 매장 멤버 (`get_member_layout` — ADMIN/STAFF 무관, 전 멤버 export 가능).
    경로 검증: spec path 의 store_id 와 layout.store_id 일치 여부 확인 — 불일치 시
    LAYOUT_STORE_MISMATCH 400 (view-points spec 의 같은 결).

    응답 schema (spec 정합):
      file_id, file_name, download_url, expires_at

    파일명 정책: 영문 + ID 패턴 — `store_layout_{store_id}_{layout_id}_{ts}.pdf`.
    spec 예시는 한국어 매장명 포함 (`store_layout_1번매장_*.pdf`) 이지만 다운로드 시
    Content-Disposition 인코딩 깨짐 우려 → 영문 패턴으로 divergence. PR 본문 명시.
    """
    from django.utils import timezone as dj_timezone

    from common.pdf.layout_plan import render_layout_plan_pdf
    from common.pdf.storage import save_pdf_and_get_url

    layout, _ = get_member_layout(user, layout_id)
    if layout.store_id != store_id:
        raise BusinessException(
            "LAYOUT_STORE_MISMATCH",
            "요청하신 레이아웃이 해당 매장에 속해있지 않습니다.",
            status=400,
        )

    store = layout.store
    fixtures_qs = (
        LayoutFixture.objects.filter(layout=layout)
        .select_related("fixture_version__fixture_master")
        .order_by("id")
    )
    fixtures_payload = [
        {
            "name": fx.fixture_version.fixture_master.name,
            "world_pos_x": fx.world_pos_x,
            "world_pos_z": fx.world_pos_z,
            "world_rot_y": fx.world_rot_y,
            "width": fx.fixture_version.fixture_master.width,
            "depth": fx.fixture_version.fixture_master.depth,
        }
        for fx in fixtures_qs
    ]

    try:
        pdf_bytes = render_layout_plan_pdf(
            store_name=store.name,
            store_width_cm=store.width,
            store_depth_cm=store.depth,
            layout_name=layout.name,
            fixtures=fixtures_payload,
            paper_size=payload.paper_size,
            orientation=payload.orientation,
            include_labels=payload.include_labels,
            show_grid=payload.show_grid,
        )
    except Exception as exc:
        raise BusinessException(
            "EXPORT_FAILED",
            "PDF 생성 중 오류가 발생했습니다.",
            status=500,
        ) from exc

    timestamp = dj_timezone.localtime(dj_timezone.now()).strftime("%Y%m%d%H%M%S")
    file_name = f"store_layout_{store_id}_{layout_id}_{timestamp}.pdf"
    file_id, download_url, expires_at = save_pdf_and_get_url(
        pdf_bytes=pdf_bytes, file_name=file_name
    )

    return {
        "file_id": file_id,
        "file_name": file_name,
        "download_url": download_url,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Floorplan parse — multipart 도면 → 자동 fixture/version/layout_fixture 생성
# ---------------------------------------------------------------------------

# ai-worker sync wait — 10초. OpenCV 검출 자체는 보통 1~3초이나 안전 마진.
_AI_PARSE_TIMEOUT_SEC = 10

# 도면에서 추출 불가능한 정보 — 기본값으로 부여 후 사용자가 추후 수정.
_DEFAULT_FIXTURE_HEIGHT_CM = 500


def _call_ai_floorplan_parse(image_bytes: bytes) -> dict:
    """ai-worker 의 parse_floorplan task 를 sync 호출.

    테스트에서는 이 함수만 mock 하면 충분 — Celery 인스턴스 patch 없이 격리.
    응답: {image_width, image_height, fixtures: [{x, y, width, height, rotation}, ...]}
    """
    from common.ai_worker_celery import AI_CELERY_QUEUE, ai_celery_app

    async_result = ai_celery_app.send_task(
        "ai_app.tasks.parse_floorplan",
        args=[image_bytes],
        queue=AI_CELERY_QUEUE,
    )
    return async_result.get(timeout=_AI_PARSE_TIMEOUT_SEC)


@transaction.atomic
def parse_layout_floorplan(user, layout_id: int, file) -> dict:
    """도면 이미지 분석 + 자동 배치 (한 transaction).

    한 호출로:
      1. 권한 검증 (ADMIN_ROLES — STAFF 거부)
      2. 파일 검증 (jpg/png, ≤10MB)
      3. ai-worker 위임 (Celery sync wait, timeout=10s) — OpenCV 검출
      4. 도면 디스크 저장 + Layout.floorplan_image_url 갱신
      5. FixtureMaster N개 INSERT (name="집기 N", height=500cm 기본)
      6. FixtureVersion N개 INSERT (version_name="진열 1")
      7. LayoutFixture N개 INSERT (해당 layout 에 좌표 + scaling 변환)

    좌표 변환:
      scale_x = store.width / image_pixel_width  (도면 픽셀 → cm)
      scale_z = store.depth / image_pixel_height
      world_pos_x = (parsed.x + parsed.width/2) * scale_x  (중심점)
      world_pos_y = 0  (바닥)
      world_pos_z = (parsed.y + parsed.height/2) * scale_z

    실패 시 PARSE_FAILED 500 + 전체 롤백. 단 디스크 파일은 orphan 가능
    (stores.upload_store_floorplan 와 동일 정책 — S3 전환 시 cleanup).
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from django.utils import timezone as dj_timezone

    # 1. 권한 검증
    layout, role = get_member_layout(user, layout_id)
    if role not in ADMIN_ROLES:
        raise BusinessException(
            "FORBIDDEN_ACCESS",
            "해당 레이아웃의 도면을 분석할 권한이 없습니다.",
            status=403,
        )

    # 2. 파일 검증
    content_type = getattr(file, "content_type", None)
    if content_type not in ALLOWED_FLOORPLAN_CONTENT_TYPES:
        raise BusinessException(
            "UNSUPPORTED_MEDIA_TYPE",
            "지원하지 않는 파일 형식입니다. (jpg, png만 허용)",
            status=415,
        )
    if file.size > MAX_FLOORPLAN_BYTES:
        raise BusinessException(
            "PAYLOAD_TOO_LARGE",
            "업로드 가능한 파일 용량(10MB)을 초과했습니다.",
            status=413,
        )

    store = layout.store
    file_bytes = file.read()

    # 3. ai-worker 위임 (sync wait)
    try:
        parse_result = _call_ai_floorplan_parse(file_bytes)
    except Exception as exc:
        raise BusinessException(
            "PARSE_FAILED",
            "도면 인식에 실패했습니다.",
            status=500,
        ) from exc

    image_pixel_w = int(parse_result.get("image_width") or 0)
    image_pixel_h = int(parse_result.get("image_height") or 0)
    raw_fixtures = parse_result.get("fixtures") or []
    if image_pixel_w <= 0 or image_pixel_h <= 0:
        raise BusinessException(
            "PARSE_FAILED",
            "도면 인식에 실패했습니다.",
            status=500,
        )

    # 4. 디스크 저장 + Layout.floorplan_image_url 갱신
    timestamp = dj_timezone.now().strftime("%Y%m%d%H%M%S")
    file_ext = "png" if content_type == "image/png" else "jpg"
    save_name = f"layouts/floorplans/layout_{layout_id}_{timestamp}.{file_ext}"
    saved_path = default_storage.save(save_name, ContentFile(file_bytes))
    layout.floorplan_image_url = default_storage.url(saved_path)

    # 5~7. scaling + FixtureMaster/Version/LayoutFixture 생성
    scale_x = store.width / image_pixel_w
    scale_z = store.depth / image_pixel_h

    created_triples: list[tuple[LayoutFixture, FixtureVersion, FixtureMaster]] = []
    for idx, fx in enumerate(raw_fixtures, start=1):
        master_width = max(1, int(round(fx["width"] * scale_x)))
        master_depth = max(1, int(round(fx["height"] * scale_z)))

        master = FixtureMaster.objects.create(
            user=user,
            name=f"집기 {idx}",
            width=master_width,
            height=_DEFAULT_FIXTURE_HEIGHT_CM,
            depth=master_depth,
        )
        version = FixtureVersion.objects.create(
            fixture_master=master,
            version_name="진열 1",
        )
        world_pos_x = int(round((fx["x"] + fx["width"] / 2) * scale_x))
        world_pos_z = int(round((fx["y"] + fx["height"] / 2) * scale_z))
        layout_fixture = LayoutFixture.objects.create(
            layout=layout,
            fixture_version=version,
            world_pos_x=world_pos_x,
            world_pos_y=0,
            world_pos_z=world_pos_z,
            world_rot_y=int(fx.get("rotation", 0)) % 360,
            width=master_width,
            height=_DEFAULT_FIXTURE_HEIGHT_CM,
            depth=master_depth,
        )
        created_triples.append((layout_fixture, version, master))

    layout.save(update_fields=["floorplan_image_url", "updated_at"])

    # 8. 응답 (LayoutDetailOut 형태 + parsed_at)
    fixtures_payload = []
    for lf, version, master in created_triples:
        fixtures_payload.append(
            {
                "layout_fixture_id": lf.id,
                "fixture_id": master.id,
                "fixture_version_id": version.id,
                "world_pos_x": lf.world_pos_x,
                "world_pos_y": lf.world_pos_y,
                "world_pos_z": lf.world_pos_z,
                "world_rot_y": lf.world_rot_y,
                "width": lf.width,
                "height": lf.height,
                "depth": lf.depth,
                "fixture_info": {
                    "name": master.name,
                    "width": master.width,
                    "height": master.height,
                    "depth": master.depth,
                    "asset_3d": None,
                },
            }
        )

    return {
        "layout_id": layout.id,
        "store_id": store.id,
        "name": layout.name,
        "comment": layout.comment,
        "is_active": layout.is_active,
        "floorplan_image_url": layout.floorplan_image_url,
        "store_dimensions": {
            "width": store.width,
            "height": store.height,
            "depth": store.depth,
        },
        "fixtures": fixtures_payload,
        "parsed_at": dj_timezone.now(),
    }
