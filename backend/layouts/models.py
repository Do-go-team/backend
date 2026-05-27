from django.db import models

from common.models import SoftDeleteModel, TimeStampedModel
from fixtures.models import FixtureVersion
from stores.models import Store


class Layout(TimeStampedModel, SoftDeleteModel):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="layouts")
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=False)
    comment = models.CharField(max_length=1000, null=True, blank=True)
    floorplan_image_url = models.URLField(max_length=512, null=True, blank=True)

    class Meta:
        db_table = "layouts"

    def __str__(self):
        return self.name


class LayoutFixture(models.Model):
    layout = models.ForeignKey(
        Layout, on_delete=models.CASCADE, related_name="fixtures"
    )
    fixture_version = models.ForeignKey(
        FixtureVersion, on_delete=models.PROTECT, related_name="layout_placements"
    )
    world_pos_x = models.IntegerField()
    world_pos_y = models.IntegerField()
    world_pos_z = models.IntegerField()
    world_rot_y = models.SmallIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    depth = models.PositiveIntegerField()

    class Meta:
        db_table = "layout_fixtures"
