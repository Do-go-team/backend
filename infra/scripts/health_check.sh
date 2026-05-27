#!/usr/bin/env bash
# =============================================================================
#  health_check.sh — Wait for vmd-django-<color> /healthz to become healthy
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §5.4 (Stage 5)
#
#  동작:
#    1. <color>(blue|green), [timeout_seconds] (기본 60) 인자 검증
#    2. vmd-django-<color> 컨테이너에서 curl http://localhost:8000/healthz 폴링
#    3. 200 OK 응답이 오면 즉시 exit 0
#    4. 타임아웃이면 컨테이너 로그 마지막 200줄 출력 후 exit 1
#
#  예시:
#    bash infra/scripts/health_check.sh green 90
# =============================================================================

set -euo pipefail

# ──── usage ────
usage() {
  cat <<'EOF'
Usage: health_check.sh <blue|green> [timeout_seconds]

Poll http://localhost:8000/healthz inside vmd-django-<color> until it returns
200, or until timeout_seconds (default 60) elapses.

On timeout, prints the last 200 lines of the container log and exits 1.
EOF
}

# ──── arg parsing ────
if (( $# < 1 || $# > 2 )); then
  usage >&2; exit 2
fi

COLOR="$1"
case "$COLOR" in
  blue|green) ;;
  -h|--help)  usage; exit 0 ;;
  *)          usage >&2; exit 2 ;;
esac

TIMEOUT="${2:-60}"
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || (( TIMEOUT < 1 )); then
  echo "timeout_seconds must be a positive integer (got: $TIMEOUT)" >&2
  exit 2
fi

# ──── config ────
CONTAINER="vmd-django-${COLOR}"
# BE 가 /healthz 미구현 → 일단 /api/v1/hello 로 정합 (compose healthcheck 와 동일).
# BE 가 /healthz 추가하면 Jenkinsfile environment 에 HEALTH_URL 박아 override.
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/api/v1/hello}"
INTERVAL="${HEALTH_INTERVAL:-3}"
CURL_MAX_TIME="${HEALTH_CURL_MAX_TIME:-2}"

# ──── helpers ────
log() { printf '[health_check][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
err() { printf '[health_check][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# ──── pre-flight ────
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
  err "container not found or not inspectable: $CONTAINER"
  err "is it up? docker compose -f infra/compose/docker-compose.prod.yml ps"
  exit 1
fi

state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
if [[ "$state" != "running" ]]; then
  err "container state is '$state' (expected 'running'): $CONTAINER"
  exit 1
fi

log "polling $CONTAINER  url=$HEALTH_URL  timeout=${TIMEOUT}s  interval=${INTERVAL}s"

# ──── polling loop ────
deadline=$(( $(date +%s) + TIMEOUT ))
attempt=0

while (( $(date +%s) < deadline )); do
  attempt=$(( attempt + 1 ))

  # -f: fail on HTTP >=400, -s: silent, -S: show errors, --max-time: per-attempt cap
  if docker exec "$CONTAINER" \
       curl -fsS --max-time "$CURL_MAX_TIME" "$HEALTH_URL" >/dev/null 2>&1; then
    elapsed=$(( $(date +%s) - (deadline - TIMEOUT) ))
    log "HEALTHY after attempt #${attempt} (${elapsed}s elapsed) — $CONTAINER"
    exit 0
  fi

  remaining=$(( deadline - $(date +%s) ))
  log "attempt #${attempt}: not healthy yet (remaining ${remaining}s)"
  sleep "$INTERVAL"
done

# ──── failure: dump logs and exit ────
err "$CONTAINER did NOT become healthy within ${TIMEOUT}s"
err "─── last 200 lines of $CONTAINER ───"
docker logs --tail 200 "$CONTAINER" 2>&1 || true
err "─── end of log dump ───"
exit 1