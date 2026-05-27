import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from assets_3d.models import Asset3D
from common.exceptions import BusinessException
from layouts.models import Layout
from products.models import (
    ProductMaster,
    ProductVariant,
    StoreInventory,
    StoreProduct,
)

from .models import Store, StoreImage, StoreInvitation, StoreMember
from .schemas import (
    InvitationAcceptIn,
    InvitationCreateIn,
    MemberRoleUpdateIn,
    StoreCreateIn,
    StoreProductUpdateIn,
    StoreUpdateIn,
)
from .tasks import send_store_invitation_email

User = get_user_model()

# 초대 토큰 만료 시간 — 스펙 예시 "발급 시점으로부터 24시간 뒤" 일치.
INVITATION_TTL = timedelta(hours=24)

# 매장 admin tier — 매장 단위 write 권한 게이트.
# STAFF 만 read-only, 나머지(OWNER/MANAGER/VICE_MANAGER/VMD)는 모두 write 가능.
# 향후 액션별 세분화(예: 매장 정보 수정은 점장단까지만, 진열은 VMD 까지) 가
# 필요해지면 helper 를 분기. 현재는 단일 게이트.
ADMIN_ROLES = frozenset(
    {
        StoreMember.Role.OWNER,
        StoreMember.Role.MANAGER,
        StoreMember.Role.VICE_MANAGER,
        StoreMember.Role.VMD,
    }
)

# 매장 단위 admin 정원 카운트 — 159 응답의 admin_quota.current,
# 161 의 editor_quota 와 사실상 동일 개념 (점장+부점장+VMD).
# OWNER 는 본사 소속이므로 매장 정원에서 제외.
# (159 스펙 line 49 는 OWNER+MANAGER 만 명시했으나, role 체계 재정의
# 결과 MANAGER+VICE_MANAGER+VMD 로 운영하기로 결정 — 스펙 정정 follow-up
# 필요. PR 본문 참조.)
STORE_QUOTA_ROLES = frozenset(
    {
        StoreMember.Role.MANAGER,
        StoreMember.Role.VICE_MANAGER,
        StoreMember.Role.VMD,
    }
)

_IMAGE_FIELD_TO_TYPE = {
    "floorplan_image_url": StoreImage.ImageType.FLOORPLAN,
    "actual_photo_url": StoreImage.ImageType.ACTUAL_PHOTO,
}

# 2D 도면 업로드 검증.
# webp 도 spec intro 에 언급됐지만 에러 메시지가 jpg/png 만 명시 → 보수적으로 2종.
ALLOWED_FLOORPLAN_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})
MAX_FLOORPLAN_BYTES = 10 * 1024 * 1024


def get_member_store(user, store_id: int) -> tuple[Store, str]:
    """Resolve a store the caller has access to, raising STORE_NOT_FOUND
    otherwise. Existence + soft-delete + membership are collapsed into a
    single 404 so non-members cannot probe for store IDs (spec requirement).

    Returns (store, member_role) for reuse by downstream services.
    """
    membership = (
        StoreMember.objects.select_related("store")
        .filter(user=user, store_id=store_id, store__deleted_at__isnull=True)
        .first()
    )
    if membership is None:
        raise BusinessException(
            "STORE_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 매장입니다.",
            status=404,
        )
    return membership.store, membership.role


def get_member_store_for_admin(
    user,
    store_id: int,
    *,
    error_code: str = "FORBIDDEN_ACCESS",
    error_message: str | None = None,
) -> tuple[Store, str]:
    """Admin-only variant of get_member_store. Non-members still get the
    STORE_NOT_FOUND 404 (no ID probing), but members without ADMIN_ROLES
    get a 403 per spec.

    Pass error_message to override the default — different admin operations
    (edit, assign products, ...) want spec-specific 403 messages while
    sharing the same role-check.

    Pass error_code to use a code other than FORBIDDEN_ACCESS.
    160 (직원 권한 수정) requires FORBIDDEN_ACTION per its spec.
    """
    store, role = get_member_store(user, store_id)
    if role not in ADMIN_ROLES:
        raise BusinessException(
            error_code,
            error_message or "매장 정보를 수정할 권한이 없습니다.",
            status=403,
        )
    return store, role


def get_member_store_for_manager(user, store_id: int) -> tuple[Store, str]:
    """MANAGER-only variant for destructive store-level ops (DELETE).
    매장 등록자 = 점장(MANAGER) 정책에 따라, 매장 삭제는 점장 본인만 가능.
    부점장/VMD/STAFF 모두 거부. legacy OWNER row 도 여기서는 거부 — 매장 삭제는
    *현재 점장* 의 결정이라는 명시적 정책.
    """
    store, role = get_member_store(user, store_id)
    if role != StoreMember.Role.MANAGER:
        raise BusinessException(
            "FORBIDDEN_ACCESS",
            "매장을 삭제할 권한이 없습니다. (점장 권한 필요)",
            status=403,
        )
    return store, role


def _apply_image_changes(store: Store, image_changes: dict[str, str | None]) -> None:
    """Apply store_images upsert/delete semantics for a PATCH payload.
    value=URL → update_or_create; value=None → delete row for that image_type.
    """
    for field, value in image_changes.items():
        image_type = _IMAGE_FIELD_TO_TYPE[field]
        if value is None:
            StoreImage.objects.filter(store=store, image_type=image_type).delete()
        else:
            StoreImage.objects.update_or_create(
                store=store,
                image_type=image_type,
                defaults={"image_url": value},
            )


@transaction.atomic
def create_store(user, payload: StoreCreateIn) -> dict:
    """매장 생성 + 가상 공간 캔버스 정보 등록.

    동작:
      1. Store row — 요청자가 user(=점장) 로 귀속.
      2. StoreMember(role=MANAGER) — 생성자 본인을 점장으로 등록.
         (이후 GET /stores/{id}, list_my_stores 등 멤버십 기반 조회의 진입점.)
         OWNER role 은 enum 에 보존되어 있으나 본 흐름에서는 미사용 — 본사/점장
         분리가 필요해지면 그때 재활성화.
      3. StoreImage — floorplan/actual_photo 가 들어온 경우만 row 생성.
         update_store 의 _apply_image_changes 와 동일하게 image_type 별 1 row.
    """
    store = Store.objects.create(
        user=user,
        name=payload.name,
        address=payload.address,
        width=payload.width,
        height=payload.height,
        depth=payload.depth,
    )
    StoreMember.objects.create(
        store=store,
        user=user,
        role=StoreMember.Role.MANAGER,
    )

    if payload.floorplan_image_url:
        StoreImage.objects.create(
            store=store,
            image_type=StoreImage.ImageType.FLOORPLAN,
            image_url=payload.floorplan_image_url,
        )
    if payload.actual_photo_url:
        StoreImage.objects.create(
            store=store,
            image_type=StoreImage.ImageType.ACTUAL_PHOTO,
            image_url=payload.actual_photo_url,
        )

    return {
        "store_id": store.id,
        "name": store.name,
        "address": store.address,
        "width": store.width,
        "height": store.height,
        "depth": store.depth,
        "created_at": store.created_at,
    }


@transaction.atomic
def update_store(user, store_id: int, payload: StoreUpdateIn) -> dict:
    store, _ = get_member_store_for_admin(user, store_id)

    # exclude_unset distinguishes "key omitted" from "key sent as null":
    # PATCH semantics require the former to be ignored and the latter to clear.
    changes = payload.model_dump(exclude_unset=True)
    image_changes = {
        key: changes.pop(key) for key in list(changes) if key in _IMAGE_FIELD_TO_TYPE
    }

    if changes:
        for field, value in changes.items():
            setattr(store, field, value)
        store.save(update_fields=list(changes.keys()) + ["updated_at"])

    if image_changes:
        _apply_image_changes(store, image_changes)

    return {
        "store_id": store.id,
        "name": store.name,
        "address": store.address,
        "width": store.width,
        "height": store.height,
        "depth": store.depth,
        "updated_at": store.updated_at,
    }


@transaction.atomic
def upload_store_floorplan(user, store_id: int, file) -> dict:
    """매장 2D 도면 이미지 업로드 / 덮어쓰기.

    권한: ADMIN_ROLES (PATCH /stores/{id} 와 동일 — STAFF 는 도면 변경 불가).
    파일 검증:
      - content_type 화이트리스트 (jpg/png) → UNSUPPORTED_MEDIA_TYPE 415
      - 크기 10MB 이하 → PAYLOAD_TOO_LARGE 413
    저장: StoreImage(image_type=FLOORPLAN) upsert. 기존 row 가 있으면 image_url
    교체, 없으면 신규 생성. update_store 의 _apply_image_changes 와 동일하게
    'image_type 별 1 row' 정책 유지.

    Store.updated_at 도 갱신해 응답 envelope 의 'updated_at' 으로 사용
    (스펙 응답 필드 일치). FE 캐시 무효화 신호도 겸함.

    Note: 기존 파일은 disk 에 그대로 남음 (orphan). MEDIA_ROOT 가 로컬일 때는
    수동 cleanup 필요. S3 전환 후 storage 백엔드의 delete 정책으로 처리 예정
    (CLAUDE.md 'S3/CloudFront is planned but not wired' 참조).
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="해당 매장의 도면 이미지를 업로드할 권한이 없습니다.",
    )

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

    image, _ = StoreImage.objects.update_or_create(
        store=store,
        image_type=StoreImage.ImageType.FLOORPLAN,
        defaults={"image_url": file},
    )
    store.save(update_fields=["updated_at"])

    return {
        "store_id": store.id,
        "floorplan_image_url": image.image_url.url,
        "updated_at": store.updated_at,
    }


@transaction.atomic
def assign_products_to_store(user, store_id: int, product_ids: list[int]) -> dict:
    """Bulk-assign product masters to a store.

    Policy decisions baked in (deviation from spec defaults below; recorded
    in PR for traceability):
      - Strict: any invalid product_master_id (missing or soft-deleted)
        rejects the entire request with 422 INVALID_PRODUCT_IDS. Avoids
        silent drops that frontends could mistake for backend bugs.
      - Reactivation: a previously-PAUSED assignment flips back to ACTIVE
        and counts toward assigned_count.
      - total_count covers ACTIVE + PAUSED — the spec's "취급 중" reads
        broadly per ERD comment ("취급 상태").
      - Inventory cascade: every variant under each assigned product
        gets a StoreInventory(stock_quantity=0) row, idempotently.
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="해당 매장에 상품을 할당할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
    )

    unique_ids = list({pid for pid in product_ids})
    if not unique_ids:
        return {
            "assigned_count": 0,
            "total_count": StoreProduct.objects.filter(store=store).count(),
        }

    found = ProductMaster.objects.alive().filter(id__in=unique_ids)
    found_ids = set(found.values_list("id", flat=True))
    invalid_ids = sorted(set(unique_ids) - found_ids)
    if invalid_ids:
        raise BusinessException(
            "INVALID_PRODUCT_IDS",
            f"유효하지 않은 상품 ID 가 포함되어 있습니다: {invalid_ids}",
            status=422,
        )

    assigned_count = 0
    for product in found:
        sp, created = StoreProduct.objects.get_or_create(
            store=store,
            product_master=product,
            defaults={"status": StoreProduct.Status.ACTIVE},
        )
        if created:
            assigned_count += 1
        elif sp.status == StoreProduct.Status.PAUSED:
            sp.status = StoreProduct.Status.ACTIVE
            sp.save(update_fields=["status", "updated_at"])
            assigned_count += 1
        # ACTIVE → ACTIVE: no-op, not counted

        # Inventory cascade — idempotent. New variants for an existing
        # product also get inventory rows when assignment is re-confirmed.
        variant_ids = (
            ProductVariant.objects.alive()
            .filter(
                product_master=product,
            )
            .values_list("id", flat=True)
        )
        for variant_id in variant_ids:
            StoreInventory.objects.get_or_create(
                store=store,
                variant_id=variant_id,
                defaults={"stock_quantity": 0},
            )

    return {
        "assigned_count": assigned_count,
        "total_count": StoreProduct.objects.filter(store=store).count(),
    }


# Synthesized status for the GET response. Not stored in DB — when the
# product_master is soft-deleted by HQ, the store's matching row keeps its
# original status (ACTIVE/PAUSED) but the response surface flips to this.
STATUS_DISCONTINUED = "DISCONTINUED"


def list_store_products(user, store_id: int) -> dict:
    """Return all products handled by the store, including soft-deleted
    masters/variants surfaced as DISCONTINUED / is_discontinued so the
    client can render them distinctly instead of having them silently vanish.

    Query plan (~4 round-trips regardless of product count):
      1. StoreProduct + product_master (select_related) + variants (prefetch)
      2. StoreInventory bulk fetch by variant_id
      3. Asset3D bulk fetch by product_master_id
    """
    store, _ = get_member_store(user, store_id)

    store_products = list(
        StoreProduct.objects.filter(store=store)
        .select_related("product_master")
        .prefetch_related("product_master__variants")
        .order_by("product_master_id")
    )

    if not store_products:
        return {"products": []}

    product_master_ids: list[int] = []
    variant_ids: list[int] = []
    for sp in store_products:
        product_master_ids.append(sp.product_master_id)
        variant_ids.extend(v.id for v in sp.product_master.variants.all())

    inventory_map = dict(
        StoreInventory.objects.filter(
            store=store, variant_id__in=variant_ids
        ).values_list("variant_id", "stock_quantity")
    )

    # Latest asset per product (decision #4) — order by created_at desc and
    # take the first hit per target_id.
    asset_map: dict[int, str | None] = {}
    for asset in Asset3D.objects.filter(
        target_type=Asset3D.TargetType.PRODUCT, target_id__in=product_master_ids
    ).order_by("target_id", "-created_at"):
        if asset.target_id not in asset_map:
            asset_map[asset.target_id] = asset.model_url or None

    products: list[dict] = []
    for sp in store_products:
        pm = sp.product_master
        # Decision #1: synthesize DISCONTINUED on output. DB still stores
        # ACTIVE/PAUSED; the master's deleted_at is the single source of truth
        # for "discontinued by HQ".
        status = STATUS_DISCONTINUED if pm.deleted_at is not None else sp.status

        variants_payload: list[dict] = []
        for v in pm.variants.all():
            variants_payload.append(
                {
                    "id": v.id,
                    "size": v.size,
                    "color": v.color,
                    "sku_code": v.sku_code,
                    "barcode_image_url": v.barcode_image_url.url
                    if v.barcode_image_url
                    else None,
                    # Decision #3: spec marks stock_quantity required (Y INT) — fall
                    # back to 0 when the inventory row is absent (variant added after
                    # assignment). Distinct from "no data" which we don't represent.
                    "stock_quantity": inventory_map.get(v.id, 0),
                    # Decision #2: variant-level discontinuation as additive boolean
                    # (avoids overloading variant.status which doesn't exist).
                    "is_discontinued": v.deleted_at is not None,
                }
            )

        products.append(
            {
                "id": pm.id,
                "name": pm.name,
                "price": pm.price,
                "status": status,
                "width": pm.width,
                "height": pm.height,
                "depth": pm.depth,
                # 163 의 URLField 전환 — string 자체가 외부 URL 이라 .url 호출 X.
                "image_url": pm.image_url if pm.image_url else None,
                "model_url": asset_map.get(pm.id),
                "variants": variants_payload,
            }
        )

    return {"products": products}


@transaction.atomic
def update_store_product(
    user,
    store_id: int,
    product_id: int,
    payload: StoreProductUpdateIn,
) -> dict:
    """Toggle the store-side handling status (ACTIVE/PAUSED) for one
    assigned product.

    Soft-deleted product_masters remain patchable on purpose. The
    DISCONTINUED status surfaced in GET (decision from 156) is a
    response-time overlay representing HQ's decision; it is orthogonal
    to the store's own handling intent. A store may still want to PAUSE
    a discontinued product to clear its display, or keep it ACTIVE while
    running down stock. PATCH operates on the stored value; the GET
    overlay continues to win at read time when the master is deleted.

    No-op (current == requested) skips the save so updated_at does not
    spuriously bump — protects against PATCH being used as a touch.
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="해당 매장의 상품 정보를 수정할 권한이 없습니다.",
    )

    sp = StoreProduct.objects.filter(store=store, product_master_id=product_id).first()
    if sp is None:
        raise BusinessException(
            "PRODUCT_NOT_ASSIGNED",
            "해당 매장에 할당되어 있지 않은 상품입니다.",
            status=404,
        )

    if sp.status != payload.status:
        sp.status = payload.status
        sp.save(update_fields=["status", "updated_at"])

    return {
        "product_id": product_id,
        "status": sp.status,
        "updated_at": sp.updated_at,
    }


@transaction.atomic
def delete_store_product(user, store_id: int, product_id: int) -> dict:
    """Hard-delete one product from a store's handling list.

    Spec calls for hard delete (not soft) on store_products and a cascading
    hard delete of the matching store_inventories rows so the store's
    inventory book stays consistent. The product_master itself is HQ-owned
    and untouched — only the store-side assignment + per-store stock vanish.

    Soft-deleted product_masters are still deletable: 158 means "stop
    handling", which is orthogonal to HQ's discontinuation flag (same
    direction as the 156/157 policy on DISCONTINUED).

    total_count uses the same definition as 155's response — every
    StoreProduct row for this store regardless of status, mirroring
    the GET projection so the FE counter stays in sync.
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="해당 매장의 정보를 수정할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
    )

    sp = StoreProduct.objects.filter(store=store, product_master_id=product_id).first()
    if sp is None:
        raise BusinessException(
            "PRODUCT_NOT_ASSIGNED",
            "해당 매장에 할당되어 있지 않은 상품입니다.",
            status=404,
        )

    # Inventory cascade — variant.deleted_at intentionally ignored so a
    # discontinued variant's stock row doesn't leak after the parent
    # assignment is gone.
    StoreInventory.objects.filter(
        store=store,
        variant__product_master_id=product_id,
    ).delete()
    sp.delete()

    return {
        "deleted_product_id": product_id,
        "total_count": StoreProduct.objects.filter(store=store).count(),
    }


@transaction.atomic
def delete_store(user, store_id: int) -> None:
    """Soft-delete the store and cascade-soft-delete its layouts in one
    transaction. Spec marks the layout cascade as recommended; we apply
    it so a deleted store can't leave stranded layouts visible to clients.
    """
    store, _ = get_member_store_for_manager(user, store_id)
    store.soft_delete()
    Layout.objects.alive().filter(store=store).update(deleted_at=timezone.now())


def get_store_detail(user, store_id: int) -> dict:
    store, my_role = get_member_store(user, store_id)

    floorplan_url: str | None = None
    actual_url: str | None = None
    for image in store.images.all():
        if image.image_type == StoreImage.ImageType.FLOORPLAN and floorplan_url is None:
            floorplan_url = image.image_url.url if image.image_url else None
        elif (
            image.image_type == StoreImage.ImageType.ACTUAL_PHOTO and actual_url is None
        ):
            actual_url = image.image_url.url if image.image_url else None

    return {
        "store_id": store.id,
        "name": store.name,
        "width": store.width,
        "height": store.height,
        "depth": store.depth,
        "my_role": my_role,
        "floorplan_image_url": floorplan_url,
        "actual_photo_url": actual_url,
        "created_at": store.created_at,
        "updated_at": store.updated_at,
    }


def list_store_members(user, store_id: int) -> dict:
    """List all members of a store + admin quota meta.

    Permission: any membership (same as detail/list endpoints) — read-only
    so STAFF can see colleague info too.

    admin_quota.current counts members in STORE_QUOTA_ROLES
    (MANAGER/VICE_MANAGER/VMD). OWNER excluded — HQ user, not part of the
    matjang's admin headcount budget. STAFF excluded — not an admin tier.
    Same definition as 161's editor_quota.

    Members are ordered by joined_at (created_at) for stable rendering.
    """
    store, _ = get_member_store(user, store_id)

    memberships = list(
        StoreMember.objects.filter(store=store)
        .select_related("user")
        .order_by("created_at")
    )

    members_payload: list[dict] = []
    admin_count = 0
    for m in memberships:
        u = m.user
        if m.role in STORE_QUOTA_ROLES:
            admin_count += 1
        members_payload.append(
            {
                "user_id": u.id,
                "name": u.name,
                "email": u.email,
                "profile_image_url": u.profile_image_url.url
                if u.profile_image_url
                else None,
                "role": m.role,
                "joined_at": m.created_at,
            }
        )

    return {
        "admin_quota": {
            "current": admin_count,
            "max": store.max_admin_count,
        },
        "members": members_payload,
    }


@transaction.atomic
def update_store_member_role(
    user,
    store_id: int,
    target_user_id: int,
    payload: MemberRoleUpdateIn,
) -> dict:
    """Change a member's role in a store.

    Permission gate uses ADMIN_ROLES (159 정합 — STAFF 제외 모두 가능).
    Spec error code FORBIDDEN_ACTION (다른 엔드포인트의 FORBIDDEN_ACCESS 와 다름).

    Allowed target roles: MANAGER / VICE_MANAGER / VMD / STAFF
    (schema 단계에서 강제). OWNER 임명은 MVP 범위 밖 — 소유권 이관 advanced.

    OWNER 의 role 변경 시도는 거부 — 소유권 이관 의미라 별도 endpoint 필요.

    Quota check: 변경 후 STORE_QUOTA_ROLES 멤버 수가 max_admin_count 를 넘으면
    ADMIN_QUOTA_EXCEEDED. STAFF→{quota role} 승급에서만 카운트가 증가하므로
    그 케이스에서만 검증.

    No-op (현재 == 요청) 은 save 스킵하여 updated_at 보호 (157 와 동일 정책).
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_code="FORBIDDEN_ACTION",
        error_message="직원의 권한을 변경할 수 있는 관리자 권한이 없습니다.",
    )

    target = StoreMember.objects.filter(store=store, user_id=target_user_id).first()
    if target is None:
        raise BusinessException(
            "MEMBER_NOT_FOUND",
            "해당 매장의 직원이 아닙니다.",
            status=404,
        )

    if target.role == StoreMember.Role.OWNER:
        # 본사(OWNER) 권한은 소유권 이관 별도 엔드포인트에서만 변경 가능.
        raise BusinessException(
            "FORBIDDEN_ACTION",
            "OWNER 의 권한은 변경할 수 없습니다.",
            status=403,
        )

    new_role = payload.role
    old_role = target.role

    if new_role != old_role:
        # Quota 검증: STAFF → quota tier 승급만 카운트 증가. lateral / 강등은 무관.
        promoting_into_quota = (
            new_role in STORE_QUOTA_ROLES and old_role not in STORE_QUOTA_ROLES
        )
        if promoting_into_quota:
            current_count = StoreMember.objects.filter(
                store=store, role__in=STORE_QUOTA_ROLES
            ).count()
            if current_count + 1 > store.max_admin_count:
                raise BusinessException(
                    "ADMIN_QUOTA_EXCEEDED",
                    f"해당 매장의 관리자 계정 생성 한도({store.max_admin_count}명)를 "
                    "초과하여 승급할 수 없습니다.",
                    status=409,
                )

        target.role = new_role
        target.save(update_fields=["role", "updated_at"])

    return {
        "store_id": store.id,
        "user_id": target_user_id,
        "role": target.role,
        "updated_at": target.updated_at,
    }


def _build_invite_link(token: str) -> str:
    """프론트 절대 URL + token 쿼리 — 스펙 예시 형식 일치.
    FRONTEND_URL 미설정이면 path-only (FE 가 같은 origin 가정 시 동작)."""
    base = (settings.FRONTEND_URL or "").rstrip("/")
    return f"{base}/invite?token={token}"


@transaction.atomic
def create_store_invitation(
    user,
    store_id: int,
    payload: InvitationCreateIn,
) -> dict:
    """매장 워크스페이스 초대 링크 생성 + 메일 발송.

    권한: ADMIN_ROLES (159 정합). 스펙 코드 FORBIDDEN_ACCESS.

    검증 순서:
      1. 권한 (FORBIDDEN_ACCESS)
      2. 이미 매장 멤버인 사용자 (ALREADY_MEMBER)
      3. 같은 store + 같은 email 의 대기 invitation 존재 (ALREADY_INVITED — 스펙
         미정의. 메일 폭탄/quota 인플레이션 방지 위해 추가)
      4. Editor quota — target_role 이 STORE_QUOTA_ROLES 안일 때만.
         카운트 = 현재 STORE_QUOTA_ROLES 멤버 + 같은 tier 의 alive pending invitation.
         스펙 line 99 는 5명 하드코드지만 159 정합으로 store.max_admin_count 사용.

    invite_token: secrets.token_urlsafe(32) (~256bit, URL-safe).
    expires_at: now + 24h (스펙 일치).

    Email: send_store_invitation_email.delay() — DB row 는 트랜잭션 commit 후
    Celery 워커가 처리해야 안전 (transaction.on_commit 으로 디스패치).
    """
    store, _ = get_member_store_for_admin(
        user,
        store_id,
        error_message="매장에 인원을 초대할 권한이 없습니다. (OWNER/MANAGER 권한 필요)",
    )

    invite_email = payload.invite_email
    target_role = payload.target_role

    # 2. 이미 매장 멤버인지 (User row 가 있을 때만 체크 — 미가입자 초대는 정상 시나리오)
    invitee_user = User.objects.filter(email=invite_email).first()
    if (
        invitee_user is not None
        and StoreMember.objects.filter(
            store=store,
            user=invitee_user,
        ).exists()
    ):
        raise BusinessException(
            "ALREADY_MEMBER",
            "해당 이메일 사용자는 이미 이 매장의 멤버입니다.",
            status=409,
        )

    # 3. 중복 대기 invitation
    now = timezone.now()
    if StoreInvitation.objects.filter(
        store=store,
        invitee_email=invite_email,
        is_used=False,
        expires_at__gt=now,
    ).exists():
        raise BusinessException(
            "ALREADY_INVITED",
            "해당 이메일로 이미 대기 중인 초대장이 있습니다.",
            status=409,
        )

    # 4. Editor quota — STORE_QUOTA_ROLES 대상일 때만
    if target_role in STORE_QUOTA_ROLES:
        member_count = StoreMember.objects.filter(
            store=store,
            role__in=STORE_QUOTA_ROLES,
        ).count()
        pending_count = StoreInvitation.objects.filter(
            store=store,
            target_role__in=STORE_QUOTA_ROLES,
            is_used=False,
            expires_at__gt=now,
        ).count()
        if member_count + pending_count + 1 > store.max_admin_count:
            raise BusinessException(
                "EDITOR_QUOTA_EXCEEDED",
                f"현재 에디터(MANAGER/VICE_MANAGER/VMD) 정원({store.max_admin_count}명)이 "
                "꽉 찼거나, 대기 중인 초대장이 있어 더 이상 해당 직급으로 초대할 수 없습니다.",
                status=409,
            )

    # 토큰 생성 — 충돌 확률 ≈ 0 이지만 unique constraint 가 있어 IntegrityError 시 500.
    token = secrets.token_urlsafe(32)
    expires_at = now + INVITATION_TTL

    invitation = StoreInvitation.objects.create(
        store=store,
        inviter=user,
        invitee_email=invite_email,
        invite_token=token,
        target_role=target_role,
        expires_at=expires_at,
    )

    invite_link = _build_invite_link(token)

    # 트랜잭션 commit 후 메일 dispatch — commit 전 워커가 row 못 찾을 위험 회피
    transaction.on_commit(
        lambda: send_store_invitation_email.delay(
            invitee_email=invite_email,
            store_name=store.name,
            inviter_name=user.name,
            invite_link=invite_link,
        )
    )

    return {
        "invitation_id": invitation.id,
        "invitee_email": invite_email,
        "target_role": target_role,
        "invite_link": invite_link,
        "expires_at": expires_at,
    }


@transaction.atomic
def accept_store_invitation(user, payload: InvitationAcceptIn) -> dict:
    """초대 토큰으로 매장 워크스페이스 합류.

    검증 순서 (보안 우선 — 토큰 존재성 노출 최소화):
      1. 토큰 미존재 → INVALID_TOKEN (404)
      2. is_used=True → INVALID_TOKEN (404, 같은 코드로 묶음)
      3. expires_at < now → INVITATION_EXPIRED (410)
      4. invitee_email 미일치 → INVALID_TOKEN (404, mismatch 노출 안 함)
      5. store soft-deleted → INVALID_TOKEN (404, store 존재성 노출 안 함)
      6. 이미 매장 멤버 → ALREADY_MEMBER (409)
      7. 통과 → store_member 생성 + token used 마킹 (단일 트랜잭션)

    select_for_update 로 토큰 row lock — 동시 accept 시 한쪽만 성공.
    Quota 재검증 안 함 — 발급 시점(161) 책임. accept UX 보호.

    잘못된 사람 가입 위험: 관리자 typo 로 외부 이메일에 보내면 그 사람도
    가입 가능. 162 범위 밖 (follow-up 티켓: 초대 취소 / 멤버 강제 제거).
    """
    invitation = (
        StoreInvitation.objects.select_for_update()
        .select_related("store")
        .filter(invite_token=payload.invite_token)
        .first()
    )

    # 1. 토큰 미존재 / 2. is_used (보안상 같은 코드)
    if invitation is None or invitation.is_used:
        raise BusinessException(
            "INVALID_TOKEN",
            "유효하지 않거나 이미 사용된 초대 토큰입니다.",
            status=404,
        )

    # 3. 만료
    if invitation.expires_at < timezone.now():
        raise BusinessException(
            "INVITATION_EXPIRED",
            "초대 유효 기간이 만료되었습니다. 관리자에게 재발송을 요청하세요.",
            status=410,
        )

    # 4. 이메일 mismatch — INVALID_TOKEN 으로 묶음 (토큰 존재성 노출 안 함)
    if invitation.invitee_email.lower() != user.email.lower():
        raise BusinessException(
            "INVALID_TOKEN",
            "유효하지 않거나 이미 사용된 초대 토큰입니다.",
            status=404,
        )

    store = invitation.store
    # 5. 매장 soft-delete 후 토큰 사용 시도 — store 존재성 노출 안 함
    if store.deleted_at is not None:
        raise BusinessException(
            "INVALID_TOKEN",
            "유효하지 않거나 이미 사용된 초대 토큰입니다.",
            status=404,
        )

    # 6. 이미 멤버 (다른 경로로 추가됐거나, 동일 user 가 이전에 다른 토큰으로 합류)
    if StoreMember.objects.filter(store=store, user=user).exists():
        raise BusinessException(
            "ALREADY_MEMBER",
            "이미 이 매장의 멤버입니다.",
            status=409,
        )

    # 7. 통과 — 합류 + 토큰 used 마킹
    member = StoreMember.objects.create(
        store=store,
        user=user,
        role=invitation.target_role,
    )
    invitation.is_used = True
    invitation.save(update_fields=["is_used"])

    return {
        "store_id": store.id,
        "store_name": store.name,
        "granted_role": member.role,
        "joined_at": member.created_at,
    }


def list_my_stores(user) -> list[dict]:
    memberships = (
        StoreMember.objects.filter(user=user, store__deleted_at__isnull=True)
        .select_related("store")
        .prefetch_related("store__images")
        .order_by("store__created_at")
    )

    results: list[dict] = []
    for membership in memberships:
        store = membership.store
        floorplan_url: str | None = None
        actual_url: str | None = None
        for image in store.images.all():
            if (
                image.image_type == StoreImage.ImageType.FLOORPLAN
                and floorplan_url is None
            ):
                floorplan_url = image.image_url.url if image.image_url else None
            elif (
                image.image_type == StoreImage.ImageType.ACTUAL_PHOTO
                and actual_url is None
            ):
                actual_url = image.image_url.url if image.image_url else None

        results.append(
            {
                "store_id": store.id,
                "name": store.name,
                "width": store.width,
                "height": store.height,
                "depth": store.depth,
                "my_role": membership.role,
                "floorplan_image_url": floorplan_url,
                "actual_photo_url": actual_url,
                "created_at": store.created_at,
            }
        )
    return results
