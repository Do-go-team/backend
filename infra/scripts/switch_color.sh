#!/usr/bin/env bash
# =============================================================================
#  switch_color.sh — Atomic Nginx upstream color swap
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §3.2, §5.4
#
#  동작:
#    1. <new-color>(blue|green) 검증
#    2. infra/nginx/conf/conf.d/00-upstream-active.conf 심볼릭 링크를
#       ../upstreams/upstream.<color>.conf 로 atomic 교체 (ln -sfn)
#    3. vmd-nginx 컨테이너 안에서 nginx -t 로 문법 검증
#    4. 성공 시 nginx -s reload, 실패 시 이전 심볼릭 링크로 자동 복구
#
#  예시:
#    bash infra/scripts/switch_color.sh green
# =============================================================================

set -euo pipefail

# ──── usage ────
usage() {
  cat <<'EOF'
Usage: switch_color.sh <blue|green>

Atomically swap the active Nginx upstream symlink to the given color,
validate config inside vmd-nginx, then reload.
On validation failure, the previous symlink is restored and exit 1.
EOF
}

# ──── arg parsing ────
if (( $# != 1 )); then
  usage >&2; exit 2
fi

NEW_COLOR="$1"
case "$NEW_COLOR" in
  blue|green) ;;
  -h|--help)  usage; exit 0 ;;
  *)          usage >&2; exit 2 ;;
esac

# ──── path resolution (script-relative, works on host & in CI) ────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NGINX_CONF_DIR="$(cd "$SCRIPT_DIR/../nginx/conf" && pwd)"
ACTIVE_LINK="$NGINX_CONF_DIR/conf.d/00-upstream-active.conf"
NEW_TARGET_REL="../upstreams/upstream.${NEW_COLOR}.conf"
NEW_TARGET_ABS="$NGINX_CONF_DIR/upstreams/upstream.${NEW_COLOR}.conf"

NGINX_CONTAINER="${NGINX_CONTAINER:-vmd-nginx}"

# ──── helpers ────
log()  { printf '[switch_color][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
err()  { printf '[switch_color][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# ──── pre-flight ────
if [[ ! -f "$NEW_TARGET_ABS" ]]; then
  err "missing upstream target file: $NEW_TARGET_ABS"
  exit 1
fi

if ! docker inspect -f '{{.State.Running}}' "$NGINX_CONTAINER" >/dev/null 2>&1; then
  err "container not running: $NGINX_CONTAINER"
  err "start it first: docker compose -f infra/compose/docker-compose.prod.yml up -d nginx"
  exit 1
fi

# ──── capture previous symlink for rollback ────
PREVIOUS_TARGET=""
if [[ -L "$ACTIVE_LINK" ]]; then
  PREVIOUS_TARGET="$(readlink "$ACTIVE_LINK")"
  log "previous active target: $PREVIOUS_TARGET"
elif [[ -e "$ACTIVE_LINK" ]]; then
  err "$ACTIVE_LINK exists but is not a symlink — refusing to overwrite"
  err "remove it manually if you intended to convert from a regular file"
  exit 1
else
  log "no previous active symlink (first switch)"
fi

# ──── atomic swap ────
log "switching active upstream → $NEW_COLOR ($NEW_TARGET_REL)"
ln -sfn "$NEW_TARGET_REL" "$ACTIVE_LINK"

# ──── rollback handler (registered AFTER the swap) ────
rollback() {
  err "validation failed — reverting symlink"
  if [[ -n "$PREVIOUS_TARGET" ]]; then
    ln -sfn "$PREVIOUS_TARGET" "$ACTIVE_LINK"
    log "symlink restored to: $PREVIOUS_TARGET"
  else
    rm -f "$ACTIVE_LINK"
    log "symlink removed (no previous target to restore)"
  fi
}

# ──── validate config inside container ────
log "running 'nginx -t' inside $NGINX_CONTAINER"
if ! docker exec "$NGINX_CONTAINER" nginx -t; then
  rollback
  exit 1
fi

# ──── reload ────
log "config valid — reloading nginx"
if ! docker exec "$NGINX_CONTAINER" nginx -s reload; then
  err "nginx -s reload failed (config was valid — investigate worker state)"
  rollback
  exit 1
fi

log "DONE — active upstream is now: $NEW_COLOR"