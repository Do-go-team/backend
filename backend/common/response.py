from typing import Any


def ok(data: Any = None, message: str = "") -> dict:
    """Wrap a payload in the standard success envelope.

    Routers should ``return ok(data=..., message="...")`` so every response
    emits ``{"success": true, "message": "...", "data": ...}``.
    """
    return {"success": True, "message": message, "data": data}
