#!/usr/bin/env bash
# =============================================================================
#  setup_swap.sh — Phase 0 선행 작업 (ELK 도입 전 호스트 튜닝)
# -----------------------------------------------------------------------------
#  설계 근거: infra/.ai/plan/observability_elk_plan.md §1, §5 Phase 0
#
#  목적:
#    1. swapfile 4GB 추가 — 호스트의 swap=0 이슈 완화 (메모리 압박 시 OOM 방어)
#    2. vm.swappiness=10 — 가능한 swap 회피, 정말 필요할 때만 사용
#    3. vm.max_map_count=262144 — Elasticsearch 의 강제 요구사항 (boot fail 방지)
#
#  실행:
#    sudo bash /opt/vmd/source/infra/scripts/setup_swap.sh
#
#  멱등성: 이미 적용된 설정은 skip. 여러 번 실행 안전.
#  ※ 호스트 root 권한 필요. 본 스크립트는 Jenkins Job 에서 자동 호출하지 않음.
#     (Jenkins 가 sudo 로 호스트 sysctl 건드리는 건 권한 모델 위반)
# =============================================================================

set -euo pipefail

SWAPFILE=/swapfile
SIZE_MB=4096

# ── root 권한 확인 ───────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root — try: sudo bash $0" >&2
  exit 1
fi

echo "===== [Phase 0] VMD swap + sysctl setup ====="
echo

# ── 1) swapfile 생성 ────────────────────────────────────────────────────────
if [[ -f $SWAPFILE ]]; then
  echo "[1/4] swapfile already exists at $SWAPFILE — skip create"
else
  echo "[1/4] creating $SWAPFILE (${SIZE_MB}MB)"
  fallocate -l "${SIZE_MB}M" "$SWAPFILE"
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE" >/dev/null
  swapon "$SWAPFILE"
fi

# ── 2) /etc/fstab 영속화 ────────────────────────────────────────────────────
if grep -qE "^${SWAPFILE} " /etc/fstab; then
  echo "[2/4] fstab entry already present — skip"
else
  echo "[2/4] adding fstab entry for $SWAPFILE"
  echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# ── 3) vm.swappiness=10 (sysctl + persist) ──────────────────────────────────
echo "[3/4] vm.swappiness=10"
sysctl -w vm.swappiness=10 >/dev/null
if grep -qE "^vm\.swappiness" /etc/sysctl.conf; then
  sed -i 's/^vm\.swappiness.*/vm.swappiness=10/' /etc/sysctl.conf
else
  echo "vm.swappiness=10" >> /etc/sysctl.conf
fi

# ── 4) vm.max_map_count=262144 (ES 필수) ────────────────────────────────────
echo "[4/4] vm.max_map_count=262144 (Elasticsearch requirement)"
sysctl -w vm.max_map_count=262144 >/dev/null
if grep -qE "^vm\.max_map_count" /etc/sysctl.conf; then
  sed -i 's/^vm\.max_map_count.*/vm.max_map_count=262144/' /etc/sysctl.conf
else
  echo "vm.max_map_count=262144" >> /etc/sysctl.conf
fi

# ── 검증 출력 ────────────────────────────────────────────────────────────────
echo
echo "===== 결과 ====="
free -h
echo
sysctl vm.swappiness vm.max_map_count
echo
echo "✓ Phase 0 complete. ES/Kibana/Filebeat 컨테이너 기동 가능 상태."
