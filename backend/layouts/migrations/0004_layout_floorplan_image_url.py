"""Layout 에 floorplan_image_url 컬럼 추가.

레이아웃 단위 작업 도면 URL. POST /layouts/{id}/floorplan/parse 가 채움.
StoreImage(FLOORPLAN) 는 그대로 유지 — 매장 단위 도면과 별개 영역.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("layouts", "0003_layoutfixture_size"),
    ]

    operations = [
        migrations.AddField(
            model_name="layout",
            name="floorplan_image_url",
            field=models.URLField(blank=True, max_length=512, null=True),
        ),
    ]
