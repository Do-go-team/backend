"""Asset3D file_format 에 PLY 추가 + model_url FileField → URLField 변경
+ asset_generation_tasks 신규 테이블.

배경:
- SAM 3D 결과 포맷이 .ply 라 file_format choices 에 PLY 추가.
- GPU Worker 가 외부에서 .ply 를 업로드하고 URL 문자열만 BE 가 저장하는 모델로
  바뀜 → FileField(upload_to=...) 가 더는 적합하지 않아 URLField 로 교체.
- AssetGenerationTask: GPU Worker polling 큐.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assets_3d", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="asset3d",
            name="file_format",
            field=models.CharField(
                choices=[
                    ("PLY", "Ply"),
                    ("GLB", "Glb"),
                    ("GLTF", "Gltf"),
                    ("OBJ", "Obj"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="asset3d",
            name="model_url",
            field=models.URLField(max_length=512),
        ),
        migrations.CreateModel(
            name="AssetGenerationTask",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "target_type",
                    models.CharField(
                        choices=[
                            ("PRODUCT", "Product"),
                            ("FIXTURE", "Fixture"),
                            ("STORE", "Store"),
                        ],
                        max_length=20,
                    ),
                ),
                ("target_id", models.BigIntegerField()),
                ("source_image_url", models.URLField(max_length=512)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                (
                    "worker_id",
                    models.CharField(blank=True, max_length=100, null=True),
                ),
                (
                    "result_url",
                    models.URLField(blank=True, max_length=512, null=True),
                ),
                ("error_message", models.TextField(blank=True, null=True)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "asset_3d",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="generation_tasks",
                        to="assets_3d.asset3d",
                    ),
                ),
            ],
            options={
                "db_table": "asset_generation_tasks",
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="idx_task_status_created",
                    ),
                    models.Index(
                        fields=["target_type", "target_id"],
                        name="idx_task_target",
                    ),
                    models.Index(
                        fields=["worker_id"],
                        name="idx_task_worker",
                    ),
                ],
            },
        ),
    ]
