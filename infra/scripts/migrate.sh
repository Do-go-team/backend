#!/usr/bin/env bash
# =============================================================================
#  migrate.sh — Run Django DB migration against the active color
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §5.4 (Stage 3) + §5.4.2 Expand-Contract
#
#  동작:
#    1. Nginx 의 활성 색상 심볼릭 링크에서 active color 를 읽어옴
#       (없으면 blue 기본 — 첫 배포 호환)
#    2. vmd-django-<color> 컨테이너에서
#         python manage.py migrate --noinput
#       을 docker exec 로 실행
#
#  옵션:
#    --color blue|green        활성 색상 자동 감지 대신 강제 지정
#    --plan                    실제 적용 대신 marker plan 만 출력 (--plan 옵션)
#    --                        이후 인자를 manage.py 에 그대로 전달
#                              예: migrate.sh -- showmigrations
#
#  예시:
#    bash infra/scripts/migrate.sh
#    bash infra/scripts/migrate.sh --color green
#    bash infra/scripts/migrate.sh --plan
#    bash infra/scripts/migrate.sh -- showmigrations app_name
#
#  주의:
#    Blue/Green 무중단 배포 원칙상 모든 마이그레이션은 backward-compatible
#    이어야 함 (Expand-Contract 5단계, plan §5.1 #9 / §5.4.2 참조).
# =============================================================================

set -euo pipefail


# ─── usage / args ──────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: migrate.sh [--color blue|green] [--plan] [-- <manage.py args>]

Run Django DB migration against the currently active color (or override via --color).

Options:
  --color blue|green   Override active-color detection (default: read symlink)
  --plan               Run 'migrate --plan' (preview only, no changes)
  --                   Pass-through to manage.py (replaces 'migrate --noinput')
  -h, --help           Show this help

Defaults:
  When no flags given: 'python manage.py migrate --noinput' on active color.
EOF
}

COLOR_OVERRIDE=""
PLAN_MODE=0
PASSTHROUGH=()
SAW_DOUBLE_DASH=0

while (( $# > 0 )); do
  if (( SAW_DOUBLE_DASH == 1 )); then
    PASSTHROUGH+=("$1")
    shift
    continue
  fi
  case "$1" in
    --color)   COLOR_OVERRIDE="${2:?--color requires blue|green}"; shift 2 ;;
    --plan)    PLAN_MODE=1; shift ;;
    --)        SAW_DOUBLE_DASH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Validate --color override
if [[ -n "$COLOR_OVERRIDE" ]]; then
  case "$COLOR_OVERRIDE" in
    blue|green) ;;
    *) echo "ERROR: --color must be blue|green (got: $COLOR_OVERRIDE)" >&2; exit 2 ;;
  esac
fi


# ─── path resolution ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ACTIVE_LINK="$INFRA_ROOT/nginx/conf/conf.d/00-upstream-active.conf"


# ─── helpers ───────────────────────────────────────────────────────────────
log() { printf '[migrate][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
err() { printf '[migrate][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }


# ─── 1. detect active color ────────────────────────────────────────────────
if [[ -n "$COLOR_OVERRIDE" ]]; then
  COLOR="$COLOR_OVERRIDE"
  log "color forced via --color: $COLOR"
elif [[ -L "$ACTIVE_LINK" ]]; then
  TARGET="$(readlink "$ACTIVE_LINK")"
  case "$TARGET" in
    *upstream.green.conf) COLOR="green" ;;
    *upstream.blue.conf)  COLOR="blue"  ;;
    *) err "unrecognized symlink target: $TARGET"; exit 1 ;;
  esac
  log "active color detected: $COLOR (from symlink)"
else
  COLOR="blue"
  log "no active symlink — defaulting to blue"
fi

CONTAINER="vmd-django-${COLOR}"


# ─── 2. pre-flight ─────────────────────────────────────────────────────────
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
  err "container not running: $CONTAINER"
  err "is it up? docker compose -f infra/compose/docker-compose.prod.yml ps"
  exit 1
fi

state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
if [[ "$state" != "running" ]]; then
  err "container state is '$state' (expected 'running'): $CONTAINER"
  exit 1
fi


# ─── 3. build manage.py argv ───────────────────────────────────────────────
if (( ${#PASSTHROUGH[@]} > 0 )); then
  CMD=(python manage.py "${PASSTHROUGH[@]}")
elif (( PLAN_MODE == 1 )); then
  CMD=(python manage.py migrate --plan)
else
  CMD=(python manage.py migrate --noinput)
fi

log "executing on $CONTAINER: ${CMD[*]}"


# ─── 4. exec ───────────────────────────────────────────────────────────────
if ! docker exec -i "$CONTAINER" "${CMD[@]}"; then
  err "command failed: ${CMD[*]}"
  err "─── last 100 lines of $CONTAINER ───"
  docker logs --tail 100 "$CONTAINER" 2>&1 || true
  exit 1
fi

log "DONE — $CONTAINER  (${CMD[*]})"
