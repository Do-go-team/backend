import logging
import os
from datetime import timedelta
from pathlib import Path

import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

BASE_DIR = Path(__file__).resolve().parent.parent

# Load backend-local .env (DJANGO_*, CELERY_*, FRONTEND_URL).
# Container-injected env vars (POSTGRES_*, anything overridden by the platform)
# take precedence — load_dotenv does not override existing values.
load_dotenv(BASE_DIR / ".env")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default).strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-dev-only-do-not-use-in-prod"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY environment variable must be set when DJANGO_DEBUG is off."
        )

ALLOWED_HOSTS = _csv("DJANGO_ALLOWED_HOSTS") or (["*"] if DEBUG else [])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "corsheaders",
    "stores",
    "users",
    "auth.apps.AuthConfig",
    "products",
    "fixtures",
    "layouts",
    "assets_3d.apps.AssetsConfig",
    "ninja_jwt",
    "ninja_jwt.token_blacklist",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # CSP — REPORT_ONLY 모드 시작. 본격 enforce 전환은 별도 후속 티켓.
    "csp.middleware.CSPMiddleware",
    # PATCH/PUT multipart 처리 — Django default 가 PATCH 의 request.FILES 를 채우지
    # 않아 ninja 가 강제. PATCH /products/{id} (165, multipart) 가 의존.
    "ninja.compatibility.files.fix_request_files_middleware",
    # 4xx 응답을 Sentry 로 캡쳐. 가장 마지막에 둬 ninja exception_handler 가
    # 빚어낸 최종 응답(envelope JSON)을 본다. 5xx 는 DjangoIntegration 자동 캡쳐.
    "common.middleware.FourXxSentryMiddleware",
]

# 4xx Sentry 캡쳐 토글 — quota 폭주 시 환경변수로 즉시 비활성 (재배포 필요).
SENTRY_FOURXX_CAPTURE_ENABLED = (
    os.getenv("SENTRY_FOURXX_CAPTURE_ENABLED", "True").lower() == "true"
)

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.getenv("POSTGRES_DB", "dogo_db"),
        "USER": os.getenv("POSTGRES_USER", "dogo_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "dogo_pass"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"

# 161 등 응답 본문에 frontend 절대 URL 을 끼워 넣는 케이스에서 사용.
# CORS 설정과 별개로 명시적으로 노출 (env 미설정 시 빈 문자열 → 호출처에서 처리).
FRONTEND_URL = os.getenv("FRONTEND_URL", "")

_cors_origins = _csv("CORS_ALLOWED_ORIGINS")
if not _cors_origins:
    _cors_origins = [FRONTEND_URL] if FRONTEND_URL else []
CORS_ALLOWED_ORIGINS = _cors_origins

# Required for the browser to (a) send the HttpOnly refresh_token cookie on
# cross-origin XHR and (b) read authenticated responses from a different origin.
# Without this, `Access-Control-Allow-Credentials: true` is never emitted and
# the frontend's `credentials: "include"` fetches silently drop the cookie.
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = _csv("DJANGO_CSRF_TRUSTED_ORIGINS") or list(CORS_ALLOWED_ORIGINS)

# CSRF 쿠키 — CookieJWTAuth 가 mutating method 에서 ninja.utils.check_csrf() 로
# Django CsrfViewMiddleware 를 직접 호출. csrftoken 쿠키는 FE JS 가 읽어
# X-CSRFToken 헤더에 첨부해야 하므로 HTTPONLY 는 False (메커니즘 요구).
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"

# Content-Security-Policy — REPORT_ONLY 로 시작 (소프트 런칭).
# 차단 X, 위반만 보고. 1~2주 운영하며 위반 분석 → 정책 조정 → enforce 전환은 별도 티켓.
# 적용 대상: Django 가 직접 렌더링하는 페이지 (admin, Swagger UI). API JSON 응답은
# 브라우저가 코드로 실행 안 해서 사실상 무관. React 앱은 Vite 가 서빙하므로 별도.
CONTENT_SECURITY_POLICY_REPORT_ONLY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "script-src": ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net"],
        "style-src": ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net"],
        "img-src": ["'self'", "data:", "https:"],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
CELERY_TASK_DEFAULT_QUEUE = os.getenv("CELERY_TASK_DEFAULT_QUEUE", "celery")
CELERY_AI_QUEUE = os.getenv("CELERY_AI_QUEUE", "ai")
CELERY_TASK_ACKS_LATE = os.getenv("CELERY_TASK_ACKS_LATE", "False") == "True"
CELERY_TASK_REJECT_ON_WORKER_LOST = (
    os.getenv("CELERY_TASK_REJECT_ON_WORKER_LOST", "False") == "True"
)
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "600"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "540"))
AI_DETECTION_TASK_NAME = os.getenv(
    "AI_DETECTION_TASK_NAME", "ai_app.tasks.segment_product_image"
)

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
AI_CALLBACK_BASE_URL = os.getenv("AI_CALLBACK_BASE_URL", "").rstrip("/")
PRODUCT_DETECTION_MAX_ITEMS = int(os.getenv("PRODUCT_DETECTION_MAX_ITEMS", "50"))
AWS_PRESIGNED_EXPIRES_SECONDS = int(os.getenv("AWS_PRESIGNED_EXPIRES_SECONDS", "3600"))

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_STORAGE_BUCKET_NAME = (
    os.getenv("AWS_STORAGE_BUCKET_NAME", "")
    or os.getenv("S3_BUCKET_NAME", "")
    or os.getenv("AWS_S3_BUCKET", "")
)
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "") or os.getenv("AWS_REGION", "")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", "").strip() or None
AWS_S3_CUSTOM_DOMAIN = os.getenv("AWS_S3_CUSTOM_DOMAIN", "").strip() or None

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_CACHE_URL", "redis://redis:6379/2"),
    },
    # Access JWT 즉시 무효화용 deny-list. 별도 Redis DB 로 격리해 캐시·broker
    # 와 key 충돌 방지. TTL = 토큰 잔여 만료시간.
    "blacklist": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_BLACKLIST_URL", "redis://redis:6379/3"),
    },
}

EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@dogo.local")

EMAIL_VERIFICATION = {
    "CODE_TTL": int(os.getenv("EMAIL_CODE_TTL", "300")),
    "RESEND_COOLDOWN": int(os.getenv("EMAIL_RESEND_COOLDOWN", "60")),
    "DAILY_LIMIT": int(os.getenv("EMAIL_DAILY_LIMIT", "10")),
}

# 로그인 실패 rate limit (이메일별 fixed-window).
# 한도 초과 → 429 TOO_MANY_REQUESTS 명시 응답 (FE 가 대기 안내).
# 모든 실패 케이스 (미가입 / 비밀번호 / 비활성 / 미인증) 카운트, 성공 시 reset.
LOGIN_RATE_LIMIT = {
    "EMAIL_WINDOW": int(os.getenv("LOGIN_EMAIL_WINDOW", "300")),
    "EMAIL_MAX": int(os.getenv("LOGIN_EMAIL_MAX", "5")),
}

NINJA_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

if not DEBUG:
    # Trust the scheme reported by the upstream proxy (ALB/nginx) so Django
    # builds correct absolute URLs and enforces cookie security flags.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "0") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "elk": {
            # filebeat dissect 와 정확히 매칭 — 포맷 변경 시
            # infra/elk/filebeat/filebeat.yml 의 tokenizer 동시 수정 필요.
            "format": "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "elk",
        },
    },
    "root": {
        "handlers": ["stdout"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {"level": "WARNING", "propagate": True},
        "django.db.backends": {"level": "WARNING", "propagate": True},
        # gunicorn 로거를 Django LOGGING (elk formatter) 으로 흡수.
        # propagate=False — root 의 stdout 핸들러 중복 방지 (자체 handler 가 elk 포매팅).
        # 효과: gunicorn 의 워커 로그가 `[ts] [pid] [level] msg` 의 자체 형식이 아니라
        #       `[ts] [level] [name] msg` 의 elk 형식으로 통일 → filebeat dissect 단일 패턴으로 매칭.
        # 주의: gunicorn 마스터의 부팅 단계 로그 (Starting/Listening) 는 Django 로드 이전이라
        #       여전히 gunicorn 자체 형식 — 빈도 매우 낮음 (배포 시 5라인 정도) 이라 허용.
        "gunicorn.error": {"handlers": ["stdout"], "level": "INFO", "propagate": False},
        "gunicorn.access": {
            "handlers": ["stdout"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# ── Sentry ────────────────────────────────────────────────────────────────────
# DSN 미설정 시 SDK no-op — 로컬/CI 에서 init 호출이 무해. release 는 Jenkins
# 가 GIT_COMMIT 으로 주입. traces_sample_rate=0 (APM 비활성) 으로 error 만 추적.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(
                transaction_style="url",
                middleware_spans=False,
                signals_spans=False,
            ),
            LoggingIntegration(
                level=logging.INFO,  # breadcrumb 수집 level
                event_level=logging.ERROR,  # logger.error 이상 → Sentry event
            ),
        ],
        environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
        release=os.getenv("SENTRY_RELEASE") or None,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
        max_breadcrumbs=50,
    )
