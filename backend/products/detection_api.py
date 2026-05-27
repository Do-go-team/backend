from ninja import Router

from auth.authentication import CookieJWTAuth
from common.response import ok

from .detection_schemas import (
    ProductDetectionCompleteIn,
    ProductDetectionFailIn,
    ProductDetectionGenerate3DIn,
)
from .detection_services import (
    complete_detection_task,
    fail_detection_task,
    generate_3d_for_detection_items,
    get_detection_task_detail,
    reject_detection_item,
)

router = Router(tags=["products"])


@router.get("/{task_id}", auth=CookieJWTAuth())
def get_detection_task(request, task_id: int, include_rejected: bool = True):
    return ok(
        data=get_detection_task_detail(
            user=request.auth,
            task_id=task_id,
            include_rejected=include_rejected,
        ),
        message="상품 탐지 작업 상세 조회에 성공했습니다.",
    )


@router.post("/{task_id}/complete")
def complete_detection_task_api(
    request,
    task_id: int,
    payload: ProductDetectionCompleteIn,
):
    # TODO: Restrict this endpoint with worker-only authentication.
    return ok(
        data=complete_detection_task(task_id=task_id, payload=payload),
        message="상품 탐지 작업 완료 콜백을 처리했습니다.",
    )


@router.post("/{task_id}/fail")
def fail_detection_task_api(request, task_id: int, payload: ProductDetectionFailIn):
    # TODO: Restrict this endpoint with worker-only authentication.
    return ok(
        data=fail_detection_task(task_id=task_id, payload=payload),
        message="상품 탐지 작업 실패 콜백을 처리했습니다.",
    )


@router.post("/{task_id}/generate-3d", auth=CookieJWTAuth())
def generate_3d_for_detection_task(
    request,
    task_id: int,
    payload: ProductDetectionGenerate3DIn,
):
    return ok(
        data=generate_3d_for_detection_items(
            user=request.auth,
            task_id=task_id,
            payload=payload,
        ),
        message="선택된 탐지 항목의 3D 생성 요청을 등록했습니다.",
    )


@router.patch("/{task_id}/items/{item_id}/reject", auth=CookieJWTAuth())
def reject_detection_task_item(request, task_id: int, item_id: int):
    return ok(
        data=reject_detection_item(
            user=request.auth,
            task_id=task_id,
            item_id=item_id,
        ),
        message="탐지 상품 후보가 제외 처리되었습니다.",
    )
