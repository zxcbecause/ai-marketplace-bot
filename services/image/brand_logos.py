"""
Локальная библиотека логотипов брендов — services/image/logos/brands/<key>.png.

services/image/logo.py::get_brand_logo() пытался тянуть логотипы через
бесплатный Clearbit Logo API — сервис закрыт (проверено: не отвечает даже
для apple.com/samsung.com, не только нишевых брендов), поэтому для реальных
карточек логотипы храним и подключаем сами.

Как добавить бренд:
1. Найти официальный логотип (желательно SVG/PNG с прозрачным фоном —
   Wikimedia Commons обычно лицензионно чище, чем агрегаторы логотипов).
2. SVG → PNG: tools/render_svg_logo.py path/to/logo.svg
   services/image/logos/brands/<key>.png
3. Добавить запись в _REGISTRY ниже.
"""
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parent / "logos" / "brands"

_REGISTRY: dict[str, str] = {
    "dreame": "dreame.png",
    # Топ-20 брендов по количеству карточек в кабинете Ozon (см.
    # /v4/product/info/attributes, атрибут 85 "Бренд") — сессия 2026-06-24.
    "asus": "asus.png",
    "hp": "hp.png",
    "gigabyte": "gigabyte.png",
    "lenovo": "lenovo.png",
    "kingston": "kingston.png",
    "tp-link": "tp-link.png",
    "msi": "msi.png",
    "canon": "canon.png",
    "acer": "acer.png",
    "logitech": "logitech.png",
    "razer": "razer.png",
    "corsair": "corsair.png",
    "dell": "dell.png",
    "samsung": "samsung.png",
    "xiaomi": "xiaomi.png",
    "huawei": "huawei.png",
    "amd": "amd.png",
    "hikvision": "hikvision.png",
    "jbl": "jbl.png",
    "aoc": "aoc.png",
    "ugreen": "ugreen.png",
}


def get_local_brand_logo(brand: str) -> Image.Image | None:
    """RGBA-логотип бренда из локальной библиотеки или None, если бренда
    в _REGISTRY нет/файл не найден — вызывающий код должен просто не
    рисовать лого в этом случае, а не падать."""
    key = brand.lower().strip()
    filename = _REGISTRY.get(key)
    if not filename:
        return None
    path = _DIR / filename
    if not path.exists():
        log.warning(f"Brand logo registered but file missing: {path}")
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception as e:
        log.warning(f"Brand logo load failed for {brand!r}: {e}")
        return None
