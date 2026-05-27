from django.contrib import admin

from .models import ProductDetectionItem, ProductDetectionTask


@admin.register(ProductDetectionTask)
class ProductDetectionTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "store", "fixture", "requested_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("id", "source_image_key")


@admin.register(ProductDetectionItem)
class ProductDetectionItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task",
        "slot",
        "status",
        "asset_generation_status",
        "asset_generation_task",
        "asset_3d",
        "created_at",
    )
    list_filter = ("status", "asset_generation_status", "created_at")
    search_fields = ("id", "thumbnail_key")
