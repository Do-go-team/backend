"""Router for the "assets" domain — 3D asset upload + generation task pipeline.

엔드포인트 (모두 /api/v1/assets prefix 하위):
  POST   /assets/3d                  — 이미 생성된 3D 파일 직접 업로드 (admin/test)
  POST   /assets/3d-tasks            — task 생성 (PENDING)
  GET    /assets/3d-tasks/{task_id}  — task 상태 조회 (FE polling)
  POST   /assets/3d-tasks/claim      — GPU Worker 가 PENDING 1건 가져감
  POST   /assets/3d-tasks/{id}/complete — GPU Worker 가 .ply 업로드 + COMPLETED
  POST   /assets/3d-tasks/{id}/fail  — GPU Worker 실패 보고 + FAILED

인증:
  - /assets/3d 직접 업로드: JWTAuth (기존 프로젝트 user-facing 컨벤션과 일관).
    추후 STAFF/OWNER 권한 검증을 더 좁게 붙이는 건 service layer 에 분리.
  - /assets/3d-tasks/* 는 내부 GPU Worker 가 호출 — 일단 무인증.
    추후 X-GPU-WORKER-TOKEN 같은 헤더 인증으로 좁힐 수 있게 endpoint 별 auth 만
    바꾸면 되도록 구조 유지.
"""

from ninja import File, Form, Router
from ninja.files import UploadedFile
from ninja_jwt.authentication import JWTAuth

from common.response import ok

from .schemas import (
    Asset3DTaskClaimRequest,
    Asset3DTaskCreateRequest,
    Asset3DTaskFailRequest,
)
from .services import (
    claim_task,
    complete_task,
    create_task,
    fail_task,
    get_task,
    save_asset_3d_file,
)


router = Router(tags=["assets"])


@router.post("/3d", auth=JWTAuth())
def asset_3d_upload(
    request,
    target_type: str = Form(...),
    target_id: int = Form(...),
    file: UploadedFile = File(...),
):
    """이미 생성된 3D 파일을 직접 업로드해 Asset3D row 만 만든다 (task 미경유).

    파일 저장 + Asset3D row 생성 로직은 /assets/3d-tasks/{id}/complete 와
    공유 (services.save_asset_3d_file).
    """
    asset = save_asset_3d_file(
        target_type=target_type,
        target_id=target_id,
        upload_file=file,
    )
    return ok(
        data={
            "asset_id": asset.id,
            "target_type": asset.target_type,
            "target_id": asset.target_id,
            "file_format": asset.file_format,
            "model_url": asset.model_url,
            "file_size_bytes": asset.file_size_bytes,
        },
        message="3D 에셋 파일이 성공적으로 업로드되었습니다.",
    )


@router.post("/3d-tasks")
def asset_task_create(request, payload: Asset3DTaskCreateRequest):
    task = create_task(
        target_type=payload.target_type,
        target_id=payload.target_id,
        source_image_url=payload.source_image_url,
    )
    return ok(
        data={"task_id": task.id, "status": task.status},
        message="3D 생성 작업이 등록되었습니다.",
    )


@router.post("/3d-tasks/claim")
def asset_task_claim(request, payload: Asset3DTaskClaimRequest):
    """GPU Worker polling endpoint.

    PENDING 1건을 select_for_update(skip_locked=True) 로 lock 하여 PROCESSING 으로
    전이. 비어 있으면 {"task": null} (worker 가 None 분기 처리).
    응답은 mock worker 호환을 위해 snake_case + camelCase 둘 다 포함.
    """
    claimed = claim_task(worker_id=payload.worker_id)
    if claimed is None:
        return ok(data={"task": None}, message="대기 중인 3D 작업이 없습니다.")
    return ok(data=claimed, message="3D 작업을 점유했습니다.")


@router.post("/3d-tasks/{task_id}/complete")
def asset_task_complete(
    request,
    task_id: int,
    workerId: str = Form(...),
    file: UploadedFile = File(...),
):
    """GPU Worker 가 .ply 업로드. multipart/form-data.

    - workerId: claim 시 받은 worker 식별자 (camelCase — GPU Worker mock 호환).
    - file: .ply 파일 (~20MB 가능 → service 가 chunk 저장).
    """
    data = complete_task(task_id=task_id, worker_id=workerId, upload_file=file)
    return ok(data=data, message="3D 작업이 성공적으로 완료되었습니다.")


@router.post("/3d-tasks/{task_id}/fail")
def asset_task_fail(request, task_id: int, payload: Asset3DTaskFailRequest):
    data = fail_task(
        task_id=task_id,
        worker_id=payload.worker_id,
        error_message=payload.error_message,
    )
    return ok(data=data, message="3D 작업이 실패 처리되었습니다.")


@router.get("/3d-tasks/{task_id}")
def asset_task_status(request, task_id: int):
    task = get_task(task_id)
    return ok(
        data={
            "task_id": task.id,
            "target_type": task.target_type,
            "target_id": task.target_id,
            "source_image_url": task.source_image_url,
            "status": task.status,
            "result_url": task.result_url,
            "asset_3d_id": task.asset_3d_id,
            "error_message": task.error_message,
        },
        message="3D 작업 상태 조회에 성공했습니다.",
    )
