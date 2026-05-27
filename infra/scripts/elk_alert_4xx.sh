#!/usr/bin/env bash
# =============================================================================
#  elk_alert_4xx.sh — VMD 4xx 임계 초과 시 Mattermost 알림
# -----------------------------------------------------------------------------
#  설계 근거: infra/.ai/plan/observability_elk_4xx_alert_plan.md
#
#  동작:
#    1. ES 에 최근 WINDOW_MIN 분간 nginx.status 가 4xx 인 카운트 쿼리
#       — 단 401 (만료 토큰 routine), 404 (봇/크롤러 노이즈), 429 (rate limit) 제외
#    2. 임계 초과 시 mm webhook 호출 (5xx 와 동일 채널 — MM_WEBHOOK_ALERT,
#       색깔 amber 로 시각 구분)
#    3. cooldown 파일 (/var/lib/vmd/elk_alert_4xx.cooldown) 로 30분 dedup
#       — 5xx 와 독립 (각자 임계 / 발사 이력)
#
#  실행:
#    sudo bash /opt/vmd/source/infra/scripts/elk_alert_4xx.sh
#
#  cron 등록 (5분 주기, root):
#    /etc/cron.d/vmd-elk-alerts:
#      */5 * * * * root /opt/vmd/source/infra/scripts/elk_alert_4xx.sh \
#                       >> /var/log/vmd-elk-alert.log 2>&1
#
#  의존:
#    - /opt/vmd/source/infra/env/prod.env (ELASTIC_PASSWORD, MM_WEBHOOK_ALERT)
#    - vmd-elasticsearch 컨테이너 healthy
#    - jq, curl (호스트 / 컨테이너)
#
#  종료 코드:
#    0 = 정상 (임계 미달 또는 cooldown 으로 알림 skip 또는 알림 발송 성공)
#    1 = 환경 / 의존성 문제 (prod.env 누락, ES 연결 실패 등)
#    2 = mm webhook 호출 실패
# =============================================================================

set -euo pipefail

# ── 설정 (plan §D2 결정) ────────────────────────────────────────────────────
WINDOW_MIN=5                                        # 5분 윈도우
THRESHOLD=20                                        # 5분당 20건 이상 → 알림 (4xx 자연 발생량 고려)
COOLDOWN_MIN=30                                     # 30분 동안 동일 알림 1회만
ES_CONTAINER="vmd-elasticsearch"
INDEX_PATTERN="vmd-logs"
PROD_ENV="/opt/vmd/source/infra/env/prod.env"
COOLDOWN_FILE="/var/lib/vmd/elk_alert_4xx.cooldown"
KIBANA_URL="https://do-goproject.com/kibana/"

# 제외 status 목록 (plan §D1 결정):
#   - 401: 토큰 만료 / 미인증 routine
#   - 404: 봇 / 크롤러 노이즈 (path burst 처리는 v2 분리)
#   - 429: rate limit — 정상 동작, 거의 0건 (24h 1건)
EXCLUDED_STATUSES="[401, 404, 429]"

# ── 의존성 / 환경 검증 ──────────────────────────────────────────────────────
if [[ ! -f "$PROD_ENV" ]]; then
  echo "ERROR: prod.env not found at $PROD_ENV" >&2
  exit 1
fi
# 필요한 키만 grep 으로 추출 (elk_alert_5xx.sh 와 동일 정책 — prod.env 의 quirky
# 라인이 source 시 깨질 수 있어 명시적 키 안전 추출).
ELASTIC_PASSWORD=$(grep -E "^ELASTIC_PASSWORD=" "$PROD_ENV" | head -1 | cut -d= -f2- || true)
MM_WEBHOOK_ALERT=$(grep -E "^MM_WEBHOOK_ALERT=" "$PROD_ENV" | head -1 | cut -d= -f2- || true)

if [[ -z "${ELASTIC_PASSWORD:-}" ]]; then
  echo "ERROR: ELASTIC_PASSWORD missing in prod.env" >&2
  exit 1
fi
if [[ -z "${MM_WEBHOOK_ALERT:-}" ]]; then
  echo "ERROR: MM_WEBHOOK_ALERT missing in prod.env" >&2
  exit 1
fi

if ! docker inspect -f '{{.State.Status}}' "$ES_CONTAINER" 2>/dev/null | grep -q running; then
  echo "ERROR: $ES_CONTAINER not running — alert check skipped" >&2
  exit 1
fi

mkdir -p "$(dirname "$COOLDOWN_FILE")"

# ── 4xx 카운트 쿼리 (제외 status 빼고) ──────────────────────────────────────
# nginx.status 는 filebeat dissect 가 채워준 long 필드 (Phase 2.1).
# bool.filter (범위) + bool.must_not (제외) 조합.
QUERY=$(cat <<EOF
{
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-${WINDOW_MIN}m"}}},
        {"range": {"nginx.status": {"gte": 400, "lt": 500}}}
      ],
      "must_not": [
        {"terms": {"nginx.status": ${EXCLUDED_STATUSES}}}
      ]
    }
  }
}
EOF
)

RESPONSE=$(docker exec -i "$ES_CONTAINER" curl -fsS \
  -u "elastic:${ELASTIC_PASSWORD}" \
  -H 'Content-Type: application/json' \
  "http://localhost:9200/${INDEX_PATTERN}/_count" \
  -d "$QUERY" 2>&1) || {
    echo "ERROR: ES query failed: $RESPONSE" >&2
    exit 1
  }

COUNT=$(echo "$RESPONSE" | jq -r '.count // 0')

echo "[$(date -Iseconds)] 4xx count last ${WINDOW_MIN}m = ${COUNT} (threshold ${THRESHOLD}, excluded ${EXCLUDED_STATUSES})"

# ── 임계 미달이면 종료 ──────────────────────────────────────────────────────
if (( COUNT < THRESHOLD )); then
  exit 0
fi

# ── cooldown 검사 (30분 내 발송 이력 있으면 skip) ───────────────────────────
NOW=$(date +%s)
COOLDOWN_SEC=$(( COOLDOWN_MIN * 60 ))

if [[ -f "$COOLDOWN_FILE" ]]; then
  LAST=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)
  ELAPSED=$(( NOW - LAST ))
  if (( ELAPSED < COOLDOWN_SEC )); then
    REMAIN=$(( COOLDOWN_SEC - ELAPSED ))
    echo "  → ALERT suppressed (cooldown: ${REMAIN}s remaining)"
    exit 0
  fi
fi

# ── mm webhook 발송 ─────────────────────────────────────────────────────────
# attachment.title 의 emoji shortcode 는 mm 가 렌더링 안 함 (icon_emoji 만 렌더).
# 따라서 title 에는 Unicode emoji 직접 임베드.
PAYLOAD=$(cat <<EOF
{
  "username": "VMD-Alert",
  "icon_emoji": ":warning:",
  "attachments": [{
    "color": "#ff8c00",
    "title": "⚠️ 4xx 임계 초과",
    "title_link": "${KIBANA_URL}",
    "text": "최근 ${WINDOW_MIN}분간 **${COUNT}건** 의 4xx 응답 발생 (임계 ${THRESHOLD}). 제외: 401/404/429. Kibana 에서 상세 확인.",
    "fields": [
      {"title": "Host", "value": "$(hostname)", "short": true},
      {"title": "Window", "value": "${WINDOW_MIN}m", "short": true},
      {"title": "Excluded", "value": "401, 404, 429", "short": true},
      {"title": "Threshold", "value": "${THRESHOLD}", "short": true}
    ]
  }]
}
EOF
)

if curl -fsS -X POST -H 'Content-Type: application/json; charset=utf-8' \
     --data "$PAYLOAD" "$MM_WEBHOOK_ALERT" >/dev/null; then
  echo "$NOW" > "$COOLDOWN_FILE"
  echo "  → ALERT sent to mm-webhook-alert (cooldown until $(date -d "@$(( NOW + COOLDOWN_SEC ))" -Iseconds))"
else
  echo "ERROR: mm webhook POST failed" >&2
  exit 2
fi
