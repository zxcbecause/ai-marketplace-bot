import asyncio
import io
import logging
import os
from pathlib import Path

import aiohttp
from PIL import Image, ImageStat

log = logging.getLogger(__name__)

LOGO_CACHE_DIR = Path(r"C:\AI-Bot-V2\data\logos")
LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Известные домены брендов
_BRAND_DOMAINS: dict[str, str] = {
    "samsung":   "samsung.com",
    "apple":     "apple.com",
    "xiaomi":    "xiaomi.com",
    "redmi":     "mi.com",
    "poco":      "poco.net",
    "realme":    "realme.com",
    "honor":     "honor.com",
    "huawei":    "huawei.com",
    "oneplus":   "oneplus.com",
    "oppo":      "oppo.com",
    "vivo":      "vivo.com",
    "nokia":     "nokia.com",
    "motorola":  "motorola.com",
    "sony":      "sony.com",
    "lg":        "lg.com",
    "asus":      "asus.com",
    "lenovo":    "lenovo.com",
    "acer":      "acer.com",
    "hp":        "hp.com",
    "dell":      "dell.com",
    "msi":       "msi.com",
    "gigabyte":  "gigabyte.com",
    "nvidia":    "nvidia.com",
    "intel":     "intel.com",
    "amd":       "amd.com",
    "logitech":  "logitech.com",
    "razer":     "razer.com",
    "corsair":   "corsair.com",
    "kingston":  "kingston.com",
    "seagate":   "seagate.com",
    "oscal":     "oscal.hk",
    "blackview": "blackview.biz",
    "infinix":   "infinixmobility.com",
    "tecno":     "tecno-mobile.com",
    "itel":      "itel-mobile.com",
    "lian li":   "lian-li.com",
    "be quiet":  "bequiet.com",
    "thermaltake": "thermaltake.com",
    "deepcool":  "deepcool.com",
    "cooler master": "coolermaster.com",
    "noctua":    "noctua.at",
    "arctic":    "arctic.de",
    "ajazz":     "ajazz.com",
    "sjcam":     "sjcam.com",
    "gopro":     "gopro.com",
    "dji":       "dji.com",
    "jbl":       "jbl.com",
    "bose":      "bose.com",
    "sennheiser": "sennheiser.com",
    "anker":     "anker.com",
    "ugreen":    "ugreen.com",
    "baseus":    "baseus.com",
    "western digital": "westerndigital.com",
    "wd":        "westerndigital.com",
}


def _candidate_domains(brand: str) -> list[str]:
    """Возвращает список доменов-кандидатов для бренда."""
    bl = brand.lower().strip()
    if not bl:
        return []
    known = _BRAND_DOMAINS.get(bl)
    if known:
        return [known]
    parts = bl.split()
    if len(parts) == 1:
        return [f"{parts[0]}.com"]
    # multi-word без записи в словаре — пробуем варианты
    joined = "".join(parts)
    hyphen = "-".join(parts)
    return [f"{hyphen}.com", f"{joined}.com", f"{parts[0]}.com"]


async def _download_logo(domain: str) -> bytes | None:
    url = f"https://logo.clearbit.com/{domain}?size=200"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        log.debug(f"Logo download failed for {domain}: {e}")
    return None


async def get_brand_logo(brand: str, size: int = 80) -> Image.Image | None:
    """
    Загружает логотип бренда через Clearbit.
    Кэширует локально. Возвращает PIL Image с прозрачностью (RGBA) или None.
    Поддерживает multi-word бренды (Lian Li → lian-li.com).
    """
    brand_lower = brand.lower().strip()
    if not brand_lower:
        return None

    cache_key = brand_lower.replace(" ", "_")
    cache_path = LOGO_CACHE_DIR / f"{cache_key}.png"

    # Проверяем кэш
    if cache_path.exists():
        try:
            img = Image.open(cache_path).convert("RGBA")
            img.thumbnail((size, size), Image.LANCZOS)
            return img
        except Exception:
            cache_path.unlink(missing_ok=True)

    # Скачиваем — пробуем все домены-кандидаты по очереди
    domains = _candidate_domains(brand_lower)
    data = None
    used_domain = ""
    for d in domains:
        data = await _download_logo(d)
        if data and len(data) >= 500:
            used_domain = d
            break

    if not data or len(data) < 500:
        log.info(f"Logo not found for brand: {brand} (tried {domains})")
        return None

    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        img.save(cache_path, format="PNG")
        log.info(f"Logo cached: {brand} → {used_domain} → {cache_path}")
        return img
    except Exception as e:
        log.warning(f"Logo parse failed for {brand}: {e}")
        return None


def detect_bg_dark(img: Image.Image, corner: str = "bottom-left", scale: float = 0.25) -> bool:
    """Определяет, тёмный ли фон в углу, куда ляжет водяной знак —
    по средней яркости региона (без обращений к API)."""
    iw, ih = img.size
    wm_w = int(iw * scale)
    pad = int(iw * 0.018)
    box_size = wm_w + pad
    if corner == "top-left":
        box = (0, 0, box_size, box_size)
    elif corner == "bottom-right":
        box = (iw - box_size, ih - box_size, iw, ih)
    else:  # bottom-left
        box = (0, ih - box_size, box_size, ih)
    region = img.convert("L").crop(box)
    return ImageStat.Stat(region).mean[0] < 128


_BRAND_WATERMARK = Path(os.getenv("WATERMARK_PATH", "./data/watermark.png"))

def paste_brand_watermark(img: Image.Image, corner: str = "top-left",
                           scale: float = 0.13, opacity: float = 0.65,
                           bg_dark: bool = False, normalize_alpha: bool = False) -> Image.Image:
    """Вставляет логотип магазина в угол изображения.
    corner:  'top-left' | 'bottom-left' | 'bottom-right'
    scale:   ширина логотипа относительно ширины изображения
    opacity: прозрачность 0..1
    bg_dark: True → перекрашивает логотип в белый (для тёмного фона)
    normalize_alpha: True → исходный PNG сам по себе полупрозрачный (макс альфа ~77/255),
        перед применением opacity растягиваем альфу до 255 — иначе итоговая
        прозрачность получается едва заметной (для самостоятельной /watermark
        нужна чёткая видимость, а не лёгкий оттиск как в инфографике)
    """
    if not _BRAND_WATERMARK.exists():
        return img
    try:
        wm = Image.open(_BRAND_WATERMARK).convert("RGBA")
        iw, ih = img.size
        wm_w = int(iw * scale)
        wm_h = int(wm_w * wm.height / wm.width)
        wm = wm.resize((wm_w, wm_h), Image.LANCZOS)

        r, g, b, a = wm.split()

        if bg_dark:
            # Белый логотип на тёмном фоне — заменяем RGB на белый, сохраняем alpha
            import PIL.Image as _PI
            white = _PI.new("L", wm.size, 255)
            wm = _PI.merge("RGBA", (white, white, white, a))

        if normalize_alpha:
            max_a = a.getextrema()[1] or 1
            a = a.point(lambda x: min(255, int(x * (255 / max_a) * opacity)))
        else:
            a = a.point(lambda x: int(x * opacity))
        wm.putalpha(a)

        pad = int(iw * 0.018)
        if corner == "top-left":
            pos = (pad, pad)
        elif corner == "bottom-right":
            pos = (iw - wm_w - pad, ih - wm_h - pad)
        else:  # bottom-left
            pos = (pad, ih - wm_h - pad)

        base = img.convert("RGBA")
        base.paste(wm, pos, wm)
        return base.convert("RGB")
    except Exception as e:
        log.warning(f"Watermark paste failed: {e}")
        return img
