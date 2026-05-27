# =============================================================================
#  VMD Backend Dockerfile
#  Django 5.1 + django-ninja 1.3 + Python 3.12 (WSGI / Gunicorn sync workers)
#
#  설계 근거: infra/.ai/plan/dockerfile_plan.md §3 (BE 통합 시 ASGI→WSGI 회귀)
#
#  의도:
#    - 장시간 실행되는 WSGI 앱 (Channels/WebSocket 미사용; 도입 시 ASGI 전환).
#    - Blue/Green 두 색상이 동일 이미지를 공유 (이미지 태그만 다름).
#    - django-blue / django-green / celery-worker 모두 같은 이미지를 사용,
#      compose 의 command 만 다르게 override (entrypoint 의 "$@" 분기 활용).
#
#  핵심 원칙:
#    1) Multi-stage (deps-builder → runtime) 로 런타임 이미지 슬림화
#    2) requirements 를 소스보다 먼저 COPY → pip 레이어 캐시 활용
#    3) Non-root 실행 (vmd_user, UID/GID 10001)
#    4) tini 로 PID 1 → SIGTERM 정확한 graceful shutdown
#    5) collectstatic 은 entrypoint 에서 런타임 수행
#       (settings.py 가 import 시점에 DJANGO_SECRET_KEY 를 강제하므로
#        빌드 시점 collectstatic 은 불가; entrypoint.sh 가 boot 마다 실행)
# =============================================================================
# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG APP_UID=10001
ARG APP_GID=10001

# -----------------------------------------------------------------------------
# Stage 1 — dependency builder (wheel 사전 컴파일)
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 빌드 전용 시스템 패키지 (runtime 에는 미포함 → 공격 표면 ↓)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# requirements 파일만 먼저 복사 (소스 변경과 무관하게 캐시 적중)
# 단일 requirements.txt — dev/prod 의존성 차이가 생기는 시점에 분할 검토
COPY requirements.txt .

# wheel 사전 컴파일 → runtime 단계에서 오프라인 설치
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel \
        --wheel-dir=/wheels \
        -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2 — runtime (slim)
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_UID
ARG APP_GID

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DJANGO_SETTINGS_MODULE=config.settings \
    PORT=8000 \
    PATH="/home/vmd_user/.local/bin:${PATH}"

# 런타임 시스템 의존성만 (빌드 도구 없음)
#   - libpq5            : PostgreSQL 클라이언트 라이브러리 (psycopg 의존)
#   - curl              : HEALTHCHECK 용
#   - tini              : PID 1 신호 처리 (graceful shutdown)
#   - ca-certificates   : TLS (S3 / Sentry / OAuth)
#   - netcat-openbsd    : entrypoint.sh 의 Postgres 대기 (nc -z)
#   - GeoDjango 런타임  : binutils libproj-dev gdal-bin libgdal-dev python3-gdal
#                        (settings.py 의 INSTALLED_APPS 에 django.contrib.gis 등록,
#                         DB 엔진 = postgis. libgdal.so 가 ctypes 로 필요)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
        ca-certificates \
        netcat-openbsd \
        binutils \
        libproj-dev \
        gdal-bin \
        libgdal-dev \
        python3-gdal \
        fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# Non-root 사용자 생성 (UID/GID 고정 → 호스트 bind mount 권한 일관성)
RUN groupadd --system --gid ${APP_GID} vmd_user \
 && useradd  --system --uid ${APP_UID} --gid ${APP_GID} \
             --home-dir /home/vmd_user --create-home \
             --shell /usr/sbin/nologin vmd_user

WORKDIR /app

# Stage 1 에서 만든 wheel 만 가져와 오프라인 설치 (네트워크 미사용 → 재현 가능)
COPY --from=deps-builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# 애플리케이션 소스 복사 (캐시 무효화 지점 = 가장 마지막)
COPY --chown=${APP_UID}:${APP_GID} . /app/

# 디렉토리만 미리 생성 + entrypoint 실행 권한 / 줄바꿈 정규화
# (collectstatic 은 entrypoint.sh 가 boot 시점에 수행)
RUN mkdir -p /app/staticfiles /app/run \
 && sed -i 's/\r$//' /app/entrypoint.sh \
 && chmod +x /app/entrypoint.sh \
 && chown -R ${APP_UID}:${APP_GID} /app/staticfiles /app/run /app

USER vmd_user

EXPOSE 8000

# 헬스체크: Django 측 /healthz 미구현 → 임시로 /api/v1/hello 사용
# TODO: BE 가 /healthz (DB+Redis ping 포함) 추가하면 그쪽으로 교체
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/v1/hello -o /dev/null || exit 1

# tini → entrypoint.sh (postgres 대기 → 옵션 migrate → collectstatic → exec gunicorn)
# CMD 미지정: entrypoint 가 인자 없으면 gunicorn 을 기동, 인자 있으면 exec "$@" 로 위임
# (compose 의 command: 로 celery worker 등 다른 명령을 주입할 때 활용)
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]