from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models

from common.models import SoftDeleteModel, TimeStampedModel


class UserManager(BaseUserManager):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password is not None:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)
        extra_fields.setdefault("confirmed", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel, SoftDeleteModel):
    class Role(models.TextChoices):
        USER = "USER"
        ADMIN = "ADMIN"

    class OAuthProvider(models.TextChoices):
        GOOGLE = "google"

    email = models.EmailField(max_length=100, unique=True)
    name = models.CharField(max_length=20)
    oauth_provider = models.CharField(
        max_length=20, choices=OAuthProvider.choices, null=True, blank=True
    )
    oauth_uid = models.CharField(max_length=100, null=True, blank=True)
    confirmed = models.BooleanField(default=False)
    profile_image_url = models.ImageField(
        upload_to="users/profile/", max_length=512, null=True, blank=True
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.USER)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        db_table = "users"

    def __str__(self):
        return self.email
