"""LayoutFixture 에 width/height/depth NOT NULL 컬럼 추가.

3-step:
  1) nullable 로 AddField (기존 row 에 NULL 채워짐)
  2) RunPython 으로 fixture_master 값 복사
  3) AlterField 로 NOT NULL 강제

기존 row 없는 환경에서도 안전 (RunPython iterate 가 빈 queryset 이면 no-op).
"""

from django.db import migrations, models


def fill_sizes_from_master(apps, schema_editor):
    LayoutFixture = apps.get_model("layouts", "LayoutFixture")
    rows = LayoutFixture.objects.select_related("fixture_version__fixture_master")
    for lf in rows:
        fm = lf.fixture_version.fixture_master
        lf.width = fm.width
        lf.height = fm.height
        lf.depth = fm.depth
        lf.save(update_fields=["width", "height", "depth"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("layouts", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="layoutfixture",
            name="width",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="layoutfixture",
            name="height",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="layoutfixture",
            name="depth",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.RunPython(fill_sizes_from_master, reverse_noop),
        migrations.AlterField(
            model_name="layoutfixture",
            name="width",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="layoutfixture",
            name="height",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="layoutfixture",
            name="depth",
            field=models.PositiveIntegerField(),
        ),
    ]
