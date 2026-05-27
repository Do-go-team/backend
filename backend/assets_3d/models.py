from django.db import models

from common.models import TimeStampedModel


class Asset3D(TimeStampedModel):
    class TargetType(models.TextChoices):
        PRODUCT = "PRODUCT"
        FIXTURE = "FIXTURE"
        STORE = "STORE"
        DETECTION_ITEM = "DETECTION_ITEM"

    class FileFormat(models.TextChoices):
        # SAM 3D 결과가 .ply (Gaussian Splat) — PLY 가 1순위 포맷.
        # GLB/GLTF/OBJ 는 향후 변환/대안 포맷 대비.
        PLY = "PLY"
        GLB = "GLB"
        GLTF = "GLTF"
        OBJ = "OBJ"

    target_type = models.CharField(max_length=20, choices=TargetType.choices)
    target_id = models.BigIntegerField()
    file_format = models.CharField(max_length=10, choices=FileFormat.choices)
    # GPU Worker 가 업로드한 .ply 의 최종 접근 URL (예: "/media/assets_3d/xxx.ply").
    # 로컬 dev 는 MEDIA_URL 상대경로, 운영은 S3/CDN 절대 URL — URL 문자열만 저장.
    model_url = models.URLField(max_length=512)
    file_size_bytes = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "assets_3d"
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="idx_asset_target"),
        ]

    def __str__(self):
        return f"{self.target_type}:{self.target_id} ({self.file_format})"


class AssetGenerationTask(TimeStampedModel):
    """GPU Worker 가 polling 으로 가져가서 .ply 를 생성하는 비동기 작업 큐.

    상태 머신:
      PENDING     → POST /assets/3d-tasks 에서 생성 (target/source_image_url 만 채워짐)
      PROCESSING  → POST /assets/3d-tasks/claim 으로 worker 가 lock 하면서 전이.
                    select_for_update(skip_locked=True) 로 중복 claim 방지.
      COMPLETED   → POST /assets/3d-tasks/{id}/complete (worker 가 .ply 업로드).
                    Asset3D row 와 1:1 묶임 (asset_3d FK).
      FAILED      → POST /assets/3d-tasks/{id}/fail (worker 보고).

    asset_3d FK 는 SET_NULL — Asset3D 가 지워져도 task 기록(감사 로그)은 보존.
    """

    class TargetType(models.TextChoices):
        PRODUCT = "PRODUCT"
        FIXTURE = "FIXTURE"
        STORE = "STORE"
        DETECTION_ITEM = "DETECTION_ITEM"

    class Status(models.TextChoices):
        PENDING = "PENDING"
        PROCESSING = "PROCESSING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    target_type = models.CharField(max_length=20, choices=TargetType.choices)
    target_id = models.BigIntegerField()
    source_image_url = models.URLField(max_length=512)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    worker_id = models.CharField(max_length=100, null=True, blank=True)
    asset_3d = models.ForeignKey(
        Asset3D,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generation_tasks",
    )
    result_url = models.URLField(max_length=512, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "asset_generation_tasks"
        indexes = [
            # claim API 가 status=PENDING + created_at asc 로 fetch — 복합 인덱스.
            models.Index(
                fields=["status", "created_at"], name="idx_task_status_created"
            ),
            models.Index(fields=["target_type", "target_id"], name="idx_task_target"),
            models.Index(fields=["worker_id"], name="idx_task_worker"),
        ]

    def __str__(self):
        return f"Task#{self.pk} {self.target_type}:{self.target_id} [{self.status}]"
