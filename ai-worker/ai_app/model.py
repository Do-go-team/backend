import os
from pathlib import Path

from ultralytics import YOLO

_model = None
_model_load_error = None


def _resolve_model_path() -> Path:
    model_path = os.getenv("YOLO_MODEL_PATH", "/models/best.pt")
    return Path(model_path)


def load_model():
    global _model, _model_load_error

    if _model is not None:
        return _model

    model_path = _resolve_model_path()
    try:
        _model = YOLO(str(model_path))
        _model_load_error = None
        print(f"[INFO] YOLO model loaded successfully: {model_path}")
    except Exception as exc:
        _model = None
        _model_load_error = str(exc)
        print(f"[ERROR] Failed to load YOLO model: {exc}")
        raise

    return _model


def get_model():
    if _model is None:
        return load_model()
    return _model


def get_model_load_error():
    return _model_load_error
