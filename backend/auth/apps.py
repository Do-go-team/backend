from django.apps import AppConfig


class AuthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auth"
    # Override label to avoid collision with django.contrib.auth (label="auth").
    # Python import path stays `auth`; only Django's internal app label changes.
    label = "dogo_auth"
