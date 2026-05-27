#!/usr/bin/env bash
# =============================================================================
#  bootstrap_host.sh — Day 0 Lightsail host initialization
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §5.1 #7 (호스트 사전 준비)
#             vmd_infra_plan.md §6 Step 4 검증 항목
#
#  역할:
#    텅 빈 호스트(Lightsail xlarge)에 docker compose up 을 처음 돌리기 전,
#    아래 4가지 "닭과 달걀" 문제를 한 번에 해결한다.
#
#    1. 디렉토리 + 권한
#       /opt/vmd/models (UID/GID 10001:10001) — AI 모델 bind mount target
#       /etc/letsencrypt/live/<domain>/        — TLS cert path
#       /var/www/certbot                       — ACME HTTP-01 challenge root
#
#    2. 초기 AI 모델 다운로드 (Phase 1)
#       /opt/vmd/models/yolov11s-seg.pt 부재 시 ultralytics 공식 릴리즈에서 받음
#       (Phase 2 진입 시: entrypoint 의 S3 다운로드 패턴으로 마이그레이션 필요.
#        plan §2.3 ⚠️ 박스 참조)
#
#    3. Nginx 활성 색상 심볼릭 링크 (BLUE 기본)
#       infra/nginx/conf/conf.d/00-upstream-active.conf → ../upstreams/upstream.blue.conf
#       이게 없으면 nginx 가 첫 부팅에서 'no such file' 로 죽는다.
#
#    4. 더미 self-signed TLS 인증서
#       /etc/letsencrypt/live/<domain>/{fullchain,privkey,chain}.pem
#       /etc/letsencrypt/ssl-dhparams.pem (RFC 7919 ffdhe2048)
#       Certbot 발급 전 nginx 가 죽지 않도록 임시 인증서를 깔아둔다.
#       Issuer 에 "VMD-Bootstrap-Dummy" 마커를 넣어 추후 진짜 인증서와 구분.
#
#  사용:
#    sudo bash infra/scripts/bootstrap_host.sh
#    sudo bash infra/scripts/bootstrap_host.sh --domain do-goproject.com --force
#    sudo DOMAIN=staging.do-goproject.com bash infra/scripts/bootstrap_host.sh
#
#  멱등성: 모든 단계는 idempotent. 재실행 안전 (단, --force 시 dummy cert 재생성).
# =============================================================================

set -euo pipefail


# ─────────────────────────────────────────────────────────────────────────────
# usage / args
# ─────────────────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: sudo bootstrap_host.sh [--domain <fqdn>] [--force] [--skip-model]

Initialize a fresh Lightsail host so that 'docker compose up' can succeed.

Options:
  --domain <fqdn>   TLS cert CN (default: do-goproject.com or $DOMAIN env)
  --force           Regenerate dummy TLS cert even if one exists.
                    REFUSES to overwrite real (non-dummy) certs as a safety guard.
  --skip-model      Skip YOLO model download (useful when model is provisioned
                    by a separate process / Ansible / S3-sync).
  -h, --help        Show this help

Environment overrides:
  DOMAIN, MODEL_DIR, YOLO_MODEL_FILE, YOLO_MODEL_URL,
  VMD_USER_UID, VMD_USER_GID, LETSENCRYPT_DIR

Re-run safe. Run once after host provisioning, then 'docker compose up -d'.
EOF
}

DOMAIN="${DOMAIN:-do-goproject.com}"
FORCE=0
SKIP_MODEL=0

while (( $# > 0 )); do
  case "$1" in
    --domain)      DOMAIN="${2:?--domain requires value}"; shift 2 ;;
    --force)       FORCE=1; shift ;;
    --skip-model)  SKIP_MODEL=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done


# ─────────────────────────────────────────────────────────────────────────────
# config
# ─────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NGINX_CONF_DIR="$INFRA_ROOT/nginx/conf"
ACTIVE_LINK="$NGINX_CONF_DIR/conf.d/00-upstream-active.conf"
INITIAL_COLOR="${INITIAL_COLOR:-blue}"

# 호스트 절대 경로 (compose 의 bind mount target 들)
MODEL_DIR="${MODEL_DIR:-/opt/vmd/models}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"
CERT_DIR="$LETSENCRYPT_DIR/live/$DOMAIN"
DHPARAM_PATH="$LETSENCRYPT_DIR/ssl-dhparams.pem"
ACME_WEBROOT="${ACME_WEBROOT:-/var/www/certbot}"

# 컨테이너 내 vmd_user 와 일치 (Dockerfile 에서 고정)
VMD_USER_UID="${VMD_USER_UID:-10001}"
VMD_USER_GID="${VMD_USER_GID:-10001}"

# YOLO 모델 (Phase 1: 호스트 다운로드, Phase 2: S3 entrypoint 로 이전)
YOLO_MODEL_FILE="${YOLO_MODEL_FILE:-yolov11s-seg.pt}"
YOLO_MODEL_URL="${YOLO_MODEL_URL:-https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s-seg.pt}"

# Dummy cert 마커 (재실행 시 진짜 인증서와 구분하기 위함)
DUMMY_CERT_MARKER="VMD-Bootstrap-Dummy"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
log()  { printf '[bootstrap][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
warn() { printf '[bootstrap][WARN][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
err()  { printf '[bootstrap][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    err "missing required command: $1 — install it first (apt-get install $1)"
    exit 1
  }
}

is_dummy_cert() {
  local cert="$1"
  [[ -f "$cert" ]] || return 1
  openssl x509 -in "$cert" -noout -issuer 2>/dev/null \
    | grep -q "$DUMMY_CERT_MARKER"
}


# ─────────────────────────────────────────────────────────────────────────────
# pre-flight
# ─────────────────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  err "must be run as root (chown / write to /opt and /etc/letsencrypt). Try: sudo $0"
  exit 1
fi

require_cmd mkdir
require_cmd chown
require_cmd ln
require_cmd openssl
if (( SKIP_MODEL == 0 )); then
  if command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl"
  elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget"
  else
    err "neither curl nor wget found — install one or pass --skip-model"
    exit 1
  fi
fi

log "domain=$DOMAIN  initial_color=$INITIAL_COLOR  force=$FORCE  skip_model=$SKIP_MODEL"
log "infra_root=$INFRA_ROOT"


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — directories + permissions
# ─────────────────────────────────────────────────────────────────────────────
log "─── Step 1/4: directories + permissions ─────────────────────────────"

mkdir -p \
  "$MODEL_DIR" \
  "$CERT_DIR" \
  "$ACME_WEBROOT" \
  "$NGINX_CONF_DIR/conf.d" \
  "$NGINX_CONF_DIR/upstreams" \
  "$NGINX_CONF_DIR/snippets"

# AI worker 컨테이너의 vmd_user(10001) 가 read 할 수 있어야 함
chown -R "${VMD_USER_UID}:${VMD_USER_GID}" "$MODEL_DIR"
chmod 0755 "$MODEL_DIR"

log "  ✓ $MODEL_DIR (chown ${VMD_USER_UID}:${VMD_USER_GID})"
log "  ✓ $CERT_DIR"
log "  ✓ $ACME_WEBROOT"
log "  ✓ nginx conf subdirs verified"


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — YOLO model download (Phase 1 only)
# ─────────────────────────────────────────────────────────────────────────────
log "─── Step 2/4: YOLO model (Phase 1 bind-mount) ──────────────────────"

MODEL_PATH="$MODEL_DIR/$YOLO_MODEL_FILE"

if (( SKIP_MODEL == 1 )); then
  log "  → SKIP (--skip-model)"
elif [[ -s "$MODEL_PATH" ]]; then
  log "  → already present, skipping (size $(stat -c%s "$MODEL_PATH" 2>/dev/null || echo '?') bytes)"
else
  log "  ↓ downloading $YOLO_MODEL_URL"
  log "    → $MODEL_PATH"
  TMP_PATH="${MODEL_PATH}.partial"
  case "$DOWNLOADER" in
    curl)
      curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 10 \
           -o "$TMP_PATH" "$YOLO_MODEL_URL"
      ;;
    wget)
      wget -q --tries=3 --timeout=30 -O "$TMP_PATH" "$YOLO_MODEL_URL"
      ;;
  esac

  # sanity: non-empty file
  if [[ ! -s "$TMP_PATH" ]]; then
    err "downloaded file is empty — URL may be wrong"
    rm -f "$TMP_PATH"
    exit 1
  fi

  mv "$TMP_PATH" "$MODEL_PATH"
  chown "${VMD_USER_UID}:${VMD_USER_GID}" "$MODEL_PATH"
  chmod 0644 "$MODEL_PATH"
  log "  ✓ saved ($(stat -c%s "$MODEL_PATH") bytes)"
fi

# ⚠️ Phase 2 알림 (잊지 말 것)
warn "Phase 2 reminder: when migrating to ECS/EKS/Auto-Scaling, replace this"
warn "  bind-mount pattern with S3 download in ai-worker entrypoint."
warn "  See: infra/.ai/plan/vmd_infra_plan.md §2.3 ⚠️ box"


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Nginx active upstream symlink
# ─────────────────────────────────────────────────────────────────────────────
log "─── Step 3/4: Nginx active upstream symlink ────────────────────────"

TARGET_REL="../upstreams/upstream.${INITIAL_COLOR}.conf"
TARGET_ABS="$NGINX_CONF_DIR/upstreams/upstream.${INITIAL_COLOR}.conf"

if [[ ! -f "$TARGET_ABS" ]]; then
  err "missing upstream file: $TARGET_ABS"
  err "did you forget to populate infra/nginx/conf/upstreams/?"
  exit 1
fi

if [[ -L "$ACTIVE_LINK" ]]; then
  CURRENT_TARGET="$(readlink "$ACTIVE_LINK")"
  log "  → existing symlink: $CURRENT_TARGET (leaving as-is)"
elif [[ -e "$ACTIVE_LINK" ]]; then
  err "$ACTIVE_LINK exists but is NOT a symlink — refusing to overwrite"
  err "remove it manually if you intended to convert to a symlink"
  exit 1
else
  ln -sfn "$TARGET_REL" "$ACTIVE_LINK"
  log "  ✓ created: 00-upstream-active.conf → $TARGET_REL"
fi


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Dummy self-signed TLS cert + dhparam
# ─────────────────────────────────────────────────────────────────────────────
log "─── Step 4/4: TLS cert (dummy if absent) ───────────────────────────"

CERT_FULL="$CERT_DIR/fullchain.pem"
CERT_KEY="$CERT_DIR/privkey.pem"
CERT_CHAIN="$CERT_DIR/chain.pem"

generate_dummy_cert() {
  log "  ↻ generating dummy self-signed cert (10 years) for CN=$DOMAIN"

  # SAN 포함 (Modern 브라우저는 CN 만으로는 거부)
  local san_cnf
  san_cnf="$(mktemp)"
  cat > "$san_cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = $DOMAIN
O  = $DUMMY_CERT_MARKER

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $DOMAIN
DNS.2 = *.$DOMAIN
EOF

  openssl req -x509 -nodes \
    -newkey rsa:2048 \
    -days 3650 \
    -keyout "$CERT_KEY" \
    -out "$CERT_FULL" \
    -config "$san_cnf" \
    -extensions v3_req \
    2>/dev/null

  rm -f "$san_cnf"

  # chain.pem 은 OCSP stapling 에 사용. 더미 단계에서는 fullchain 복사.
  cp "$CERT_FULL" "$CERT_CHAIN"

  chmod 0600 "$CERT_KEY"
  chmod 0644 "$CERT_FULL" "$CERT_CHAIN"

  log "  ✓ wrote $CERT_FULL (chmod 644)"
  log "  ✓ wrote $CERT_KEY  (chmod 600)"
  log "  ✓ wrote $CERT_CHAIN (chmod 644)"
}

if [[ -f "$CERT_FULL" && -f "$CERT_KEY" ]]; then
  if is_dummy_cert "$CERT_FULL"; then
    if (( FORCE == 1 )); then
      log "  → existing dummy cert detected, --force given → regenerate"
      generate_dummy_cert
    else
      log "  → dummy cert already in place, skipping (use --force to regenerate)"
    fi
  else
    if (( FORCE == 1 )); then
      err "REAL (non-dummy) cert detected at $CERT_FULL"
      err "refusing to overwrite even with --force — would break TLS"
      err "to replace: manually delete the cert files first, then re-run"
      exit 1
    fi
    log "  → real cert detected (issuer not '$DUMMY_CERT_MARKER'), leaving alone"
  fi
else
  generate_dummy_cert
fi


# ─── DH parameters (RFC 7919 ffdhe2048 — 표준화된 well-known group) ────────
if [[ -f "$DHPARAM_PATH" ]]; then
  log "  → dhparam already exists: $DHPARAM_PATH"
else
  log "  ↻ writing RFC 7919 ffdhe2048 dhparam → $DHPARAM_PATH"
  cat > "$DHPARAM_PATH" <<'EOF'
-----BEGIN DH PARAMETERS-----
MIIBCAKCAQEA//////////+t+FRYortKmq/cViAnPTzx2LnFg84tNpWp4TZBFGQz
+8yTnc4kmz75fS/jY2MMddj2gbICrsRhetPfHtXV/WVhJDP1H18GbtCFY2VVPe0a
87VXE15/V8k1mE8McODmi3fipona8+/och3xWKE2rec1MKzKT0g6eXq8CrGCsyT7
YdEIqUuyyOP7uWrat2DX9GgdT0Kj3jlN9K5W7edjcrsZCwenyO4KbXCeAvzhzffi
7MA0BM0oNC9hkXL+nOmFg/+OTxIy7vKBg8P+OxtMb61zO7X8vC7CIAXFjvGDfRaD
ssbzSibBsu/6iGtCOGEoXJf//////////wIBAg==
-----END DH PARAMETERS-----
EOF
  chmod 0644 "$DHPARAM_PATH"
  log "  ✓ wrote $DHPARAM_PATH"
fi


# ─────────────────────────────────────────────────────────────────────────────
# summary
# ─────────────────────────────────────────────────────────────────────────────
cat <<EOF

═══════════════════════════════════════════════════════════════════════════
  bootstrap_host.sh — DONE
───────────────────────────────────────────────────────────────────────────
  Host is ready for first 'docker compose up -d'.

  Created / verified:
    - Model dir:    $MODEL_DIR  (UID/GID ${VMD_USER_UID}:${VMD_USER_GID})
    - Model file:   $MODEL_PATH
    - Cert dir:     $CERT_DIR
    - Cert (dummy): $CERT_FULL  (issuer: $DUMMY_CERT_MARKER)
    - DH params:    $DHPARAM_PATH
    - Active link:  $ACTIVE_LINK → $TARGET_REL
    - ACME webroot: $ACME_WEBROOT

  Next steps (in order):
    1. Populate infra/env/prod.env (secrets injection — see infra/env/README.md)
    2. Bring up the stack:
         cd infra/compose
         docker compose --env-file ../env/prod.env -f docker-compose.prod.yml up -d
    3. Replace dummy cert with real Let's Encrypt cert (after DNS resolves):
         certbot certonly --webroot -w $ACME_WEBROOT -d $DOMAIN \\
           --non-interactive --agree-tos -m ops@$DOMAIN
         docker exec vmd-nginx nginx -s reload
    4. Schedule auto-renew (certbot.timer or cron — twice daily recommended)
═══════════════════════════════════════════════════════════════════════════
EOF