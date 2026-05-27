#!/usr/bin/env bash
# =============================================================================
#  elk_alert_5xx.sh — VMD 5xx 임계 초과 시 Mattermost 알림
# -----------------------------------------------------------------------------
#  설계 근거: infra/.ai/plan/observability_elk_phase2_plan.md §1.3
#
#  동작:
#    1. ES 에 최근 WINDOW_MIN 분간 nginx.status >= 500 카운트 쿼리
#    2. 임계 초과 시 mm webhook 호출 (별도 채널 — MM_WEBHOOK_ALERT)
#    3. cooldown 파일 (/var/lib/vmd/elk_alert_5xx.cooldown) 로 30분 dedup
#
#  실행:
#    sudo bash /opt/vmd/source/infra/scripts/elk_alert_5xx.sh
#
#  cron 등록 (5분 주기, root):
#    /etc/cron.d/vmd-elk-alerts:
#      */5 * * * * root /opt/vmd/source/infra/scripts/elk_alert_5xx.sh \
#                       >> /var/log/vmd-elk-alert.log 2>&1
#
#  의존:
#    - /opt/vmd/source/infra/env/prod.env (ELASTIC_PASSWORD, MM_WEBHOOK_ALERT)
#    - vmd-elasticsearch 컨테이너 healthy
#    - jq, curl (호스트 / 컨테이너)
#
#  ES 접근 패턴: docker exec vmd-elasticsearch curl ... (Phase 2 plan §2.2 옵션 B)
#  → ES 9200 포트를 호스트에 추가 publish 안 함 (보안 표면 최소).
#
#  종료 코드:
#    0 = 정상 (임계 미달 또는 cooldown 으로 알림 skip 또는 알림 발송 성공)
#    1 = 환경 / 의존성 문제 (prod.env 누락, ES 연결 실패 등)
#    2 = mm webhook 호출 실패
# =============================================================================

set -euo pipefail

# ── 설정 (plan §1.3 결정) ───────────────────────────────────────────────────
WINDOW_MIN=5                                        # 5분 윈도우
THRESHOLD=10                                        # 5분당 10건 이상 → 알림
COOLDOWN_MIN=30                                     # 30분 동안 동일 알림 1회만
ES_CONTAINER="vmd-elasticsearch"
# data stream 이름 직접 사용 (datastream-fix 후). backing index `.ds-vmd-logs-*` 는
# ES 가 자동 라우팅. 옛 legacy alias 패턴 `vmd-logs-*` 는 어떤 인덱스도 매칭 못 함.
INDEX_PATTERN="vmd-logs"
PROD_ENV="/opt/vmd/source/infra/env/prod.env"
COOLDOWN_FILE="/var/lib/vmd/elk_alert_5xx.cooldown"
KIBANA_URL="https://do-goproject.com/kibana/"

# ── 의존성 / 환경 검증 ──────────────────────────────────────────────────────
if [[ ! -f "$PROD_ENV" ]]; then
  echo "ERROR: prod.env not found at $PROD_ENV" >&2
  exit 1
fi
# 필요한 키만 grep 으로 추출. prod.env 전체 `source` 는 다른 라인의 quirky
# 문자 (특수 char / unquoted value) 가 bash 파싱을 깨면 cron 마다 실패하므로
# 본 스크립트가 실제 필요한 키 (ELASTIC_PASSWORD, MM_WEBHOOK_ALERT) 만 안전 추출.
# `|| true` — set -euo pipefail 환경에서 grep 0건 시 즉시 종료 막아 명시적
# ERROR 메시지에 도달.
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

# ── 5xx 카운트 쿼리 ─────────────────────────────────────────────────────────
# docker exec 로 컨테이너 내부 curl 실행 → ES 9200 호스트 노출 불필요.
# nginx.status 는 filebeat dissect 가 채워준 필드 (Phase 2.1).
QUERY=$(cat <<EOF
{
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-${WINDOW_MIN}m"}}},
        {"range": {"nginx.status": {"gte": 500}}}
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

# 정상 동작 로그 (cron 로그로 흘러감 — 운영 가시성)
echo "[$(date -Iseconds)] 5xx count last ${WINDOW_MIN}m = ${COUNT} (threshold ${THRESHOLD})"

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
PAYLOAD=$(cat <<EOF
{
  "username": "VMD-Alert",
  "icon_emoji": ":fire:",
  "attachments": [{
    "color": "#d62728",
    "title": "🔥 5xx 임계 초과",
    "title_link": "${KIBANA_URL}",
    "text": "최근 ${WINDOW_MIN}분간 **${COUNT}건** 의 5xx 응답 발생 (임계 ${THRESHOLD}). Kibana 에서 상세 확인.",
    "fields": [
      {"title": "Host", "value": "$(hostname)", "short": true},
      {"title": "Window", "value": "${WINDOW_MIN}m", "short": true}
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
