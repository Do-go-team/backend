# `infra/env/` — 환경 변수 / 시크릿 관리 가이드

본 디렉토리는 VMD 시스템의 **환경별 환경변수 템플릿**과 **시크릿 운영 룰**을 담는다.
실제 시크릿 값은 어떠한 형태로도 git에 커밋되지 않는다.

---

## 1. 디렉토리 파일

| 파일 | 용도 | git 추적 여부 |
|---|---|---|
| `prod.env.example` | 프로덕션용 키 목록 템플릿 (값 비어 있음) | ✅ 추적 |
| `staging.env.example` | 스테이징용 키 목록 템플릿 (값 비어 있음) | ✅ 추적 |
| `prod.env` | **실제 프로덕션 시크릿** | ❌ `.gitignore` 의 `.env.*` 룰로 차단 |
| `staging.env` | **실제 스테이징 시크릿** | ❌ 동일 |
| `README.md` | 본 문서 | ✅ 추적 |

> `*.env.example` 만 추적, `*.env` 는 추적 금지 — 이 원칙은 절대 깨지면 안 된다.

---

## 2. 신규 팀원 온보딩 절차

```bash
# 1) 템플릿 복사
cp infra/env/prod.env.example    infra/env/prod.env       # 운영 권한자만
cp infra/env/staging.env.example infra/env/staging.env

# 2) 시크릿 값 채우기
#    - 프로젝트 시크릿 매니저(또는 운영자) 로부터 값을 전달받아 채운다.
#    - 절대 메신저/이메일/Notion 평문에 남기지 않는다.

# 3) 권한 제한
chmod 600 infra/env/prod.env infra/env/staging.env
```

> **운영자 외에는 `prod.env` 에 접근하지 않는다.** 개발자는 `staging.env` 까지만 보유.

---

## 3. 변수 영역 분류 (요약)

`*.env.example` 에 동일한 번호로 그룹핑되어 있다. 새 키를 추가할 때는 같은 영역 번호 안에 넣고, 두 환경 파일에 모두 반영한다.

| # | 영역 | 대표 키 |
|---|---|---|
| 0  | Compose Meta              | `COMPOSE_PROJECT_NAME`, `ENVIRONMENT`, `TZ` |
| 1  | Container Registry / Tag  | `REGISTRY`, `IMAGE_TAG_BLUE`, `IMAGE_TAG_GREEN` |
| 2  | Django                    | `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `GUNICORN_WORKERS` |
| 3  | PostgreSQL                | `POSTGRES_*`, `DATABASE_URL` |
| 4  | Redis                     | `REDIS_*`, `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` |
| 5  | Celery                    | `CELERY_TASK_*`, 큐 이름 |
| 6  | YOLOv11-seg / AI          | `YOLO_*`, `OMP_NUM_THREADS`, Phase 2 마이그레이션 후보 키 |
| 7  | AWS (S3 + CloudFront)     | `AWS_*`, `AWS_S3_*`, `AWS_CLOUDFRONT_*` |
| 8  | Google OAuth              | `GOOGLE_OAUTH_*` |
| 9  | JWT                       | `JWT_*` |
| 10 | Security / CORS           | `CORS_ALLOWED_ORIGINS`, `*_COOKIE_SECURE` |
| 11 | Observability             | `SENTRY_*`, `LOG_LEVEL` |
| 12 | Frontend Build args       | `VITE_*` |
| 13 | Backup                    | `BACKUP_*` |
| 14 | E2E (staging only)        | `E2E_*` |

---

## 4. 시크릿 분류와 보관처

시크릿은 민감도에 따라 **세 등급**으로 나누고 보관처를 분리한다.

| 등급 | 예시 | 1차 보관처 | 백업 |
|---|---|---|---|
| **Tier S — 치명적** | `DJANGO_SECRET_KEY`, `JWT_SIGNING_KEY`, `AWS_SECRET_ACCESS_KEY`, `AWS_CLOUDFRONT_PRIVATE_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GOOGLE_OAUTH_CLIENT_SECRET` | **Jenkins Credentials** + 수동 회전 | 운영 책임자 1인의 오프라인 백업(암호화 USB / 1Password Vault) |
| **Tier A — 노출 시 영향 큼** | `SENTRY_DSN`, `JENKINS_TOKEN`, `GITLAB_REGISTRY_TOKEN`, OAuth Client ID | Jenkins Credentials | 동일 |
| **Tier B — 비밀이지만 장기 식별자** | `AWS_ACCESS_KEY_ID`, `AWS_CLOUDFRONT_KEY_PAIR_ID`, `AWS_S3_BUCKET` 이름 | `*.env` 평문 (호스트 600 권한) | 운영 문서 |

### 권장 시크릿 매니저 (택1)

- **AWS Secrets Manager** — IAM 통합, 자동 회전. Phase 2 진입 시 권장.
- **AWS Systems Manager Parameter Store (SecureString)** — 무료, KMS 암호화. Phase 1 추천.
- **HashiCorp Vault (self-host)** — 멀티 클라우드 시.
- **1Password Business / Bitwarden** — 사람-친화적, 팀 공유 쉬움.

---

## 5. Compose / Jenkins 에서 주입하는 방식

### 5.1 Compose

`docker-compose.prod.yml` 의 각 서비스에서:

```yaml
env_file:
  - ../env/prod.env
```

추가로 **Tier S** 시크릿은 `env_file` 대신 docker secret 또는 호스트의 `/run/secrets/*` bind mount 권장.

### 5.2 Jenkins

`Jenkinsfile` 안에서:

```groovy
withCredentials([
    string(credentialsId: 'vmd-prod-postgres-password', variable: 'POSTGRES_PASSWORD'),
    string(credentialsId: 'vmd-prod-django-secret-key', variable: 'DJANGO_SECRET_KEY'),
    file(credentialsId:   'vmd-prod-cf-private-key',   variable: 'CF_PRIVATE_KEY')
]) {
    sh 'envsubst < infra/env/prod.env.template > /opt/vmd/env/prod.env'
}
```

> 주의: `withCredentials` 블록 밖으로 변수 값을 echo 하지 않는다 (콘솔 로그에 누출 위험).

### 5.3 GitLab CI

GitLab → Settings → CI/CD → Variables 에 등록.
- `PROTECTED` + `MASKED` 옵션 모두 ON.
- `Environment scope` 로 `production` / `staging` 분리.

---

## 6. 시크릿 회전 (Rotation) 정책

| 시크릿 | 권장 회전 주기 | 트리거 |
|---|---|---|
| `DJANGO_SECRET_KEY` | 6개월 | 키 노출 의심 시 즉시 |
| `JWT_SIGNING_KEY` | 3개월 | (회전 시 dual-key window 필요) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 90일 | 최소 권한 점검 동시 수행 |
| `POSTGRES_PASSWORD` | 90일 | DB 사용자별 분리 권장 |
| `REDIS_PASSWORD` | 180일 | - |
| `GOOGLE_OAUTH_CLIENT_SECRET` | 1년 | Google Cloud Console 통지 시 |
| `JENKINS_TOKEN` | 90일 | 사용자 이탈 시 즉시 |
| Lightsail SSH 키 | 90일 | Jenkins Credentials 등록과 동시 회전 |

---

## 7. Phase 2 마이그레이션 시 추가 키

리서치/계획 문서 §2.3 ⚠️ 박스에 따라 **AI 모델 가중치는 호스트 bind mount → S3 다운로드 패턴으로 반드시 이전**해야 한다.
이전 시 다음 키들이 활성화된다 (`*.env.example` 에 주석으로 미리 박혀 있음):

```
YOLO_MODEL_S3_URI       # s3://vmd-models/yolov11-seg/v1.2.3/weights.pt
YOLO_MODEL_VERSION      # 캐시 키
YOLO_MODEL_SHA256       # 무결성 검증
YOLO_MODEL_CACHE_DIR    # 컨테이너 내부 캐시 경로
```

또한 다음 키는 Phase 2 (RDS 분리) 진입 시 활성화 검토:
```
DATABASE_SSLMODE=require
DATABASE_CONN_MAX_AGE=600
```

---

## 8. 절대 하지 말 것 (Hard Rules)

1. `*.env`(실값 파일)을 git add 하지 않는다. (실수 방지를 위해 pre-commit hook 권장)
2. 시크릿을 메신저/이메일/티켓/PR 본문/스크린샷에 평문으로 남기지 않는다.
3. `prod.env` 와 `staging.env` 의 시크릿을 공유하지 않는다 (도메인/계정/버킷 모두 분리).
4. 시크릿 값이 노출된 경우 **즉시 회전**하고 사고 보고 (ticket + 운영 책임자 공지).
5. `env/` 하위에 시크릿 외 운영 메모를 적지 않는다 (검색/감사가 어려워짐). 운영 메모는 `infra/.ai/` 또는 Notion.

---

## 9. 검증 체크리스트 (PR 머지 / 배포 전)

- [ ] `*.env.example` 에 새 키가 추가되었는가? 두 환경(`prod`, `staging`) 모두?
- [ ] 새 키의 등급(Tier S/A/B) 분류가 명시되었는가?
- [ ] 평문 시크릿이 diff 에 포함되어 있지 않은가? (`git diff --cached | grep -E '(SECRET|PASSWORD|TOKEN|KEY).*=.+'`)
- [ ] `staging.env` / `prod.env` 가 stash/work-in-progress 로 git 상태에 잡혀있지 않은가?
- [ ] 새 키를 사용하는 코드가 누락 시 fail-fast 하는가? (런타임 분기 X)