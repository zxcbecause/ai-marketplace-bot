"""Композиция левой зоны рич-контента: главное фото + миниатюры под ним."""
import io
import logging
from PIL import Image

from .background import (
    remove_background, remove_background_monitor, remove_solid_background,
    place_on_transparent, MONITOR_LIKE_CATEGORIES,
)
from utils.gpu_safety import run_gpu

log = logging.getLogger(__name__)


def estimate_content_ar(img_bytes: bytes, default: float = 1.0) -> float:
    """Дешёвая (без rembg/GPU) оценка соотношения сторон САМОГО товара на
    фото — по bbox непустых (не близких к белому) пикселей, а не по
    размеру файла-канваса. WB-фото часто хранятся на портретном канвасе с
    большими белыми полями (напр. широкая узкая веб-камера-балка в файле
    900x1200) — размер файла в таких случаях даёт AR=0.75 (портрет) вместо
    реальных ~5.6 (товар широкий и тонкий), из-за чего левая зона рич-
    контента получает слишком узкую ширину и товар рисуется крошечным
    (баг найден вживую 03.09.2026, артикул 960-001681). Не заменяет rembg —
    только для выбора размера зоны ДО дорогого фонового вырезания."""
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        mask = img.point(lambda p: 255 if p < 245 else 0)
        bbox = mask.getbbox()
        if not bbox:
            return default
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w < 5 or h < 5:
            return default
        return w / h
    except Exception:
        return default


async def _to_rgba(img_bytes: bytes, category: str = "") -> Image.Image | None:
    """rembg + autocrop, фоллбэк на remove_white_background, затем на
    исходное фото в RGBA. Для Мониторов — безопасное вырезание (заливка
    замкнутых дыр в экране + детект дыр/белого квадрата с фолбэком на
    flood-fill).

    11.09.2026: раньше при таймауте/неудаче rembg сразу шли к сырому фото
    (белый студийный фон оставался прямоугольником поверх градиента рич-
    контента — живой баг, замечен пользователем на партии из 41 карточки,
    где GPU-очередь была перегружена последовательными вызовами и rembg
    массово не укладывался в 110с). Промежуточный remove_white_background
    (тот же, что уже стоит в infographic_widgets.py::prepare_product)
    ловит большинство таких случаев до отказа в сырое фото."""
    if category in MONITOR_LIKE_CATEGORIES:
        img = await run_gpu(remove_background_monitor, img_bytes, timeout=90, label="remove_background_monitor")
    else:
        img = await run_gpu(remove_background, img_bytes, timeout=110, label="remove_background")
    if img is not None:
        return img
    log.warning("_to_rgba: rembg failed/timeout — fallback: solid bg removal (auto-detect color)")
    try:
        img = await run_gpu(remove_solid_background, img_bytes, label="remove_solid_background")
        if img is not None:
            return img
    except Exception as e:
        log.debug(f"_to_rgba: remove_solid_background failed: {e}")
    try:
        return Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception as e:
        log.debug(f"_to_rgba failed: {e}")
        return None


async def compose_left_zone(main_bytes: bytes,
                            extra_bytes: list[bytes],
                            zone_w: int,
                            zone_h: int,
                            main_ratio: float = 0.66,
                            gap: int = 20,
                            padding: int = 30,
                            category: str = "") -> Image.Image:
    """
    Левая зона рич-контента — прозрачный RGBA размера (zone_w, zone_h).
    - main: главное фото в верхней части (main_ratio высоты)
    - extras: до 3 миниатюр в ряд под главным (1 / 3 высоты зоны)

    Если extra_bytes пуст — вся зона отдана главному фото.
    """
    canvas = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))

    extras = [b for b in extra_bytes if b][:3]

    if not extras:
        main = await _to_rgba(main_bytes, category)
        if main is not None:
            # Широкие товары (ноутбуки, мониторы) — AR > 1.35 — заполняем зону
            # плотнее (0.92), иначе 16:9 девайс теряется. Узкие — 0.80 как раньше.
            _ar = main.width / max(main.height, 1)
            shrink = 0.92 if _ar > 1.35 else 0.80
            sub_w = int(zone_w * shrink)
            sub_h = int(zone_h * shrink)
            placed = place_on_transparent(main, sub_w, sub_h, padding=padding)
            x = (zone_w - sub_w) // 2
            y = (zone_h - sub_h) // 2
            canvas.paste(placed, (x, y), placed)
        return canvas

    main_h = int(zone_h * main_ratio)
    thumbs_h = zone_h - main_h - gap

    main = await _to_rgba(main_bytes, category)
    if main is not None:
        main_placed = place_on_transparent(main, zone_w, main_h, padding=padding)
        canvas.paste(main_placed, (0, 0), main_placed)

    n = len(extras)
    thumb_w = (zone_w - gap * (n - 1) - padding * 2) // n
    thumb_pad = max(8, padding // 3)
    for i, eb in enumerate(extras):
        thumb_img = await _to_rgba(eb)
        if thumb_img is None:
            continue
        placed = place_on_transparent(thumb_img, thumb_w, thumbs_h, padding=thumb_pad)
        x = padding + i * (thumb_w + gap)
        y = main_h + gap
        canvas.paste(placed, (x, y), placed)

    return canvas
