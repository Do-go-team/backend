from django.db import IntegrityError, transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from assets_3d.models import Asset3D, AssetGenerationTask
from assets_3d.services import create_task as create_asset_task
from common.exceptions import BusinessException
from stores.services import get_member_store

from .models import ProductMaster, ProductVariant, StoreInventory, StoreProduct
from .schemas import (
    ProductCreateIn,
    ProductUpdateIn,
    ProductVariantUpdateIn,
)


SKU_DUPLICATED = BusinessException(
    "SKU_DUPLICATED",
    "이미 등록된 SKU 코드가 포함되어 있습니다.",
    status=409,
)
INVALID_VARIANT_ID = BusinessException(
    "INVALID_VARIANT_ID",
    "해당 상품에 속하지 않는 옵션 ID가 포함되어 있습니다.",
    status=422,
)
INVALID_IMAGE_INDEX = BusinessException(
    "INVALID_IMAGE_INDEX",
    "전송된 이미지 파일과 매칭되지 않는 image_index 가 포함되어 있습니다.",
    status=422,
)


def _visible_master_queryset(user) -> QuerySet[ProductMaster]:
    """현재 사용자에게 가시 가능한 master queryset.

    가시성 = master 가 store_products bridge 로 *명시적*으로 등록된 매장의 멤버.
    list (162) 와 detail (166) 양쪽이 같은 정의 공유 — leak 정책 일관성.

    필터:
      - master.alive() (deleted_at IS NULL)
      - store_products → store 의 멤버에 current user 포함
      - store.deleted_at IS NULL — 삭제된 매장의 매핑은 가시성 무효
    distinct() 는 한 master 가 여러 매장에 등록돼 있을 때 row 중복 방지.
    """
    return (
        ProductMaster.objects.alive()
        .filter(
            store_assignments__store__members__user=user,
            store_assignments__store__deleted_at__isnull=True,
        )
        .distinct()
    )


def _serialize_master(master: ProductMaster, asset_3d: dict | None) -> dict:
    """list 와 detail 의 응답 모양 일치화.

    응답 키:
      - master 9개: id/name/price/image_url/width/height/depth/asset_3d/variants
      - variant 5개: id/size/color/sku_code/barcode_image_url

    응답 키 잠금은 각 endpoint 의 단위 테스트가 set 비교로 enforce.
    """
    return {
        "id": master.id,
        "name": master.name,
        "price": master.price,
        "image_url": master.image_url,
        "width": master.width,
        "height": master.height,
        "depth": master.depth,
        "asset_3d": asset_3d,
        "variants": [
            {
                "id": v.id,
                "size": v.size,
                "color": v.color,
                "sku_code": v.sku_code,
                "barcode_image_url": (
                    v.barcode_image_url.url if v.barcode_image_url else None
                ),
            }
            for v in master.variants.all()
        ],
    }


def _latest_asset_3d_for_master(master_id: int) -> dict | None:
    """master 1건의 최신 Asset3D 1개 — 다중 등록 시 created_at desc 첫 번째."""
    asset = (
        Asset3D.objects.filter(
            target_type=Asset3D.TargetType.PRODUCT, target_id=master_id
        )
        .order_by("-created_at")
        .first()
    )
    if asset is None:
        return None
    return {
        "file_format": asset.file_format,
        # Asset3D.model_url 는 URLField (문자열) — 외부에서 업로드된 .ply 의 URL 만 보관.
        "model_url": asset.model_url or None,
    }


@transaction.atomic
def create_products(user, payload: ProductCreateIn) -> dict:
    """사진 촬영 시나리오 — AI 가 인식한 객체 N개를 master+variant 쌍으로 일괄 등록
    + 매장 단위 가시성 bridge(store_products) 채움.

    스코프:
      - 입력: store_id + AI 가 주는 image_url/width/height (3개) array
      - 출력: master_id + variant_id (FE 가 후속 placement endpoint 에서 사용)
      - fixture/version/placement/layout 책임 X — 별도 endpoint 가 처리
      - 3D 파일 생성 책임 X — create_3d_task=true 일 때만 assets_3d 큐에
        PENDING task 1건 예약 (실제 생성은 GPU Worker 가 polling).

    가시성 (162 와 정합):
      - store_products row 를 master 마다 1건씩 같은 트랜잭션에 INSERT.
      - 162 의 list 가 store_products → store → members__user 체인으로 필터링.
      - 사용자가 멤버 아닌 매장 store_id 보내면 STORE_NOT_FOUND 404 (ID 프로빙 차단).

    null 정책 (master.name/price/depth + variant.size/color/sku_code/barcode_image_url):
      - 모두 null 로 시작. 사용자가 추후 채움. sku_code 는 unique 유지 (PG NULL 다중 허용).

    트랜잭션:
      - master + variant + store_products 모두 한 transaction.atomic — 일관성 보장.
      - 3D task 도 *같은* atomic 안에서 INSERT — 상품 등록이 rollback 되면 task 도
        함께 사라짐 (요구사항: 트랜잭션 실패 시 task 생성 X). on_commit 대신
        atomic 내부 INSERT 를 택한 이유는 응답에 asset_3d_task_id 를 즉시 포함
        해야 하기 때문 (FE 가 polling URL 즉시 알 수 있어야 함).
      - 별도 endpoint(placement) 와의 일관성은 FE 책임 (orphan 가능 — 후속 ticket).
    """
    store, _ = get_member_store(user, payload.store_id)

    # create_3d_task 와 auto_create_3d_task 는 동의어 — 어느 쪽이라도 true 면 예약.
    enqueue_3d = bool(payload.create_3d_task or payload.auto_create_3d_task)

    items: list[dict] = []
    for entry in payload.products:
        master = ProductMaster.objects.create(
            user=user,
            image_url=entry.image_url,
            width=entry.width,
            height=entry.height,
        )
        variant = ProductVariant.objects.create(product_master=master)
        StoreProduct.objects.create(store=store, product_master=master)

        item = {
            "master_id": master.id,
            "variant_id": variant.id,
            "image_url": master.image_url,
            "width": master.width,
            "height": master.height,
            "created_at": master.created_at,
        }

        # source_image_url 이 비어 있으면 GPU Worker 가 다운로드할 게 없어 무의미 —
        # 그 경우는 task 자체를 안 만든다.
        if enqueue_3d and master.image_url:
            task = create_asset_task(
                target_type=AssetGenerationTask.TargetType.PRODUCT,
                target_id=master.id,
                source_image_url=master.image_url,
            )
            item["asset_3d_task_id"] = task.id

        items.append(item)

    return {"products": items}


def list_products(user) -> dict:
    """매장 단위 공유 카탈로그 조회 — store_products bridge 로 가시성 결정.

    가시성 규칙:
      - master 가 store_products row 를 통해 *명시적으로* 등록된 매장의 멤버에게만 노출.
      - 163 이 master 생성 시 store_products(store=요청 매장, master=신규) 같은 트랜잭션
        에 INSERT 하므로 정상 등록된 master 는 항상 한 매장에 묶여 있음.

    스펙 line 19 의 "내 소유" 표현은 레거시 — 실제 의도는 매장 단위 공유 카탈로그.
    PR 본문에 divergence 명시.

    bridge 채택의 의미:
      - 사용자가 매장 X+Y 양쪽 멤버여도 master 가 X 에만 등록되어 있으면 Y 컨텍스트엔 안 흘러감.
      - StoreMember chain 방식의 "bridge user leak" 해소. master 의 store 소속이
        store_products row 로 명확히 박힘.

    정렬: created_at desc (최신 사진 촬영 직후 위로 — UX).

    쿼리 계획 (master 개수와 무관하게 ~3 round-trips):
      1. ProductMaster join store_products → store → members (current user 매칭) + alive variants prefetch
      2. Asset3D 일괄 조회 (target_type=PRODUCT, target_id__in=master_ids)

    소프트 삭제:
      - master alive() 필터
      - store__deleted_at__isnull=True — 삭제된 매장의 store_products 는 가시성 무효
      - variant 도 alive() Prefetch
    """
    masters = list(
        _visible_master_queryset(user)
        .prefetch_related(Prefetch("variants", queryset=ProductVariant.objects.alive()))
        .order_by("-created_at")
    )

    # Asset3D 일괄 조회 — 같은 master 에 다중 시 created_at 최신 1개 채택.
    asset_map: dict[int, dict] = {}
    if masters:
        master_ids = [m.id for m in masters]
        for asset in Asset3D.objects.filter(
            target_type=Asset3D.TargetType.PRODUCT,
            target_id__in=master_ids,
        ).order_by("target_id", "-created_at"):
            if asset.target_id not in asset_map:
                asset_map[asset.target_id] = {
                    "file_format": asset.file_format,
                    "model_url": asset.model_url or None,
                }

    return {
        "products": [_serialize_master(m, asset_map.get(m.id)) for m in masters],
    }


def get_product_detail(user, product_id: int) -> dict:
    """상품 상세 조회 (master + variants + asset_3d).

    가시성 정책: 162 의 list 와 동일 — store_products bridge 통해 매장 단위 노출.
    비가시 케이스 (미존재 / 다른 매장 / 매장 삭제 / master 소프트 삭제) 모두
    PRODUCT_NOT_FOUND 404 로 통일 (ID 프로빙 차단, stores 의 STORE_NOT_FOUND 패턴).

    응답 키 (스펙):
      - master: product_id (alias of id) / name / price / image_url / width / height /
        depth / asset_3d / variants
      - variants[]: variant_id (alias of id) / size / color / sku_code / barcode_image_url
      → 직렬화는 _serialize_master 가 'id' 키로 만들고 호출 측에서 product_id/variant_id
        키로 변환. detail 만 alias 응답 (스펙 명시).

    쿼리 계획 (~3 round-trips):
      1. master + bridge 매칭 + alive variants prefetch
      2. Asset3D 단건 조회 (target=PRODUCT, master_id 1개)
    """
    master = (
        _visible_master_queryset(user)
        .filter(id=product_id)
        .prefetch_related(Prefetch("variants", queryset=ProductVariant.objects.alive()))
        .first()
    )
    if master is None:
        raise BusinessException(
            "PRODUCT_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 상품입니다.",
            status=404,
        )

    # 스펙은 detail 에서 product_id / variant_id alias 사용 (list 는 id 그대로) —
    # _serialize_master 재사용 X. 직렬화 명시적으로 분리.
    return {
        "product_id": master.id,
        "name": master.name,
        "price": master.price,
        "image_url": master.image_url,
        "width": master.width,
        "height": master.height,
        "depth": master.depth,
        "asset_3d": _latest_asset_3d_for_master(master.id),
        "variants": [
            {
                "variant_id": v.id,
                "size": v.size,
                "color": v.color,
                "sku_code": v.sku_code,
                "barcode_image_url": (
                    v.barcode_image_url.url if v.barcode_image_url else None
                ),
            }
            for v in master.variants.all()
        ],
    }


# ── 상품 수정 (메타 + variants bulk-sync) ─────────────────────────


def _validate_variant_rows(
    master: ProductMaster,
    items: list[ProductVariantUpdateIn],
    images_count: int,
) -> None:
    """DB 쓰기 전 사전 검증 — fail-fast.

    - cross-master id 차단: row.id 가 *이 master* 의 alive variant 가 아니면 INVALID_VARIANT_ID.
      soft-deleted variant 의 id 도 거부 (이미 삭제된 옵션 재참조 불가).
    - sku_code 중복:
        a) request 내부 중복 → SKU_DUPLICATED
        b) DB 의 *다른 master* 에 같은 sku_code → SKU_DUPLICATED
        c) 같은 master 내 다른 alive variant 에 같은 sku_code → SKU_DUPLICATED
           (단, 본인 row 자체는 자기 sku_code 와 비교 안 함 — id 로 자기 자신 제외)
    - image_index 범위: 0 ≤ idx < len(images). 범위 밖이면 INVALID_IMAGE_INDEX.
        같은 image 가 여러 variant 에 매핑되는 건 허용 (FE 의도일 수 있음).

    검증 통과 후 호출 측이 _sync_variants 진입.
    """
    if not items:
        return

    existing_alive_ids = set(
        ProductVariant.objects.alive()
        .filter(product_master=master)
        .values_list("id", flat=True)
    )
    for row in items:
        if row.id is not None and row.id not in existing_alive_ids:
            raise INVALID_VARIANT_ID
        if row.image_index is not None and row.image_index >= images_count:
            raise INVALID_IMAGE_INDEX

    # sku_code 검증 — None 인 row 는 충돌 없음 (PG NULL 다중 허용)
    new_skus = [r.sku_code for r in items if r.sku_code is not None]
    if len(set(new_skus)) != len(new_skus):
        raise SKU_DUPLICATED

    for row in items:
        if row.sku_code is None:
            continue
        # 다른 master 의 같은 sku
        if (
            ProductVariant.objects.alive()
            .filter(sku_code=row.sku_code)
            .exclude(product_master=master)
            .exists()
        ):
            raise SKU_DUPLICATED
        # 같은 master 내 다른 alive variant 의 같은 sku (자기 row 는 제외)
        same_master_qs = ProductVariant.objects.alive().filter(
            product_master=master, sku_code=row.sku_code
        )
        if row.id is not None:
            same_master_qs = same_master_qs.exclude(id=row.id)
        if same_master_qs.exists():
            raise SKU_DUPLICATED


def _sync_variants(
    master: ProductMaster,
    items: list[ProductVariantUpdateIn],
    images: list,
) -> tuple[int, int, list[ProductVariant]]:
    """3-rule bulk sync. _validate_variant_rows 통과한 items 만 들어옴.

    - id 있음 → UPDATE (보낸 필드만 — `model_dump(exclude_unset=True)` 로 키 부재와
      명시적 None 구분)
    - id 없음 → INSERT (신규 variant)
    - alive 중 items 에 누락된 id → SOFT DELETE (deleted_at 세팅)

    image 처리:
      - row.image_index 가 None/누락 → image 변경 X (UPDATE 시 기존 유지) 또는
        image 없음 (INSERT 시).
      - row.image_index 값 있음 → images[idx] 를 ImageField 에 저장 (UPDATE 시
        기존 image 덮어쓰기). bulk_create 우회 — ImageField storage upload 가
        row 별로 .save() 필요해서.

    Returns (synced_count, deleted_count, synced_variants).
    synced_variants 는 INSERT/UPDATE 한 instance 들 (응답용).

    트랜잭션 한계 (PR 본문 명시):
      - @transaction.atomic 으로 묶이지만 ImageField 의 storage write 는 파일시스템
        대상이라 rollback 안 됨. 중간 IntegrityError 발생 시 이미 저장된 image
        파일이 disk 에 orphan 으로 남을 수 있음. 후속 cleanup 정책은 #10
        (protected media) ticket 과 함께 처리.
    """
    existing_alive_ids = set(
        ProductVariant.objects.alive()
        .filter(product_master=master)
        .values_list("id", flat=True)
    )

    request_ids: set[int] = set()
    inserted = 0
    updated = 0
    synced: list[ProductVariant] = []

    try:
        for row in items:
            if row.id is None:
                # INSERT — image 있으면 ImageField 처리 위해 instance + .save()
                variant = ProductVariant(
                    product_master=master,
                    size=row.size,
                    color=row.color,
                    sku_code=row.sku_code,
                )
                if row.image_index is not None:
                    variant.barcode_image_url = images[row.image_index]
                variant.save()
                inserted += 1
                synced.append(variant)
            else:
                # UPDATE — 보낸 필드만. exclude_unset 으로 키 부재 vs 명시적 null 구분.
                # image_index 있으면 ImageField 도 같이 저장.
                sent = row.model_dump(exclude_unset=True)
                update_keys = [k for k in ("size", "color", "sku_code") if k in sent]
                has_image_change = row.image_index is not None
                if update_keys or has_image_change:
                    variant = ProductVariant.objects.get(id=row.id)
                    for k in update_keys:
                        setattr(variant, k, sent[k])
                    if has_image_change:
                        variant.barcode_image_url = images[row.image_index]
                    variant.save()
                    synced.append(variant)
                else:
                    # 아무 필드도 안 바뀐 row — DB 안 건드림. 응답엔 현재 상태 그대로.
                    synced.append(ProductVariant.objects.get(id=row.id))
                updated += 1
                request_ids.add(row.id)
    except IntegrityError as exc:
        # 사전 검증 통과했어도 race 시 DB unique 제약 위반 가능 — 동일 envelope 으로 응답
        raise SKU_DUPLICATED from exc

    to_delete_ids = existing_alive_ids - request_ids
    deleted_count = 0
    if to_delete_ids:
        deleted_count = ProductVariant.objects.filter(id__in=to_delete_ids).update(
            deleted_at=timezone.now()
        )

    return inserted + updated, deleted_count, synced


@transaction.atomic
def update_product(
    user,
    product_id: int,
    payload: ProductUpdateIn,
    images: list,
) -> dict:
    """master 메타 부분 수정 + variants bulk sync (multipart, image 포함).

    167 endpoint(POST /products/{id}/variants) 의 책임을 흡수 — 카메라 스캔 흐름
    (sku/size/color + barcode image 일괄 등록) 도 본 endpoint 한 번으로 처리.

    권한: 매장 멤버 누구나 (162/166 가시성과 일관). master 가 store_products bridge
    통해 보이는 매장의 멤버라면 STAFF 도 수정 가능.

    PATCH 의미론:
      - exclude_unset 으로 키 자체 부재(메타 미수정) 와 명시적 None 구분.
      - variants 키 자체 부재 (None) → 메타만 수정. variants_count 응답 X.
      - variants=[] → 모든 alive variant SOFT DELETE.
      - variants=[{...}] → 3-rule bulk sync (UPDATE/INSERT/SOFT DELETE).
      - variants[i].image_index → multipart images[idx] 에 매칭, ImageField 에 저장.

    검증 → DB 쓰기 분리 (_validate_variant_rows 사전 통과 후 _sync_variants 진입):
      - cross-master id 차단 → INVALID_VARIANT_ID 422
      - sku 중복 (request 내부 + DB 다른 master + DB 같은 master 다른 row) → SKU_DUPLICATED 409
      - image_index 범위 밖 → INVALID_IMAGE_INDEX 422
      - 한 row 라도 invalid 면 어떤 변경도 일어나지 않음 (transaction.atomic + fail-fast)

    응답:
      - synced_variants_count = INSERT + UPDATE 합산
      - deleted_variants_count = SOFT DELETE 개수
      - updated_at = 항상 현재 (메타 변경 없고 variants 만 수정해도 bump)
      - variants = INSERT/UPDATE 한 variant 의 현재 상태 (barcode_image_url 포함).
    """
    master = _visible_master_queryset(user).filter(id=product_id).first()
    if master is None:
        raise BusinessException(
            "PRODUCT_NOT_FOUND",
            "존재하지 않거나 접근 권한이 없는 상품입니다.",
            status=404,
        )

    changes = payload.model_dump(exclude_unset=True)
    variants_in_request = "variants" in changes and payload.variants is not None
    items = payload.variants if variants_in_request else []

    # 1. 사전 검증
    if variants_in_request:
        _validate_variant_rows(master, items, len(images))

    # 2. 메타 부분 수정
    meta_keys = [
        k
        for k in ("name", "price", "image_url", "width", "height", "depth")
        if k in changes
    ]
    if meta_keys:
        for key in meta_keys:
            setattr(master, key, changes[key])
        master.save(update_fields=meta_keys + ["updated_at"])

    # 3. variants 동기화
    synced_count = 0
    deleted_count = 0
    synced_variants: list[ProductVariant] = []
    if variants_in_request:
        synced_count, deleted_count, synced_variants = _sync_variants(
            master,
            items,
            images,
        )
        # 메타 변경 없이 variants 만 수정한 경우에도 updated_at 명시 bump
        if not meta_keys:
            master.save(update_fields=["updated_at"])

    return {
        "product_id": master.id,
        "name": master.name,
        "synced_variants_count": synced_count,
        "deleted_variants_count": deleted_count,
        "updated_at": master.updated_at,
        "variants": [
            {
                "variant_id": v.id,
                "size": v.size,
                "color": v.color,
                "sku_code": v.sku_code,
                "barcode_image_url": (
                    v.barcode_image_url.url if v.barcode_image_url else None
                ),
            }
            for v in synced_variants
        ],
    }


# ── 상품 삭제 (4단계 트랜잭션) ────────────────────────────────────


@transaction.atomic
def delete_product(user, product_id: int) -> dict:
    """상품 마스터 삭제 — 스펙 line 19~25 의 4단계 트랜잭션.

    소프트 vs 하드 정책 (스펙 의도):
      - master + variants → SOFT DELETE: 과거 fixture_version_products 의 진열 기록이
        깨지지 않도록 row 보존. 미래 복원 가능성 고려.
      - store_products + store_inventories → HARD DELETE: 매장 매핑/재고는 *재할당
        방지* 의도라 영구 삭제. master 가 복원돼도 매장에 새로 등록해야 함.

    SQL CASCADE 가 soft delete 에 작동하지 않으므로 4단계를 명시적으로 트랜잭션에 묶음.

    권한: 162/166/165 와 동일 — 매장 멤버 누구나 (STAFF 포함). 비가시 master 는
    PRODUCT_NOT_FOUND 404 (ID 프로빙 차단). 스펙 line 79 의 ACCESS_DENIED 는 안 씀
    (카탈로그 공유 모델 일관 — divergence 는 PR 본문 명시).

    fixture_version_products (PROTECT FK):
      - variant 가 soft delete 돼도 row 자체는 살아있음 → PROTECT 위반 X.
      - 단, 그 후 fixture detail 응답에서 alive() 필터로 사라짐 (스펙 의도와 정합).
    """
    master = _visible_master_queryset(user).filter(id=product_id).first()
    if master is None:
        raise BusinessException(
            "PRODUCT_NOT_FOUND",
            "존재하지 않거나 이미 삭제된 상품입니다.",
            status=404,
        )

    now = timezone.now()
    # 1) variants soft delete — alive 만 카운트 (이미 deleted 인 건 응답에서 제외)
    deleted_variants_count = (
        ProductVariant.objects.alive()
        .filter(product_master=master)
        .update(deleted_at=now)
    )

    # 2) store_inventories hard delete — variant FK 통해 master 매칭
    StoreInventory.objects.filter(variant__product_master=master).delete()

    # 3) store_products hard delete (매장 매핑 영구 정리)
    StoreProduct.objects.filter(product_master=master).delete()

    # 4) master soft delete 마지막 — 이전 단계가 master 참조 가능해야 하므로
    master.deleted_at = now
    master.save(update_fields=["deleted_at"])

    return {
        "deleted_product_id": master.id,
        "deleted_variants_count": deleted_variants_count,
    }
