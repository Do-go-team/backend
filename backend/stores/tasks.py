from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail


@shared_task
def send_store_invitation_email(
    invitee_email: str,
    store_name: str,
    inviter_name: str,
    invite_link: str,
) -> None:
    """매장 워크스페이스 초대 메일 발송.

    EMAIL_BACKEND 가 DEBUG=console / prod=SMTP 라 별도 backend 분기 불필요.
    초대장 row 자체는 호출처(create_store_invitation) 에서 미리 만들어두므로
    이 태스크는 발송만 책임짐 (실패해도 초대 자체는 유효 — 사용자가
    수동으로 링크 공유 가능).
    """
    subject = f"[DoGo] '{store_name}' 매장 워크스페이스 초대"
    body = (
        f"{inviter_name}님이 '{store_name}' 매장 워크스페이스에 회원님을 초대하셨습니다.\n\n"
        f"아래 링크로 24시간 안에 가입을 완료해 주세요:\n"
        f"{invite_link}\n\n"
        f"본인이 초대받은 적이 없다면 이 메일을 무시하셔도 됩니다."
    )
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invitee_email],
        fail_silently=False,
    )
