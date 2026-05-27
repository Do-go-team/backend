"""레이아웃 평면도 (Top View) PDF 렌더링.

매장 평면도를 2D 정사영으로 PDF 한 페이지에 그림. reportlab 사용. 한국어 폰트는
시스템에 설치된 fonts-noto-cjk (Dockerfile.local 에서 apt install) 의 NotoSansCJK
ttc 를 ttf 로 등록해 활용. 시스템 폰트 못 찾으면 reportlab 의 Helvetica 로 fallback
(한국어 미표시 — dev 환경 외 가능성 낮음).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from reportlab.lib.pagesizes import A2, A3, A4, landscape, portrait
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

# 시스템 폰트 후보 — NanumGothic (fonts-nanum) 추천.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

KOREAN_FONT_NAME = "NanumGothic"
_FONT_REGISTERED: bool | None = None


def _register_korean_font() -> str:
    """한국어 폰트 1회 등록 후 폰트 이름 반환. 실패 시 'Helvetica'."""
    global _FONT_REGISTERED
    if _FONT_REGISTERED is True:
        return KOREAN_FONT_NAME
    if _FONT_REGISTERED is False:
        return "Helvetica"

    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                # .ttc 는 collection — subfontIndex 로 첫 폰트 선택
                pdfmetrics.registerFont(TTFont(KOREAN_FONT_NAME, path, subfontIndex=0))
                _FONT_REGISTERED = True
                return KOREAN_FONT_NAME
            except Exception:
                continue

    _FONT_REGISTERED = False
    return "Helvetica"


PAPER_SIZES = {"A4": A4, "A3": A3, "A2": A2}


def _resolve_page_size(paper_size: str, orientation: str):
    page = PAPER_SIZES[paper_size]
    return landscape(page) if orientation == "landscape" else portrait(page)


def render_layout_plan_pdf(
    *,
    store_name: str,
    store_width_cm: int,
    store_depth_cm: int,
    layout_name: str,
    fixtures: Iterable[dict],
    paper_size: str = "A4",
    orientation: str = "landscape",
    include_labels: bool = True,
    show_grid: bool = False,
) -> bytes:
    """매장 평면도 PDF 1페이지 생성 후 bytes 반환.

    fixtures 의 각 항목 dict: name, world_pos_x, world_pos_z, world_rot_y,
    width, depth (모두 cm 단위, fixture_master 기준). y/height 는 평면도 무관.

    스케일 계산: 종이의 안쪽 여백을 제외한 영역에 store_width × store_depth (cm)
    가 비례 fit 되도록 자동 산출. 1cm = N pt.
    """
    from common.pdf.storage import to_buffer

    font_name = _register_korean_font()
    buf = to_buffer()
    page_size = _resolve_page_size(paper_size, orientation)
    page_w, page_h = page_size

    margin = 20 * mm
    title_h = 30
    label_h = 12
    drawable_w = page_w - 2 * margin
    drawable_h = page_h - 2 * margin - title_h

    # cm → pt 스케일 — 매장 전체가 fit 되는 최댓값.
    scale_w = drawable_w / max(store_width_cm, 1)
    scale_d = drawable_h / max(store_depth_cm, 1)
    scale = min(scale_w, scale_d)

    plan_w = store_width_cm * scale
    plan_d = store_depth_cm * scale

    # 평면도 영역 중앙 정렬
    origin_x = margin + (drawable_w - plan_w) / 2
    origin_y = margin + (drawable_h - plan_d) / 2

    c = Canvas(buf, pagesize=page_size)

    # 제목
    c.setFont(font_name, 14)
    c.drawString(margin, page_h - margin, f"{store_name} — {layout_name}")
    c.setFont(font_name, 9)
    c.drawString(
        margin,
        page_h - margin - 14,
        f"매장 규격: {store_width_cm}cm × {store_depth_cm}cm (W×D, Top View)",
    )

    # 매장 외곽선
    c.setLineWidth(1.2)
    c.rect(origin_x, origin_y, plan_w, plan_d, fill=0)

    # 그리드 (50cm 간격, optional)
    if show_grid:
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.3)
        grid_step_cm = 50
        x = 0
        while x <= store_width_cm:
            gx = origin_x + x * scale
            c.line(gx, origin_y, gx, origin_y + plan_d)
            x += grid_step_cm
        y = 0
        while y <= store_depth_cm:
            gy = origin_y + y * scale
            c.line(origin_x, gy, origin_x + plan_w, gy)
            y += grid_step_cm
        c.setStrokeColorRGB(0, 0, 0)

    # Fixture 박스 + 라벨
    for fx in fixtures:
        cx_cm = fx["world_pos_x"]
        cz_cm = fx["world_pos_z"]  # Screen Z (Top-down)
        w_cm = fx["width"]
        d_cm = fx["depth"]
        rot_deg = fx.get("world_rot_y", 0) or 0

        # 직사각형 중심 기준 회전 그리기
        # Screen (0,0 at top-left) -> PDF (0,0 at bottom-left)
        # PDF Y = origin_y + (plan_d - cz_cm * scale)
        c.saveState()
        c.translate(origin_x + cx_cm * scale, origin_y + (plan_d - cz_cm * scale))

        # Screen 은 시계방향(+) 회전, ReportLab 은 반시계방향(+) 회전.
        # 방향을 맞추기 위해 부호 반전.
        c.rotate(-rot_deg)

        rect_w = w_cm * scale
        rect_d = d_cm * scale
        c.setLineWidth(0.8)
        c.setFillColorRGB(0.93, 0.93, 0.93)
        c.rect(-rect_w / 2, -rect_d / 2, rect_w, rect_d, fill=1, stroke=1)

        if include_labels:
            c.setFillColorRGB(0, 0, 0)
            c.setFont(font_name, min(label_h, max(6, int(rect_d / 2.2))))
            label = fx.get("name") or ""
            c.drawCentredString(0, -3, label)
        c.restoreState()

    c.showPage()
    c.save()
    return buf.getvalue()
