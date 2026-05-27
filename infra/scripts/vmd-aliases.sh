# =============================================================================
#  VMD Server Aliases — 운영 호스트(EC2)에서 자주 쓰는 명령 단축
# -----------------------------------------------------------------------------
#  활성화 (1회만):
#    echo 'source /opt/vmd/source/infra/scripts/vmd-aliases.sh' >> ~/.bashrc
#    source ~/.bashrc
#
#  운영 소스(/opt/vmd/source)는 Jenkins 가 dev 브랜치와 자동 sync 하므로
#  새 alias 가 추가되면 새 셸을 열거나 `source ~/.bashrc` 하면 즉시 반영.
#
#  설계 원칙:
#    - 시크릿(REDIS_PASSWORD 등)은 export 하지 않음 — 함수 안에서만 즉석 사용.
#    - 파괴적 명령(restart / stop / prune / force-recreate)은 의도적으로
#      alias 로 만들지 않음 — 사고 방지 위해 풀 명령을 입력하게 둠.
#    - 각 alias 의 풀 명령 / 의도는 팀 가이드 문서 (별도 배포) 참조.
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# [공통] docker / 시스템 상태
# ─────────────────────────────────────────────────────────────────────────────

alias dps='sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias dpsa='sudo docker ps -a --format "table {{.Names}}\t{{.Status}}"'
alias dstats='sudo docker stats --no-stream'

# 로그 — 함수가 alias 보다 인자 처리에 유리
dlog()    { sudo docker logs "$1" --tail "${2:-100}"; }                   # dlog vmd-django-blue 200
dlogf()   { sudo docker logs "$1" -f; }                                   # dlogf vmd-ai-worker
dlogerr() { sudo docker logs "$1" --tail "${2:-500}" 2>&1 | grep -iE "error|traceback|exception"; }

# 컨테이너 진입 ($2 미지정 시 bash, alpine 계열은 dex <name> sh)
dex() { sudo docker exec -it "$1" "${2:-bash}"; }

# 호스트 종합 상태 (한 번에)
vmd-status() {
  echo "=== uptime ===";     uptime
  echo "=== mem ===";        free -h
  echo "=== disk ===";       df -h /
  echo "=== containers ==="; sudo docker ps --format "{{.Names}}: {{.Status}}"
}

# 활성 BE 색상 (Blue/Green) 확인
alias active-color='sudo readlink -f /opt/vmd/source/infra/nginx/conf/conf.d/00-upstream-active.conf'

# ─────────────────────────────────────────────────────────────────────────────
# [백엔드] Django / PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

alias psql-vmd='sudo docker exec -it vmd-postgres psql -U vmd_user -d vmd_db'
alias dj-shell='sudo docker exec -it vmd-django-blue python manage.py shell'
alias dj-mig='sudo docker exec vmd-django-blue python manage.py showmigrations | grep "\[ \]"'
alias dj-health='sudo docker exec vmd-django-blue curl -sf http://localhost:8000/api/v1/hello'

# psql 원샷 쿼리 (한 줄로 실행)
psqlc() { sudo docker exec vmd-postgres psql -U vmd_user -d vmd_db -c "$*"; }
# 예: psqlc "SELECT count(*) FROM users_user;"

# ─────────────────────────────────────────────────────────────────────────────
# [AI] Celery / 모델 / 큐
# ─────────────────────────────────────────────────────────────────────────────

alias aiping='sudo docker exec vmd-ai-worker celery -A ai_app inspect ping'
alias aiactive='sudo docker exec vmd-ai-worker celery -A ai_app inspect active'
alias aimem='sudo docker stats vmd-ai-worker --no-stream'
alias aimodel='sudo docker exec vmd-ai-worker ls -la /models/'

# Redis 비번을 prod.env 에서 매번 즉석 추출 (export 안 함 — 시크릿 잔류 방지)
aiq() {
  local pw
  pw=$(sudo grep -E '^REDIS_PASSWORD=' /opt/vmd/source/infra/env/prod.env | cut -d= -f2-)
  sudo docker exec vmd-redis redis-cli -a "$pw" -n 1 LLEN ai
}

# ─────────────────────────────────────────────────────────────────────────────
# [프론트] nginx
# ─────────────────────────────────────────────────────────────────────────────

alias ndist='sudo docker exec vmd-nginx ls -la /usr/share/nginx/html/'
alias nlog='sudo docker exec vmd-nginx tail -f /var/log/nginx/access.log'
alias nlog5xx='sudo docker exec vmd-nginx awk "\$9 ~ /^5/" /var/log/nginx/access.log | tail -50'
alias nlog4xx='sudo docker exec vmd-nginx awk "\$9 ~ /^4/" /var/log/nginx/access.log | tail -50'
alias nerr='sudo docker exec vmd-nginx tail -50 /var/log/nginx/error.log'
alias nginxt='sudo docker exec vmd-nginx nginx -t'
