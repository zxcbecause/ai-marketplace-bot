"""Рендер одного слайда /rarity по фоновому шаблону (см. rarity_templates.py).
Фон уже содержит плашку заголовка, до 3 плашек характеристик и ватермарку
магазина — рендерер только вырезает фото товара в product_box и кладёт текст."""
import io
import logging

from PIL import Image, ImageDraw

from .background import remove_background, place_on_transparent
from .logo import paste_brand_watermark
from .fonts import best_lines, get_font
from utils.gpu_safety import run_gpu

log = logging.getLogger(__name__)


def _draw_centered_block(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                          text: str, role: str, color: tuple[int, int, int],
                          size_frac: float = 0.5, x_bias: int = 0) -> None:
    x0, y0, x1, y1 = box
    max_w = int((x1 - x0) * 0.82)
    max_size = int((y1 - y0) * size_frac)
    lines, size = best_lines(draw, text, max_w, role, max_size)
    font = get_font(size, role)

    heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        heights.append(bbox[3] - bbox[1])
    total_h = sum(heights) + max(0, len(lines) - 1) * 6
    cy = y0 + (y1 - y0 - total_h) // 2

    for line, lh in zip(lines, heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        cx = x0 + (x1 - x0 - lw) // 2 + x_bias
        draw.text((cx, cy), line, font=font, fill=color)
        cy += lh + 6


async def build_rarity_slide(template: dict, title: str,
                              char_pairs: list[tuple[str, str]],
                              product_bytes: bytes) -> bytes | None:
    try:
        bg = Image.open(template["bg"]).convert("RGB")
        canvas_w, canvas_h = template["canvas"]
        if bg.size != (canvas_w, canvas_h):
            bg = bg.resize((canvas_w, canvas_h), Image.LANCZOS)
        bg = bg.convert("RGBA")

        cutout = await run_gpu(remove_background, product_bytes, timeout=110, label="rarity_remove_background")
        if cutout:
            x0, y0, x1, y1 = template["product_box"]
            zone_w, zone_h = x1 - x0, y1 - y0
            placed = place_on_transparent(cutout, zone_w, zone_h, padding=max(10, int(zone_w * 0.04)))
            bg.paste(placed, (x0, y0), placed)
        else:
            log.warning("rarity: вырезание фона товара не удалось — слайд без фото")

        draw = ImageDraw.Draw(bg)
        title_color = tuple(template.get("title_color", (255, 255, 255)))
        _draw_centered_block(draw, template["title_box"], title.upper(), "title", title_color, size_frac=0.55)

        char_color = tuple(template.get("char_color", (255, 255, 255)))
        boxes = template["char_boxes"]
        # Плашки визуально "уезжают" за правый край холста — центр текста
        # смещаем чуть влево от геометрического центра плашки, иначе текст
        # выглядит прижатым к обрезанному краю.
        x_bias = -int((boxes[0][2] - boxes[0][0]) * 0.06) if boxes else 0
        for (label, value), box in zip(char_pairs, boxes):
            text = (value or label).upper()
            _draw_centered_block(draw, box, text, "label", char_color, size_frac=0.42, x_bias=x_bias)

        result = bg.convert("RGB")
        if not template.get("has_watermark"):
            result = paste_brand_watermark(result, corner="bottom-left", scale=0.32, bg_dark=False)

        buf = io.BytesIO()
        result.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        log.error(f"build_rarity_slide failed: {e}", exc_info=True)
        return None
