from django.conf import settings
from django.db import models

from common.models import SoftDeleteModel, TimeStampedModel


class Store(TimeStampedModel, SoftDeleteModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_stores",
    )
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    max_admin_count = models.PositiveIntegerField(default=5)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    depth = models.PositiveIntegerField()

    class Meta:
        db_table = "stores"

    def __str__(self):
        return self.name


class StoreMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "OWNER"
        MANAGER = "MANAGER"
        VICE_MANAGER = "VICE_MANAGER"
        VMD = "VMD"
        STAFF = "STAFF"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="store_memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "store_members"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "user"], name="uq_store_members_store_user"
            )
        ]


class StoreImage(models.Model):
    class ImageType(models.TextChoices):
        FLOORPLAN = "FLOORPLAN"
        ACTUAL_PHOTO = "ACTUAL_PHOTO"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="images")
    image_type = models.CharField(max_length=20, choices=ImageType.choices)
    image_url = models.ImageField(upload_to="stores/images/", max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "store_images"


class StoreInvitation(models.Model):
    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, related_name="invitations"
    )
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_invitations",
    )
    invitee_email = models.EmailField(max_length=100)
    invite_token = models.CharField(max_length=255, unique=True)
    target_role = models.CharField(
        max_length=20,
        choices=StoreMember.Role.choices,
        default=StoreMember.Role.MANAGER,
    )
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "store_invitations"
        indexes = [models.Index(fields=["invitee_email"], name="idx_invitee_email")]
