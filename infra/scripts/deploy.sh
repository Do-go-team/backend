#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — Blue/Green deploy orchestrator (host-side, manual or Jenkins)
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §5.4 / §5.4.1 (Stages 2–10)
#             operations_activation_plan.md §1 (Option A)
#
#  배포 모델:
#    Option A — Jenkins/호스트가 직접 docker build. registry pull 없음.
#    이미지 태깅은 compose 자동 (vmd-prod-django-blue 등). 롤백은
#    git checkout <prev-SHA> + 본 스크립트 재실행.
#
#  대상:
#    Django Blue/Green (django-<color>) + 단일 celery-worker 재기동
#    AI Worker 는 별도 (deploy_ai.sh, 추후 작성). 본 스크립트는 건드리지 않음.
#
#  흐름:
#    1. 활성 색상 감지 (Nginx 심볼릭 링크 기준; 없으면 blue 가정)
#    2. standby 색상 Django 빌드 (현재 worktree 기준)
#    3. (선택) DB migration — standby 새 이미지 컨테이너로 1회 실행
#    4. standby 색상 기동 (--force-recreate 로 새 이미지 적용 보장)
#    5. health_check.sh 로 standby Django healthz 대기
#    6. switch_color.sh 로 Nginx upstream 스왑
#    7. DRAIN_SECONDS 동안 in-flight 요청 종료 대기
#    8. 이전 active Django stop
#    9. celery-worker 재빌드 + recreate (코드 드리프트 방지)
#
#  사전 조건:
#    - 첫 배포는 본 스크립트 X. 수동 부트스트랩 (operations_activation_plan §4):
#        dc build django-blue celery-worker
#        dc up -d postgres redis nginx
#        dc run --rm django-blue python manage.py migrate
#        dc up -d django-blue celery-worker
#    - 본 스크립트는 Blue 가 이미 떠있는 SUBSEQUENT 배포에서만 사용.
#
#  예시:
#    bash infra/scripts/deploy.sh abc1234
#    bash infra/scripts/deploy.sh abc1234 --skip-migration
#
#  환경변수 override:
#    HEALTH_TIMEOUT=180  DRAIN_SECONDS=90  bash infra/scripts/deploy.sh abc1234
# =============================================================================

set -euo pipefail

# ──── usage ────
usage() {
  cat <<'EOF'
Usage: deploy.sh <git-sha> [--skip-migration]

Blue/Green deploy orchestrator for VMD production (single-host EC2, Option A).

Arguments:
  <git-sha>           Git short SHA (informational — used for logging only;
                      actual image content is determined by current worktree)

Options:
  --skip-migration    Skip 'manage.py migrate' step (for hotfix re-deploy)
  -h, --help          Show this help

Environment overrides:
  HEALTH_TIMEOUT      Seconds to wait for standby health (default 120)
  DRAIN_SECONDS       Seconds to drain old color before stop (default 60)

Steps:
  1. Detect active color (default: blue)
  2. Build standby Django image (from current worktree)
  3. Run DB migration with new image (unless --skip-migration)
  4. Boot standby (--force-recreate)
  5. Health check standby
  6. Swap Nginx upstream
  7. Drain old color
  8. Stop old Django color
  9. Rebuild + recreate celery-worker (sync with new code)
EOF
}

# ──── arg parsing ────
if (( $# < 1 )); then
  usage >&2; exit 2
fi

case "$1" in
  -h|--help) usage; exit 0 ;;
esac

NEW_TAG="$1"; shift
SKIP_MIGRATION=0

while (( $# > 0 )); do
  case "$1" in
    --skip-migration) SKIP_MIGRATION=1 ;;
    -h|--help)        usage; exit 0 ;;
    *)                echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "$NEW_TAG" || "$NEW_TAG" == "latest" ]]; then
  echo "ERROR: refusing mutable/empty tag '$NEW_TAG' — pass git short SHA for traceability" >&2
  exit 2
fi

# ──── path resolution ────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$INFRA_ROOT/compose/docker-compose.prod.yml"
ENV_FILE="$INFRA_ROOT/env/prod.env"
ACTIVE_LINK="$INFRA_ROOT/nginx/conf/conf.d/00-upstream-active.conf"

HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
DRAIN_SECONDS="${DRAIN_SECONDS:-60}"

# ──── helpers ────
log() { printf '[deploy][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
err() { printf '[deploy][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# `--profile green` 은 green 프로파일 서비스를 그래프에 포함시키기 위함.
# 명시적으로 service 이름을 지정하므로 blue/green 어느 색상이든 안전하게 동작.
dc() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile green "$@"
}

# ──── pre-flight ────
[[ -f "$COMPOSE_FILE" ]] || { err "missing: $COMPOSE_FILE"; exit 1; }
[[ -f "$ENV_FILE" ]]     || { err "missing: $ENV_FILE";     exit 1; }
[[ -x "$SCRIPT_DIR/switch_color.sh" ]] || { err "switch_color.sh not executable"; exit 1; }
[[ -x "$SCRIPT_DIR/health_check.sh" ]] || { err "health_check.sh not executable"; exit 1; }

# ──── 1. detect active color ────
ACTIVE="blue"
if [[ -L "$ACTIVE_LINK" ]]; then
  TARGET="$(readlink "$ACTIVE_LINK")"
  case "$TARGET" in
    *upstream.green.conf) ACTIVE="green" ;;
    *upstream.blue.conf)  ACTIVE="blue"  ;;
    *) err "unrecognized symlink target: $TARGET"; exit 1 ;;
  esac
else
  err "no active symlink — first deploy should use manual bootstrap, not deploy.sh"
  err "see operations_activation_plan.md §4 for bootstrap commands"
  exit 1
fi

if [[ "$ACTIVE" == "blue" ]]; then
  STANDBY="green"
else
  STANDBY="blue"
fi

DJANGO_STANDBY="django-$STANDBY"
DJANGO_ACTIVE="django-$ACTIVE"

log "active=$ACTIVE  standby=$STANDBY  new_tag=$NEW_TAG (informational)"
log "config: HEALTH_TIMEOUT=${HEALTH_TIMEOUT}s  DRAIN_SECONDS=${DRAIN_SECONDS}s"

# ──── 2. build standby image (from current worktree) ────
log "building standby image: $DJANGO_STANDBY"
dc build "$DJANGO_STANDBY"

# ──── 3. DB migration ────
if (( SKIP_MIGRATION == 0 )); then
  log "running DB migration with new image"
  if ! dc run --rm "$DJANGO_STANDBY" python manage.py migrate --noinput; then
    err "migration failed — aborting deploy (active still on $ACTIVE)"
    exit 1
  fi
  log "migration complete"
else
  log "SKIP migration (--skip-migration)"
fi

# ──── 4. boot standby ────
# --force-recreate: 이미지가 동일 태그(auto)라도 새로 빌드된 layer 가 적용되도록 강제.
log "bringing up standby color: $STANDBY"
dc up -d --force-recreate "$DJANGO_STANDBY"

# ──── rollback fn (boot standby down on subsequent failure) ────
rollback_standby() {
  err "rolling back: stopping standby ($STANDBY) to release resources"
  dc stop "$DJANGO_STANDBY" || true
  err "active color ($ACTIVE) is unchanged"
}

# ──── 5. health check standby ────
log "waiting for standby Django to become healthy (timeout ${HEALTH_TIMEOUT}s)"
if ! "$SCRIPT_DIR/health_check.sh" "$STANDBY" "$HEALTH_TIMEOUT"; then
  err "standby health check failed"
  rollback_standby
  exit 1
fi

# ──── 6. swap Nginx upstream ────
log "swapping Nginx upstream → $STANDBY"
if ! "$SCRIPT_DIR/switch_color.sh" "$STANDBY"; then
  err "Nginx switch failed (config or reload error)"
  rollback_standby
  exit 1
fi

# ──── 7. drain old color ────
log "sleeping ${DRAIN_SECONDS}s to drain in-flight requests on $ACTIVE"
sleep "$DRAIN_SECONDS"

# ──── 8. stop old Django color ────
log "stopping old Django color: $ACTIVE"
dc stop "$DJANGO_ACTIVE"

# ──── 9. recreate celery-worker with new code ────
# Option A: celery 는 별도 이미지(vmd-prod-celery-worker) 라 Django 빌드와
# 분리됨. 매 배포마다 재빌드+recreate 하여 Django 와 코드 동기화 보장.
# acks_late=True + reject_on_worker_lost=True 로 in-flight 작업은 재큐됨.
log "rebuilding celery-worker (sync with new code)"
dc build celery-worker
dc up -d --force-recreate celery-worker

log "DONE — deploy complete  active=$STANDBY  tag=$NEW_TAG"
