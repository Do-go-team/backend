# =============================================================================
#  VMD AI Worker Dockerfile
#  YOLOv11-seg + Celery + PyTorch (CPU) + OpenCV + Python 3.12
#
#  설계 근거: infra/.ai/plan/dockerfile_plan.md §4
#
#  의도:
#    - Celery worker (-Q ai) 로 무거운 YOLO 추론을 격리 처리.
#    - Django API 와 자원/메모리 격리 → API 응답 보호.
#
#  중요:
#    1) 모델 가중치 파일은 이미지에 굽지 않는다.
#       호스트 /opt/vmd/models/best.pt 를 컨테이너 /models 에
#       read-only bind mount 하여 사용. (마스터플랜 §2.3)
#    2) Phase 2 (ECS/EKS/Auto Scaling) 진입 시 본 패턴은 반드시
#       "S3 entrypoint 다운로드 + 로컬 캐시" 로 마이그레이션해야 한다.
#       관련 코드 자리는 entrypoint.sh 에 주석으로 미리 박혀있다.
#       (vmd_infra_plan.md §2.3 ⚠️ 박스 참조)
#
#  핵심 원칙:
#    - Multi-stage (deps-builder → runtime)
#    - Torch CPU wheel 만 사용 (CUDA 미포함 → 이미지 ~3GB 절감)
#    - libgl1 / libglib2.0-0 등 OpenCV 런타임 시스템 라이브러리 설치
#    - Non-root (vmd_user, UID/GID 10001 — backend 와 통일)
#    - tini 로 PID 1
#    - 모델 warmup 후 ready flag 작성 → 헬스체크 신호
# =============================================================================
# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG APP_UID=10001
ARG APP_GID=10001
# Torch CPU 전용 인덱스 (CUDA 빌드 회피)
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# -----------------------------------------------------------------------------
# Stage 1 — dependency builder
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 빌드 전용 시스템 패키지 (runtime 에는 미포함)
#   - git: ultralytics 가 일부 의존성을 git+https 로 가져올 수 있어 보험
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# requirements 만 먼저 복사 → pip 캐시 적중
COPY requirements.txt ./

# Torch CPU wheel 은 별도 인덱스에서 받아야 함 (--extra-index-url)
ARG TORCH_INDEX_URL
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel \
        --wheel-dir=/wheels \
        --extra-index-url ${TORCH_INDEX_URL} \
        -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_UID
ARG APP_GID

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PATH="/home/vmd_user/.local/bin:${PATH}" \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    YOLO_MODEL_PATH=/models/best.pt \
    YOLO_READY_FLAG_PATH=/tmp/worker_ready \
    YOLO_DEVICE=cpu

# OpenCV / Pillow / 일반 런타임 시스템 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libgomp1 \
        tini \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root 사용자 (backend 와 동일 UID/GID 10001 → 호스트 권한 일관성)
RUN groupadd --system --gid ${APP_GID} vmd_user \
 && useradd  --system --uid ${APP_UID} --gid ${APP_GID} \
             --home-dir /home/vmd_user --create-home \
             --shell /usr/sbin/nologin vmd_user

WORKDIR /app

# 의존성 오프라인 설치 (Stage 1 의 wheel 만 사용)
COPY --from=deps-builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# 애플리케이션 소스 복사 (entrypoint.sh, ai_app/ 등 포함)
COPY --chown=${APP_UID}:${APP_GID} . /app/

# 마운트 지점 + Phase 2 캐시 디렉토리 사전 생성
RUN mkdir -p /models /var/cache/vmd-models /tmp /app/thumbnails \
 && chown -R ${APP_UID}:${APP_GID} /var/cache/vmd-models /tmp /app \
 && chmod +x /app/entrypoint.sh

USER vmd_user

# 헬스체크:
#   1) entrypoint 가 만들어 놓은 ready flag 파일 존재
#   2) Celery worker 가 inspect ping 에 응답
# start_period=90s : 모델 warmup (수십 초) 흡수
HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=5 \
    CMD test -f "${YOLO_READY_FLAG_PATH}" \
     && celery -A ai_app inspect ping -d "celery@${HOSTNAME}" >/dev/null 2>&1 \
     || exit 1

# entrypoint.sh 가:
#   - 모델 파일 존재 검증 (없으면 fail-fast)
#   - 모델 로드 + warmup
#   - ready flag 작성
#   - exec "$@"  → CMD 인자(celery worker)로 교체
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]

# 기본 명령: AI 전용 큐를 처리하는 Celery worker
#   - -Q ai                       : Django 의 default 큐와 분리
#   - -c 2                        : 동시 실행 워커 (CPU 4코어 환경에서 OOM 방지)
#   - --prefetch-multiplier=1     : 무거운 작업이라 prefetch 최소화
#   - -Ofair                      : 균등 분배
#   - --max-tasks-per-child=20    : 메모리 누수 방어 (PyTorch가 가끔 누수)
CMD ["celery", "-A", "ai_app", "worker", \
     "-Q", "ai", \
     "-c", "2", \
     "--prefetch-multiplier=1", \
     "-Ofair", \
     "--max-tasks-per-child=20"]