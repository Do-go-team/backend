from django.db import IntegrityError, transaction
from django.utils import timezone

from assets_3d.models import Asset3D
from common.exceptions import BusinessException
from layouts.models import LayoutFixture
from products.models import ProductVariant
from products.services import _visible_master_queryset
from stores.models import StoreMember

from .models import FixtureMaster, FixtureVersion, FixtureVersionProduct
from .schemas import (
    FixtureCreateIn,
    FixtureUpdateIn,
    PlacementsUpdateIn,
    VersionCreateIn,
)

# fixtures 도메인 admin-tier — 매장 등록자=점장 정책에 맞춰 OWNER 는 미포함.
# 본사/점장 분리 모델이 재도입되면 stores.services.ADMIN_ROLES 와 정합 검토.
FIXTURE_ADMIN_ROLES = frozenset(
    {
        StoreMember.Role.MANAGER,
        StoreMember.Role.VICE_MANAGER,
        StoreMember.Role.VMD,
    }
)

_DIMENSION_FIELDS = frozenset({"width", "height", "depth"})


def _visible_user_ids(user) -> list[int]:
    """매장 단위 공유 정책의 가시 범위 — viewer 가 볼 수 있는 fixture 등록자
    user_id 들.

    user 가 멤버인 매장들의 모든 멤버 user_id (자기 포함, distinct).
    매장 멤버십 0건 사용자는 빈 list — 자기 자신도 포함 안 됨.
    POST /fixtures 가 매장 멤버 1+ 를 요구하므로 *정상* 시나리오에선 항상 비어
    있지 않음. 0건 케이스는 데이터 정합성 위반(매장 떠난 사용자 등) 시 발생.

    list/detail/PATCH/DELETE 의 가시성 게이트 공유 — 한 곳에 둠.
    """
    my_store_ids = list(
        StoreMember.objects.filter(user=user).values_list("store_id", flat=True)
    )
    if not my_store_ids:
        return []
    return list(
        StoreMember.objects.filter(store_id__in=my_store_ids)
        .values_list("user_id", flat=True)
        .distinct()
    )


def list_fixtures(user) -> dict:
    """전체 집기 목록 — 매장 단위 공유 정책.

    가시성: _visible_user_ids 가 정의 — 본인 + 같은 매장 동료가 등록한 fixture.
    soft-deleted 제외. order: created_at asc (스펙 example 순서).
    """
    visible_ids = _visible_user_ids(user)
    if not visible_ids:
        return {"fixtures": []}

    fixtures = (
        FixtureMaster.objects.alive()
        .filter(user_id__in=visible_ids)
        .order_by("created_at")
    )

    return {
        "fixtures": [
            {
                "fixture_id": f.id,
                "name": f.name,
                "width": f.width,
                "height": f.height,
                "depth": f.depth,
                "created_at": f.created_at,
            }
            for f in fixtures
        ],
    }


def create_fixture(user, payload: FixtureCreateIn) -> dict:
    """새 마스터 집기 등록 — 매장 멤버 1개 이상 필수.

    매장에 소속되지 않은 사용자는 fixture 를 만들어도 본인 외엔 아무도 못 봄
    (list_fixtures 의 매장 단위 공유 정책상). 즉 무의미한 row 가 누적되므로
    최소 1개 이상 매장 멤버여야 등록 허용. 명세 amend 됨 — API2 spec 참고.

    검증 실패 시 FORBIDDEN_NO_STORE 403 — 명세 line 21 의 동등 코드 명시 자리.
    """
    if not StoreMember.objects.filter(user=user).exists():
        raise BusinessException(
            "FORBIDDEN_NO_STORE",
            "fixture 를 등록하려면 1개 이상의 매장에 멤버로 속해 있어야 합니다.",
            status=403,
        )

    fixture = FixtureMaster.objects.create(
        user=user,
        name=payload.name,
        width=payload.width,
        height=payload.height,
        depth=payload.depth,
    )
    return {
        "fixture_id": fixture.id,
        "name": fixture.name,
        "width": fixture.width,
        "height": fixture.height,
        "depth": fixture.depth,
        "created_at": fixture.created_at,
    }


def get_visible_fixture(user, fixture_id: int) -> FixtureMaster:
    """가시 범위 + alive fixture 1건 fetch. 비가시/미존재/soft-deleted 모두
    FIXTURE_NOT_FOUND 404 로 묶어 ID 프로빙 차단.

    detail / versions / admin 게이트 모두의 진입점 — 여기에 가시성 정책 단일화.
    """
    fixture = (
        FixtureMaster.objects.alive()
        .filter(id=fixture_id, user_id__in=_visible_user_ids(user))
        .first()
    )
    if fixture is None:
        raise BusinessException(
            "FIXTURE_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 집기 정보입니다.",
            status=404,
        )
    return fixture


def get_fixture_detail(user, fixture_id: int) -> dict:
    """단건 조회 + asset_3d join.

    가시성: get_visible_fixture (매장 단위 공유 + alive). 비가시/미존재/soft-deleted
    모두 FIXTURE_NOT_FOUND 404.

    asset_3d 는 nullable — assets_3d 테이블에 target_type=FIXTURE,
    target_id=fixture_id 인 row 의 최신 1건만 채택 (created_at desc).
    POST /assets/3d 가 advanced 라 MVP 중엔 거의 항상 None.
    """
    fixture = get_visible_fixture(user, fixture_id)

    asset = (
        Asset3D.objects.filter(
            target_type=Asset3D.TargetType.FIXTURE,
            target_id=fixture.id,
        )
        .order_by("-created_at")
        .first()
    )
    asset_3d: dict | None = None
    if asset is not None:
        asset_3d = {
            "model_url": asset.model_url or None,
            "file_format": asset.file_format,
            "file_size": asset.file_size_bytes,
        }

    return {
        "fixture_id": fixture.id,
        "name": fixture.name,
        "dimensions": {
            "width": fixture.width,
            "height": fixture.height,
            "depth": fixture.depth,
        },
        "asset_3d": asset_3d,
        "created_at": fixture.created_at,
        "updated_at": fixture.updated_at,
    }


def get_visible_fixture_for_admin(user, fixture_id: int) -> FixtureMaster:
    """가시성 + admin-tier 게이트. 비가시/미존재/soft-deleted/STAFF/non-admin
    모두 FIXTURE_NOT_FOUND 404 로 묶어 ID 프로빙 차단 (spec line 20).

    admin 판정: fixture 등록자(creator)와 user 가 함께 속한 매장 중 하나에서라도
    user 의 role 이 FIXTURE_ADMIN_ROLES 면 OK. 다중 매장 hub 케이스에서 user 가
    한 매장은 STAFF, 다른 매장은 MANAGER 라도 creator 와 공유하는 store 에서의
    role 만이 판정 기준.
    """
    fixture = get_visible_fixture(user, fixture_id)

    creator_store_ids = list(
        StoreMember.objects.filter(user_id=fixture.user_id).values_list(
            "store_id", flat=True
        )
    )
    has_admin = StoreMember.objects.filter(
        user=user,
        store_id__in=creator_store_ids,
        role__in=FIXTURE_ADMIN_ROLES,
    ).exists()
    if not has_admin:
        raise BusinessException(
            "FIXTURE_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 집기 정보입니다.",
            status=404,
        )

    return fixture


@transaction.atomic
def update_fixture(user, fixture_id: int, payload: FixtureUpdateIn) -> dict:
    """집기 이름/크기 부분 수정 — admin-tier 만, 크기 변경 시 placement 차단.

    검증/처리 순서:
      1. get_visible_fixture_for_admin — 가시성 + admin-tier (실패 시 404)
      2. exclude_unset 으로 들어온 필드 중 *실제 값이 다른* 필드만 추출.
         같은 값이면 크기 변경으로 보지 않아 placement 검증도 생략 (no-op skip).
      3. 실제 변경 필드에 width/height/depth 가 하나라도 포함 → alive version 의
         placement 가 1+ 있으면 FIXTURE_IS_NOT_EMPTY 409.
         (soft-deleted version 은 cascade 무시 — 이미 비활성 시안)
      4. 변경 필드만 update_fields 로 save. 빈 변경 → no-op (updated_at 보호).
    """
    fixture = get_visible_fixture_for_admin(user, fixture_id)

    changes = payload.model_dump(exclude_unset=True)
    actual_changes = {
        field: value
        for field, value in changes.items()
        if getattr(fixture, field) != value
    }

    if _DIMENSION_FIELDS & actual_changes.keys():
        has_placement = FixtureVersionProduct.objects.filter(
            fixture_version__fixture_master=fixture,
            fixture_version__deleted_at__isnull=True,
        ).exists()
        if has_placement:
            raise BusinessException(
                "FIXTURE_IS_NOT_EMPTY",
                "집기 크기를 수정하시려면 진열된 제품을 모두 제거해주세요.",
                status=409,
            )

    if actual_changes:
        for field, value in actual_changes.items():
            setattr(fixture, field, value)
        fixture.save(update_fields=list(actual_changes.keys()) + ["updated_at"])

    return {
        "fixture_id": fixture.id,
        "name": fixture.name,
        "width": fixture.width,
        "height": fixture.height,
        "depth": fixture.depth,
        "updated_at": fixture.updated_at,
    }


@transaction.atomic
def delete_fixture(user, fixture_id: int) -> None:
    """집기 soft-delete + alive version cascade soft-delete.

    검증/처리:
      1. get_visible_fixture_for_admin — 가시성 + admin-tier (실패 시 404).
         이미 soft-deleted 는 .alive() filter 로 제외 → 두 번째 DELETE 자연 404.
      2. layout 사용 검증 — alive Layout 의 LayoutFixture 가 alive FixtureVersion 을
         경유해 이 fixture 를 참조하면 FIXTURE_IN_USE 409.
         soft-deleted layout/version 은 비활성으로 간주, 차단 대상 아님.
      3. FixtureMaster.soft_delete() + alive FixtureVersion cascade soft-delete
         (stores.delete_store 의 layouts cascade 패턴 정합).
         placements (FixtureVersionProduct) 는 SoftDeleteModel 아님 → row 자체는
         유지하되 부모 version 이 soft-deleted 라 read-side 에서 자연 무시.
    """
    fixture = get_visible_fixture_for_admin(user, fixture_id)

    in_use = LayoutFixture.objects.filter(
        fixture_version__fixture_master=fixture,
        fixture_version__deleted_at__isnull=True,
        layout__deleted_at__isnull=True,
    ).exists()
    if in_use:
        raise BusinessException(
            "FIXTURE_IN_USE",
            "현재 레이아웃에 배치되어 사용 중인 집기는 삭제할 수 없습니다. 배치를 먼저 해제해주세요.",
            status=409,
        )

    fixture.soft_delete()
    FixtureVersion.objects.alive().filter(fixture_master=fixture).update(
        deleted_at=timezone.now()
    )


def get_visible_version(user, fixture_id: int, version_id: int) -> FixtureVersion:
    """가시 fixture + alive version + version↔fixture 정합. 단일 쿼리.

    실패 케이스 모두 VERSION_NOT_FOUND 404 로 묶어 ID 프로빙 차단:
      - viewer 의 매장 멤버십 없음
      - fixture_id / version_id 비가시 (다른 매장)
      - fixture / version 미존재 또는 soft-deleted
      - version 이 다른 fixture 소속 (정합 위반)

    placements 조회/수정/삭제, version DELETE 의 진입점 — 가시성 단일화.
    """
    visible_ids = _visible_user_ids(user)
    if not visible_ids:
        raise BusinessException(
            "VERSION_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 프리셋(진열 버전)입니다.",
            status=404,
        )

    version = (
        FixtureVersion.objects.alive()
        .select_related("fixture_master")
        .filter(
            id=version_id,
            fixture_master_id=fixture_id,
            fixture_master__deleted_at__isnull=True,
            fixture_master__user_id__in=visible_ids,
        )
        .first()
    )
    if version is None:
        raise BusinessException(
            "VERSION_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 프리셋(진열 버전)입니다.",
            status=404,
        )
    return version


def list_version_placements(user, fixture_id: int, version_id: int) -> dict:
    """진열 버전 내부 상품 배치 목록 — 매장 단위 공유, 매장 멤버 누구나.

    가시성: get_visible_version (실패 시 VERSION_NOT_FOUND 404).
    response: placements 정렬은 placement.id asc — spec 미명시지만 안정 정렬용.
    variant.sku_code 는 nullable (167 partial unique 정책).
    """
    version = get_visible_version(user, fixture_id, version_id)

    placements = (
        FixtureVersionProduct.objects.filter(fixture_version=version)
        .select_related("variant")
        .order_by("id")
    )

    return {
        "version_id": version.id,
        "placements": [
            {
                "placement_id": p.id,
                "local_pos_x": p.local_pos_x,
                "local_pos_y": p.local_pos_y,
                "local_pos_z": p.local_pos_z,
                "status": p.status,
                "memo": p.memo,
                "variant": {
                    "variant_id": p.variant_id,
                    "sku_code": p.variant.sku_code,
                },
            }
            for p in placements
        ],
    }


def _validate_placement_rows(version: FixtureVersion, user, items: list) -> None:
    """DB 쓰기 전 사전 검증 — fail-fast (products._validate_variant_rows 패턴 정합).

    - placement_id cross-version 차단: 이 version 의 row 가 아니면
      INVALID_PLACEMENT_ID 422 (spec 미명시, 본 구현 신설 — PR 본문 명시).
    - variant_id 가시성: products 도메인 정합 (_visible_master_queryset 기준
      store_products bridge). 미존재/soft-deleted/가시 밖 모두 INVALID_VARIANT_ID 422.

    한 row 라도 실패하면 즉시 raise — DB 변화 0 (all-or-nothing).
    """
    if not items:
        return

    existing_placement_ids = set(
        FixtureVersionProduct.objects.filter(fixture_version=version).values_list(
            "id", flat=True
        )
    )
    for row in items:
        if (
            row.placement_id is not None
            and row.placement_id not in existing_placement_ids
        ):
            raise BusinessException(
                "INVALID_PLACEMENT_ID",
                "배치 목록에 유효하지 않은 placement_id 가 포함되어 있습니다.",
                status=422,
            )

    visible_master_ids = set(
        _visible_master_queryset(user).values_list("id", flat=True)
    )
    requested_variant_ids = {row.variant_id for row in items}
    valid_variant_ids = set(
        ProductVariant.objects.alive()
        .filter(
            id__in=requested_variant_ids,
            product_master_id__in=visible_master_ids,
        )
        .values_list("id", flat=True)
    )
    if requested_variant_ids - valid_variant_ids:
        raise BusinessException(
            "INVALID_VARIANT_ID",
            "배치 목록 중 유효하지 않은 상품 옵션(variant)이 포함되어 있습니다.",
            status=422,
        )


def _sync_placements(version: FixtureVersion, items: list) -> tuple[int, int]:
    """3-rule bulk sync. _validate_placement_rows 통과한 items 만 들어옴.

    - placement_id 있음 → UPDATE (좌표/메모/status/variant 갱신)
    - placement_id 없음 → INSERT (신규 row)
    - DB 의 alive placement 중 items 에 누락된 id → HARD DELETE
      (FixtureVersionProduct 는 SoftDeleteModel 비상속)

    Returns (synced_count, deleted_count). synced = INSERT + UPDATE 합산.
    """
    existing_ids = set(
        FixtureVersionProduct.objects.filter(fixture_version=version).values_list(
            "id", flat=True
        )
    )
    request_ids: set[int] = set()
    synced = 0

    for row in items:
        status_value = row.status or FixtureVersionProduct.Status.DISPLAY
        if row.placement_id is None:
            FixtureVersionProduct.objects.create(
                fixture_version=version,
                variant_id=row.variant_id,
                local_pos_x=row.local_pos_x,
                local_pos_y=row.local_pos_y,
                local_pos_z=row.local_pos_z,
                memo=row.memo,
                status=status_value,
            )
        else:
            FixtureVersionProduct.objects.filter(id=row.placement_id).update(
                variant_id=row.variant_id,
                local_pos_x=row.local_pos_x,
                local_pos_y=row.local_pos_y,
                local_pos_z=row.local_pos_z,
                memo=row.memo,
                status=status_value,
            )
            request_ids.add(row.placement_id)
        synced += 1

    to_delete_ids = existing_ids - request_ids
    deleted_count = 0
    if to_delete_ids:
        deleted_count, _ = FixtureVersionProduct.objects.filter(
            id__in=to_delete_ids
        ).delete()

    return synced, deleted_count


@transaction.atomic
def update_version_placements(
    user, fixture_id: int, version_id: int, payload: PlacementsUpdateIn
) -> dict:
    """집기 내부 상품 배치 벌크 동기화 — 매장 멤버 누구나 (STAFF 포함, spec line 20).

    검증/처리:
      1. get_visible_version — 가시성 + 정합 (실패 시 VERSION_NOT_FOUND 404).
         일상적 진열 작업이라 admin-tier 게이트 없음.
      2. _validate_placement_rows — fail-fast (placement_id cross-version,
         variant 가시성/존재/alive). 실패 시 422, DB 변화 0.
      3. _sync_placements — 3-rule bulk (UPDATE/INSERT/HARD DELETE) 단일 atomic.
      4. version.updated_at touch — products 패턴 일관, no-op skip 없음.

    빈 배열 (`placements: []`) — 모든 alive placement hard delete (products 의
    variants=[] = 전부 삭제 패턴 정합). FE confirm 책임.
    """
    version = get_visible_version(user, fixture_id, version_id)

    _validate_placement_rows(version, user, payload.placements)

    try:
        synced, deleted = _sync_placements(version, payload.placements)
    except IntegrityError as exc:
        # 방어적: variant FK 는 PROTECT 라 race 로도 거의 안 남. products 패턴 일관.
        raise BusinessException(
            "INVALID_VARIANT_ID",
            "배치 목록 중 유효하지 않은 상품 옵션(variant)이 포함되어 있습니다.",
            status=422,
        ) from exc

    # version.updated_at 명시 bump — placement 변경 자체가 version "수정" 이라
    # FE 가 최신순 정렬에서 활용 (list_fixture_versions order_by updated_at desc)
    version.save(update_fields=["updated_at"])

    return {
        "version_id": version.id,
        "updated_count": synced,
        "deleted_count": deleted,
        "updated_at": version.updated_at,
    }


def get_visible_version_for_admin(
    user, fixture_id: int, version_id: int
) -> FixtureVersion:
    """get_visible_version + admin-tier 게이트. 실패 시 VERSION_NOT_FOUND 404.

    spec line 20 — STAFF / non-admin 도 NOT_FOUND 로 묶음 (ID 프로빙 차단).
    admin 판정은 fixture creator 와 user 가 공유하는 매장에서의 user role
    (get_visible_fixture_for_admin 와 동일 로직 — 다중 매장 hub 케이스 정합).
    """
    version = get_visible_version(user, fixture_id, version_id)
    creator_id = version.fixture_master.user_id

    creator_store_ids = list(
        StoreMember.objects.filter(user_id=creator_id).values_list(
            "store_id", flat=True
        )
    )
    has_admin = StoreMember.objects.filter(
        user=user,
        store_id__in=creator_store_ids,
        role__in=FIXTURE_ADMIN_ROLES,
    ).exists()
    if not has_admin:
        raise BusinessException(
            "VERSION_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 프리셋(진열 버전)입니다.",
            status=404,
        )
    return version


@transaction.atomic
def delete_fixture_version(user, fixture_id: int, version_id: int) -> None:
    """진열 버전 soft-delete — admin-tier 만, layout 사용 중이면 차단.

    검증/처리:
      1. get_visible_version_for_admin — 가시성 + 정합 + admin-tier
         (실패 시 VERSION_NOT_FOUND 404). 이미 soft-deleted 는 .alive() filter
         로 제외 → 두 번째 DELETE 자연 404 (idempotent NOT, spec line 75).
      2. layout 사용 검증 — alive Layout 의 LayoutFixture 가 이 version 을
         가리키면 VERSION_IN_USE 409. soft-deleted layout 은 비활성으로 간주.
         (delete_fixture 의 cascade 정책 정합)
      3. version.soft_delete(). placements (FixtureVersionProduct) 는
         SoftDeleteModel 아님 → row 자체는 유지하되 부모 version 이 dead 라
         read-side 에서 자연 무시.
    """
    version = get_visible_version_for_admin(user, fixture_id, version_id)

    in_use = LayoutFixture.objects.filter(
        fixture_version=version,
        layout__deleted_at__isnull=True,
    ).exists()
    if in_use:
        raise BusinessException(
            "VERSION_IN_USE",
            "현재 매장 레이아웃에 배치되어 사용 중인 진열 버전은 삭제할 수 없습니다. 배치를 먼저 해제해주세요.",
            status=409,
        )

    version.soft_delete()


def create_fixture_version(user, fixture_id: int, payload: VersionCreateIn) -> dict:
    """집기 진열 버전 생성 — 매장 단위 공유, 매장 멤버 누구나 (STAFF 포함).

    검증/처리:
      1. get_visible_fixture — 가시 범위 + alive (실패 시 FIXTURE_NOT_FOUND 404).
         일상적인 진열 작업이라 admin-tier 게이트 없음 — STAFF 도 시안 추가 가능.
      2. version_name 공백/빈 문자열 거부 → INVALID_PARAMETER 400 (스펙 line 90 정합).
         strip 적용해 저장 — 양 끝 공백은 의미 없음.
      3. FixtureVersion row 생성 (TimeStamped + SoftDelete 상속, alive 시작).
    """
    fixture = get_visible_fixture(user, fixture_id)

    version_name = (payload.version_name or "").strip()
    if not version_name:
        raise BusinessException(
            "INVALID_PARAMETER",
            "진열 버전 이름(version_name)은 필수 입력값입니다.",
            status=400,
        )

    version = FixtureVersion.objects.create(
        fixture_master=fixture,
        version_name=version_name,
    )
    return {
        "version_id": version.id,
        "fixture_id": fixture.id,
        "version_name": version.version_name,
        "created_at": version.created_at,
    }


def list_fixture_versions(user, fixture_id: int) -> dict:
    """집기에 속한 alive 진열 버전 목록 — 매장 단위 공유 정책.

    가시성: get_visible_fixture (매장 멤버 누구나, STAFF 포함). read 전용이라
    admin-tier 게이트 없음.

    정렬: updated_at desc, id desc — spec "최신순" 해석. 같은 시각 동시 등록
    케이스에서 id desc 가 안정 정렬 tie-breaker.
    """
    fixture = get_visible_fixture(user, fixture_id)

    versions = (
        FixtureVersion.objects.alive()
        .filter(fixture_master=fixture)
        .order_by("-updated_at", "-id")
    )

    return {
        "fixture_id": fixture.id,
        "versions": [
            {
                "version_id": v.id,
                "version_name": v.version_name,
                "created_at": v.created_at,
                "updated_at": v.updated_at,
            }
            for v in versions
        ],
    }
