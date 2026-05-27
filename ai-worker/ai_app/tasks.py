from io import BytesIO
import os
from pathlib import Path
from urllib.request import urlopen

import cv2
import numpy as np
import requests
from PIL import Image

from .celery import app as celery_app
from .floorplan_parse import parse_image as _parse_floorplan_image
from .model import get_model

MIN_CONFIDENCE = 0.3
MIN_MASK_AREA_RATIO = 0.00003
MIN_BBOX_AREA_RATIO = 0.00003
BORDER_MARGIN = 3

BASE_DIR = Path(__file__).resolve().parent.parent
THUMBNAIL_DIR = BASE_DIR / "thumbnails"
THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
YOLO_DEVICE = os.getenv("YOLO_DEVICE", "cpu").strip()
YOLO_CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.5").strip())
YOLO_IOU_THRESHOLD = float(os.getenv("YOLO_IOU_THRESHOLD", "0.7").strip())
YOLO_IMG_SIZE = int(os.getenv("YOLO_IMG_SIZE", "1024").strip())

THUMBNAIL_PADDING = 24
CALLBACK_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 60


def evaluate_detection(
    confidence: float,
    bbox_xyxy: list[float],
    mask_area: int | None,
    image_width: int,
    image_height: int,
):
    image_area = image_width * image_height

    x1, y1, x2, y2 = bbox_xyxy
    bbox_width = max(0.0, x2 - x1)
    bbox_height = max(0.0, y2 - y1)
    bbox_area = bbox_width * bbox_height

    mask_area_ratio = (mask_area / image_area) if mask_area is not None else 0.0
    bbox_area_ratio = bbox_area / image_area if image_area > 0 else 0.0

    touches_border = (
        x1 <= BORDER_MARGIN
        or y1 <= BORDER_MARGIN
        or x2 >= image_width - BORDER_MARGIN
        or y2 >= image_height - BORDER_MARGIN
    )

    reject_reasons = []

    if confidence < MIN_CONFIDENCE:
        reject_reasons.append("LOW_CONFIDENCE")

    if mask_area_ratio < MIN_MASK_AREA_RATIO:
        reject_reasons.append("SMALL_MASK_AREA")

    if bbox_area_ratio < MIN_BBOX_AREA_RATIO:
        reject_reasons.append("SMALL_BBOX_AREA")

    if touches_border:
        reject_reasons.append("TOUCHES_BORDER")

    accepted = len(reject_reasons) == 0

    return {
        "accepted": accepted,
        "reject_reasons": reject_reasons,
        "metrics": {
            "mask_area_ratio": round(mask_area_ratio, 6),
            "bbox_area_ratio": round(bbox_area_ratio, 6),
            "touches_border": touches_border,
            "bbox_width": round(bbox_width, 2),
            "bbox_height": round(bbox_height, 2),
        },
    }


def create_thumbnail(image_np, polygon, filename, idx, padding=THUMBNAIL_PADDING):
    try:
        rgba = _render_thumbnail_rgba(image_np, polygon, padding=padding)
        if rgba is None:
            return None

        save_path = THUMBNAIL_DIR / f"{filename}_thumb_{idx}.png"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgba, mode="RGBA").save(save_path)
        return str(save_path)

    except Exception as exc:
        print(f"[ERROR] Thumbnail creation failed: {exc}")
        return None


def _render_thumbnail_rgba(image_np, polygon, padding=THUMBNAIL_PADDING):
    if not polygon or len(polygon) < 3:
        return None

    h, w, _ = image_np.shape
    pts = np.array(polygon, dtype=np.float32)

    x, y, bw, bh = cv2.boundingRect(pts.astype(np.int32))
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(w, x + bw + padding)
    y2 = min(h, y + bh + padding)

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts.astype(np.int32)], 255)

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    mask = cv2.GaussianBlur(mask, (3, 3), 0)

    cropped_rgb = image_np[y1:y2, x1:x2]
    cropped_alpha = mask[y1:y2, x1:x2]

    if cropped_rgb.size == 0 or cropped_alpha.size == 0:
        return None

    return np.dstack([cropped_rgb, cropped_alpha]).astype(np.uint8)


def _create_thumbnail_png_bytes(image_np, polygon, padding=THUMBNAIL_PADDING):
    rgba = _render_thumbnail_rgba(image_np, polygon, padding=padding)
    if rgba is None:
        return None

    buffer = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def _read_image_arrays_from_url(image_url: str):
    """Load an image as cv2-compatible BGR + thumbnail-ready RGB arrays.

    YOLO inference must run on the same byte-exact pixels that ultralytics
    produces via `source=path` (which internally uses cv2). PIL JPEG decoding
    yields subtly different pixel values that measurably collapse detection
    count and confidence on this segmentation model, so the URL flow now
    decodes through cv2 as well.

    - http(s) URL  -> urlopen bytes -> cv2.imdecode (BGR)
    - file:// URL  -> strip scheme, cv2.imread the path
    - local path   -> cv2.imread directly

    Returns (image_bgr, image_rgb). YOLO consumes image_bgr; thumbnail
    rendering consumes image_rgb so PNG colors stay correct.
    """
    if image_url.startswith(("http://", "https://")):
        with urlopen(image_url, timeout=30) as response:
            image_bytes = response.read()
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        image_bgr = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        source_desc = image_url
    else:
        path = (
            image_url[len("file://") :]
            if image_url.startswith("file://")
            else image_url
        )
        image_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        source_desc = path

    if image_bgr is None:
        raise ValueError(f"Failed to decode image: {source_desc}")

    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    return image_bgr, image_rgb


def _compute_mask_area_in_image_space(
    polygon,
    masks_data,
    idx: int,
    image_width: int,
    image_height: int,
) -> int | None:
    """Mask area in ORIGINAL image pixel coordinates.

    ultralytics `masks.xy` is already scaled to the original image, so
    rasterizing the polygon onto an (image_height, image_width) canvas gives
    an area directly comparable to image_width * image_height. `masks.data`
    lives in model-input resolution — using its raw .sum() against image_area
    causes a coordinate mismatch and disproportionately rejects valid masks on
    high-resolution inputs. Fall back to it only when no polygon is available.
    """
    if polygon and len(polygon) >= 3 and image_width > 0 and image_height > 0:
        canvas = np.zeros((image_height, image_width), dtype=np.uint8)
        pts = np.array(polygon, dtype=np.float32).round().astype(np.int32)
        cv2.fillPoly(canvas, [pts.reshape(-1, 1, 2)], 1)
        return int(canvas.sum())

    if masks_data is not None and idx < len(masks_data):
        # Fallback: model-input-resolution count — not strictly comparable to
        # image_area, but better than returning None.
        return int(masks_data[idx].sum().item())

    return None


def _run_segmentation(image_np):
    model = get_model()
    results = model(
        image_np,
        device=YOLO_DEVICE,
        conf=YOLO_CONF_THRESHOLD,
        iou=YOLO_IOU_THRESHOLD,
        imgsz=YOLO_IMG_SIZE,
        verbose=False,
    )
    return results[0]


def _extract_detections(result, image_width: int, image_height: int):
    detections = []
    boxes = result.boxes
    masks = result.masks
    names = getattr(result, "names", {}) or {}

    if boxes is None:
        return detections

    for idx, box in enumerate(boxes):
        cls_id = int(box.cls[0].item())
        conf = float(box.conf[0].item())

        xyxy = box.xyxy[0].tolist()
        xywh = box.xywh[0].tolist()

        polygon = []

        if masks is not None and masks.xy is not None and idx < len(masks.xy):
            raw_polygon = masks.xy[idx]
            polygon = [
                [round(float(x), 2), round(float(y), 2)]
                for x, y in raw_polygon.tolist()
            ]

        mask_area = _compute_mask_area_in_image_space(
            polygon=polygon,
            masks_data=masks.data if masks is not None else None,
            idx=idx,
            image_width=image_width,
            image_height=image_height,
        )

        evaluation = evaluate_detection(
            confidence=conf,
            bbox_xyxy=xyxy,
            mask_area=mask_area,
            image_width=image_width,
            image_height=image_height,
        )

        cx, cy, bw, bh = xywh
        relative_position = {
            "x": round(cx / image_width, 6) if image_width else 0.0,
            "y": round(cy / image_height, 6) if image_height else 0.0,
        }
        relative_size = {
            "width": round(bw / image_width, 6) if image_width else 0.0,
            "height": round(bh / image_height, 6) if image_height else 0.0,
        }

        detections.append(
            {
                "idx": idx,
                "class_id": cls_id,
                "class_name": names.get(cls_id, str(cls_id))
                if isinstance(names, dict)
                else str(cls_id),
                "confidence": round(conf, 4),
                "bbox_xyxy": [round(v, 2) for v in xyxy],
                "bbox_xywh": [round(v, 2) for v in xywh],
                "relative_position": relative_position,
                "relative_size": relative_size,
                "mask_area": mask_area,
                "polygon": polygon,
                "accepted": evaluation["accepted"],
                "reject_reasons": evaluation["reject_reasons"],
                "metrics": evaluation["metrics"],
            }
        )

    return detections


def _select_accepted_detections(detections, limit: int):
    if limit <= 0:
        return []
    accepted = [det for det in detections if det.get("accepted") and det.get("polygon")]
    accepted.sort(key=lambda d: d.get("confidence", 0.0), reverse=True)
    return accepted[:limit]


def _upload_thumbnail_to_presigned_url(upload_url: str, png_bytes: bytes) -> None:
    response = requests.put(
        upload_url,
        data=png_bytes,
        headers={"Content-Type": "image/png"},
        timeout=UPLOAD_TIMEOUT_SECONDS,
    )
    if not (200 <= response.status_code < 300):
        raise RuntimeError(
            f"Thumbnail upload failed: status={response.status_code} body={response.text[:200]}"
        )


def _post_complete_callback(callback_url: str, payload: dict) -> None:
    response = requests.post(
        callback_url,
        json=payload,
        timeout=CALLBACK_TIMEOUT_SECONDS,
    )
    if not (200 <= response.status_code < 300):
        raise RuntimeError(
            f"Complete callback failed: status={response.status_code} body={response.text[:200]}"
        )


def _post_fail_callback(
    fail_callback_url: str, callback_token: str, error_message: str
) -> None:
    payload = {
        "callback_token": callback_token,
        "error_message": (error_message or "unknown error")[:2000],
    }
    response = requests.post(
        fail_callback_url,
        json=payload,
        timeout=CALLBACK_TIMEOUT_SECONDS,
    )
    if not (200 <= response.status_code < 300):
        raise RuntimeError(
            f"Fail callback failed: status={response.status_code} body={response.text[:200]}"
        )


def _process_detection_payload(payload: dict) -> dict:
    detection_task_id = payload.get("detection_task_id")
    source_image_url = payload.get("source_image_url")
    callback_url = payload.get("callback_url")
    fail_callback_url = payload.get("fail_callback_url")
    callback_token = payload.get("callback_token") or ""
    max_items = int(payload.get("max_items") or 50)
    upload_slots = payload.get("upload_slots") or []

    try:
        if not source_image_url:
            raise ValueError("source_image_url is required")
        if not callback_url:
            raise ValueError("callback_url is required")
        if not isinstance(upload_slots, list):
            raise ValueError("upload_slots must be a list")

        image_bgr, image_rgb = _read_image_arrays_from_url(source_image_url)
        image_height, image_width = image_bgr.shape[:2]

        result = _run_segmentation(image_bgr)
        detections = _extract_detections(result, image_width, image_height)

        usable_slot_count = min(max_items, len(upload_slots))
        accepted_detections = _select_accepted_detections(detections, usable_slot_count)

        items_out: list[dict] = []
        uploaded_count = 0

        for i, detection in enumerate(accepted_detections):
            if i >= len(upload_slots):
                break
            slot_entry = upload_slots[i]
            slot_value = slot_entry.get("slot")
            thumbnail_key = slot_entry.get("thumbnail_key")
            upload_url = slot_entry.get("upload_url")
            if slot_value is None or not thumbnail_key or not upload_url:
                print(
                    f"[WARN] Skipping invalid upload slot for detection_task_id={detection_task_id} index={i}"
                )
                continue

            png_bytes = _create_thumbnail_png_bytes(image_rgb, detection["polygon"])
            if png_bytes is None:
                print(
                    f"[WARN] Thumbnail bytes generation returned None for detection_task_id={detection_task_id} slot={slot_value}"
                )
                continue

            _upload_thumbnail_to_presigned_url(upload_url, png_bytes)
            uploaded_count += 1

            items_out.append(
                {
                    "slot": slot_value,
                    "thumbnail_key": thumbnail_key,
                    "relative_position_x": detection["relative_position"]["x"],
                    "relative_position_y": detection["relative_position"]["y"],
                    "relative_size_width": detection["relative_size"]["width"],
                    "relative_size_height": detection["relative_size"]["height"],
                    "confidence": round(float(detection["confidence"]), 4),
                    "bbox_xyxy": detection["bbox_xyxy"],
                }
            )

        complete_payload = {
            "callback_token": callback_token,
            "image_width": image_width,
            "image_height": image_height,
            "items": items_out,
        }
        _post_complete_callback(callback_url, complete_payload)

        accepted_count = sum(1 for d in detections if d.get("accepted"))
        return {
            "status": "ok",
            "detection_task_id": detection_task_id,
            "uploaded_count": uploaded_count,
            "num_detections": len(detections),
            "accepted_count": accepted_count,
        }

    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__
        print(
            f"[ERROR] Detection payload processing failed: detection_task_id={detection_task_id} error={error_message}"
        )
        if fail_callback_url:
            try:
                _post_fail_callback(fail_callback_url, callback_token, error_message)
            except Exception as fail_exc:
                print(
                    f"[ERROR] Fail callback also failed: detection_task_id={detection_task_id} error={fail_exc}"
                )
        return {
            "status": "error",
            "detection_task_id": detection_task_id,
            "message": error_message,
        }


def _process_image_url(image_url: str) -> dict:
    try:
        image_bgr, image_rgb = _read_image_arrays_from_url(image_url)
        image_height, image_width = image_bgr.shape[:2]

        result = _run_segmentation(image_bgr)
        detections = _extract_detections(result, image_width, image_height)

        image_stem = Path(image_url.split("?")[0]).stem or "image"

        rich_detections = []
        thumbnail_paths: list[str] = []

        for detection in detections:
            thumbnail_path = None
            if detection["accepted"] and detection["polygon"]:
                thumbnail_path = create_thumbnail(
                    image_np=image_rgb,
                    polygon=detection["polygon"],
                    filename=image_stem,
                    idx=detection["idx"],
                )
                if thumbnail_path:
                    thumbnail_paths.append(thumbnail_path)

            rich_detections.append(
                {
                    "class_id": detection["class_id"],
                    "class_name": detection["class_name"],
                    "confidence": detection["confidence"],
                    "bbox_xyxy": detection["bbox_xyxy"],
                    "bbox_xywh": detection["bbox_xywh"],
                    "relative_position": detection["relative_position"],
                    "relative_size": detection["relative_size"],
                    "mask_area": detection["mask_area"],
                    "polygon": detection["polygon"],
                    "accepted": detection["accepted"],
                    "reject_reasons": detection["reject_reasons"],
                    "metrics": detection["metrics"],
                    "thumbnail_path": thumbnail_path,
                }
            )

        accepted_count = sum(1 for d in rich_detections if d["accepted"])
        rejected_count = len(rich_detections) - accepted_count

        return {
            "status": "ok",
            "image_url": image_url,
            "image_width": image_width,
            "image_height": image_height,
            "num_detections": len(rich_detections),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "thumbnail_count": len(thumbnail_paths),
            "thumbnail_paths": thumbnail_paths,
            "detections": rich_detections,
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "image_url": image_url,
        }


@celery_app.task(name="ai_app.tasks.segment_product_image")
def segment_product_image(payload) -> dict:
    if isinstance(payload, dict):
        return _process_detection_payload(payload)
    if isinstance(payload, str):
        return _process_image_url(payload)
    return {
        "status": "error",
        "message": f"Unsupported payload type: {type(payload).__name__}",
    }


@celery_app.task(name="ai_app.tasks.parse_floorplan")
def parse_floorplan(image_bytes: bytes) -> dict:
    """도면 이미지 bytes → OpenCV 검출 결과 (raw 좌표).

    BE 측은 sync wait (timeout=10s) 으로 결과를 받아 store.width/depth 기준
    scaling + 3D 변환 후 FixtureMaster/Version/LayoutFixture 를 INSERT.

    응답: {image_width, image_height, fixtures: [{x, y, width, height, rotation}, ...]}
    """
    return _parse_floorplan_image(image_bytes)
