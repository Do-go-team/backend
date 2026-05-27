#!/usr/bin/env bash
# =============================================================================
#  backup_postgres.sh — Daily PostgreSQL dump → gzip → S3 + local rotation
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §6 Step 11 (백업/모니터링 cron)
#             vmd_infra_plan.md §4.4 옵션 A (Phase 1: 컨테이너 PG)
#
#  동작:
#    1. infra/env/prod.env 를 source 해 POSTGRES_* / BACKUP_S3_* / AWS_* 로드
#    2. vmd-postgres 컨테이너에서 pg_dump (plain SQL, custom 옵션) 실행
#    3. gzip -9 압축, 파일명: vmd-prod-postgres-<YYYYMMDD-HHMMSS>.sql.gz
#    4. aws s3 cp 으로 s3://${BACKUP_S3_BUCKET}/${BACKUP_S3_PREFIX}/ 에 업로드
#    5. ${BACKUP_RETENTION_DAYS} 일 (기본 7) 보다 오래된 로컬 백업 삭제
#
#  cron 등록 예시 (매일 03:15 KST):
#    15 18 * * * /opt/vmd/infra/scripts/backup_postgres.sh \
#                  >> /var/log/vmd/backup.log 2>&1
#    (cron 은 UTC 기준 — KST 는 +9h offset)
#
#  보안:
#    - 시크릿(POSTGRES_PASSWORD, AWS_*)은 prod.env 에서만 읽음 (코드 미박힘)
#    - AWS 자격증명은 우선순위:
#        1) 호스트의 IAM Instance Role  (Lightsail Managed IAM, 권장)
#        2) prod.env 의 AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (fallback)
#    - 임시 파일은 ${LOCAL_BACKUP_DIR} 로 격리 (chmod 700 권장)
#
#  실패 처리:
#    - 어느 단계든 실패 시 set -e 로 즉시 중단 + 에러 로그
#    - 부분 백업 파일(.partial)은 cleanup 단계에서 자동 제거 (trap)
# =============================================================================

set -euo pipefail


# ─── usage ─────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: backup_postgres.sh [--env-file <path>] [--no-upload] [--no-rotate]

Daily PostgreSQL dump → gzip → S3 + local rotation.

Options:
  --env-file <path>   Override default infra/env/prod.env
  --no-upload         Skip S3 upload (local backup only — for testing)
  --no-rotate         Skip local retention cleanup
  -h, --help          Show this help

Reads from env (via --env-file):
  POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
  BACKUP_S3_BUCKET, BACKUP_S3_PREFIX, BACKUP_RETENTION_DAYS
  AWS_REGION, AWS_ACCESS_KEY_ID*, AWS_SECRET_ACCESS_KEY*  (* if no IAM role)

Environment overrides:
  PG_CONTAINER          (default: vmd-postgres)
  LOCAL_BACKUP_DIR      (default: /var/backups/vmd)
  BACKUP_NAME_PREFIX    (default: vmd-prod-postgres)
EOF
}

ENV_FILE_OVERRIDE=""
DO_UPLOAD=1
DO_ROTATE=1

while (( $# > 0 )); do
  case "$1" in
    --env-file)  ENV_FILE_OVERRIDE="${2:?--env-file requires path}"; shift 2 ;;
    --no-upload) DO_UPLOAD=0; shift ;;
    --no-rotate) DO_ROTATE=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done


# ─── path resolution ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE_OVERRIDE:-$INFRA_ROOT/env/prod.env}"

PG_CONTAINER="${PG_CONTAINER:-vmd-postgres}"
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-/var/backups/vmd}"
BACKUP_NAME_PREFIX="${BACKUP_NAME_PREFIX:-vmd-prod-postgres}"


# ─── helpers ───────────────────────────────────────────────────────────────
log() { printf '[backup_pg][%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
err() { printf '[backup_pg][ERROR][%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }


# ─── 1. load env ───────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  err "env file not found: $ENV_FILE"
  err "supply via --env-file or create $INFRA_ROOT/env/prod.env"
  exit 1
fi

log "sourcing env: $ENV_FILE"
# `set -a` 로 source 한 변수를 자동 export.
# (PGPASSWORD / AWS_* 가 docker exec / aws cli 에 전달되어야 함)
set +u
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
set -u


# ─── 2. validate required vars ─────────────────────────────────────────────
require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    err "missing required env var: $name"
    err "  → fill it in $ENV_FILE and re-run"
    exit 1
  fi
}

require_var POSTGRES_USER
require_var POSTGRES_PASSWORD
require_var POSTGRES_DB
if (( DO_UPLOAD == 1 )); then
  require_var BACKUP_S3_BUCKET
fi

BACKUP_S3_PREFIX="${BACKUP_S3_PREFIX:-postgres/}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"


# ─── 3. pre-flight (container, tools) ──────────────────────────────────────
if ! docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" >/dev/null 2>&1; then
  err "container not running: $PG_CONTAINER"
  exit 1
fi

if (( DO_UPLOAD == 1 )) && ! command -v aws >/dev/null 2>&1; then
  err "aws CLI not found on host (required for --upload)"
  err "install: 'snap install aws-cli --classic' OR use --no-upload"
  exit 1
fi


# ─── 4. prepare local backup file ──────────────────────────────────────────
mkdir -p "$LOCAL_BACKUP_DIR"
chmod 0700 "$LOCAL_BACKUP_DIR" 2>/dev/null || true

TS="$(date +%Y%m%d-%H%M%S)"
BASENAME="${BACKUP_NAME_PREFIX}-${TS}.sql.gz"
LOCAL_PATH="${LOCAL_BACKUP_DIR}/${BASENAME}"
LOCAL_PARTIAL="${LOCAL_PATH}.partial"

# 실패 시 부분 파일 자동 제거
cleanup_partial() {
  if [[ -f "$LOCAL_PARTIAL" ]]; then
    rm -f "$LOCAL_PARTIAL"
    log "cleaned up partial file: $LOCAL_PARTIAL"
  fi
}
trap cleanup_partial EXIT


# ─── 5. pg_dump → gzip ─────────────────────────────────────────────────────
log "starting pg_dump  container=$PG_CONTAINER  db=$POSTGRES_DB  user=$POSTGRES_USER"

# pg_dump 는 컨테이너 내부에서 실행. PGPASSWORD 는 -e 로 전달.
# stdout 으로 SQL 텍스트를 흘려보내고 호스트에서 gzip 압축.
# `set -o pipefail` (set -euo pipefail 의 일부) 덕에 pg_dump 실패는 즉시 감지.
docker exec \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    "$PG_CONTAINER" \
  pg_dump \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    --format=plain \
    --encoding=UTF8 \
    --no-owner \
    --no-privileges \
    --quote-all-identifiers \
    --serializable-deferrable \
  | gzip -9 > "$LOCAL_PARTIAL"

# 결과물 sanity check (gzip header + 비어있지 않음)
if [[ ! -s "$LOCAL_PARTIAL" ]]; then
  err "dump produced empty file"
  exit 1
fi
if ! gzip -t "$LOCAL_PARTIAL" 2>/dev/null; then
  err "dump file is not a valid gzip — pg_dump may have errored mid-stream"
  exit 1
fi

mv "$LOCAL_PARTIAL" "$LOCAL_PATH"
trap - EXIT  # partial 정리 트랩 해제 (이제 정상 파일)

SIZE_BYTES="$(stat -c%s "$LOCAL_PATH" 2>/dev/null || stat -f%z "$LOCAL_PATH" 2>/dev/null || echo 0)"
log "dump complete: $LOCAL_PATH  (${SIZE_BYTES} bytes)"


# ─── 6. upload to S3 ───────────────────────────────────────────────────────
if (( DO_UPLOAD == 1 )); then
  S3_URI="s3://${BACKUP_S3_BUCKET}/${BACKUP_S3_PREFIX%/}/${BASENAME}"
  log "uploading → $S3_URI"

  # IAM Instance Role 이 있으면 AWS_ACCESS_KEY_ID 없이도 동작.
  # prod.env 에 AWS_* 가 있으면 source 단계에서 export 되어 fallback.
  if ! aws s3 cp \
        --region "$AWS_REGION" \
        --only-show-errors \
        --no-progress \
        --metadata "source=vmd-prod,timestamp=${TS}" \
        "$LOCAL_PATH" "$S3_URI"; then
    err "S3 upload failed (local backup retained at $LOCAL_PATH)"
    exit 1
  fi

  log "uploaded ✓  $S3_URI"
else
  log "SKIP S3 upload (--no-upload)"
fi


# ─── 7. local rotation ─────────────────────────────────────────────────────
if (( DO_ROTATE == 1 )); then
  log "rotating local backups older than ${BACKUP_RETENTION_DAYS} days in $LOCAL_BACKUP_DIR"

  # find -mtime +N : N일 보다 오래된 파일
  # -name 패턴으로 본 스크립트가 만든 파일만 대상으로 좁힘 (다른 파일 보호)
  REMOVED=0
  while IFS= read -r -d '' OLD; do
    rm -f -- "$OLD"
    log "  removed: $OLD"
    REMOVED=$((REMOVED + 1))
  done < <(find "$LOCAL_BACKUP_DIR" -maxdepth 1 -type f \
              -name "${BACKUP_NAME_PREFIX}-*.sql.gz" \
              -mtime "+${BACKUP_RETENTION_DAYS}" \
              -print0 2>/dev/null)

  log "rotation complete (${REMOVED} file(s) removed)"
else
  log "SKIP local rotation (--no-rotate)"
fi


# ─── 8. summary ────────────────────────────────────────────────────────────
log "DONE — backup successful"
log "  local:  $LOCAL_PATH"
if (( DO_UPLOAD == 1 )); then
  log "  remote: s3://${BACKUP_S3_BUCKET}/${BACKUP_S3_PREFIX%/}/${BASENAME}"
fi
