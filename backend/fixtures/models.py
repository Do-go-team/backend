from django.conf import settings
from django.db import models

from common.models import SoftDeleteModel, TimeStampedModel
from products.models import ProductVariant


class FixtureMaster(TimeStampedModel, SoftDeleteModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fixtures",
    )
    name = models.CharField(max_length=100)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    depth = models.PositiveIntegerField()

    class Meta:
        db_table = "fixture_masters"

    def __str__(self):
        return self.name


class FixtureVersion(TimeStampedModel, SoftDeleteModel):
    fixture_master = models.ForeignKey(
        FixtureMaster, on_delete=models.CASCADE, related_name="versions"
    )
    version_name = models.CharField(max_length=100)

    class Meta:
        db_table = "fixture_versions"


class FixtureVersionProduct(models.Model):
    class Status(models.TextChoices):
        DISPLAY = "DISPLAY"

    fixture_version = models.ForeignKey(
        FixtureVersion, on_delete=models.CASCADE, related_name="placements"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="fixture_placements"
    )
    local_pos_x = models.IntegerField()
    local_pos_y = models.IntegerField()
    local_pos_z = models.IntegerField()
    memo = models.CharField(max_length=500, null=True, blank=True)
    status = models.CharField(
        max_length=50, choices=Status.choices, default=Status.DISPLAY
    )

    class Meta:
        db_table = "fixture_version_products"
