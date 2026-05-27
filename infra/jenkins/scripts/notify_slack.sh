#!/usr/bin/env bash
# =============================================================================
#  notify_slack.sh — Slack Incoming Webhook 메시지 전송 (curl 기반)
# -----------------------------------------------------------------------------
#  설계 근거: vmd_infra_plan.md §4.3 post 블록 (slackSend 대체)
#
#  사용:
#    bash infra/jenkins/scripts/notify_slack.sh <color> <message...>
#
#  색상:
#    good     : 초록   (성공)
#    danger   : 빨강   (실패/롤백)
#    warning  : 노랑   (주의/시작/승인 요청)
#    #RRGGBB  : 커스텀 hex
#
#  예시:
#    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... \
#      bash notify_slack.sh good "Deploy OK tag=abc1234"
#
#    bash notify_slack.sh danger "Rollback: traffic restored to blue"
#    bash notify_slack.sh '#36a64f' "Custom color message"
#
#  환경변수:
#    SLACK_WEBHOOK_URL   (필수) Slack Incoming Webhook URL
#    SLACK_USERNAME      (선택) 봇 이름            (기본: Jenkins)
#    SLACK_ICON_EMOJI    (선택) 봇 아이콘 이모지    (기본: :rocket:)
#    SLACK_CHANNEL       (선택) 채널 override      (기본: webhook 기본 채널)
#    SLACK_MAX_TIME      (선택) curl --max-time    (기본: 10)
#    SLACK_DRY_RUN       (선택) 1 이면 전송하지 않고 payload 만 출력
#
#  종료 코드:
#    0   전송 성공 (HTTP 2xx)
#    2   잘못된 인자
#    3   SLACK_WEBHOOK_URL 미설정
#    4   webhook 응답 오류 (non-2xx 또는 curl 실패)
# =============================================================================

set -euo pipefail


# ─── usage ──────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: notify_slack.sh <color> <message...>

Colors:
  good | danger | warning | #RRGGBB

Required env:
  SLACK_WEBHOOK_URL

Optional env:
  SLACK_USERNAME     (default: Jenkins)
  SLACK_ICON_EMOJI   (default: :rocket:)
  SLACK_CHANNEL      (override webhook default channel)
  SLACK_MAX_TIME     (default: 10 seconds)
  SLACK_DRY_RUN      (1 = print payload and exit 0)
EOF
}


# ─── arg parse ──────────────────────────────────────────────────────────────
if (( $# < 1 )); then
  usage >&2; exit 2
fi

case "$1" in
  -h|--help) usage; exit 0 ;;
esac

if (( $# < 2 )); then
  echo "ERROR: message is required" >&2
  usage >&2
  exit 2
fi

COLOR="$1"; shift
MESSAGE="$*"

# 허용 팔레트: good / danger / warning / #RRGGBB
case "$COLOR" in
  good|danger|warning) ;;
  \#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;;
  *)
    echo "ERROR: invalid color '$COLOR' (expected good | danger | warning | #RRGGBB)" >&2
    exit 2
    ;;
esac


# ─── config ─────────────────────────────────────────────────────────────────
USERNAME="${SLACK_USERNAME:-Jenkins}"
ICON_EMOJI="${SLACK_ICON_EMOJI:-:rocket:}"
MAX_TIME="${SLACK_MAX_TIME:-10}"
DRY_RUN="${SLACK_DRY_RUN:-0}"

if [[ -z "${SLACK_WEBHOOK_URL:-}" ]]; then
  echo "ERROR: SLACK_WEBHOOK_URL is not set" >&2
  exit 3
fi


# ─── helpers ────────────────────────────────────────────────────────────────
log() { printf '[notify_slack][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
err() { printf '[notify_slack][ERROR][%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# JSON 문자열 이스케이프 (", \, control chars)
json_escape() {
  local s=$1
  s=${s//\\/\\\\}       # backslash 먼저
  s=${s//\"/\\\"}       # double-quote
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  # 0x00~0x1f 중 \n\r\t 외 제어문자 제거
  s=$(printf '%s' "$s" | LC_ALL=C tr -d '\000-\010\013\014\016-\037')
  printf '%s' "$s"
}


# ─── build payload ──────────────────────────────────────────────────────────
TEXT_ESC=$(json_escape "$MESSAGE")
USER_ESC=$(json_escape "$USERNAME")
ICON_ESC=$(json_escape "$ICON_EMOJI")
TS=$(date +%s)

if [[ -n "${SLACK_CHANNEL:-}" ]]; then
  CHANNEL_ESC=$(json_escape "$SLACK_CHANNEL")
  CHANNEL_FIELD=",\"channel\":\"${CHANNEL_ESC}\""
else
  CHANNEL_FIELD=""
fi

PAYLOAD=$(cat <<EOF
{"username":"${USER_ESC}","icon_emoji":"${ICON_ESC}"${CHANNEL_FIELD},"attachments":[{"color":"${COLOR}","text":"${TEXT_ESC}","ts":${TS}}]}
EOF
)


# ─── dry-run ────────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN=1 — payload only:"
  printf '%s\n' "$PAYLOAD"
  exit 0
fi


# ─── send ───────────────────────────────────────────────────────────────────
# curl -f 는 HTTP >=400 을 실패로 처리하지만, Slack 의 ok/ng 본문은 200 하에서만
# 파싱 가능하므로 직접 status code 를 확인한다.
HTTP_STATUS=$(curl -sS \
    --max-time "$MAX_TIME" \
    -o /tmp/notify_slack.out \
    -w '%{http_code}' \
    -X POST \
    -H 'Content-type: application/json; charset=utf-8' \
    --data "$PAYLOAD" \
    "$SLACK_WEBHOOK_URL" 2>/tmp/notify_slack.err) || {
  err "curl failed: $(cat /tmp/notify_slack.err 2>/dev/null || echo 'no stderr')"
  exit 4
}

case "$HTTP_STATUS" in
  2*)
    log "delivered  color=$COLOR  http=$HTTP_STATUS  bytes=${#MESSAGE}"
    exit 0
    ;;
  *)
    err "webhook returned HTTP $HTTP_STATUS"
    err "response body: $(cat /tmp/notify_slack.out 2>/dev/null || echo '(empty)')"
    exit 4
    ;;
esac
