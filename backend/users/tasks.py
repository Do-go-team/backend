from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail


@shared_task(
    bind=True,
    autoretry_for=(SMTPException,),
    retry_backoff=2,
    max_retries=3,
)
def send_verification_email(self, email: str, code: str) -> None:
    """이메일 인증 코드 발송.

    SMTPException (Gmail/SMTP 일시 오류) 발생 시 지수 backoff 로 3회 재시도.
    최초 1회 + 재시도 3회 = 총 4회 시도. backoff 간격 2→4→8초 (총 ~14초).
    그 외 예외(코드 버그 등)는 즉시 실패 — 재시도해도 같은 결과라 의미 없음.
    """
    ttl_minutes = max(1, settings.EMAIL_VERIFICATION["CODE_TTL"] // 60)
    subject = "[DoGo] 이메일 인증 번호"
    body = (
        f"요청하신 인증 번호는 {code} 입니다.\n"
        f"이 번호는 {ttl_minutes}분 동안 유효합니다.\n"
        f"본인이 요청하지 않은 경우 이 메일을 무시해 주세요."
    )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
