from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from common.models import SoftDeleteModel, TimeStampedModel
from stores.models import Store


class ProductMaster(TimeStampedModel, SoftDeleteModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="products",
    )
    # AI 가 사진에서 추출 가능한 값만 not-null. name/price/depth 는 사진만으론
    # 알 수 없어 사용자가 추후 채울 placeholder 로 두고 nullable.
    name = models.CharField(max_length=100, null=True, blank=True)
    price = models.PositiveIntegerField(null=True, blank=True)
    image_url = models.URLField(max_length=512, null=True, blank=True)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    depth = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "product_masters"

    def __str__(self):
        return self.name


class ProductVariant(TimeStampedModel, SoftDeleteModel):
    product_master = models.ForeignKey(
        ProductMaster, on_delete=models.CASCADE, related_name="variants"
    )
    # 사용자가 추후 채울 옵션 정보 — 사진 등록 시점엔 모두 null 로 시작.
    # sku_code unique 정책 (partial): *살아있는* variant 끼리만 unique 강제.
    # → 사용자가 실수로 삭제 후 같은 sku 로 재등록 가능 (soft-deleted variant 의
    #   sku 는 unique 검사 대상 X). PG NULL 다중 허용도 partial 가 자연스럽게 처리.
    size = models.CharField(max_length=20, null=True, blank=True)
    color = models.CharField(max_length=30, null=True, blank=True)
    sku_code = models.CharField(max_length=50, null=True, blank=True)
    barcode_image_url = models.ImageField(
        upload_to="products/barcode/", max_length=512, null=True, blank=True
    )

    class Meta:
        db_table = "product_variants"
        constraints = [
            models.UniqueConstraint(
                fields=["sku_code"],
                condition=Q(deleted_at__isnull=True),
                name="uq_alive_variant_sku",
            ),
        ]


class ProductDetectionTask(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING"
        PROCESSING = "PROCESSING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    store = models.ForeignKey(
        Store,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_detection_tasks",
    )
    fixture = models.ForeignKey(
        "fixtures.FixtureMaster",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_detection_tasks",
    )
    fixture_version = models.ForeignKey(
        "fixtures.FixtureVersion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_detection_tasks",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_product_detection_tasks",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(null=True, blank=True)
    source_image_key = models.CharField(max_length=512, null=True, blank=True)
    # TODO: Generate a secure callback token when creating detection tasks.
    callback_token = models.CharField(max_length=128)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "product_detection_tasks"
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="idx_det_task_status_created",
            ),
            models.Index(
                fields=["store", "created_at"],
                name="idx_det_task_store_created",
            ),
            models.Index(
                fields=["requested_by", "created_at"],
                name="idx_det_task_user_created",
            ),
            models.Index(
                fields=["fixture", "created_at"],
                name="idx_det_task_fixture_created",
            ),
        ]

    def __str__(self):
        return f"DetectionTask#{self.pk} [{self.status}]"


class ProductDetectionItem(TimeStampedModel):
    class Status(models.TextChoices):
        DETECTED = "DETECTED"
        SELECTED = "SELECTED"
        REJECTED = "REJECTED"
        REGISTERED = "REGISTERED"

    class AssetGenerationStatus(models.TextChoices):
        NOT_REQUESTED = "NOT_REQUESTED"
        PENDING = "PENDING"
        PROCESSING = "PROCESSING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    task = models.ForeignKey(
        ProductDetectionTask,
        on_delete=models.CASCADE,
        related_name="items",
    )
    slot = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(49)]
    )
    thumbnail_key = models.CharField(max_length=512)
    relative_position_x = models.DecimalField(max_digits=8, decimal_places=6)
    relative_position_y = models.DecimalField(max_digits=8, decimal_places=6)
    relative_size_width = models.DecimalField(max_digits=8, decimal_places=6)
    relative_size_height = models.DecimalField(max_digits=8, decimal_places=6)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DETECTED,
    )
    asset_generation_status = models.CharField(
        max_length=20,
        choices=AssetGenerationStatus.choices,
        default=AssetGenerationStatus.NOT_REQUESTED,
    )
    # TODO: Enforce one active generation task per item in service logic.
    asset_generation_task = models.ForeignKey(
        "assets_3d.AssetGenerationTask",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detection_items",
    )
    asset_3d = models.ForeignKey(
        "assets_3d.Asset3D",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detection_items",
    )
    asset_error_message = models.TextField(null=True, blank=True)
    product_master = models.ForeignKey(
        ProductMaster,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detection_items",
    )
    variant = models.ForeignKey(
        ProductVariant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="detection_items",
    )
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    # TODO: Validate bbox schema (list length=4, numeric) in schema/service layer.
    bbox_xyxy = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "product_detection_items"
        constraints = [
            models.UniqueConstraint(
                fields=["task", "slot"],
                name="uniq_detection_item_task_slot",
            )
        ]
        indexes = [
            models.Index(
                fields=["task", "status"],
                name="idx_detection_item_task_status",
            ),
            models.Index(
                fields=["task", "asset_generation_status"],
                name="idx_det_item_task_asset_stat",
            ),
            models.Index(
                fields=["asset_generation_task"],
                name="idx_detection_item_asset_task",
            ),
            models.Index(
                fields=["asset_3d"],
                name="idx_detection_item_asset_3d",
            ),
        ]

    def __str__(self):
        return f"DetectionItem task={self.task_id} slot={self.slot} [{self.status}]"


class StoreProduct(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        PAUSED = "PAUSED"

    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="handled_products"
    )
    product_master = models.ForeignKey(
        ProductMaster, on_delete=models.CASCADE, related_name="store_assignments"
    )
    status = models.CharField(
        max_length=50, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        db_table = "store_products"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "product_master"], name="uq_store_products"
            )
        ]


class StoreInventory(TimeStampedModel):
    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="inventories"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="inventories"
    )
    stock_quantity = models.IntegerField(default=0)
    safety_stock = models.IntegerField(default=0)

    class Meta:
        db_table = "store_inventories"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "variant"], name="uq_store_variant_inventory"
            )
        ]
