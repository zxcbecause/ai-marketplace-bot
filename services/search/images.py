import asyncio
import json
import logging
import re
import io
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import aiohttp
import numpy as np
from PIL import Image
from .searxng import searxng_search
from .common import BLACKLIST, BROWSER_SEMAPHORE
from utils.gpu_safety import run_gpu

_MASK_SIZE = 256
_ALPHA_THRESHOLD = 30
_MIN_SIDE = 400            # минимальная сторона исходной картинки (px)
_MAX_ASPECT = 2.0          # max/min stronger than → баннер/коллаж-плитка, отклоняем
# Модуль ОЗУ — длинная узкая пластина (DIMM ~133×30-53мм, SO-DIMM ~67×30мм),
# у фото лежащей планки aspect доходит до ~4.3 — обычный порог 2.0 рубил
# вообще все нормальные фото планок памяти.
_MAX_ASPECT_RAM = 4.5
# Клавиатура — горизонтально вытянутый корпус, нормальный aspect 2.5–3.5;
# обычный порог 2.0 рубил все нормальные фото клавиш.
_MAX_ASPECT_KEYBOARD = 3.5
# SSD — тонкая длинная плата, на фото магазинов/баз данных (Newegg, TechPowerUp,
# официальные store-CDN) часто снята с большими полями вокруг — обычные пороги
# preserve/min_side (рассчитанные на телефоны/мыши, занимающие весь кадр)
# рубили почти все нормальные фото SSD.
_MIN_SIDE_SSD = 300
_PRESERVE_MIN_SSD = 0.30
# Кроссовки — официальные CDN-фото брендов (проверено на assets.adidas.com,
# карточка JS4429 21.07.2026) часто сняты парой/под углом с большим полем
# вокруг — preserve стабильно 0.23-0.31 у ЗАВЕДОМО верного фото (артикул
# прямо в имени файла), обычный порог 0.50 отклонял его каждый второй раз,
# карточка уходила с фото со стоков вместо официального.
_PRESERVE_MIN_SHOES = 0.20
# 21.07.2026: rembg + scipy (маски/дыры/компоненты) на полноразмерных фото
# (магазинные CDN часто отдают 3000-4000px+) держат по десятки-сотни МБ на
# картинку; при 10+ кандидатах × 2 прохода (строгий+relaxed) это и обваливало
# RAM за 2-4 минуты на "трудных" товарах (см. память ai_bot_v2_migration).
# preserve — отношение площадей, от масштаба не зависит, так что даунскейл
# перед rembg не меняет решение, только режет память/время.
_SCORE_MAX_SIDE = 1024
_RELATIVE_SCORE = 0.05     # дополнительные ракурсы должны иметь >= 5% от main score
_MAX_COMPONENTS = 2        # больше — это коллаж/грид, а не одно фото товара
_MIN_COMPONENT_FRAC = 0.02 # компоненты мельче 2% от маски — мусор, не считаем

log = logging.getLogger(__name__)

_GENERIC_WORDS = {
    "смартфон", "телефон", "планшет", "ноутбук", "компьютер",
    "монитор", "наушники", "колонка", "мышь", "клавиатура",
}

# Домены с JS-lazy loading — для них og:image возвращает placeholder,
# поэтому скачиваем страницу через Playwright.
_LAZY_DOMAINS = {"al-style.kz"}

# Зеркала/агрегаторы, которые шлёпают водяной знак поверх фото товара —
# для карточек не годятся. Проверяются как подстрока в URL.
_WATERMARK_DOMAINS = ("gsmarena.com.ng", "mobiledokan", "gadgets360", "91mobiles",
                      "smartprix", "mysmartprice", "fonearena", "smartfon.kiev.ua",
                      "telefon.od.ua", "nix.ru", "static.nix.ru", "phone.kiev.ua",
                      "babystore.lv", "aktek.ru")

# Доверенные e-commerce домены: для них НЕ требуем имя бренда в URL.
# Эти сайты — товарные карточки, в URL обычно слаг с артикулом, не с брендом.
_TRUSTED_ECOMMERCE = {
    # РФ
    "dns-shop.ru", "citilink.ru", "mvideo.ru", "eldorado.ru",
    "ozon.ru", "wildberries.ru", "wb.ru", "technopoint.ru",
    "regard.ru", "computeruniverse.ru", "onlinetrade.ru",
    "5element.by", "21vek.by", "onliner.by",
    # KZ
    "kaspi.kz", "sulpak.kz", "technodom.kz", "mechta.kz",
    "alser.kz", "satu.kz", "shop.kz", "nextit.kz",
    "al-style.kz", "alstyle.kz", "itmag.kz",
    # UA / прочие СНГ
    "rozetka.com.ua", "comfy.ua", "allo.ua", "hotline.ua",
    "smartfon.kiev.ua", "smartfon.ua", "telefon.od.ua",
    # Глобальные крупные
    "amazon.com", "amazon.de", "ebay.com", "aliexpress.com",
    "flipkart.com", "samsung.com", "apple.com",
    # Производители планшетов/перьевых устройств
    "estore.wacom.com", "wacom.com",
    "xp-pen.com", "xp-pen.ru", "xp-pen.com.ua",
    # Производители смартфонов
    "infinix.com", "tecno-mobile.com", "tecnomobile.com",
    "xiaomi.com", "mi.com", "realme.com", "oppo.com", "vivo.com",
    "honor.com", "huawei.com", "oneplus.com", "google.com",
    # PC-комплектующие — крупный ритейлер + независимые обзорные базы
    "newegg.com", "techpowerup.com", "storagereview.com",
    # Одежда/обувь — крупный евроритейлер, студийные фото без вотермарков
    # (добавлено 22.07.2026 под партию кроссовок клиента, вместе с adidas.com)
    "zalando.ru", "zalando.com",
    # Серверное/сетевое железо — нишевые IT-ресурсы и дистрибьюторы. Добавлено
    # 22.07.2026 под партию сетевых адаптеров (Broadcom/HP/Ubiquiti) — только
    # whitelist для фильтра, БЕЗ выделенного site:-скрапера — сработает,
    # только если SearXNG сама найдёт ссылку в общем поиске.
    "servethehome.com", "cdw.com", "provantage.com", "fs.com",
}

# Обзорные/специализированные базы и крупные зарубежные ритейлеры —
# чаще дают студийные фото без вотермарков маркетплейсов (см. find_product_images
# query "{product} review"). Поднимаем им приоритет в очереди скачивания,
# чтобы они не вытеснялись менее качественными кандидатами при max_candidates.
_PRIORITY_DOMAINS = {
    "rtings.com", "techpowerup.com", "newegg.com", "neweggimages.com",
    "storagereview.com",
}


def _url_matches_brand(url: str, product: str, brand: str = "") -> bool:
    url_low = url.lower()
    # Доверенные e-commerce — пропускаем без проверки бренда в URL.
    # Их URL — это слаг с артикулом, бренд редко в нём явно
    if any(dom in url_low for dom in _TRUSTED_ECOMMERCE):
        return True
    if brand:
        # multi-word бренды: "Lian Li" → пробуем варианты "lianli", "lian-li", "lian_li"
        parts = [p for p in brand.lower().split() if len(p) > 1]
        if not parts:
            return True
        joined_variants = ["".join(parts), "-".join(parts), "_".join(parts)]
        if any(v in url_low for v in joined_variants):
            return True
        # последний шанс — первое слово бренда в URL (для коротких брендов вроде "HP")
        return parts[0] in url_low
    # старая логика (фоллбэк): первое значимое слово из product
    words = [w.lower() for w in product.split()
             if len(w) > 2 and w.lower() not in _GENERIC_WORDS]
    if not words:
        return True
    return words[0] in url_low


def _model_url_tokens(product: str, brand: str = "") -> list[str]:
    """Варианты модели для матча в URL: 'Infinix HOT 12' → hot12 / hot-12 / hot_12.
    Бренд отбрасываем. Требуется цифра в модели (иначе токен слишком общий —
    напр. 'flat' без числа отсеёт лишнее)."""
    model = product
    if brand and product.lower().startswith(brand.lower()):
        model = product[len(brand):]
    # Убираем скобки и их содержимое: "G32 (2.0)" → "G32", "SE-214-XT (ARGB)" → "SE-214-XT"
    model = re.sub(r'\([^)]*\)', '', model).strip()
    # Слэш в модели ("SPK-220/225") → заменяем на дефис для URL-матчинга
    model = model.replace("/", "-")
    words = [w for w in re.split(r"\s+", model.strip().lower()) if w]
    if not words:
        return []
    if not any(any(c.isdigit() for c in w) for w in words):
        # Короткие алфавитные модели без цифр (Edifier GX, ASUS G2 и т.п.) — один
        # строгий токен с границами слова с ОБЕИХ сторон в _url_matches_model,
        # чтобы "gx05" не матчился как "gx" (другая модель).
        joined = "".join(words)
        if 2 <= len(joined) <= 4 and joined.isalpha():
            return [joined]
        return []
    variants = {"".join(words), "-".join(words), "_".join(words), " ".join(words)}
    # Добавляем отдельные буквенно-цифровые токены (≥3 символа, ЦИФРА+БУКВА):
    # "Inspiroy H1161" → ["h1161"], "H641P-P" → ["h641p-p", "h641p"],
    # "Vacuum G12 Pro" → ["g12"] (короткие 3-симв. коды моделей вроде g12/x60 —
    # см. крах 14.07.2026: «Dreame Wet and Dry Vacuum G12 Pro» не матчился с
    # реальной страницей .../dreame-g12-pro/, порог ≥4 отсекал «g12»).
    # ЧИСТО числовые токены («5600», «512», «60») НЕ добавляем — это частоты/
    # объёмы/скорости из спеков (DDR5 5600, 60Hz, 512GB), не код модели: у
    # разного, вообще не связанного товара такое число может случайно
    # совпасть (см. крах 14.07.2026 — «UMA ... DDR5 5600» matched на URL
    # конструктора «...-5600-detalei-...», 5600 деталей набора, а не памяти).
    for w in words:
        if len(w) >= 3 and any(c.isdigit() for c in w) and any(c.isalpha() for c in w):
            variants.add(w)
            # Также без последнего сегмента через дефис (H641P-P → h641p)
            if '-' in w:
                variants.add(w.rsplit('-', 1)[0])
            # Без суффикса цвета/варианта (PTK470K0 → PTK470, PTK670K0B → PTK670)
            stripped = re.sub(r'[a-z]\d[a-z]?$', '', w)
            if stripped and stripped != w and len(stripped) >= 3:
                variants.add(stripped)
    return [v for v in variants if len(v) >= 3]


def _is_bad_image(url: str) -> bool:
    low = url.lower()
    bad = [
        "logo", "icon", "favicon", "placeholder", "noimage", "no-image",
        "no_image", "default", "preloader", "loader", "loading", "spinner",
    ]
    if any(b in low for b in bad):
        return True
    # GIF/SVG обычно UI-элементы, не товарные фото
    clean = low.split("?")[0]
    if clean.endswith(".gif") or clean.endswith(".svg"):
        return True
    return False


def _upgrade_image_url(url: str) -> str:
    # nix.ru: заменяем параметры размера
    if "static.nix.ru" in url and "width=" in url:
        url = re.sub(r"width=\d+", "width=1200", url)
        url = re.sub(r"height=\d+", "height=1200", url)
    # apltech.kz / apltech.ru: @500 → @1200 в имени файла
    if "apltech" in url:
        url = re.sub(r"@\d+(\.(jpg|jpeg|png|webp))", r"@1200\1", url, flags=re.IGNORECASE)
    # dns-shop.ru CDN: /fit/500/500/ → /fit/0/0/ (оригинальный размер)
    if "cdn1.dns-shop.ru" in url or "cdn2.dns-shop.ru" in url:
        url = re.sub(r"/fit/\d+/\d+/", "/fit/0/0/", url)
    return url


async def _fetch_og_image(url: str) -> str | None:
    """Ищет главное изображение страницы в порядке:
    1. og:image (стандарт)
    2. JSON-LD Product.image (микроразметка schema.org)
    3. Twitter card image
    4. Первый крупный img с продуктовыми классами / в детальном контейнере
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=8),
                                   headers=headers) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")
    except Exception as e:
        log.debug(f"page fetch failed {url}: {e}")
        return None

    # 1. og:image (если не lazy-loader placeholder)
    for pat in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\'>\s]+)',
        r'<meta[^>]+content=["\'](https?://[^"\'>\s]+)["\'][^>]+property=["\']og:image["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            cand = m.group(1).replace("&amp;", "&")
            if not _is_bad_image(cand):
                return cand

    # 2. Twitter card
    for pat in [
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\'>\s]+)',
        r'<meta[^>]+content=["\'](https?://[^"\'>\s]+)["\'][^>]+name=["\']twitter:image["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            cand = m.group(1).replace("&amp;", "&")
            if not _is_bad_image(cand):
                return cand

    # 3. JSON-LD Product.image (schema.org)
    for jsonld_match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    ):
        block = jsonld_match.group(1)
        if "Product" not in block and "product" not in block:
            continue
        # "image": "https://..."  ИЛИ "image": ["https://...", ...]
        m = re.search(r'"image"\s*:\s*"(https?://[^"]+)"', block)
        if m:
            return m.group(1).replace("\\/", "/").replace("&amp;", "&")
        m = re.search(r'"image"\s*:\s*\[\s*"(https?://[^"]+)"', block)
        if m:
            return m.group(1).replace("\\/", "/").replace("&amp;", "&")

    # 4. Крупный img в типичных продуктовых контейнерах.
    # Берём ПЕРВЫЙ <img>, у которого src похож на товарное фото
    # (содержит product/upload/catalog/cdn в пути).
    domain = urlparse(url).netloc
    for m in re.finditer(r'<img[^>]+(?:src|data-src|data-original)=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1).strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = f"https://{domain}{src}"
        elif not src.startswith("http"):
            continue
        # фильтр: только то, что похоже на товарное фото
        low = src.lower()
        if any(k in low for k in [
            "logo", "icon", "favicon", "sprite", "placeholder",
            "preloader", "loader", "loading", "spinner", "ajax",
            "bg-", "background", "banner", "promo",
        ]):
            continue
        if low.endswith(".gif") or low.endswith(".svg"):
            continue
        if any(k in low for k in ["upload", "product", "catalog", "cdn", "image", "media", "files"]):
            return src.replace("&amp;", "&")

    return None


async def _download_bytes(url: str) -> bytes | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
    }
    # Хотлинк-защита: добавляем Referer для доменов которые её требуют.
    if "gsmarena" in url.lower():
        headers["Referer"] = "https://www.gsmarena.com/"
    elif "apltech" in url.lower():
        headers["Referer"] = "https://www.apltech.kz/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, ssl=False, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.debug(f"Image download {url} → HTTP {resp.status}")
    except Exception as e:
        log.debug(f"Image download failed {url}: {e}")
    return None


# Максимум одновременно открытых вкладок (BrowserContext) на ОДНОМ общем
# Chromium, переданном в _search_*_playwright через _playwright_fallback.
# BROWSER_SEMAPHORE(2) в common.py ограничивает число процессов Chromium —
# не помогает здесь, т.к. все ~20 функций делят один и тот же процесс.
_PAGE_SEMAPHORE = asyncio.Semaphore(5)


class _TrackedBrowser:
    """Прокси вокруг shared Browser для _get_browser(shared=...): .new_page(**kwargs)
    вызывающего кода (user_agent=, viewport=...) — опции уровня BrowserContext,
    их принимает только Browser.new_context()/new_page(), а НЕ
    BrowserContext.new_page() (у него вообще нет таких параметров). Поэтому
    на каждый new_page(**kwargs) создаём свой BrowserContext с этими же
    опциями и запоминаем его — чтобы гарантированно закрыть в finally
    _get_browser, даже если сама функция-скрейпер page.close() не вызвала."""

    def __init__(self, browser):
        self._browser = browser
        self._contexts: list = []

    async def new_page(self, **kwargs):
        context = await self._browser.new_context(**kwargs)
        self._contexts.append(context)
        return await context.new_page()

    async def close_all(self) -> None:
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception as e:
                log.warning(f"Не закрылся BrowserContext: {e}")


@asynccontextmanager
async def _get_browser(shared=None):
    """Отдаёт готовый Chromium (`browser.new_page()` можно звать сколько
    угодно раз параллельно — каждый вызов открывает изолированную вкладку).

    19.07.2026: раньше КАЖДАЯ из ~19 функций `_search_*_playwright` сама
    поднимала ПОЛНЫЙ отдельный процесс Chromium (свой `async_playwright()` +
    `chromium.launch()`) — на один товар это до 9 живых запусков (10 из 19
    брендовых сразу возвращают `[]` без браузера, см. `if brand.lower()...`
    в начале каждой такой функции). BROWSER_SEMAPHORE(2) не даёт им работать
    больше 2 одновременно, но ВСЕ 9 всё равно проходят через полный цикл
    запуск-инициализация-закрытие процесса Chromium по очереди — самая
    дорогая часть (старт V8/движка) выполняется многократно впустую.
    Теперь `_playwright_fallback()` поднимает ОДИН браузер и передаёт его
    сюда во все функции разом — они открывают в нём свои вкладки параллельно
    (без встроенного взаимного ожидания), а закрывает браузер только тот, кто
    его поднял. Для вызовов без общего браузера (обратная совместимость)
    поведение не изменилось — свой процесс, тот же BROWSER_SEMAPHORE(2)."""
    if shared is not None:
        # Раньше отдавали shared-browser напрямую — вызывающие функции делают
        # `page = await browser.new_page(...)`, но НИ ОДНА из ~20 функций
        # `_search_*_playwright` эту page не закрывает (page.close() не
        # вызывается нигде в файле). При параллельном asyncio.gather на один
        # товар (_playwright_fallback) это ~20 одновременно открытых вкладок
        # на ОДНОМ Chromium — под такой нагрузкой общий браузер падал.
        # Вместо правки каждой функции — заворачиваем shared browser в
        # _TrackedBrowser (тот же API .new_page(**kwargs), вызывающий код не
        # меняется ни строкой) и гарантированно закрываем все созданные им
        # BrowserContext в finally, независимо от того, закрыла ли page сама
        # функция. _PAGE_SEMAPHORE дополнительно ограничивает число
        # одновременно открытых вкладок на общем браузере (а не число
        # процессов Chromium — для этого есть BROWSER_SEMAPHORE).
        async with _PAGE_SEMAPHORE:
            proxy = _TrackedBrowser(shared)
            try:
                yield proxy
            finally:
                await proxy.close_all()
        return
    from playwright.async_api import async_playwright
    async with BROWSER_SEMAPHORE:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--disable-gpu", "--disable-software-rasterizer"],
            )
            try:
                yield browser
            finally:
                await browser.close()


async def _fetch_lazy_image(url: str) -> str | None:
    """Открывает страницу через headless Chromium и ждёт пока загрузится
    реальное товарное фото (для сайтов с JS-lazy loading вроде al-style.kz).
    Возвращает URL крупнейшего товарного `<img>` или None."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright not installed — lazy fetch disabled")
        return None

    try:
        # Не больше 2 браузеров одновременно на процесс (см. common.py:
        # 12.07.2026 параллельные Chromium уронили машину)
        async with BROWSER_SEMAPHORE:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-gpu", "--disable-software-rasterizer"])
                try:
                    page = await browser.new_page(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                        viewport={"width": 1280, "height": 800},
                    )
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    # Берём крупнейший <img> с подозрительно товарным путём
                    imgs = await page.evaluate(
                        """
                        () => Array.from(document.images)
                          .filter(i => i.naturalWidth >= 200 && i.naturalHeight >= 200)
                          .map(i => ({
                            src: i.currentSrc || i.src,
                            w: i.naturalWidth,
                            h: i.naturalHeight,
                          }))
                          .sort((a, b) => b.w * b.h - a.w * a.h)
                        """
                    )
                    for entry in imgs:
                        src = entry.get("src", "")
                        if not src or _is_bad_image(src):
                            continue
                        log.info(f"Lazy fetch {url} → {src} ({entry['w']}x{entry['h']})")
                        return src
                    return None
                finally:
                    await browser.close()
    except Exception as e:
        log.warning(f"Lazy fetch failed for {url}: {e}")
        return None


async def _candidate_url(url: str, product: str, brand: str = "") -> str | None:
    """Извлекает финальный URL картинки из страницы или прямой ссылки."""
    if not url:
        return None
    domain = urlparse(url).netloc.replace("www.", "")
    if domain in BLACKLIST:
        return None
    # Зеркала с водяными знаками — фото испорчено лого поверх товара.
    if any(b in url.lower() for b in _WATERMARK_DOMAINS):
        return None
    if not _url_matches_brand(url, product, brand):
        return None
    # Фото нескольких вариантов: "FROZN-A410-DW-and-DK", "black-and-white" и т.п.
    # — два товара на одном снимке, нам не подходит.
    _fname = url.split("/")[-1].lower().split("?")[0]
    if re.search(r'[-_]and[-_]', _fname):
        log.debug(f"Multi-variant URL rejected: {_fname[:60]}")
        return None
    # Фильтр по варианту модели применяем к URL СТРАНИЦЫ (не картинки).
    # Страница tecno-spark-50-5g-... отклоняется здесь, до fetch.
    if not _url_matches_model(url, product, brand):
        log.debug(f"Page URL rejected (wrong variant): {url.split('/')[-1][:60]}")
        return None

    url_clean = url.lower().split("?")[0]
    if any(url_clean.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return _upgrade_image_url(url)

    og_url = await _fetch_og_image(url)
    if og_url and not _is_bad_image(og_url):
        return _upgrade_image_url(og_url)

    # Fallback для JS-lazy сайтов (al-style.kz и т.п.)
    if domain in _LAZY_DOMAINS:
        lazy_url = await _fetch_lazy_image(url)
        if lazy_url and not _is_bad_image(lazy_url):
            return _upgrade_image_url(lazy_url)

    return None


def _mask_from_rgba(rgba: Image.Image) -> np.ndarray:
    """Бинарная маска формы товара: alpha > threshold → True.
    Кропится по bbox альфы и ресайзится до квадрата _MASK_SIZE."""
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        alpha = alpha.crop(bbox)
    alpha = alpha.resize((_MASK_SIZE, _MASK_SIZE), Image.LANCZOS)
    return np.asarray(alpha) > _ALPHA_THRESHOLD


def _mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    """IoU двух бинарных масок одного размера."""
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union > 0 else 0.0


def _phash(data: bytes) -> np.ndarray | None:
    """Маленький цветной отпечаток (8×8 RGB) — для дедупа дополнительных
    фото, которые приходят без rembg-маски (Playwright-источники: apltech и
    т.п. часто отдают один и тот же сток-снимок для разных SKU/страниц,
    IoU-проверка по маске их не отсекает, т.к. mask=None). RGB (не grayscale) —
    иначе разные по цвету товары с похожей композицией ложно считались бы
    дубликатами (яркость может совпадать при разном цвете)."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((8, 8), Image.LANCZOS)
        return np.asarray(img, dtype=np.float32)
    except Exception:
        return None


def _phash_similar(a: np.ndarray | None, b: np.ndarray | None, max_mean_diff: float = 8.0) -> bool:
    """True если средняя разница по RGB-каналам (0-255) ниже порога —
    значит, скорее всего, один и тот же снимок (другой размер/пережатие)."""
    if a is None or b is None:
        return False
    return float(np.abs(a - b).mean()) <= max_mean_diff


def _count_components(mask: np.ndarray) -> int:
    """
    Количество значимых связных компонентов в маске.
    Перед counting делаем morphological erosion — разрывает тонкие соединения
    между касающимися объектами (например, телефоны в коллаже-пирамидке).
    Мелкие пятна < _MIN_COMPONENT_FRAC общей маски считаются мусором.
    Один товар → 1, коллаж/грид → 3+.
    """
    from scipy import ndimage
    # Эрозия структурным элементом 3×3, 4 итерации — съедает соединения <~8px
    eroded = ndimage.binary_erosion(mask, iterations=12)
    labels, n = ndimage.label(eroded)
    if n == 0:
        return 0
    total_mass = eroded.sum()
    if total_mass == 0:
        return 0
    sizes = ndimage.sum(eroded, labels, range(1, n + 1))
    threshold = total_mass * _MIN_COMPONENT_FRAC
    return int((np.asarray(sizes) >= threshold).sum())


def _score_image(data: bytes, product: str,
                  relaxed: bool = False,
                  expected_color_en: str = "",
                  category: str = "") -> tuple[float, np.ndarray | None]:
    """
    Комбинированный скор + бинарная маска силуэта (256×256).
    1. CLIP — насколько фото похоже на товар (0..1)
    2. rembg + preserve_ratio — не обрезок ли товар
    3. Площадь изображения — больше = лучше

    `relaxed=True` — fallback-режим: пониженные пороги (для нишевых товаров
    у которых в интернете мало хороших фото).
    """
    try:
        from services.image.clip_scorer import clip_score
        from services.image.background import _get_session
        from rembg import remove

        orig = Image.open(io.BytesIO(data)).convert("RGB")
        orig_area = orig.width * orig.height

        min_side = 300 if relaxed else (_MIN_SIDE_SSD if category == "SSD накопители" else _MIN_SIDE)
        if min(orig.width, orig.height) < min_side:
            log.info(f"Resolution {orig.width}x{orig.height} < {min_side} → отклонён")
            return 0.0, None

        max_aspect = (_MAX_ASPECT_RAM if category in ("Оперативная память", "SSD накопители")
                      else _MAX_ASPECT_KEYBOARD if category == "Клавиатуры"
                      else _MAX_ASPECT)
        aspect = max(orig.width, orig.height) / min(orig.width, orig.height)
        if aspect > max_aspect:
            log.info(f"Aspect {aspect:.2f} > {max_aspect} → баннер/коллаж, отклонён")
            return 0.0, None

        # Порог relaxed 0.30 (не 0.18): новый сбалансированный скорер (03.07.2026)
        # даёт живым фото 0.75-0.99, баннерам/мусору 0.18-0.24 — запас большой.
        clip_threshold = 0.30 if relaxed else 0.40
        c_score = clip_score(data, product, category=category)
        if c_score < clip_threshold:
            log.info(f"CLIP score {c_score:.3f} < {clip_threshold} → отклонён")
            return 0.0, None

        if max(orig.width, orig.height) > _SCORE_MAX_SIDE:
            _scale = _SCORE_MAX_SIDE / max(orig.width, orig.height)
            small = orig.resize(
                (max(1, round(orig.width * _scale)), max(1, round(orig.height * _scale))),
                Image.LANCZOS,
            )
        else:
            small = orig
        small_area = small.width * small.height

        _buf = io.BytesIO()
        small.save(_buf, format="PNG")
        session = _get_session()
        result = remove(_buf.getvalue(), session=session)
        rembg_img = Image.open(io.BytesIO(result)).convert("RGBA")
        bbox = rembg_img.getchannel("A").getbbox()
        rembg_cropped = rembg_img.crop(bbox) if bbox else rembg_img
        cropped_area = rembg_cropped.width * rembg_cropped.height
        # preserve — отношение площадей в масштабе даунскейла (scale-invariant,
        # совпадает с тем, что дал бы полный размер), а score ниже всё равно
        # использует orig_area (реальное разрешение) для "больше = лучше".
        preserve = cropped_area / small_area if small_area > 0 else 0

        if category == "SSD накопители":
            _cat_preserve_min = _PRESERVE_MIN_SSD
        elif category == "Кроссовки":
            _cat_preserve_min = _PRESERVE_MIN_SHOES
        else:
            _cat_preserve_min = 0.50
        preserve_min = 0.38 if relaxed else _cat_preserve_min
        if preserve < preserve_min:
            log.info(f"preserve {preserve:.2f} < {preserve_min} → отклонён")
            return 0.0, None

        # ── Проверка пропорций вырезанного объекта ──────────────────────────
        # После rembg bbox должен быть похож на телефон/планшет/устройство.
        # Слишком узкий/тонкий результат → rembg вырезал экранный контент или
        # тонкую деталь вместо всего устройства (кейс Oscal Flat 2 Glacier Blue:
        # яркие обои → rembg выделяет экран, crop 270×579 = aspect 2.14).
        cw_, ch_ = rembg_cropped.width, rembg_cropped.height
        if cw_ > 0 and ch_ > 0:
            crop_asp = max(cw_, ch_) / min(cw_, ch_)
            # Порог 2.0 (строгий) / 3.5 (relaxed): телефон ~0.5 aspect, экранный
            # вырез ≥ 2.0; башенный кулер сбоку ≈ 2.5-3.0 — relaxed пропускает.
            # Порог 2.0: телефон ~0.45-0.55 aspect, экранный вырез ≥ 2.0
            # Для ОЗУ и SSD — сама плата узкая и длинная, crop_asp легитимно ~2.5-4.3.
            crop_asp_limit = max_aspect if category in ("Оперативная память", "SSD накопители") else 2.0
            if crop_asp > crop_asp_limit:
                log.info(f"rembg crop aspect {crop_asp:.1f} > {crop_asp_limit} → экранный контент вместо устройства, отклонён")
                return 0.0, None
        # (2) preserve очень высокий (>0.88) + bbox касается краёв → весь кадр
        #     целиком (lifestyle-фото с людьми, rembg не срезал фон, кейс HOT 12).
        # Проверка lifestyle (preserve высокий + bbox до краёв) убрана:
        # студийные фото на белом фоне тоже имеют preserve≈1.0 и bbox до краёв,
        # проверка убивала лучших кандидатов (Infinix SMART 6 Plus, Oscal Flat 2).
        # Lifestyle-фото с людьми ловит aspect-ratio check выше + Gemini Vision.

        mask = _mask_from_rgba(rembg_img)
        if not relaxed:
            from scipy import ndimage as _ndi
            # Bbox fill: три планшета по диагонали → bbox большой, силуэт редкий.
            # Один товар заполняет bbox на 70-90%, lineup — на 35-50%.
            bbox_rows = np.where(mask.any(axis=1))[0]
            bbox_cols = np.where(mask.any(axis=0))[0]
            if len(bbox_rows) > 0 and len(bbox_cols) > 0:
                bbox_area = int(bbox_rows[-1] - bbox_rows[0] + 1) * int(bbox_cols[-1] - bbox_cols[0] + 1)
                fill_ratio = float(mask.sum()) / max(bbox_area, 1)
                if fill_ratio < 0.40:
                    log.info(f"Sparse bbox fill {fill_ratio:.2f} < 0.40 → lineup, отклонён")
                    return 0.0, None

            # Проверка внутренних "дыр": rembg вырезал экран/контент внутри товара.
            filled = _ndi.binary_fill_holes(mask)
            hole_frac = (filled & ~mask).sum() / max(filled.sum(), 1)
            if hole_frac > 0.20:
                log.info(f"Interior holes {hole_frac:.1%} > 20% → rembg вырезал экран, отклонён")
                return 0.0, None

            # Проверка посторонних элементов (бейджи, иконки, накладки):
            # отдельный несвязанный элемент без формы пера → маркетинговый оверлей.
            # Перо: очень вытянутое (aspect > 3). Бейдж/иконка: квадратное/круглое (aspect < 3).
            raw_labels, raw_n = _ndi.label(mask)
            if raw_n > 1:
                raw_sizes = np.array([float(_ndi.sum(mask, raw_labels, i))
                                      for i in range(1, raw_n + 1)])
                main_sz = float(raw_sizes.max())
                for idx, sz in enumerate(raw_sizes, 1):
                    if not (main_sz * 0.02 < sz < main_sz * 0.60):
                        continue  # шум или сам товар
                    comp_mask = raw_labels == idx
                    rows = np.where(comp_mask.any(axis=1))[0]
                    cols = np.where(comp_mask.any(axis=0))[0]
                    if len(rows) == 0 or len(cols) == 0:
                        continue
                    h = int(rows[-1] - rows[0] + 1)
                    w = int(cols[-1] - cols[0] + 1)
                    asp = max(h, w) / max(min(h, w), 1)
                    if asp < 3.0:
                        log.info(f"Badge/overlay detected (size={sz/main_sz:.1%}, aspect={asp:.1f}) → отклонён")
                        return 0.0, None

            n_comp = _count_components(mask)
            if n_comp > _MAX_COMPONENTS:
                log.info(f"components={n_comp} > {_MAX_COMPONENTS} → коллаж/грид, отклонён")
                return 0.0, None


        score = c_score * preserve * orig_area

        # Цветовое совпадение — умножаем score на коэф. соответствия
        if expected_color_en:
            from services.image.color_match import color_matches
            match, detected, rgb = color_matches(rembg_img, expected_color_en)
            if not match:
                # Mismatch — мощный штраф, чтобы цвет-совпадающие фото
                # ушли наверх, но не полное отклонение (вдруг это лучший
                # из возможных при отсутствии нужного цвета).
                # 0.05 (было 0.10): большое фото чужого цвета не должно
                # перебивать меньшее, но НУЖНОГО цвета (кейс Aurora Purple).
                score *= 0.05
                log.info(f"Color mismatch → score × 0.05 (was {score*20:,.0f})")
            elif detected is None and rgb is not None:
                # Цвет товара есть, но не отнесён ни к одному классу (мутный/
                # неоднозначный) — мягкий штраф, чтобы такие не перебивали фото
                # с ЧЁТКО совпавшим цветом.
                score *= 0.5
                log.info(f"Color undetermined (RGB={rgb}) → score × 0.5")
        log.info(f"Score: clip={c_score:.3f}, preserve={preserve:.2f}, area={orig_area:,} → {score:,.0f}")
        return score, mask

    except Exception as e:
        log.debug(f"scoring failed: {e}")
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            return float(img.width * img.height), None
        except Exception:
            return float(len(data)), None


async def _llm_validate_candidates(product: str,
                                    urls: list[str],
                                    llm) -> set[str]:
    """
    Спрашивает LLM, какие URL являются настоящим фото товара `product`,
    а не упаковкой, рекламой, другой моделью или аксессуаром.
    Возвращает множество URL, которые LLM подтвердил.
    """
    if not urls:
        return set()

    numbered = "\n".join(f"{i+1}. {u}" for i, u in enumerate(urls))
    prompt = (
        "Ты помогаешь отобрать фото товара из выдачи поиска. "
        "Для каждого URL реши: это РЕАЛЬНОЕ ОДИНОЧНОЕ фото указанного товара?\n\n"
        "Отклоняй (NO):\n"
        "— другая модель того же бренда (Galaxy A55 вместо A56)\n"
        "— баннер/реклама с текстом\n"
        "— фото с КОРОБКОЙ, упаковкой, чехлом, зарядкой, кабелями. Типичные "
        "  маркеры в имени файла: «box», «package», «packaging», «with-box», "
        "  «in-box», «unboxing», «kit», «set», «bundle», «with-case», "
        "  «accessories», «retail», «complect», «комплект»\n"
        "— КОЛЛАЖ из нескольких цветовых вариантов: «product-colors», "
        "  «all-colors», «lineup», «family», «variants», «range», "
        "  «collection», «group»\n"
        "— миниатюра/thumb: «-thumb», «-small», «-mini», «-preview»\n\n"
        "Анализируй ИМЯ ФАЙЛА и ПУТЬ в URL. "
        "Если ничего против явно не сказано — YES.\n\n"
        "Отвечай СТРОГО построчно `НОМЕР: YES|NO`. Без пояснений."
    )
    user = f"Товар: {product}\n\nURL для проверки:\n{numbered}"
    try:
        # max_tokens=20 на URL (строка вида "12: YES") с запасом — без лимита
        # выходило в среднем 645 output-токенов на список из нескольких строк
        # YES/NO (та же находка по reasoning-токенам, что в wb_create.py).
        resp = await llm.chat(user, prompt, max_tokens=max(30, 20 * len(urls)), enable_thinking=False)
        try:
            from config import settings as _settings
            from utils.billing import save_cost as _save_cost
            await _save_cost(_settings.admin_id, "photo_url_filter", response=resp)
        except Exception:
            pass
        approved: set[str] = set()
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            num_part, verdict = line.split(":", 1)
            num_part = num_part.strip().rstrip(".")
            verdict = verdict.strip().upper()
            if not num_part.isdigit():
                continue
            idx = int(num_part) - 1
            if 0 <= idx < len(urls) and verdict.startswith("YES"):
                approved.add(urls[idx])
        log.info(f"LLM validation: {len(approved)}/{len(urls)} URLs approved")
        return approved
    except Exception as e:
        log.warning(f"LLM validation failed: {e} — все URL приняты по умолчанию")
        return set(urls)


# Нишевые бренды — новые модели редко попадают в Exa вовремя.
# Для них сразу идём на Playwright-уровень (apltech + kaspi).
# Официальные сайты брендов для прямого поиска фото
_BRAND_OFFICIAL_SITES: dict[str, str] = {
    "tecno":     "https://www.tecno-mobile.com/ru/search/?q={query}",
    "infinix":   "https://www.infinixmobility.com/ru/search?q={query}",
    "itel":      "https://www.itel-mobile.com/search?q={query}",
    "blackview": "https://www.blackview.hk/search?q={query}",
    "oscal":     "https://www.oscal.com/search?q={query}",
    "doogee":    "https://www.doogee.cc/search?q={query}",
    "oukitel":   "https://www.oukitel.com/search?q={query}",
    "wacom":      "https://estore.wacom.com/en-US/catalogsearch/result/?q={query}",
    "xp-pen":     "https://www.xp-pen.com/search.html?keyword={query}",
    "jabra":      "https://www.jabra.com/search?q={query}",
    "poly":       "https://www.poly.com/us/en/search?q={query}",
    "plantronics":"https://www.poly.com/us/en/search?q={query}",
    "sennheiser": "https://www.sennheiser.com/en-US/search?q={query}",
    "logitech":   "https://www.logitech.com/en-us/search?q={query}",
    "dreame":     "https://ru.dreametech.com/search?q={query}",
    # ecovacs исключён отсюда 14.07.2026 — собственный /search JS-driven и
    # ненадёжен, заменён на _search_ecovacs_playwright (sitemap-индексация).
    # rapoo — проверено вживую 21.07.2026 (WebFetch): /search?q= отдаёт
    # результаты, товары лежат по /products/... (Shopify), подходит под
    # фильтр _url_matches_model без правок.
    "rapoo":      "https://shop.rapoo.com/search?q={query}",
    # acer НЕ добавлен 21.07.2026 — оба опробованных пути (store.acer.com,
    # acer.com/search) либо рвут соединение, либо висят по таймауту при
    # проверке WebFetch. Добавлять непроверенный шаблон рискованно — либо
    # тихо ничего не найдёт, либо будет тянуть таймаут каждый раз впустую.
}

_NICHE_BRANDS = {
    "infinix", "tecno", "itel", "oscal", "blackview", "doogee",
    "oukitel", "ulefone", "cubot", "umidigi", "blu", "homtom",
    "elephone", "leagoo", "lava", "karbonn", "micromax", "wiko",
    "zte", "nubia", "tcl", "alcatel", "cat", "crosscall",
}


_VARIANT_SUFFIXES = ["5g", "pro", "plus", "max", "ultra", "lite",
                     "mini", "neo", "air", "fold", "flip", "edge", "go", "se"]


def _url_matches_model(url: str, product: str, brand: str = "") -> bool:
    """Проверяет что URL-слаг содержит токены модели с границей слова.
    Дополнительно: если URL содержит вариантный суффикс (5g/pro/plus/neo/...)
    которого нет в названии продукта — отклоняем (Spark 50 ≠ Spark 50 5G)."""
    tokens = _model_url_tokens(product, brand)
    if not tokens:
        return True
    url_low = url.lower()

    # Проверяем совпадение токенов модели
    matched = False
    for t in tokens:
        if len(t) <= 4 and not any(sep in t for sep in " -_"):
            # Короткие цельные токены (gx, g2, g12...) — граница слова с ОБЕИХ
            # сторон, иначе "gx05"/"g1234" матчится под "gx"/"g12" (другая модель)
            pattern = r"(?:^|[^a-z0-9])" + re.escape(t) + r"(?:[^a-z0-9]|$)"
        else:
            pattern = re.escape(t) + r"(?:[^a-z0-9]|$)"
        if re.search(pattern, url_low):
            matched = True
            break

    if not matched:
        # Фолбэк: модель слитно в названии («Z40TangleCutFlex»), а сайт
        # дефисует по-своему («z40-tanglecut-flex» — не «z40-tangle-cut-flex»,
        # заранее не угадать где именно). Сравниваем оба без разделителей
        # вообще. Только для достаточно длинных токенов (>=6) — короткие
        # без разделителей слишком часто совпадают случайно.
        last_segment = url_low.rstrip("/").rsplit("/", 1)[-1]
        segment_stripped = re.sub(r"[^a-z0-9]", "", last_segment)
        for t in tokens:
            t_stripped = re.sub(r"[^a-z0-9]", "", t)
            if len(t_stripped) >= 6 and t_stripped in segment_stripped:
                matched = True
                break

    if not matched:
        return False

    # Проверяем лишние суффиксы варианта
    product_low = product.lower()
    for suffix in _VARIANT_SUFFIXES:
        in_url = bool(re.search(r"(?<![a-z])" + suffix + r"(?![a-z0-9])", url_low))
        in_product = suffix in product_low
        if in_url and not in_product:
            log.debug(f"URL rejected: variant suffix '{suffix}' in URL but not in product '{product}'")
            return False

    return True


async def _search_kaspi_playwright(product: str, color: str = "",
                                    brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на kaspi.kz через Playwright.
    kaspi → поиск → первые 3 карточки → CDN resources.cdn-kaspi.kz (gallery-large ≈ 290KB).
    Возвращает list[(bytes, page_url)]."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — kaspi fallback недоступен")
        return []

    query = f"{product} {color}".strip()
    search_url = f"https://kaspi.kz/shop/search/?q={query.replace(' ', '%20')}&c=750000000&sc=-1"
    log.info(f"kaspi Playwright search: {search_url}")
    UA_K = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA_K)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Берём ссылки на карточки, фильтруем по токенам модели
            all_product_urls = await page.eval_on_selector_all(
                "a[href*='/shop/p/']",
                "els => [...new Set(els.map(e => e.href))].slice(0, 10)"
            )
            product_urls = [
                u for u in all_product_urls
                if "/shop/p/" in u and _url_matches_model(u, product, brand)
            ][:3]
            log.info(f"kaspi: {len(product_urls)}/{len(all_product_urls)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                    # Собираем CDN-URL фото товара
                    img_urls = await page.eval_on_selector_all(
                        "img[src*='cdn-kaspi.kz']",
                        "els => els.map(e => e.src)"
                    )
                    # Убираем gallery-medium → gallery-large для полного разрешения
                    big_urls = []
                    for u in img_urls:
                        u = re.sub(r"\?format=gallery-medium", "?format=gallery-large", u)
                        u = re.sub(r"\?format=preview-[^&]+", "?format=gallery-large", u)
                        if "cdn-kaspi.kz" in u:
                            big_urls.append(u)

                    # Дедупликация (один и тот же CDN-хеш повторяется)
                    seen = set()
                    for img_url in big_urls:
                        key = img_url.split("?")[0]
                        if key in seen:
                            continue
                        seen.add(key)
                        # Скачиваем через in-browser fetch (обходит hotlink)
                        js = f"""fetch("{img_url}", {{headers: {{Referer: "https://kaspi.kz/"}}}})
                                .then(r => r.arrayBuffer())
                                .then(b => Array.from(new Uint8Array(b)))"""
                        arr = await page.evaluate(js)
                        data = bytes(arr)
                        if len(data) > 10_000:
                            captured.append((data, prod_url))
                            log.info(f"kaspi fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"kaspi page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break

            log.info(f"kaspi Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"kaspi Playwright search failed: {e}")
        return []


async def _search_apltech_playwright(product: str, color: str = "",
                                      brand: str = "", browser=None) -> list[bytes]:
    """Ищет и скачивает фото товара на apltech.kz через Playwright.
    Сначала поиск → берём URL первого товара → открываем → перехватываем
    CDN-изображение (WebP 650×650). Возвращает list[bytes]."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — apltech fallback недоступен")
        return []
    import asyncio as _aio
    query = f"{product} {color}".strip()
    search_url = f"https://www.apltech.kz/search/?q={query.replace(' ', '+')}"
    log.info(f"apltech Playwright search: {search_url}")
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            # ── Шаг 1: ищем /tovar/ ссылки через SearXNG site:-запрос
            #   (надёжнее чем парсить JS-поиск; раньше тут была платная Exa)
            sx_resp = await searxng_search.search_urls(
                query, max_results=5, include_domains=["apltech.kz"]
            )
            all_tovar = [r.url for r in sx_resp.results if "/tovar/" in r.url]
            # Фильтруем по токенам модели — отсекаем похожие но другие модели
            product_urls = [u for u in all_tovar if _url_matches_model(u, product, brand)][:3]
            log.info(f"apltech SearXNG: {len(product_urls)}/{len(all_tovar)} /tovar/ ссылок (model-matched)")

            if not product_urls:
                return []

            # ── Шаг 2: открываем каждый товар и скачиваем фото через
            #   внутрибраузерный fetch (те же куки/заголовки что у браузера →
            #   сайт пропускает, а прямой HTTP-запрос отдаёт 403).
            _JS_FETCH = """
                async (url) => {
                    try {
                        const r = await fetch(url);
                        if (!r.ok) return null;
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        for (let b of bytes) bin += String.fromCharCode(b);
                        return btoa(bin);
                    } catch(e) { return null; }
                }
            """
            # Возвращаем (bytes, prod_url) чтобы LLM-валидатор видел реальный URL
            captured: list[tuple[bytes, str]] = []
            import base64 as _b64

            for prod_url in product_urls:
                await page.goto(prod_url, wait_until="domcontentloaded", timeout=15000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                await _aio.sleep(1)

                img_src = await page.evaluate("""
                    () => {
                        const imgs = Array.from(document.images)
                            .filter(i => i.naturalWidth > 300
                                && !i.src.includes('placeholder'))
                            .sort((a,b) => b.naturalWidth*b.naturalHeight
                                         - a.naturalWidth*a.naturalHeight);
                        return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                    }
                """)
                if not img_src:
                    continue

                b64 = await page.evaluate(_JS_FETCH, img_src)
                if b64:
                    data = _b64.b64decode(b64)
                    if len(data) > 5_000:
                        # Сохраняем URL страницы товара для LLM-валидации
                        captured.append((data, prod_url))
                        log.info(f"apltech fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                if len(captured) >= 3:
                    break

            log.info(f"apltech Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"apltech Playwright search failed: {e}")
        return []


async def _search_dreametech_playwright(product: str, color: str = "",
                                         brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на dreametech.by (белорусский официальный ритейлер Dreame) —
    добавлен 14.07.2026: новые модели (Z40TangleCutFlex, G12 Pro) не находились
    ВООБЩЕ нигде (Exa тоже 0 кандидатов) — оказалось, у dreametech.by уже есть
    крупные фото (1000×1000), просто это .by-домен, которого не было в пуле
    источников (все остальные — .ru/.kz). Собственный /search на сайте не
    фильтрует выдачу (отдаёт общий каталог) — поэтому находим страницу товара
    через SearXNG site:-запрос (как у apltech), затем рендерим её и качаем фото."""
    if brand.lower().strip() != "dreame":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — dreametech fallback недоступен")
        return []
    import asyncio as _aio
    import base64 as _b64

    query = f"{product} {color}".strip()
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            sx_resp = await searxng_search.search_urls(
                query, max_results=5, include_domains=["dreametech.by"]
            )
            # Реальная карточка товара — минимум 3 сегмента пути
            # (/catalog/{категория}/{слаг}/), категории/меню короче.
            all_links = [r.url for r in sx_resp.results if "/catalog/" in r.url]
            deep_links = [u for u in all_links if u.rstrip("/").count("/") >= 5]
            product_urls = [u for u in deep_links if _url_matches_model(u, product, brand)][:3]
            log.info(f"dreametech.by SearXNG: {len(product_urls)}/{len(deep_links)} карточек (model-matched)")

            if not product_urls:
                return []

            _JS_FETCH = """
                async (url) => {
                    try {
                        const r = await fetch(url);
                        if (!r.ok) return null;
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        for (let b of bytes) bin += String.fromCharCode(b);
                        return btoa(bin);
                    } catch(e) { return null; }
                }
            """
            captured: list[tuple[bytes, str]] = []
            for prod_url in product_urls:
                await page.goto(prod_url, wait_until="domcontentloaded", timeout=15000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                await _aio.sleep(1)

                img_src = await page.evaluate("""
                    () => {
                        const imgs = Array.from(document.images)
                            .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                && !i.src.includes('logo') && !i.src.includes('icon'))
                            .sort((a,b) => b.naturalWidth*b.naturalHeight
                                         - a.naturalWidth*a.naturalHeight);
                        return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                    }
                """)
                if not img_src or _is_bad_image(img_src):
                    continue

                b64 = await page.evaluate(_JS_FETCH, img_src)
                if b64:
                    data = _b64.b64decode(b64)
                    if len(data) > 10_000:
                        captured.append((data, prod_url))
                        log.info(f"dreametech.by fetch OK: {len(data):,}b  {prod_url.split('/')[-2][:50]}")
                if len(captured) >= 3:
                    break

            log.info(f"dreametech.by Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"dreametech.by Playwright search failed: {e}")
        return []


_SAMSUNG_ACCESSORY_MARKERS = (
    "accessories", "case", "cover", "protector", "charger", "-cable",
    "strap", "-band", "band-", "adapter", "wallet", "flipsuit", "kindsuit",
)


async def _search_samsung_playwright(product: str, color: str = "",
                                      brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на samsung.com/ru через sitemap-индексацию (15.07.2026, второй
    кандидат нового направления — см. project_search_improvements). PDP-слаги
    содержат модель+цвет+SKU (galaxy-s23-ultra-phantom-black-256gb-sm-s918bzkgser)
    — идеальны для токен-матчинга. Флагманы без FE (S24/S25 не-FE, Note) официально
    не продаются в РФ с 2022 — их просто нет в sitemap, это ограничение каталога
    магазина, не скрапера. В отличие от ecovacs/lenovo — images.samsung.com не
    блокирует in-page fetch() (CORS открыт), качаем прямо в браузере; плюс через
    URL-параметр ?$2000_2000_PNG$ можно апгрейдить разрешение."""
    if brand.lower().strip() != "samsung":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — samsung fallback недоступен")
        return []

    _CATEGORY_SITEMAPS = ["im", "vd", "da", "memory"]
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            for cat in _CATEGORY_SITEMAPS:
                try:
                    async with s.get(f"https://www.samsung.com/ru/{cat}-sitemap.xml",
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            all_urls.extend(re.findall(r"<loc>([^<]+)</loc>", await r.text()))
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"samsung.com sitemap fetch failed: {e}")
        return []

    # Аксессуары (чехлы/ремешки/зарядки) содержат те же токены модели, что и
    # само устройство — отсекаем по маркерам в URL, иначе они забьют топ-3.
    candidates = [u for u in all_urls
                  if not any(m in u.lower() for m in _SAMSUNG_ACCESSORY_MARKERS)
                  and _url_matches_model(u, product, brand)]

    # _url_matches_model проверяет суффиксы варианта только в одну сторону
    # (суффикс в URL, которого нет в товаре → отклонить), но не наоборот —
    # для линеек с десятками PDP одной модели (S23 обычный + S23 Ultra + S23 FE
    # в одном sitemap) это даёт ложное совпадение "S23 Ultra" → "galaxy-s23"
    # (базовая версия). Здесь, в отличие от поиска по сайтам, фильтруем URL
    # напрямую из полного списка модели — доразбираем строго: если в названии
    # товара есть суффикс варианта, он ОБЯЗАН быть и в URL.
    product_low = product.lower()
    required_suffixes = [s for s in _VARIANT_SUFFIXES if s in product_low]
    if required_suffixes:
        candidates = [
            u for u in candidates
            if all(re.search(r"(?<![a-z])" + s + r"(?![a-z0-9])", u.lower()) for s in required_suffixes)
        ]
    log.info(f"samsung.com sitemap: {len(candidates)}/{len(all_urls)} товарных URL (model-matched)")
    if not candidates:
        return []

    # Приоритет нужному цвету — иначе берём первые попавшиеся 3 из N цветовых
    # вариантов одной модели в произвольном порядке сайта.
    color_tokens: set[str] = set()
    for w in re.split(r"[\s\-_]+", (color or "").lower()):
        if len(w) >= 3:
            color_tokens.add(w)
    if color_tokens:
        candidates.sort(key=lambda u: 0 if any(t in u.lower() for t in color_tokens) else 1)

    product_urls = candidates[:3]

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=15000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('images.samsung.com'))
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:2]:
                        if _is_bad_image(img_url):
                            continue
                        # Апгрейд разрешения через IS4-параметр (?$WxH_FMT$)
                        hi_res = re.sub(r"\$\d+_\d+_(PNG|JPG)\$", r"$2000_2000_\1$", img_url)
                        b64 = await page.evaluate(_JS_FETCH, hi_res)
                        if not b64:
                            b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"samsung.com fetch OK: {len(data):,}b  {prod_url.rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"samsung.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"samsung.com Playwright search failed: {e}")
        return []

    log.info(f"samsung.com Playwright: {len(captured)} фото получено")
    return captured[:3]


def _lenovo_url_matches(url: str, product: str) -> bool:
    """_url_matches_model собирает модель в ОДИН слитный токен (рассчитан на
    короткие коды типа 'G12 Pro' → 'g12pro') — для длинных структурных имён
    ноутбуков Lenovo ('ThinkPad E14 Gen 6 Intel') это ломается: в URL сайта
    между токенами модели вклинивается диагональ размера экрана
    ('...e14-gen-6-14-inch-intel'), которой нет в названии товара, и слитный
    поиск подстроки не находит совпадение вообще. Здесь вместо этого — мешок
    слов: каждое отдельное слово названия (кроме бренда) должно встретиться
    в URL как отдельный токен, неважно в каком порядке и что между ними.
    Важно: 'intel'/'amd' — тоже обычные слова тут, так что процессорная
    платформа автоматически различается (иначе матчилось бы на другой CPU)."""
    words = [w for w in re.split(r"[\s\-]+", product.lower()) if w and w != "lenovo"]
    if not words:
        return True
    url_low = url.lower()
    return all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", url_low) for w in words)


async def _search_lenovo_playwright(product: str, color: str = "",
                                     brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на lenovo.com через sitemap-индексацию (15.07.2026, третий
    кандидат — см. project_search_improvements). РФ-версия (lenovo.com/ru/ru/)
    без своего sitemap (404) — реально живой каталог только Беларусь-RU
    (lenovo.com/by/ru/), найден через sitemap-auto/007-intsitemap-by-ru.xml.
    Akamai блокирует обычный aiohttp-запрос (403 "Access Denied" по TLS/JA3-
    фингерпринту) — даже сам sitemap.xml нужно забирать настоящим headless
    Chromium, не HTTP-клиентом. Скачивание фото — тот же CORS-обход, что и
    ecovacs (CDN p*-ofp.static.pub ≠ lenovo.com, отдельный aiohttp-шаг)."""
    if brand.lower().strip() != "lenovo":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — lenovo fallback недоступен")
        return []

    UA_L = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    img_pairs: list[tuple[str, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA_L, viewport={"width": 1280, "height": 800})

            await page.goto("https://www.lenovo.com/sitemap-auto/007-intsitemap-by-ru.xml",
                            wait_until="domcontentloaded", timeout=20000)
            content = await page.content()
            all_urls = re.findall(r"<loc>([^<]+)</loc>", content)

            # Только товарные страницы (/p/...) — категории/сервисы/about исключаем.
            pdp_urls = [u for u in all_urls if "/p/" in u]
            product_urls = [u for u in pdp_urls if _lenovo_url_matches(u, product)][:3]
            log.info(f"lenovo.com sitemap: {len(product_urls)}/{len(pdp_urls)} товарных URL (model-matched)")
            if not product_urls:
                return []

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300)
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    seen = {u for u, _ in img_pairs}
                    for s in srcs:
                        if not _is_bad_image(s) and s not in seen:
                            img_pairs.append((s, prod_url))
                            seen.add(s)
                except Exception as e:
                    log.warning(f"lenovo.com page failed: {prod_url} {e}")
                    continue
                if len(img_pairs) >= 6:
                    break
    except Exception as e:
        log.warning(f"lenovo.com Playwright search failed: {e}")
        return []

    if not img_pairs:
        return []

    captured: list[tuple[bytes, str]] = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
               "Referer": "https://www.lenovo.com/"}
    try:
        async with aiohttp.ClientSession() as s:
            for img_url, prod_url in img_pairs[:6]:
                try:
                    async with s.get(img_url, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            continue
                        data = await r.read()
                        if len(data) > 10_000:
                            captured.append((data, prod_url))
                            log.info(f"lenovo.com fetch OK: {len(data):,}b  {img_url.split('/')[-1][:50]}")
                except Exception:
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"lenovo.com image download failed: {e}")

    log.info(f"lenovo.com Playwright: {len(captured)} фото получено")
    return captured[:3]


_ASUS_URL_CACHE: list[str] | None = None
_ASUS_CACHE_LOCK = asyncio.Lock()


async def _get_asus_ru_urls() -> list[str]:
    """Кэш sitemap asus.com/ru на весь процесс (не TTL) — 127 под-sitemap
    (~50к URL), перекачивать на каждый товар в батче расточительно. Сбрасывается
    только перезапуском бота, как и остальные in-memory кэши процесса."""
    global _ASUS_URL_CACHE
    if _ASUS_URL_CACHE is not None:
        return _ASUS_URL_CACHE
    async with _ASUS_CACHE_LOCK:
        if _ASUS_URL_CACHE is not None:
            return _ASUS_URL_CACHE
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
        all_urls: list[str] = []
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://www.asus.com/sitemap.xml", headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        _ASUS_URL_CACHE = []
                        return []
                    index_text = await r.text()
                sub_sitemaps = [u for u in re.findall(r"<loc>([^<]+)</loc>", index_text)
                                if "/ru/sitemap/ru" in u]

                sem = asyncio.Semaphore(15)

                async def _fetch_one(u: str) -> list[str]:
                    async with sem:
                        try:
                            async with s.get(u, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=10)) as r2:
                                if r2.status == 200:
                                    return re.findall(r"<loc>([^<]+)</loc>", await r2.text())
                        except Exception:
                            pass
                        return []

                results = await asyncio.gather(*[_fetch_one(u) for u in sub_sitemaps])
                for sub in results:
                    all_urls.extend(sub)
        except Exception as e:
            log.warning(f"asus.com sitemap fetch failed: {e}")
            _ASUS_URL_CACHE = []
            return []

        log.info(f"asus.com ru sitemap: закэшировано {len(sub_sitemaps)} под-sitemap, {len(all_urls)} URL")
        _ASUS_URL_CACHE = all_urls
        return all_urls


def _asus_all_numeric_tokens_match(url: str, product: str, brand: str) -> bool:
    """_url_matches_model матчит по ЛЮБОМУ одному токену модели — у ASUS это
    ловит ложные совпадения между РАЗНЫМИ видеокартами: 'O16G'/'O8G' (объём
    VRAM) сам по себе тоже строится как токен модели (цифра+буква, ≥3 симв.),
    но одинаков у RTX 5070Ti и RTX 5080 одновременно. Здесь — строже: ВСЕ
    отдельные слова модели, содержащие цифру (а не только одно любое), обязаны
    быть в URL, иначе 'ProArt RTX5070Ti O16G' совпадает с 'prime-rtx5080-o16g-evo'
    только по общему суффиксу объёма памяти."""
    model = product
    if brand and product.lower().startswith(brand.lower()):
        model = product[len(brand):]
    words = [w for w in re.split(r"\s+", model.strip().lower()) if w]
    numeric_words = [re.sub(r"[^a-z0-9]", "", w) for w in words if any(c.isdigit() for c in w)]
    numeric_words = [w for w in numeric_words if len(w) >= 3]
    if not numeric_words:
        return True
    url_low = url.lower()
    return all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", url_low)
               for w in numeric_words)


async def _search_asus_playwright(product: str, color: str = "",
                                   brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на asus.com/ru через sitemap-индексацию (15.07.2026, четвёртый
    кандидат — см. project_search_improvements). Читаемые слаги
    (asus-expertbook-b3-b3405, proart-rtx5070ti-o16g) покрывают ноутбуки,
    видеокарты, материнки, роутеры — плюс у каждой модели отдельно лежат
    /techspec/ и /review/ страницы (не используются здесь, только для фото,
    но потенциальный источник для будущего поиска характеристик). CDN
    dlcdnwebimgs.asus.com не блокирует in-page fetch (CORS открыт)."""
    if brand.lower().strip() != "asus":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — asus fallback недоступен")
        return []

    all_urls = await _get_asus_ru_urls()
    if not all_urls:
        return []

    # /techspec/ и /review/ — подстраницы той же модели без полноценной галереи
    # фото на главном экране; берём только основную PDP-страницу.
    pdp_urls = [u for u in all_urls if not u.rstrip("/").endswith(("techspec", "review"))]
    product_urls = [u for u in pdp_urls
                    if _url_matches_model(u, product, brand)
                    and _asus_all_numeric_tokens_match(u, product, brand)][:3]
    log.info(f"asus.com sitemap: {len(product_urls)}/{len(pdp_urls)} товарных URL (model-matched)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('dlcdnwebimgs.asus.com'))
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:2]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"asus.com fetch OK: {len(data):,}b  {prod_url.rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"asus.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"asus.com Playwright search failed: {e}")
        return []

    log.info(f"asus.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _search_edifier_playwright(product: str, color: str = "",
                                      brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на edifier.com через sitemap-индексацию (15.07.2026, пятый
    кандидат — см. project_search_improvements). У Edifier нет RU/KZ-локали —
    единственный sitemap глобальный (`global/sitemap-global.xml`), но слаги
    читаемые (neobuds-pro-3, x3-lite) и не привязаны к языку страницы: нам
    нужны только студийные фото, не текст, так что локаль неважна (см. решение
    пользователя не ограничиваться СНГ). CDN new-edifier-*-oss.edifier.com не
    блокирует in-page fetch (CORS открыт)."""
    if brand.lower().strip() != "edifier":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — edifier fallback недоступен")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.edifier.com/global/sitemap-global.xml", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    all_urls = re.findall(r"<loc>([^<]+)</loc>", await r.text())
    except Exception as e:
        log.warning(f"edifier.com sitemap fetch failed: {e}")
        return []

    # Только основные товарные страницы (/p/...) — /s/ (характеристики),
    # /product-category/, /support-model/ и /int/global/-дубликаты исключаем.
    pdp_urls = [u for u in all_urls if "/global/p/" in u and "/int/global/" not in u]
    product_urls = [u for u in pdp_urls if _url_matches_model(u, product, brand)][:3]
    log.info(f"edifier.com sitemap: {len(product_urls)}/{len(pdp_urls)} товарных URL (model-matched)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('oss.edifier.com'))
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:2]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"edifier.com fetch OK: {len(data):,}b  {prod_url.rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"edifier.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"edifier.com Playwright search failed: {e}")
        return []

    log.info(f"edifier.com Playwright: {len(captured)} фото получено")
    return captured[:3]


def _wd_url_matches(url: str, product: str, brand: str) -> bool:
    """_url_matches_model строит ОДИН слитный токен модели — 'WD Blue Desktop
    SATA HDD 4TB' → 'blue-desktop-sata-hdd-4tb', а объём (4TB) в реальном URL
    закодирован только в SKU-параметре query (?sku=WD40EZAX), не в читаемом
    слаге пути (wd-blue-desktop-sata-hdd) — слитный токен никогда не совпадёт.
    Здесь — мешок слов (как для Lenovo), но с исключением слов объёма/размера
    (TB/GB/ТБ/ГБ) из обязательных: фото линейки одно и то же для всех объёмов,
    сравниваем только с путём (без query)."""
    model = product
    if brand and product.lower().startswith(brand.lower()):
        model = product[len(brand):]
    words = [w for w in re.split(r"[\s\-]+", model.lower()) if w]
    words = [w for w in words if not re.fullmatch(r"\d+(tb|gb|тб|гб)", w)]
    if not words:
        return True
    url_path = url.split("?")[0].lower()
    return all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", url_path) for w in words)


async def _search_wd_playwright(product: str, color: str = "",
                                 brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на westerndigital.com через sitemap-индексацию (15.07.2026,
    шестой кандидат — см. project_search_improvements). `products-sitemap.xml`
    даёт читаемые слаги+SKU (wd-blue-desktop-sata-hdd?sku=WD40EZAX) — но это
    ОДНА модельная линейка на много SKU-объёмов (3TB/4TB/6TB...), сам SKU не
    входит в название товара бота — дедуплицируем по базовому пути без query,
    фото линейки одинаковое для всех объёмов. Хорошее покрытие HDD (Blue/
    Black/Red) — актуальные NVMe SSD (SN850X и т.п.) в этом sitemap не
    встретились, видимо другой домен/путь у WD. CDN тот же домен — CORS не
    мешает in-page fetch."""
    if brand.lower().strip() not in ("wd", "western digital"):
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — wd fallback недоступен")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.westerndigital.com/products-sitemap.xml", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    all_urls = re.findall(r"<loc>([^<]+)</loc>", await r.text())
    except Exception as e:
        log.warning(f"westerndigital.com sitemap fetch failed: {e}")
        return []

    matched = [u for u in all_urls if _wd_url_matches(u, product, brand)]
    seen_paths: set = set()
    product_urls = []
    for u in matched:
        base = u.split("?")[0]
        if base not in seen_paths:
            seen_paths.add(base)
            product_urls.append(u)
    product_urls = product_urls[:3]
    log.info(f"westerndigital.com sitemap: {len(product_urls)}/{len(matched)} товарных URL (model-matched, deduped)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('/dam/store/'))
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:2]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"westerndigital.com fetch OK: {len(data):,}b  {prod_url.split('?')[0].rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"westerndigital.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"westerndigital.com Playwright search failed: {e}")
        return []

    log.info(f"westerndigital.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _search_tplink_playwright(product: str, color: str = "",
                                     brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на tp-link.com через sitemap-индексацию (15.07.2026, седьмой
    кандидат). Только nl-локаль объявлена в robots.txt, но слаги не зависят от
    языка страницы (deco-be65-pro, archer-nx500-outdoor, tapo-rv20-max) —
    используем как есть (пользователь решил не ограничиваться СНГ). Покрывает
    роутеры, Deco mesh, Tapo-камеры/розетки/датчики, даже робот-пылесос Tapo
    RV20 Max."""
    if brand.lower().replace("-", "").replace(" ", "").strip() != "tplink":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — tp-link fallback недоступен")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.tp-link.com/nl/sitemap.xml", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    all_urls = re.findall(r"<loc>([^<]+)</loc>", await r.text())
    except Exception as e:
        log.warning(f"tp-link.com sitemap fetch failed: {e}")
        return []

    # Служебные разделы (поддержка/загрузки/точки продаж) исключаем — там нет
    # товарной галереи фото.
    _NON_PRODUCT_MARKERS = ("/support/", "/where-to-buy/", "/technology/",
                            "/faq/", "/download/", "/contact-", "/replacement-warranty/",
                            "/compatibility-list/")
    pdp_urls = [u for u in all_urls if not any(m in u for m in _NON_PRODUCT_MARKERS)]
    product_urls = [u for u in pdp_urls if _url_matches_model(u, product, brand)][:3]
    log.info(f"tp-link.com sitemap: {len(product_urls)}/{len(pdp_urls)} товарных URL (model-matched)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && (i.src.includes('static.tp-link.com')
                                        || i.src.includes('static-product.tp-link.com'))
                                    && !/\\.(gif|svg)(\\?|$)/i.test(i.src));
                            imgs.sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:6]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"tp-link.com fetch OK: {len(data):,}b  {prod_url.rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"tp-link.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"tp-link.com Playwright search failed: {e}")
        return []

    log.info(f"tp-link.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _search_steelseries_playwright(product: str, color: str = "",
                                          brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на steelseries.com через sitemap-индексацию (15.07.2026,
    восьмой кандидат — см. project_search_improvements). `sitemap-products.xml`
    даёт чистые слаги (gaming-mousepads/qck?color=black&mousepadSize=xxl) —
    цвет/размер в query, а не в пути, дедуплицируем по базовому пути. Фото на
    Contentful CDN (images.ctfassets.net), CORS не мешает in-page fetch."""
    if brand.lower().strip() != "steelseries":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — steelseries fallback недоступен")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://steelseries.com/sitemap-products.xml", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    all_urls = re.findall(r"<loc>([^<]+)</loc>", (await r.text()).replace("&amp;", "&"))
    except Exception as e:
        log.warning(f"steelseries.com sitemap fetch failed: {e}")
        return []

    matched = [u for u in all_urls if _url_matches_model(u, product, brand)]
    seen_paths: set = set()
    product_urls = []
    for u in matched:
        base = u.split("?")[0]
        if base not in seen_paths:
            seen_paths.add(base)
            product_urls.append(u)
    product_urls = product_urls[:3]
    log.info(f"steelseries.com sitemap: {len(product_urls)}/{len(matched)} товарных URL (model-matched, deduped)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('images.ctfassets.net')
                                    && !/\\.(gif|svg)(\\?|$)/i.test(i.src));
                            imgs.sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:6]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"steelseries.com fetch OK: {len(data):,}b  {prod_url.split('?')[0].rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"steelseries.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"steelseries.com Playwright search failed: {e}")
        return []

    log.info(f"steelseries.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _search_viewsonic_playwright(product: str, color: str = "",
                                        brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на viewsonic.com через sitemap-индексацию (15.07.2026, девятый
    кандидат — см. project_search_improvements). `media/sitemap/sitemap.xml`
    (US-магазин на Magento) даёт реальные PDP с моделью в слаге
    (vx2452mh-24-1080p-2ms-monitor...). Фото на своём домене
    (/media/catalog/product/...) — исключаем /media/wysiwyg/ (маркетинговые
    баннеры CMS, не товарные фото). Без CORS-проблем."""
    if brand.lower().strip() != "viewsonic":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — viewsonic fallback недоступен")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
    all_urls: list[str] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.viewsonic.com/media/sitemap/sitemap.xml", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    all_urls = re.findall(r"<loc>([^<]+)</loc>", await r.text())
    except Exception as e:
        log.warning(f"viewsonic.com sitemap fetch failed: {e}")
        return []

    product_urls = [u for u in all_urls if _url_matches_model(u, product, brand)][:3]
    log.info(f"viewsonic.com sitemap: {len(product_urls)}/{len(all_urls)} товарных URL (model-matched)")
    if not product_urls:
        return []

    _JS_FETCH = """
        async (url) => {
            try {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let b of bytes) bin += String.fromCharCode(b);
                return btoa(bin);
            } catch(e) { return null; }
        }
    """
    import base64 as _b64
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && i.src.includes('/media/catalog/product/')
                                    && !/\\.(gif|svg)(\\?|$)/i.test(i.src));
                            imgs.sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    for img_url in srcs[:6]:
                        if _is_bad_image(img_url):
                            continue
                        b64 = await page.evaluate(_JS_FETCH, img_url)
                        if b64:
                            data = _b64.b64decode(b64)
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"viewsonic.com fetch OK: {len(data):,}b  {prod_url.rstrip('/').split('/')[-1][:50]}")
                        if len(captured) >= 3:
                            break
                except Exception as e:
                    log.warning(f"viewsonic.com page failed: {prod_url} {e}")
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"viewsonic.com Playwright search failed: {e}")
        return []

    log.info(f"viewsonic.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _search_ecovacs_playwright(product: str, color: str = "",
                                      brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на ecovacs.com через sitemap-индексацию (добавлено 14.07.2026,
    первый кандидат нового направления — см. project_search_improvements).
    Встроенный /search на ecovacs.com ненадёжен (JS-driven), зато
    pdp_sitemap.xml отдаёт готовый список товарных URL с моделью+цветом в
    слаге (deebot-n30pro-omni-black) — просто фильтруем по токенам модели,
    без поиска вообще. Фото официальные студийные, до 8000×8000."""
    if brand.lower().strip() != "ecovacs":
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — ecovacs fallback недоступен")
        return []

    sitemap_url = "https://www.ecovacs.com/ru/pdp_sitemap.xml"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(sitemap_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.warning(f"ecovacs.com sitemap HTTP {r.status}")
                    return []
                xml_text = await r.text()
    except Exception as e:
        log.warning(f"ecovacs.com sitemap fetch failed: {e}")
        return []

    all_urls = re.findall(r"<loc>([^<]+)</loc>", xml_text)
    product_urls = [u for u in all_urls if _url_matches_model(u, product, brand)][:3]
    log.info(f"ecovacs.com sitemap: {len(product_urls)}/{len(all_urls)} товарных URL (model-matched)")
    if not product_urls:
        return []

    import asyncio as _aio

    # Playwright только чтобы найти URL фото (React рендерит галерею на клиенте —
    # в сыром HTML их нет). Скачивание — отдельным шагом через aiohttp: попытка
    # тянуть байты через in-page fetch() упирается в CORS (ecovacs.com →
    # site-static.ecovacs.com, разные origin, браузер блокирует "Failed to
    # fetch"), тогда как обычный серверный GET с Referer проходит без проблем.
    # (img_url, page_url) — page_url — это карточка товара, для единообразия
    # с остальными _search_*_playwright она же идёт вторым элементом в captured
    # (не CDN-ссылка на файл, которая ничего не говорит пользователю как источник).
    img_pairs: list[tuple[str, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                viewport={"width": 1280, "height": 800},
            )
            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, wait_until="domcontentloaded", timeout=15000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    await _aio.sleep(1)

                    srcs = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300 && i.naturalHeight > 300
                                    && !i.src.includes('logo') && !i.src.includes('icon'))
                                .sort((a,b) => b.naturalWidth*b.naturalHeight
                                             - a.naturalWidth*a.naturalHeight);
                            return [...new Set(imgs.map(i => i.currentSrc || i.src))];
                        }
                    """)
                    seen = {u for u, _ in img_pairs}
                    for s in srcs:
                        if not _is_bad_image(s) and s not in seen:
                            img_pairs.append((s, prod_url))
                            seen.add(s)
                except Exception as e:
                    log.warning(f"ecovacs.com page failed: {prod_url} {e}")
                    continue
                if len(img_pairs) >= 6:
                    break
    except Exception as e:
        log.warning(f"ecovacs.com Playwright search failed: {e}")
        return []

    if not img_pairs:
        return []

    captured: list[tuple[bytes, str]] = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
               "Referer": "https://www.ecovacs.com/"}
    try:
        async with aiohttp.ClientSession() as s:
            for img_url, prod_url in img_pairs[:6]:
                try:
                    async with s.get(img_url, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            continue
                        data = await r.read()
                        if len(data) > 10_000:
                            captured.append((data, prod_url))
                            log.info(f"ecovacs.com fetch OK: {len(data):,}b  {img_url.split('/')[-1][:50]}")
                except Exception:
                    continue
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"ecovacs.com image download failed: {e}")

    log.info(f"ecovacs.com Playwright: {len(captured)} фото получено")
    return captured[:3]


async def _pw_bytes_to_scored(pairs: list[tuple[bytes, str]], label: str,
                               product: str = "", color_en: str = "",
                               skip_clip: bool = False, category: str = "") -> list:
    """Конвертирует результаты Playwright в формат scored=(score, bytes, url, mask).
    Быстрая проверка: aspect ratio + опционально CLIP без rembg.
    skip_clip=True — когда Vision включён, пропускаем CLIP (Vision решит сам)."""
    from PIL import Image as _PIL
    import io as _io
    max_asp = (_MAX_ASPECT_RAM if category == "Оперативная память"
               else _MAX_ASPECT_KEYBOARD if category == "Клавиатуры"
               else 2.5)
    result = []
    for d, u in pairs:
        if not d or len(d) < 5_000:
            continue
        try:
            orig = _PIL.open(_io.BytesIO(d)).convert("RGB")
            w, h = orig.size
            if min(w, h) < 100:
                continue
            # Баннер/коллаж по aspect ratio
            asp = max(w, h) / min(w, h)
            if asp > max_asp:
                log.info(f"{label} rejected (aspect {asp:.1f}): {u.split('/')[-1][:50]}")
                continue
            # CLIP — пропускаем если Vision включён (он решит точнее)
            if product and not skip_clip:
                from services.image.clip_scorer import clip_score
                cs = await run_gpu(clip_score, d, product, category=category,
                                   timeout=30, default=0.0, label="clip_score(playwright)")
                if cs < 0.30:
                    log.info(f"{label} rejected (CLIP={cs:.2f}): {u.split('/')[-1][:50]}")
                    continue
                score = cs * w * h
            else:
                score = float(w * h)
            result.append((max(score, 100_000.0), d, u, None))
            log.info(f"{label} accepted: {orig.size} {u.split('/')[-1][:50]}")
        except Exception:
            pass
    return result


async def _search_official_site(product: str, brand: str,
                                color: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото на официальном сайте бренда. URL содержит модель в slug → правильная модель."""
    brand_low = brand.lower().strip()
    tmpl = _BRAND_OFFICIAL_SITES.get(brand_low)
    if not tmpl:
        return []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []

    query = f"{product} {color}".strip().replace(" ", "+")
    url = tmpl.format(query=query)
    log.info(f"Official site search: {url}")
    UA_O = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA_O)
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Ищем ссылки на страницу продукта
            all_links = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            prod_links = [
                u for u in all_links
                if _url_matches_model(u, product, brand)
                and any(k in u.lower() for k in [brand_low, "product", "phone", "smartphone"])
            ][:2]
            log.info(f"Official site: {len(prod_links)} product links")

            for prod_url in prod_links:
                await page.goto(prod_url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                imgs = await page.eval_on_selector_all(
                    "img[src]", "els => els.map(e => e.src)"
                )
                # Ищем крупные изображения продукта
                photo_re = re.compile(r"\.(jpg|jpeg|webp|png)(\?|$)", re.I)
                for img_url in imgs:
                    if not photo_re.search(img_url):
                        continue
                    if any(k in img_url.lower() for k in ["logo", "icon", "banner"]):
                        continue
                    js = f"""fetch("{img_url}").then(r=>r.arrayBuffer()).then(b=>Array.from(new Uint8Array(b)))"""
                    try:
                        arr = await page.evaluate(js)
                        data = bytes(arr)
                        if len(data) > 20_000:
                            captured.append((data, prod_url))
                            log.info(f"Official site fetch OK: {len(data):,}b")
                            break
                    except Exception:
                        continue
                if captured:
                    break
    except Exception as e:
        log.warning(f"Official site search failed: {e}")
    return captured


async def _search_deepcool_cdn(product: str, brand: str,
                               color: str = "") -> list[tuple[bytes, str]]:
    """Прямой поиск на CDN DeepCool по конструируемому URL.
    color — сырой цвет из ProductInfo (напр. 'BK', 'WH', 'White').
    Пробует несколько вариантов ключа чтобы найти правильную папку."""
    if brand.lower() != "deepcool":
        return []
    import re as _re

    # Модель без бренда
    model_raw = product.strip()
    if model_raw.lower().startswith("deepcool"):
        model_raw = model_raw[len("deepcool"):].strip()

    def _to_key(s: str) -> str:
        k = _re.sub(r"[^A-Z0-9]+", "_", s.upper()).strip("_")
        while "__" in k: k = k.replace("__", "_")
        return k

    base_key = _to_key(model_raw)

    # Нормализуем обозначение цвета: "black"→"BK", "white"→"WH" и т.д.
    _color_map = {"black": "BK", "white": "WH", "bk": "BK", "wh": "WH"}
    color_code = _color_map.get(color.lower().strip(), color.upper()[:2] if color else "")

    # Строим варианты ключей — CDN ставит цвет в разных позициях
    keys_to_try: list[str] = [base_key]

    if color_code:
        cc = color_code  # "BK" или "WH"
        # Вариант 1: цвет добавляем сразу после номера модели перед доп. суффиксами
        # AG400_DIGITAL_ARGB → AG400_DIGITAL_BK_ARGB
        for suffix in ("ARGB", "DIGITAL", "PLUS", "V2"):
            if f"_{suffix}" in base_key and f"_{cc}_" not in base_key and not base_key.endswith(f"_{cc}"):
                keys_to_try.append(base_key.replace(f"_{suffix}", f"_{cc}_{suffix}", 1))
        # Вариант 2: цвет в конце
        if not base_key.endswith(f"_{cc}"):
            keys_to_try.append(f"{base_key}_{cc}")

    # Убираем дубли, сохраняем порядок
    seen: set = set()
    keys_to_try = [k for k in keys_to_try if not (k in seen or seen.add(k))]
    log.info(f"DeepCool CDN: варианты ключей {keys_to_try}")

    captured: list[tuple[bytes, str]] = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}
    try:
        async with aiohttp.ClientSession() as s:
            for key_try in keys_to_try:
                base = f"https://cdn.deepcool.com/public/ProductFile/DEEPCOOL/Cooling/CPUAirCoolers/{key_try}/Gallery/608X760"
                key_captured = []
                for i in range(1, 7):  # 01..06
                    url = f"{base}/{i:02d}.jpg"
                    try:
                        async with s.get(url, headers=headers, ssl=False,
                                         timeout=aiohttp.ClientTimeout(total=8)) as r:
                            if r.status == 200:
                                data = await r.read()
                                if len(data) > 15_000:
                                    # Проверяем что это портретное/квадратное фото, не баннер
                                    try:
                                        from PIL import Image as _PIL
                                        import io as _io
                                        _img = _PIL.open(_io.BytesIO(data))
                                        _w, _h = _img.size
                                        if _w > _h * 1.3:  # слишком широкое — баннер
                                            log.info(f"DeepCool CDN skip banner {_w}x{_h}: {url.split('/')[-1]}")
                                            continue
                                    except Exception:
                                        pass
                                    key_captured.append((data, url))
                                    log.info(f"DeepCool CDN OK [{key_try}]: {len(data):,}b  {url.split('/')[-1]}")
                    except Exception:
                        pass
                if key_captured:
                    captured = key_captured
                    break  # нашли правильный ключ
    except Exception as e:
        log.warning(f"DeepCool CDN search failed: {e}")

    if not captured:
        log.info(f"DeepCool CDN: ничего не найдено для ключа {base_key!r}")
    return captured


async def _search_mikrotik_cdn(product: str, brand: str = "",
                                color: str = "") -> list[tuple[bytes, str]]:
    """Прямой поиск на mikrotik.com по конструируемому URL — как DeepCool CDN,
    без браузера: mikrotik.com отдаёт обычный серверный HTML, страница товара
    живёт по /product/{SKU} (совпадает с моделью в названии дословно, напр.
    'MikroTik hAP ac RB962UiGS-5HacT2HnT' → /product/RB962UiGS-5HacT2HnT).
    20.07.2026: добавлен для закрытия пробела по MikroTik (найдено вживую
    на RB962UiGS-5HacT2HnT сегодня — hi_res.png, чистое студийное фото без
    вотермарков и без CDN-игр как у DeepCool — одна попытка на кандидата."""
    if brand.lower().strip() != "mikrotik":
        return []

    # SKU MikroTik — токен с буквами И цифрами, обычно начинается с RB/CRS/
    # CCR/CSS и т.п., может содержать дефисы (RB962UiGS-5HacT2HnT). Берём
    # самый длинный подходящий токен из названия — почти всегда это модель.
    candidates = [
        t.rstrip("-") for t in re.findall(r"[A-Za-z][A-Za-z0-9+-]{4,}", product)
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
    ]
    if not candidates:
        return []
    sku = max(candidates, key=len)

    # 20.07.2026: слаг непоследователен между линейками — у RB-моделей
    # дефисы как есть (RB962UiGS-5HacT2HnT), у CCR "+" превращается в "plus"
    # и дефисы в подчёркивания (CCR2004-16G-2S+ → ccr2004_16g_2splus, найдено
    # вживую после 404 на первом варианте). Пробуем оба варианта по очереди.
    slug_variants = [sku]
    alt = sku.lower().replace("-", "_").replace("+", "plus")
    if alt != sku:
        slug_variants.append(alt)

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}
    try:
        async with aiohttp.ClientSession() as s:
            html = None
            url = ""
            for slug in slug_variants:
                url = f"https://mikrotik.com/product/{slug}"
                async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        html = await r.text()
                        break
                    log.info(f"MikroTik CDN: {url} -> HTTP {r.status}")
            if html is None:
                return []

            img_url = None
            m = re.search(r'https://cdn\.mikrotik\.com/web-assets/rb_images/[^"\']+_hi_res\.png', html)
            if m:
                img_url = m.group(0)
            else:
                m = re.search(r'https://cdn\.mikrotik\.com/web-assets/rb_images/[^"\']+_xl\.webp', html)
                if m:
                    img_url = m.group(0)
            if not img_url:
                log.info(f"MikroTik CDN: страница {url} есть, но фото не нашлось в разметке")
                return []

            async with s.get(img_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                data = await r.read()
                if len(data) <= 10_000:
                    return []
                log.info(f"MikroTik CDN OK: {sku} — {len(data):,}b")
                return [(data, url)]
    except Exception as e:
        log.warning(f"MikroTik CDN search failed ({sku}): {e}")
        return []


async def _search_dns_playwright(product: str, color: str = "",
                                  brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на dns-shop.ru через Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — dns fallback недоступен")
        return []

    query = f"{product} {color}".strip()
    search_url = f"https://www.dns-shop.ru/search/?q={query.replace(' ', '+')}"
    log.info(f"DNS Playwright search: {search_url}")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            all_links = await page.eval_on_selector_all(
                "a[href*='/product/']",
                "els => [...new Set(els.map(e => e.href))].slice(0, 10)"
            )
            product_urls = [
                u for u in all_links
                if "/product/" in u and _url_matches_model(u, product, brand)
            ][:3]
            log.info(f"DNS: {len(product_urls)}/{len(all_links)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                    og_img = await page.evaluate("""
                        () => {
                            const meta = document.querySelector('meta[property="og:image"]');
                            return meta ? meta.getAttribute('content') : null;
                        }
                    """)
                    if not og_img:
                        og_img = await page.evaluate("""
                            () => {
                                const imgs = Array.from(document.images)
                                    .filter(i => i.naturalWidth > 300
                                        && !i.src.includes('placeholder'))
                                    .sort((a, b) => b.naturalWidth * b.naturalHeight
                                                  - a.naturalWidth * a.naturalHeight);
                                return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                            }
                        """)
                    if not og_img or _is_bad_image(og_img):
                        continue
                    og_img = _upgrade_image_url(og_img)

                    js = f"""fetch("{og_img}", {{headers: {{Referer: "https://www.dns-shop.ru/"}}}})
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)))"""
                    arr = await page.evaluate(js)
                    data = bytes(arr)
                    if len(data) > 10_000:
                        captured.append((data, prod_url))
                        log.info(f"DNS fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"DNS page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break

            log.info(f"DNS Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"DNS Playwright search failed: {e}")
        return []


async def _search_citilink_playwright(product: str, color: str = "",
                                       brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на citilink.ru через Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — citilink fallback недоступен")
        return []

    query = f"{product} {color}".strip()
    search_url = f"https://www.citilink.ru/search/?text={query.replace(' ', '+')}"
    log.info(f"Citilink Playwright search: {search_url}")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            all_links = await page.eval_on_selector_all(
                "a[href*='/product/']",
                "els => [...new Set(els.map(e => e.href))].slice(0, 10)"
            )
            product_urls = [
                u for u in all_links
                if "/product/" in u and _url_matches_model(u, product, brand)
            ][:3]
            log.info(f"Citilink: {len(product_urls)}/{len(all_links)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                    # og:image у Citilink — всегда CDN-миниатюра width:220/height:220
                    # (signed imgproxy URL, увеличить нельзя — подпись не совпадёт)
                    # и весит <10KB, отсекается фильтром по размеру. Берём крупное
                    # галерейное фото (>300px) с того же CDN, og:image — запасной вариант.
                    og_img = await page.evaluate("""
                        () => {
                            const imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 300
                                    && i.src.includes('cdn.citilink.ru')
                                    && !i.src.includes('placeholder'))
                                .sort((a, b) => b.naturalWidth * b.naturalHeight
                                              - a.naturalWidth * a.naturalHeight);
                            return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                        }
                    """)
                    if not og_img:
                        og_img = await page.evaluate("""
                            () => {
                                const meta = document.querySelector('meta[property="og:image"]');
                                return meta ? meta.getAttribute('content') : null;
                            }
                        """)
                    if not og_img or _is_bad_image(og_img):
                        continue

                    js = f"""fetch("{og_img}", {{headers: {{Referer: "https://www.citilink.ru/"}}}})
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)))"""
                    arr = await page.evaluate(js)
                    data = bytes(arr)
                    if len(data) > 10_000:
                        captured.append((data, prod_url))
                        log.info(f"Citilink fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"Citilink page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break

            log.info(f"Citilink Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"Citilink Playwright search failed: {e}")
        return []


async def _search_pulser_cdn(product: str, color: str = "",
                              brand: str = "") -> list[tuple[bytes, str]]:
    """Ищет фото на pulser.kz (Алматы) — обычным HTTP, без браузера.

    20.07.2026 (живой батч БП): найден по просьбе пользователя. Реальный
    поиск — GET /search/index?SearchForm[search]=... (нашли перебором
    network-запросов при вводе в #autocomplete на сайте; угаданные
    ?searching=/site/search — мимо, отдают либо витрину, либо 404). Работает
    без Playwright — тот же приём, что у regard.ru/MikroTik, дешевле по
    ресурсам (пользователь просил не грузить браузером без необходимости).
    Бонус: SKU дистрибьютора у pulser.kz совпадает с нашими article
    (подтверждено на 194749/194750 живьём) — надёжнее случайных числовых
    совпадений на других сайтах.

    20.07.2026, вторая находка: их поиск — строгое совпадение по всем
    словам запроса, и давится даже на 3-словных запросах с дефисным кодом
    модели ('Chieftec EON' → 9 карточек, 'Chieftec EON ZPU-700S' → 0).
    Берём только первые 2 слова (бренд + первое слово модели)."""
    base = f"{product} {color}".strip()
    query = " ".join(base.split()[:2])
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}
    captured: list[tuple[bytes, str]] = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://pulser.kz/search/index",
                params={"SearchForm[search]": query},
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.info(f"Pulser search: HTTP {r.status}")
                    return []
                html = await r.text()

            links = sorted(set(re.findall(r'href="(/product/[^"]+)"', html)))
            product_urls = [
                "https://pulser.kz" + u for u in links
                if _url_matches_model(u, product, brand)
            ][:2]
            log.info(f"Pulser: {len(product_urls)}/{len(links)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    async with s.get(prod_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            continue
                        prod_html = await r.text()
                    img_paths = re.findall(
                        r'(?:src|data-src)="(/gallery/images/image-by-item-and-alias\?[^"]+)"',
                        prod_html,
                    )
                    for path in img_paths[:2]:
                        img_url = "https://pulser.kz" + path.replace("&amp;", "&")
                        if _is_bad_image(img_url):
                            continue
                        async with s.get(img_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status != 200:
                                continue
                            data = await r.read()
                            if len(data) > 10_000:
                                captured.append((data, prod_url))
                                log.info(f"Pulser fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"Pulser page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break
    except Exception as e:
        log.warning(f"Pulser search failed: {e}")
        return []

    log.info(f"Pulser: {len(captured)} фото получено")
    return captured[:3]


async def _search_regard_playwright(product: str, color: str = "",
                                     brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на regard.ru — через их внутренний JSON API, не
    парсинг HTML.

    20.07.2026: добавлен для закрытия пробела по нишевому сетевому
    оборудованию (Ubiquiti/TP-Link Archer T2U-T4U/MikroTik и т.п.) —
    hardware-каталог с хорошим покрытием там, где al-style/kaspi/dns дают 0.
    Найдено вживую (перебором network-запросов при вводе в #searchInput на
    сайте): угаданный по аналогии с другими магазинами URL вида
    `/catalog?q=...` НЕ является поиском (просто дефолтная витрина, `q`
    игнорируется) — сайт на JS-роутинге, реальный поиск идёт через
    POST /api/site/catalog/quickSearch, body {"search": "..."}. Ответ уже
    даёт готовые id/vendor/title/photos.urls на каждый хит — не нужно даже
    заходить на страницу товара за og:image, как у остальных 18 скраперов."""
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    query = f"{product} {color}".strip()
    log.info(f"Regard quickSearch: {query!r}")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            try:
                resp = await page.request.post(
                    "https://www.regard.ru/api/site/catalog/quickSearch",
                    headers={"Content-Type": "application/json", "Referer": "https://www.regard.ru/"},
                    data=json.dumps({"search": query}),
                    timeout=15000,
                )
                data = await resp.json()
            except Exception as e:
                log.warning(f"Regard quickSearch request failed: {e}")
                return []

            hits = data.get("hits") or []
            matched = [
                h for h in hits
                if h.get("id") and h.get("photos", {}).get("urls")
                and _url_matches_model(f"/product/{h['id']}/{h.get('seo_url', '')}", product, brand)
            ][:3]
            log.info(f"Regard: {len(matched)}/{len(hits)} хитов (model-matched)")

            for hit in matched:
                try:
                    img_frag = hit["photos"]["urls"][0]  # напр. "/1100689"
                    img_url = f"https://www.regard.ru/api/site/cacheimg/goods{img_frag}/800"
                    if _is_bad_image(img_url):
                        continue
                    img_resp = await page.request.get(
                        img_url, headers={"Referer": "https://www.regard.ru/"}, timeout=15000,
                    )
                    body = await img_resp.body()
                    if len(body) > 10_000:
                        prod_url = f"https://www.regard.ru/product/{hit['id']}/{hit.get('seo_url', '')}"
                        captured.append((body, prod_url))
                        log.info(f"Regard fetch OK: {len(body):,}b  {hit.get('title', '')[:50]}")
                except Exception as e:
                    log.warning(f"Regard image fetch error (id {hit.get('id')}): {e}")

            log.info(f"Regard: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"Regard search failed: {e}")
        return []


async def _search_itmag_playwright(product: str, color: str = "",
                                    brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на itmag.kz через Playwright.
    Карточки товара: /p/{id}-{slug}/, фото: /upload/iblock/.../product_image_*.webp.
    Цвет в запрос НЕ добавляем: поиск itmag — точное совпадение слов без морфологии,
    "белый" не находит карточку «...белая» (род прилагательного) и обнуляет выдачу."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — itmag fallback недоступен")
        return []

    search_url = f"https://itmag.kz/search/?q={product.replace(' ', '+')}"
    log.info(f"itmag Playwright search: {search_url}")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            all_links = await page.eval_on_selector_all(
                "a[href*='/p/']",
                "els => [...new Set(els.map(e => e.href))].slice(0, 10)"
            )
            product_urls = [
                u for u in all_links
                if "/p/" in u and _url_matches_model(u, product, brand)
            ][:3]
            log.info(f"itmag: {len(product_urls)}/{len(all_links)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    # 1500ms было недостаточно — изображения ещё не успевали
                    # декодироваться (naturalWidth=0), фильтр >200 давал пусто
                    await page.wait_for_timeout(3000)

                    id_match = re.search(r"/p/(\d+)-", prod_url)
                    prod_id = id_match.group(1) if id_match else ""

                    img_src = await page.evaluate("""
                        (pid) => {
                            let imgs = Array.from(document.images)
                                .filter(i => i.src.includes('/upload/iblock/')
                                    && i.naturalWidth > 200);
                            if (pid) {
                                const own = imgs.filter(i => i.src.includes('product_image_' + pid + '_'));
                                if (own.length) imgs = own;
                            }
                            imgs.sort((a, b) => b.naturalWidth * b.naturalHeight
                                              - a.naturalWidth * a.naturalHeight);
                            return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                        }
                    """, prod_id)
                    if not img_src or _is_bad_image(img_src):
                        continue

                    js = f"""fetch("{img_src}", {{headers: {{Referer: "https://itmag.kz/"}}}})
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)))"""
                    arr = await page.evaluate(js)
                    data = bytes(arr)
                    # itmag отдаёт реальные карточные фото 600x400 при ~5KB
                    # (сильное сжатие) — общий порог 10KB их отсекал целиком
                    if len(data) > 3_000:
                        captured.append((data, prod_url))
                        log.info(f"itmag fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"itmag page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break

            log.info(f"itmag Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"itmag Playwright search failed: {e}")
        return []


async def _search_alstyle_playwright(product: str, color: str = "",
                                      brand: str = "", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на al-style.kz через Playwright — наш поставщик,
    приоритетный источник (точные фото и характеристики реально продаваемых
    позиций). Сайт грузит фото через JS (см. _LAZY_DOMAINS) — берём наибольший
    декодированный <img> на странице карточки, как на itmag/kaspi.
    Цвет в запрос не добавляем (риск морфологии, как у itmag) — только для
    единообразия сигнатуры с другими _search_*_playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — al-style fallback недоступен")
        return []

    search_url = f"https://al-style.kz/search/?q={product.replace(' ', '+')}"
    log.info(f"al-style Playwright search: {search_url}")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            title = await page.title()
            if "cloudflare" in title.lower() or "just a moment" in title.lower():
                log.warning(f"al-style: заблокировано ({title[:50]})")
                return []

            # На странице поиска сотни ссылок меню/категорий ИДУТ ПЕРЕД товарной
            # сеткой в DOM — обрезка списка до среза здесь недопустима (резала
            # все настоящие карточки). Сначала полный список, фильтр по глубине
            # пути (товар — минимум 2 сегмента после /catalog/, категории короче)
            # и по модели, обрезка до топ-3 — только в конце.
            all_links = await page.eval_on_selector_all(
                "a.product-card__title, a[href*='/catalog/'], .product-item a",
                "els => [...new Set(els.map(e => e.href))]"
            )
            deep_links = [u for u in all_links if u.rstrip("/").count("/") >= 5]
            product_urls = [
                u for u in deep_links
                if _url_matches_model(u, product, brand)
            ][:3]
            log.info(f"al-style: {len(product_urls)}/{len(deep_links)} карточек (model-matched, всего ссылок {len(all_links)})")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)

                    img_src = await page.evaluate("""
                        () => {
                            let imgs = Array.from(document.images)
                                .filter(i => i.naturalWidth > 200 && i.naturalHeight > 200);
                            imgs.sort((a, b) => b.naturalWidth * b.naturalHeight
                                              - a.naturalWidth * a.naturalHeight);
                            return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
                        }
                    """)
                    if not img_src or _is_bad_image(img_src):
                        continue

                    js = f"""fetch("{img_src}", {{headers: {{Referer: "https://al-style.kz/"}}}})
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)))"""
                    arr = await page.evaluate(js)
                    data = bytes(arr)
                    if len(data) > 10_000:
                        captured.append((data, prod_url))
                        log.info(f"al-style fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"al-style page error {prod_url}: {e}")
                if len(captured) >= 3:
                    break

            log.info(f"al-style Playwright: {len(captured)} фото получено")
            return captured[:3]
    except Exception as e:
        log.warning(f"al-style Playwright search failed: {e}")
        return []


async def _search_rtings_playwright(product: str, brand: str = "",
                                     rtings_category: str = "mouse", browser=None) -> list[tuple[bytes, str]]:
    """Ищет фото товара на rtings.com по категории (mouse, keyboard и т.д.).
    Студийные фото без вотермарок, топовые бренды (Logitech, Razer, SteelSeries...).
    Для бюджетных CIS-брендов обычно ничего не находит — вернёт пустой список."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — rtings fallback недоступен")
        return []

    search_url = f"https://www.rtings.com/search?q={product.replace(' ', '+')}"
    log.info(f"rtings Playwright search: {search_url}")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    captured: list[tuple[bytes, str]] = []
    try:
        async with _get_browser(browser) as browser:
            page = await browser.new_page(user_agent=UA)
            await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            all_links = await page.eval_on_selector_all(
                f"a[href*='/{rtings_category}/reviews/']",
                "els => [...new Set(els.map(e => e.href))]"
            )
            product_urls = [
                u for u in all_links
                if re.search(rf"/{rtings_category}/reviews/[^/]+/[^/]+$", u)
                and "/reviews/best/" not in u
                and _url_matches_model(u, product, brand)
            ][:1]
            log.info(f"rtings: {len(product_urls)}/{len(all_links)} карточек (model-matched)")

            for prod_url in product_urls:
                try:
                    await page.goto(prod_url, timeout=20000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)

                    og_img = await page.evaluate("""
                        () => {
                            const m = document.querySelector('meta[property="og:image"]');
                            return m ? m.content : null;
                        }
                    """)
                    if not og_img or _is_bad_image(og_img):
                        continue
                    og_img = og_img.replace("design-medium.jpg", "design-large.jpg")

                    js = f"""fetch("{og_img}", {{headers: {{Referer: "https://www.rtings.com/"}}}})
                            .then(r => r.arrayBuffer())
                            .then(b => Array.from(new Uint8Array(b)))"""
                    arr = await page.evaluate(js)
                    data = bytes(arr)
                    if len(data) > 10_000:
                        captured.append((data, prod_url))
                        log.info(f"rtings fetch OK: {len(data):,}b  {prod_url.split('/')[-1][:50]}")
                except Exception as e:
                    log.warning(f"rtings page error {prod_url}: {e}")

            log.info(f"rtings Playwright: {len(captured)} фото получено")
            return captured
    except Exception as e:
        log.warning(f"rtings Playwright search failed: {e}")
        return []


async def _with_timeout(coro, default, timeout: float = 240, label: str = ""):
    """Обёртка-страховка от зависания Playwright-задач (08.07.2026): каждая
    site-функция сама ловит свои исключения, но НЕ зависание — если браузер
    падает грязно (у нас так было: EXCEPTION_ACCESS_VIOLATION в видеодрайвере
    при конкуренции за GPU между CLIP-инференсом и Chromium), await может не
    завершиться никогда, и общий asyncio.gather виснет навсегда, утаскивая
    за собой весь батч. Таймаут гарантирует, что одна протухшая площадка не
    убьёт остальные и не подвесит пайплайн."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(f"Playwright: таймаут {timeout:.0f}с ({label}) — считаем пустым результатом")
        return default
    except Exception as e:
        log.warning(f"Playwright: ошибка ({label}): {e}")
        return default


async def _playwright_fallback(product: str, color: str, color_en: str,
                                llm, brand: str = "",
                                vision_validate: bool = False,
                                category: str = "") -> list:
    """Запускает rtings.com, apltech.kz, kaspi.kz, dns-shop.ru, citilink.ru и itmag.kz параллельно.

    19.07.2026: все ниже — теперь на ОДНОМ общем Chromium (см. _get_browser),
    вместо того чтобы каждая функция поднимала свой процесс. Функции,
    брендово нерелевантные товару (ecovacs/samsung/lenovo/asus/edifier/wd/
    tplink/steelseries/viewsonic/dreametech — гейт по brand внутри каждой),
    возвращают [] мгновенно, ДО обращения к браузеру — общий браузер реально
    трогают обычно 6-9 задач (rtings/al-style/apltech/kaspi/official/dns/
    citilink/itmag + максимум одна брендовая), вместо 6-9 полных отдельных
    процессов Chromium — тот же результат и тот же параллелизм (открывают
    вкладки в одном процессе одновременно), но без многократного повторного
    старта движка."""
    q = color_en or color
    _RTINGS_CATEGORY = {"Мыши": "mouse", "Клавиатуры": "keyboard"}
    rtings_cat = _RTINGS_CATEGORY.get(category, "mouse")
    async with _get_browser() as shared_browser:
        rtn_task = asyncio.create_task(_with_timeout(
            _search_rtings_playwright(product, brand=brand, rtings_category=rtings_cat, browser=shared_browser), [], label="rtings"))
        als_task = asyncio.create_task(_with_timeout(
            _search_alstyle_playwright(product, q, brand=brand, browser=shared_browser), [], label="alstyle"))
        ecv_task = asyncio.create_task(_with_timeout(
            _search_ecovacs_playwright(product, q, brand=brand, browser=shared_browser), [], label="ecovacs"))
        ssg_task = asyncio.create_task(_with_timeout(
            _search_samsung_playwright(product, q, brand=brand, browser=shared_browser), [], label="samsung"))
        lnv_task = asyncio.create_task(_with_timeout(
            _search_lenovo_playwright(product, q, brand=brand, browser=shared_browser), [], label="lenovo"))
        asu_task = asyncio.create_task(_with_timeout(
            _search_asus_playwright(product, q, brand=brand, browser=shared_browser), [], label="asus"))
        edf_task = asyncio.create_task(_with_timeout(
            _search_edifier_playwright(product, q, brand=brand, browser=shared_browser), [], label="edifier"))
        wd_task = asyncio.create_task(_with_timeout(
            _search_wd_playwright(product, q, brand=brand, browser=shared_browser), [], label="wd"))
        tpl_task = asyncio.create_task(_with_timeout(
            _search_tplink_playwright(product, q, brand=brand, browser=shared_browser), [], label="tplink"))
        sst_task = asyncio.create_task(_with_timeout(
            _search_steelseries_playwright(product, q, brand=brand, browser=shared_browser), [], label="steelseries"))
        vsn_task = asyncio.create_task(_with_timeout(
            _search_viewsonic_playwright(product, q, brand=brand, browser=shared_browser), [], label="viewsonic"))
        drt_task = asyncio.create_task(_with_timeout(
            _search_dreametech_playwright(product, q, brand=brand, browser=shared_browser), [], label="dreametech"))
        apl_task = asyncio.create_task(_with_timeout(
            _search_apltech_playwright(product, q, brand=brand, browser=shared_browser), [], label="apltech"))
        ksp_task = asyncio.create_task(_with_timeout(
            _search_kaspi_playwright(product, q, brand=brand, browser=shared_browser), [], label="kaspi"))
        off_task = asyncio.create_task(_with_timeout(
            _search_official_site(product, brand, q, browser=shared_browser), [], label="official"))
        cdn_task = asyncio.create_task(_with_timeout(
            _search_deepcool_cdn(product, brand, color=color), [], label="deepcool_cdn"))
        dns_task = asyncio.create_task(_with_timeout(
            _search_dns_playwright(product, q, brand=brand, browser=shared_browser), [], label="dns"))
        cit_task = asyncio.create_task(_with_timeout(
            _search_citilink_playwright(product, q, brand=brand, browser=shared_browser), [], label="citilink"))
        itm_task = asyncio.create_task(_with_timeout(
            _search_itmag_playwright(product, q, brand=brand, browser=shared_browser), [], label="itmag"))
        rgd_task = asyncio.create_task(_with_timeout(
            _search_regard_playwright(product, q, brand=brand, browser=shared_browser), [], label="regard"))
        mkt_task = asyncio.create_task(_with_timeout(
            _search_mikrotik_cdn(product, brand=brand, color=color), [], label="mikrotik_cdn"))
        pls_task = asyncio.create_task(_with_timeout(
            _search_pulser_cdn(product, q, brand=brand), [], label="pulser"))
        rtn_raw, als_raw, ecv_raw, ssg_raw, lnv_raw, asu_raw, edf_raw, wd_raw, tpl_raw, sst_raw, vsn_raw, drt_raw, apl_raw, ksp_raw, off_raw, cdn_raw, dns_raw, cit_raw, itm_raw, rgd_raw, mkt_raw, pls_raw = await asyncio.gather(
            rtn_task, als_task, ecv_task, ssg_task, lnv_task, asu_task, edf_task, wd_task, tpl_task, sst_task, vsn_task, drt_task, apl_task, ksp_task, off_task, cdn_task, dns_task, cit_task, itm_task, rgd_task, mkt_task, pls_task
        )

    rtn_pairs = rtn_raw
    als_pairs = als_raw
    ecv_pairs = ecv_raw
    ssg_pairs = ssg_raw
    lnv_pairs = lnv_raw
    asu_pairs = asu_raw
    edf_pairs = edf_raw
    wd_pairs = wd_raw
    tpl_pairs = tpl_raw
    sst_pairs = sst_raw
    vsn_pairs = vsn_raw
    drt_pairs = drt_raw
    apl_pairs = apl_raw if apl_raw and isinstance(apl_raw[0], tuple) else []
    ksp_pairs = ksp_raw
    off_pairs = off_raw
    cdn_pairs = cdn_raw
    dns_pairs = dns_raw
    cit_pairs = cit_raw
    itm_pairs = itm_raw
    rgd_pairs = rgd_raw
    mkt_pairs = mkt_raw
    pls_pairs = pls_raw

    # al-style — наш поставщик, приоритет сразу после rtings (студийные фото
    # без вотермарок). ecovacs/samsung/lenovo — sitemap-based официальные
    # источники (14.07-15.07.2026), тоже студийное качество, приоритет сразу
    # после al-style. dreametech — узкоспециализированный (только brand=dreame),
    # но даёт крупные официальные фото там, где вообще ничего больше не находится
    # (см. крах 14.07.2026 — Exa тоже 0 кандидатов на новые модели Dreame).
    all_pairs = rtn_pairs + als_pairs + ecv_pairs + ssg_pairs + lnv_pairs + asu_pairs + edf_pairs + wd_pairs + tpl_pairs + sst_pairs + vsn_pairs + drt_pairs + cdn_pairs + mkt_pairs + apl_pairs + ksp_pairs + dns_pairs + cit_pairs + itm_pairs + rgd_pairs + pls_pairs + off_pairs
    log.info(f"Playwright sources: rtings={len(rtn_pairs)}, al-style={len(als_pairs)}, ecovacs={len(ecv_pairs)}, samsung={len(ssg_pairs)}, lenovo={len(lnv_pairs)}, asus={len(asu_pairs)}, edifier={len(edf_pairs)}, wd={len(wd_pairs)}, tplink={len(tpl_pairs)}, steelseries={len(sst_pairs)}, viewsonic={len(vsn_pairs)}, dreametech={len(drt_pairs)}, deepcool_cdn={len(cdn_pairs)}, mikrotik_cdn={len(mkt_pairs)}, apltech={len(apl_pairs)}, kaspi={len(ksp_pairs)}, dns={len(dns_pairs)}, citilink={len(cit_pairs)}, itmag={len(itm_pairs)}, regard={len(rgd_pairs)}, pulser={len(pls_pairs)}, official={len(off_pairs)}")

    # Если цвет был в запросе, но ни одного результата — повторяем без цвета.
    # Редкие цветовые варианты (pink, gold, purple) часто отсутствуют на CIS-маркетплейсах.
    if not all_pairs and q:
        log.info(f"Playwright: 0 results with color='{q}' — retrying without color")
        als2 = asyncio.create_task(_with_timeout(_search_alstyle_playwright(product, "", brand=brand), [], label="alstyle2"))
        ecv2 = asyncio.create_task(_with_timeout(_search_ecovacs_playwright(product, "", brand=brand), [], label="ecovacs2"))
        ssg2 = asyncio.create_task(_with_timeout(_search_samsung_playwright(product, "", brand=brand), [], label="samsung2"))
        lnv2 = asyncio.create_task(_with_timeout(_search_lenovo_playwright(product, "", brand=brand), [], label="lenovo2"))
        asu2 = asyncio.create_task(_with_timeout(_search_asus_playwright(product, "", brand=brand), [], label="asus2"))
        edf2 = asyncio.create_task(_with_timeout(_search_edifier_playwright(product, "", brand=brand), [], label="edifier2"))
        wd2 = asyncio.create_task(_with_timeout(_search_wd_playwright(product, "", brand=brand), [], label="wd2"))
        tpl2 = asyncio.create_task(_with_timeout(_search_tplink_playwright(product, "", brand=brand), [], label="tplink2"))
        sst2 = asyncio.create_task(_with_timeout(_search_steelseries_playwright(product, "", brand=brand), [], label="steelseries2"))
        vsn2 = asyncio.create_task(_with_timeout(_search_viewsonic_playwright(product, "", brand=brand), [], label="viewsonic2"))
        drt2 = asyncio.create_task(_with_timeout(_search_dreametech_playwright(product, "", brand=brand), [], label="dreametech2"))
        apl2 = asyncio.create_task(_with_timeout(_search_apltech_playwright(product, "", brand=brand), [], label="apltech2"))
        ksp2 = asyncio.create_task(_with_timeout(_search_kaspi_playwright(product, "", brand=brand), [], label="kaspi2"))
        off2 = asyncio.create_task(_with_timeout(_search_official_site(product, brand, ""), [], label="official2"))
        dns2 = asyncio.create_task(_with_timeout(_search_dns_playwright(product, "", brand=brand), [], label="dns2"))
        cit2 = asyncio.create_task(_with_timeout(_search_citilink_playwright(product, "", brand=brand), [], label="citilink2"))
        itm2 = asyncio.create_task(_with_timeout(_search_itmag_playwright(product, "", brand=brand), [], label="itmag2"))
        als_r2, ecv_r2, ssg_r2, lnv_r2, asu_r2, edf_r2, wd_r2, tpl_r2, sst_r2, vsn_r2, drt_r2, apl_r2, ksp_r2, off_r2, dns_r2, cit_r2, itm_r2 = await asyncio.gather(als2, ecv2, ssg2, lnv2, asu2, edf2, wd2, tpl2, sst2, vsn2, drt2, apl2, ksp2, off2, dns2, cit2, itm2)
        als_pairs = als_r2
        ecv_pairs = ecv_r2
        ssg_pairs = ssg_r2
        lnv_pairs = lnv_r2
        asu_pairs = asu_r2
        edf_pairs = edf_r2
        wd_pairs = wd_r2
        tpl_pairs = tpl_r2
        sst_pairs = sst_r2
        vsn_pairs = vsn_r2
        drt_pairs = drt_r2
        apl_pairs = apl_r2 if apl_r2 and isinstance(apl_r2[0], tuple) else []
        ksp_pairs = ksp_r2
        off_pairs = off_r2
        cdn_pairs = []
        dns_pairs = dns_r2
        cit_pairs = cit_r2
        itm_pairs = itm_r2
        all_pairs = als_pairs + ecv_pairs + ssg_pairs + lnv_pairs + asu_pairs + edf_pairs + wd_pairs + tpl_pairs + sst_pairs + vsn_pairs + drt_pairs + apl_pairs + ksp_pairs + dns_pairs + cit_pairs + itm_pairs + off_pairs
        log.info(f"Playwright retry (no color): al-style={len(als_pairs)}, ecovacs={len(ecv_pairs)}, samsung={len(ssg_pairs)}, lenovo={len(lnv_pairs)}, asus={len(asu_pairs)}, edifier={len(edf_pairs)}, wd={len(wd_pairs)}, tplink={len(tpl_pairs)}, steelseries={len(sst_pairs)}, viewsonic={len(vsn_pairs)}, dreametech={len(drt_pairs)}, apltech={len(apl_pairs)}, kaspi={len(ksp_pairs)}, dns={len(dns_pairs)}, citilink={len(cit_pairs)}, itmag={len(itm_pairs)}, official={len(off_pairs)}")

    if not all_pairs:
        return []

    # LLM-валидация URL
    if llm is not None:
        page_urls = [u for _, u in all_pairs]
        approved = await _llm_validate_candidates(product, page_urls, llm)
        filtered = [(d, u) for d, u in all_pairs if u in approved]
        if filtered:
            all_pairs = filtered
            log.info(f"Playwright LLM approved: {len(filtered)}/{len(page_urls)}")
        else:
            log.info("Playwright LLM: все отклонены — берём без фильтра")

    scored = await _pw_bytes_to_scored(all_pairs, "Playwright fallback",
                                        product=product, color_en=color_en,
                                        skip_clip=vision_validate, category=category)
    if scored:
        log.info(f"Playwright fallback: {len(scored)} фото (al-style={len(als_pairs)}, apltech={len(apl_pairs)}, kaspi={len(ksp_pairs)})")
    return scored


async def find_product_images(product: str, n: int = 1,
                                max_candidates: int = 10,
                                brand: str = "",
                                color: str = "",
                                color_en: str = "",
                                category: str = "",
                                iou_threshold: float = 0.85,
                                llm=None,
                                vision_validate: bool = False,
                                exclude_urls: list[str] | None = None,
                                article: str = "") -> list[bytes]:
    """
    Возвращает до `n` фото товара, отсортированных по score.
    Фильтр дубликатов — по IoU силуэтов (rembg-маски).

    Опции:
    - `llm` — DeepSeek валидация URL топ-5 (отсекает по имени файла/путям)
    - `vision_validate=True` — каждое выбираемое фото проходит Gemini Vision
       (смотрит САМО фото и говорит «одиночный товар без коробки или нет»).
       Если REJECT — пробуем следующего по score.
    - `exclude_urls` — URL, которые уже использовались ранее (например, при
       /altphoto — переподбор без уже показанных фото).
    - `article` — точный артикул товара (vendorCode). Если он встречается в
       самом URL фото (типично для official-CDN, напр.
       assets.adidas.com/.../JS4429_04_standard.jpg) — это единственный
       способ реально проверить, что фото именно ЭТОГО SKU, а не похожего
       (найдено 21.07.2026: чужой артикул JP9197 прошёл как "Duramo RC2
       чёрный" наравне с настоящим JS4429, дедуп по силуэту выкинул верное
       фото просто потому что у неверного был выше числовой score раньше).
       Даёт таким кандидатам приоритетный буст скора.

    n=1 эквивалентно старому поведению find_product_image.
    """
    # Для поисковых запросов чистим спецсимволы из имени:
    # "SPK-220/225 (2.0)" → "SPK-220-225" — Exa не матчит слэши в кавычках
    _pq = re.sub(r'\([^)]*\)', '', product).strip()  # убираем скобки
    _pq = _pq.replace("/", "-").strip()               # слэш → дефис

    # Адаптивная схема запросов (03.07.2026, экономия Exa ~50%):
    # сначала ЯДРО (3 запроса — кавычки+купить, official photo, с цветом),
    # добор ДОПОЛНИТЕЛЬНЫХ — только если уникальных кандидатов мало.
    # Обоснование: скачиваются всё равно только первые max_candidates (10),
    # у популярных товаров хвостовые запросы приносили URL, которые
    # выбрасывались до скачивания. Нишевые товары автоматически получают
    # полный набор через добор.
    if color_en:
        core_queries = [
            f'"{_pq}" {color_en} купить',
            f"{_pq} {color_en} official product photo",
            f'"{_pq}" купить',
        ]
        extra_queries = [
            f"{_pq} official product photo",
            f"{_pq} {color_en} купить",   # без кавычек — шире для нишевых брендов
        ]
    else:
        core_queries = [
            f'"{_pq}" купить',
            f"{_pq} official product photo",
        ]
        extra_queries = [
            f"{_pq} купить",              # без кавычек — шире для нишевых брендов
        ]
    # Обзорные/специфичные базы (rtings, techpowerup, storagereview и т.п.) часто
    # дают студийные фото без вотермарков маркетплейсов — но их не находят
    # "купить"/"official photo" запросы (это не торговые страницы). og:image
    # у таких сайтов лежит в обычном HTML — резолвится через _candidate_url
    # без Playwright.
    extra_queries.append(f"{_pq} review")
    if n > 1:
        # Доп. ракурсы нужны только когда просят несколько фото — оставляем в ядре
        core_queries.extend([
            f"{product} {color_en} back view" if color_en else f"{product} back view",
            f"{product} {color_en} side view" if color_en else f"{product} side view",
        ])

    # Фолбэк: если последнее слово — артикул (буквы+цифры, напр. PTK470K0),
    # добавляем запросы без него — "Wacom Intuos Pro Small" вместо "Wacom Intuos Pro Small PTK470K0"
    _last_word = _pq.split()[-1] if _pq.split() else ""
    if (re.search(r'\d', _last_word) and re.search(r'[A-Za-z]', _last_word)
            and len(_pq.split()) > 2):
        _pq_short = ' '.join(_pq.split()[:-1])
        extra_queries.extend([
            f'"{_pq_short}" {color_en} купить' if color_en else f'"{_pq_short}" купить',
            f"{_pq_short} {color_en} official product photo" if color_en else f"{_pq_short} official product photo",
        ])

    # (Принудительный gsmarena-запрос убран 2026-06-02: настоящий gsmarena.com
    #  закрыт Cloudflare, а через include_domains просачивалось зеркало
    #  gsmarena.com.ng с водяными знаками MOBILEDOKAN — портило карточки.)

    async def _collect_candidates(qs: list[str]) -> list[str]:
        """Запросы к поиску параллельно, затем параллельный резолв og:image.
        SearXNG-first (self-hosted, бесплатно) вместо Exa; Exa дёргается
        внутри HybridSearch только если включена (exa_enabled) и SearXNG пуст."""
        from .hybrid import product_search
        responses = await asyncio.gather(
            *[product_search.search_urls(q, max_results=8) for q in qs]
        )
        tasks = [_candidate_url(r.url, product, brand)
                 for resp in responses for r in resp.results]
        return await asyncio.gather(*tasks) if tasks else []

    _MIN_CANDIDATES = 8
    seen_urls: set = set()
    candidate_urls = []
    for u in await _collect_candidates(core_queries):
        if u and u not in seen_urls:
            seen_urls.add(u)
            candidate_urls.append(u)

    # Прямые кандидаты из image-поиска SearXNG (Google/Bing Images через
    # локальный инстанс, бесплатно): img_src — готовый URL картинки, без
    # резолва og:image. Фильтры те же, что в _candidate_url, но применяются
    # к URL страницы-источника (page_url), а «плохая картинка» — к img_url.
    _img_query = f"{_pq} {color_en}".strip() if color_en else _pq
    for img_url, page_url in await searxng_search.search_images(_img_query, max_results=20):
        if img_url in seen_urls:
            continue
        _low = (img_url + " " + page_url).lower()
        if any(b in _low for b in _WATERMARK_DOMAINS):
            continue
        _pdom = urlparse(page_url).netloc.replace("www.", "")
        if _pdom in BLACKLIST:
            continue
        if not _url_matches_brand(page_url, product, brand):
            continue
        if not _url_matches_model(page_url, product, brand):
            continue
        if _is_bad_image(img_url):
            continue
        seen_urls.add(img_url)
        candidate_urls.append(_upgrade_image_url(img_url))

    if len(candidate_urls) < _MIN_CANDIDATES and extra_queries:
        log.info(f"Image candidates: ядро дало {len(candidate_urls)} < {_MIN_CANDIDATES} — "
                 f"добор {len(extra_queries)} доп. запросами")
        for u in await _collect_candidates(extra_queries):
            if u and u not in seen_urls:
                seen_urls.add(u)
                candidate_urls.append(u)

    log.info(f"Image candidates: {len(candidate_urls)} unique URLs (need top-{n})")

    if not candidate_urls:
        log.warning(f"No candidate URLs for: {product} — веб-поиск пуст, trying Playwright/CDN fallback")

    # Приоритет цвету: URL, где нужный цвет указан прямо в адресе/имени файла
    # (…ink-black…, …titanium-grey…), двигаем в начало — чтобы они гарантированно
    # попали в скачиваемые max_candidates. Иначе правильное по цвету фото могло
    # «вылететь за восьмёрку» из-за плавающего порядка выдачи Exa. Бесплатно
    # (сравнение строк), на цвет-проверку score это НЕ влияет — только на то,
    # какие кандидаты вообще будут скачаны и оценены.
    # Приоритет в очереди скачивания: вперёд двигаем URL, где в адресе есть номер
    # МОДЕЛИ (вес 2 — важнее) и/или нужный ЦВЕТ (вес 1) — чтобы правильные
    # кандидаты гарантированно попали в скачиваемые max_candidates. Бесплатно
    # (сравнение строк), на скоринг не влияет, только на порядок скачивания.
    model_tokens = _model_url_tokens(product, brand)
    color_tokens: set[str] = set()
    if color_en:
        color_tokens.add(color_en)
        if color_en == "grey":
            color_tokens.add("gray")
        elif color_en == "gray":
            color_tokens.add("grey")
    for w in re.split(r"[\s\-_]+", (color or "").lower()):
        if len(w) >= 3:
            color_tokens.add(w)
    color_tokens.discard("")

    if model_tokens or color_tokens or any(d in u.lower() for u in candidate_urls for d in _PRIORITY_DOMAINS):
        def _url_prio(u: str) -> int:
            low = u.lower()
            score = 0
            if model_tokens and any(t in low for t in model_tokens):
                score += 2
            if color_tokens and any(t in low for t in color_tokens):
                score += 1
            if any(d in low for d in _PRIORITY_DOMAINS):
                score += 1
            return score
        if any(_url_prio(u) > 0 for u in candidate_urls):
            n_model = sum(1 for u in candidate_urls
                          if model_tokens and any(t in u.lower() for t in model_tokens))
            candidate_urls = sorted(candidate_urls, key=_url_prio, reverse=True)
            log.info(f"URL priority → front (model-match={n_model}, "
                     f"model_tokens={model_tokens}, color_tokens={sorted(color_tokens)})")

    download_tasks = [_download_bytes(u) for u in candidate_urls[:max_candidates]]
    downloads = await asyncio.gather(*download_tasks)

    async def _score_all(relaxed: bool):
        tag = " [relaxed]" if relaxed else ""

        # Дедуп по хешу СНАЧАЛА, до параллельного скоринга — иначе при
        # параллельных задачах гонка может пропустить повторный хеш мимо
        # проверки (несколько одинаковых картинок стартуют скоринг
        # одновременно, ни одна ещё не попала в seen_hashes).
        seen_hashes: set = set()
        candidates: list[tuple[str, bytes]] = []
        for url, data in zip(candidate_urls[:max_candidates], downloads):
            domain = urlparse(url).netloc or url
            if not data:
                log.info(f"Download failed{tag}: {domain}")
                continue
            if len(data) < 5_000:
                log.info(f"Too small{tag} ({len(data)}b): {domain}")
                continue
            file_hash = hash(data)
            if file_hash in seen_hashes:
                continue
            seen_hashes.add(file_hash)
            candidates.append((url, data))

        # Скоринг — до 3 кандидатов параллельно. Не больше: на рабочей
        # машине 6 ядер CPU, и параллельно с перебором обычно ещё крутятся
        # Chromium (Playwright-фоллбэк) и SearXNG — не хотим их душить.
        sem = asyncio.Semaphore(3)

        async def _score_one(url: str, data: bytes):
            domain = urlparse(url).netloc or url
            async with sem:
                log.debug(f"Scoring{tag}: {domain}")
                score, mask = await run_gpu(
                    _score_image, data, product, relaxed, color_en, category,
                    timeout=45, default=(0.0, None), label="_score_image",
                )
            if score <= 0:
                log.info(f"Rejected{tag}: {domain}")
                return None
            # Точный артикул в URL — единственная реальная проверка "это
            # именно наш SKU, не похожий" (см. docstring find_product_images).
            # Буст ×5 гарантированно выигрывает у обычных кандидатов и
            # переживает IoU-дедуп силуэтов (тот сравнивает уже отсортированный
            # по score список, оставляя первый — то есть более достоверный).
            if article and article.lower() in url.lower():
                score *= 5
                log.info(f"Verified{tag} (артикул «{article}» в URL): {domain} — буст ×5 → {score:,.0f}")
            log.info(f"Candidate{tag}: {domain} — {len(data):,}b → final_score={score:,.0f}")
            return (score, data, url, mask)

        results = await asyncio.gather(*[_score_one(u, d) for u, d in candidates])
        return [r for r in results if r is not None]

    scored = await _score_all(relaxed=False)

    # Fallback 1: пониженные пороги для нишевых товаров
    used_relaxed = False
    if not scored:
        log.info(f"No candidates passed strict filters for '{product}' — retrying with relaxed thresholds")
        scored = await _score_all(relaxed=True)
        used_relaxed = bool(scored)

    # Fallback 2: Playwright — apltech + kaspi + официальный сайт
    # Запускаем если Exa дал 0 ИЛИ лучший score слишком низкий ИЛИ нужно больше ракурсов.
    # used_relaxed=True тоже триггерит Playwright: раз ни один кандидат не прошёл
    # строгий CLIP-порог (0.40), score = clip*preserve*area может быть высоким
    # просто за счёт большого разрешения фото (площадь доминирует в формуле),
    # маскируя низкую релевантность (кейс Edifier P180 Plus: clip=0.195,
    # area=1100×1100 → score=141k > порога, Playwright не запускался).
    best_exa_score = scored[0][0] if scored else 0
    _SCORE_THRESHOLD = 50_000
    playwright_tried = False
    if not scored or used_relaxed or best_exa_score < _SCORE_THRESHOLD:
        if not scored:
            reason = "0 кандидатов"
        elif used_relaxed:
            reason = f"только relaxed-кандидаты (лучший score={best_exa_score:,.0f})"
        else:
            reason = f"лучший score={best_exa_score:,.0f} < {_SCORE_THRESHOLD:,}"
        log.info(f"Exa: {reason} — подключаем Playwright")
        playwright_tried = True
        pw_scored = await _playwright_fallback(product, color, color_en, llm, brand=brand,
                                               vision_validate=vision_validate, category=category)
        if pw_scored:
            scored = pw_scored + scored  # Playwright вперёд, Exa как запасной

    if not scored:
        log.warning(f"Product image not found: {product}")
        return []

    if exclude_urls:
        excl = set(exclude_urls)
        before = len(scored)
        scored = [t for t in scored if t[2] not in excl]
        log.info(f"exclude_urls: отфильтровано {before - len(scored)} из {before} (уже использовались)")

    if not scored:
        log.warning(f"Product image not found (всё в exclude_urls): {product}")
        return []

    scored.sort(key=lambda t: t[0], reverse=True)

    # LLM-валидация: режем мусор по URL топа (опционально)
    if llm is not None and scored:
        top_urls = [t[2] for t in scored[:5]]
        approved = await _llm_validate_candidates(product, top_urls, llm)
        if approved:
            scored = [t for t in scored if t[2] in approved or t[2] not in top_urls]

    # 20.07.2026: Vision-вердикты для топ-кандидатов запрашиваются ПАРАЛЛЕЛЬНО
    # заранее, а не по одному внутри цикла отбора — раньше ожидания Gemini
    # складывались (замер 20.07: 3.7+30+3+27+9 ≈ 73с последовательно на одну
    # карточку), теперь секция ждёт только самый медленный вызов. Порядок и
    # логика отбора НЕ меняются — цикл потребляет готовые вердикты из
    # _vision_prefetch; кандидаты вне топа идут старым последовательным путём.
    # Осознанная цена: 2-3 Vision-вызова на кандидатов, до которых отбор не
    # дойдёт (~$0.001-0.004/карточку) — дешевле 40-60с ожидания.
    _vision_prefetch: dict[str, asyncio.Task] = {}

    async def _vision_verdict_raw(data: bytes, url: str) -> bool | None:
        try:
            Image.open(io.BytesIO(data)).load()
        except Exception:
            log.info(f"Invalid image data (not decodable) — skip: {url}")
            return False
        from services.image.gemini import validate_product_image
        return await validate_product_image(data, product, color=color)

    async def _vision_prefetch_batch(candidates: list) -> dict[str, bool | None]:
        """03.08.2026: батч-путь для префетча (см. _vision_verdict_raw) —
        вместо N поштучных Gemini-вызовов (каждый пересылает заново весь
        промпт правил) делает 2 параллельных batch-вызова по половинам
        кандидатов, промпт правил пересылается один раз на вызов. Фото,
        не декодируемые PIL, и позиции, для которых батч не дал вердикт
        (сбой API или ответ не распарсился построчно) — докрываются
        поштучным validate_product_image, как и раньше. Batch — чистая
        оптимизация поверх старого пути, отбор дальше работает как прежде,
        просто получает то же bool|None на url."""
        from services.image.gemini import validate_images_batch

        decodable: list[tuple[str, bytes]] = []
        results: dict[str, bool | None] = {}
        for _s, _data, _url, _m in candidates:
            try:
                Image.open(io.BytesIO(_data)).load()
            except Exception:
                log.info(f"Invalid image data (not decodable) — skip: {_url}")
                results[_url] = False
                continue
            decodable.append((_url, _data))

        if decodable:
            mid = (len(decodable) + 1) // 2
            halves = [h for h in (decodable[:mid], decodable[mid:]) if h]
            half_results = await asyncio.gather(*(
                validate_images_batch([d for _, d in half], product, color=color)
                for half in halves
            ))
            fallback: list[tuple[str, bytes]] = []
            for half, verdicts in zip(halves, half_results):
                for (_url, _data), verdict in zip(half, verdicts):
                    if verdict is None:
                        fallback.append((_url, _data))
                    else:
                        results[_url] = verdict
            for _url, _data in fallback:
                results[_url] = await _vision_verdict_raw(_data, _url)
        return results

    if vision_validate and scored:
        _prefetch_candidates = [t for t in scored[: max(2 * n + 2, 6)]
                                 if t[2] not in _vision_prefetch]
        from config import settings as _settings
        if _prefetch_candidates and _settings.gemini_vision_batch:
            batch_task = asyncio.create_task(_vision_prefetch_batch(_prefetch_candidates))
            batch_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            async def _pick(url: str) -> bool | None:
                res = await batch_task
                return res.get(url)

            for _s, _data, _url, _m in _prefetch_candidates:
                t = asyncio.create_task(_pick(_url))
                t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                _vision_prefetch[_url] = t
        else:
            for _s, _data, _url, _m in _prefetch_candidates:
                t = asyncio.create_task(_vision_verdict_raw(_data, _url))
                # забираем исключение, если задача не будет востребована
                t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                _vision_prefetch[_url] = t

    async def _vision_ok(data: bytes, url: str, allow_dock: bool = False) -> bool:
        """Gemini Vision: одиночное фото товара без коробки/коллажа?
        Если задан color — Vision проверяет цвет.
        allow_dock=True — отключает правило 'мышь+зарядный док' (запасной проход).
        Если Vision недоступен (None) — считаем пройденным, не блокируем поток."""
        # Бракованные кандидаты (например голый домен вида "https://ants.kz"
        # без пути) отдают HTML вместо изображения — PIL не открывает такие
        # байты, а Gemini Vision на них падает с 400 INVALID_ARGUMENT, который
        # ошибочно пропускался как True. Отсекаем нечитаемые байты до Vision.
        # allow_dock=True меняет правила проверки — префетч (стандартные
        # правила) для него не годится, только прямой вызов.
        task = _vision_prefetch.get(url) if not allow_dock else None
        if task is not None:
            ok = await task
        else:
            try:
                Image.open(io.BytesIO(data)).load()
            except Exception:
                log.info(f"Invalid image data (not decodable) — skip: {url}")
                return False
            if not vision_validate:
                return True
            from services.image.gemini import validate_product_image
            ok = await validate_product_image(data, product, color=color, allow_dock=allow_dock)
        if ok is False:
            log.info(f"Gemini Vision REJECT: {url}")
        return ok is not False

    async def _vision_check(data: bytes, url: str) -> bool | None:
        """Tri-state проверка для отбора доп. ракурсов: True — подтверждён,
        False — отклонён, None — Vision недоступен (вердикт неизвестен,
        кандидата откладываем, но не принимаем сразу)."""
        task = _vision_prefetch.get(url)
        if task is not None:
            ok = await task
        else:
            try:
                Image.open(io.BytesIO(data)).load()
            except Exception:
                log.info(f"Invalid image data (not decodable) — skip: {url}")
                return False
            if not vision_validate:
                return True
            from services.image.gemini import validate_product_image
            ok = await validate_product_image(data, product, color=color)
        if ok is False:
            log.info(f"Gemini Vision REJECT: {url}")
        elif ok is None:
            log.info(f"Gemini Vision недоступен — откладываем кандидата: {url}")
        return ok

    async def _vision_ok_angle(data: bytes, url: str) -> tuple[bool, bool]:
        """Как _vision_ok, но дополнительно сообщает is_3q — снят ли товар
        под объёмным ракурсом 3/4 (а не плоским фасом/профилем)."""
        try:
            Image.open(io.BytesIO(data)).load()
        except Exception:
            log.info(f"Invalid image data (not decodable) — skip: {url}")
            return False, False
        if not vision_validate:
            return True, False
        from services.image.gemini import validate_product_image
        ok, is_3q = await validate_product_image(data, product, color=color, check_angle=True)
        if not ok:
            log.info(f"Gemini Vision REJECT: {url}")
        return ok, is_3q

    # Колонки/акустика: предпочитаем объёмный ракурс 3/4 (видно несколько
    # граней — верх+перед+бок) для главного фото — плоский фас/профиль
    # выглядит скучно на инфографике. Смотрим первые кандидаты по очереди;
    # первый прошедший Vision и снятый под 3/4 поднимаем в начало списка —
    # дальше всё (выбор главного фото и доп. ракурсов) идёт как обычно.
    if category == "Акустика" and vision_validate and len(scored) > 1:
        _ANGLE_CHECK_LIMIT = 4
        for idx in range(min(_ANGLE_CHECK_LIMIT, len(scored))):
            score, data, url, mask = scored[idx]
            ok, is_3q = await _vision_ok_angle(data, url)
            if ok and is_3q:
                if idx > 0:
                    log.info(f"Ракурс 3/4 найден среди кандидатов (#{idx+1}) — поднимаем в начало: {url}")
                    scored.insert(0, scored.pop(idx))
                break

    if n <= 1:
        for score, data, url, _ in scored:
            if await _vision_ok(data, url):
                log.info(f"Best image selected: {url} score={score:,.0f}")
                with open(r"C:\AI-Bot-V2\data\debug_original.jpg", "wb") as f:
                    f.write(data)
                find_product_images._last_url = url
                find_product_images._last_urls = [url]
                return [data]
        if vision_validate:
            log.info("Vision: 0 кандидатов — повтор без правила 'мышь+док'")
            for score, data, url, _ in scored:
                if await _vision_ok(data, url, allow_dock=True):
                    log.info(f"Best image selected (док допущен): {url} score={score:,.0f}")
                    find_product_images._last_url = url
                    find_product_images._last_urls = [url]
                    return [data]
        log.warning(f"Product image not found (all rejected by Vision): {product}")
        find_product_images._last_urls = []
        return []

    # Для n>1 — фильтр по IoU силуэтов + минимальному относительному score
    main_score = scored[0][0]
    min_extra_score = main_score * _RELATIVE_SCORE
    selected: list[bytes] = []
    selected_masks: list[np.ndarray] = []
    selected_urls: list[str] = []
    selected_phashes: list[np.ndarray | None] = []

    def _is_dup_phash(data: bytes) -> bool:
        """True если картинка почти совпадает с уже выбранной (perceptual hash) —
        ловит дубли без rembg-маски (Playwright-источники)."""
        h = _phash(data)
        return any(_phash_similar(h, prev) for prev in selected_phashes)
    vision_rejected: list[tuple[float, bytes, str, np.ndarray | None]] = []
    vision_unknown: list[tuple[float, bytes, str, np.ndarray | None]] = []

    async def _color_strict_ok(data: bytes) -> bool:
        """Для thumbs (не main) цвет должен жёстко совпадать.
        Главное фото может пройти даже при mismatch (penalty x0.10),
        но дополнительные ракурсы — только нужного цвета."""
        if not color_en:
            return True
        try:
            from services.image.color_match import color_matches
            from services.image.background import remove_background
            rgba = await run_gpu(remove_background, data, timeout=110, label="remove_background(color strict)")
            if rgba is None:
                return True
            match, _, _ = color_matches(rgba, color_en)
            return match
        except Exception:
            return True

    for score, data, url, mask in scored:
        if len(selected) >= n:
            break

        # Главное фото — без relative-фильтра; дополнительные проверяем
        if selected and score < min_extra_score:
            log.info(f"Skip low-score angle ({score:,.0f} < {min_extra_score:,.0f}): {url}")
            continue

        # IoU-дедуп силуэтов — только если маска есть (Playwright-кандидаты
        # приходят с mask=None; раньше это значило безусловный skip,
        # из-за чего весь Playwright fallback отсекался при n>1).
        if mask is not None:
            too_similar = False
            for prev_mask in selected_masks:
                iou = _mask_iou(mask, prev_mask)
                if iou > iou_threshold:
                    log.info(f"Skip same silhouette (IoU={iou:.3f}): {url}")
                    too_similar = True
                    break
            if too_similar:
                continue

        # Для дополнительных ракурсов — жёсткий color match
        if selected and not await _color_strict_ok(data):
            log.info(f"Skip color-mismatch thumb: {url}")
            continue

        if selected and _is_dup_phash(data):
            log.info(f"Skip near-duplicate photo (phash): {url}")
            continue

        verdict = await _vision_check(data, url)
        if verdict is False:
            vision_rejected.append((score, data, url, mask))
            continue
        if verdict is None:
            vision_unknown.append((score, data, url, mask))
            continue

        selected.append(data)
        if mask is not None:
            selected_masks.append(mask)
        selected_phashes.append(_phash(data))
        selected_urls.append(url)
        log.info(f"Picked angle #{len(selected)}: score={score:,.0f} url={url}")

    # Если не набрали n подтверждённых Vision кандидатов — добираем из тех,
    # для кого Vision был недоступен (лучше неподтверждённый, чем заведомо
    # отклонённый по содержанию).
    if len(selected) < n and vision_unknown:
        for score, data, url, mask in vision_unknown:
            if len(selected) >= n:
                break
            if mask is not None:
                too_similar = False
                for prev_mask in selected_masks:
                    iou = _mask_iou(mask, prev_mask)
                    if iou > iou_threshold:
                        too_similar = True
                        break
                if too_similar:
                    continue
            if selected and not await _color_strict_ok(data):
                continue
            if selected and _is_dup_phash(data):
                continue
            selected.append(data)
            if mask is not None:
                selected_masks.append(mask)
            selected_phashes.append(_phash(data))
            selected_urls.append(url)
            log.info(f"Picked angle #{len(selected)} (Vision недоступен): score={score:,.0f} url={url}")

    if len(selected) < n and vision_rejected:
        log.info(f"Selected {len(selected)}/{n} — повтор {len(vision_rejected)} отклонённых без правила 'мышь+док'")
        for score, data, url, mask in vision_rejected:
            if len(selected) >= n:
                break
            if mask is not None:
                too_similar = False
                for prev_mask in selected_masks:
                    iou = _mask_iou(mask, prev_mask)
                    if iou > iou_threshold:
                        too_similar = True
                        break
                if too_similar:
                    continue
            if selected and not await _color_strict_ok(data):
                continue
            if selected and _is_dup_phash(data):
                continue
            if await _vision_ok(data, url, allow_dock=True):
                selected.append(data)
                if mask is not None:
                    selected_masks.append(mask)
                selected_phashes.append(_phash(data))
                selected_urls.append(url)
                log.info(f"Picked angle #{len(selected)} (док допущен): score={score:,.0f} url={url}")

    # Если для n>1 не набрали нужное число даже после всех фоллбэков —
    # ранний выбор "пропустить Playwright" был основан на score ДО Vision
    # (кейс: Exa дал один кандидат с высоким score, Playwright не подключался,
    # а Vision потом отклонила почти всё — добирать было уже не от кого).
    # Подключаем Playwright сейчас, раз он ещё не запускался.
    if n > 1 and len(selected) < n and not playwright_tried:
        log.info(f"Selected {len(selected)}/{n} — добираем через Playwright (не запускался ранее)")
        pw_extra = await _playwright_fallback(product, color, color_en, llm, brand=brand,
                                               vision_validate=vision_validate, category=category)
        pw_extra = [t for t in pw_extra if t[2] not in selected_urls]
        pw_extra.sort(key=lambda t: t[0], reverse=True)
        for score, data, url, mask in pw_extra:
            if len(selected) >= n:
                break
            if mask is not None:
                too_similar = False
                for prev_mask in selected_masks:
                    if _mask_iou(mask, prev_mask) > iou_threshold:
                        too_similar = True
                        break
                if too_similar:
                    continue
            if selected and not await _color_strict_ok(data):
                continue
            if selected and _is_dup_phash(data):
                continue
            if await _vision_ok(data, url, allow_dock=True):
                selected.append(data)
                if mask is not None:
                    selected_masks.append(mask)
                selected_phashes.append(_phash(data))
                selected_urls.append(url)
                log.info(f"Picked angle #{len(selected)} (добор Playwright): score={score:,.0f} url={url}")

    # 17.07.2026: если целевой цвет заранее НЕ известен (color_en пуст),
    # _color_strict_ok выше молча пропускает все фото без проверки — а
    # разные источники могут показывать РАЗНЫЕ цветовые варианты одной и
    # той же модели (кейс Teltonika RUTX08/RUTX10: 2 фото чёрных + 1 белое
    # среди 3 отобранных, хотя модель на всех верная). Сверяем отобранные
    # фото друг с другом, оставляем только цветовое большинство. По
    # просьбе пользователя — БЕЗ доискивания замены, просто отбрасываем
    # лишнее и остаёмся с тем, что есть.
    if not color_en and len(selected) > 1:
        selected, selected_urls = await _filter_color_majority(selected, selected_urls)

    find_product_images._last_url = selected_urls[0] if selected_urls else ""
    find_product_images._last_urls = selected_urls
    log.info(f"Selected {len(selected)}/{n} distinct angles")
    return selected


def _center_dominant_color(data: bytes) -> tuple[int, int, int] | None:
    """Дешёвая оценка цвета корпуса товара БЕЗ rembg/GPU — берёт моду по
    центральным 50% кадра (обычно там сам товар, а не фон/края), отбросив
    почти-белые пиксели фона, которые могли затесаться и в центр.

    17.07.2026: изначально эта проверка использовала remove_background()
    (полноценный rembg-вырез) — но у на этой машине CUDA-провайдер
    нестабилен (см. ONNXRuntimeError LoadLibrary error 126, известная
    проблема сессии), и CPU-фолбэк для birefnet-general на некоторых фото
    занимал 110+ секунд НА ОДНО фото даже в полной изоляции — таймаут
    срабатывал ИМЕННО на нужном для сравнения фото, и цветовое большинство
    было невозможно посчитать. Грубая оценка по центру кадра работает за
    миллисекунды и для этой задачи (отличить чёрный корпус от белого)
    точности хватает с большим запасом."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    if w < 20 or h < 20:
        return None
    x0, x1 = int(w * 0.25), int(w * 0.75)
    y0, y1 = int(h * 0.25), int(h * 0.75)
    crop = img.crop((x0, y0, x1, y1))
    crop.thumbnail((100, 100), Image.LANCZOS)
    arr = np.asarray(crop).reshape(-1, 3).astype(np.int32)
    non_white = arr[~((arr[:, 0] > 235) & (arr[:, 1] > 235) & (arr[:, 2] > 235))]
    sample = non_white if len(non_white) >= 20 else arr
    step = 24
    q = sample // step
    keys = q[:, 0] * 100_000 + q[:, 1] * 1_000 + q[:, 2]
    uniq, counts_ = np.unique(keys, return_counts=True)
    top_key = uniq[int(np.argmax(counts_))]
    body = sample[keys == top_key].mean(axis=0)
    return int(body[0]), int(body[1]), int(body[2])


# 20.07.2026: домены sitemap-based официальных сайтов производителей (см.
# _search_*_playwright с sitemap-индексацией) — единственный источник, чьё
# фото сайт публикует сам производитель, поэтому ему можно доверять при
# ничьей вместо того чтобы выбрасывать всё (см. _filter_color_majority).
_OFFICIAL_SITEMAP_DOMAINS = (
    "ecovacs.com", "samsung.com", "lenovo.com", "asus.com", "edifier.com",
    "westerndigital.com", "tp-link.com", "steelseries.com", "viewsonic.com",
)


async def _filter_color_majority(photos: list[bytes], urls: list[str]) -> tuple[list[bytes], list[str]]:
    """Оставляет только фото, чей цвет корпуса совпадает с цветовым
    большинством среди уже отобранных — отбрасывает явных «отступников»
    (другой цветовой вариант той же модели) без попытки найти замену.
    Если явного большинства нет (ничья) — доверяем официальному сайту
    производителя (sitemap-источник), если он есть среди кандидатов;
    иначе (ничья без официального источника, либо конфликт между двумя
    официальными доменами — редкий край) выкидываем всё, как раньше.

    20.07.2026 (живой батч БП): дешёвый пиксельный детектор регулярно путал
    grey/black/white между собой (3 ничьи из 6 первых карточек!) — при этом
    у части источников (regard.ru и др.) цвет прямо в слаге URL
    ('...-atx-31-black' / '...-atx-31-white'), надёжнее любого пиксельного
    сэмплинга. Проверяем URL ПЕРЕД пиксельным анализом, используем его как
    источник истины, если слово однозначное; пиксели — только фолбэк."""
    from collections import Counter
    from services.image.color_match import rgb_to_color_name, _luminance

    _URL_COLOR_WORDS = {
        "black": "black", "white": "white", "grey": "grey", "gray": "grey",
        "silver": "silver", "blue": "blue", "red": "red", "green": "green",
        "gold": "gold", "pink": "pink", "purple": "purple", "beige": "beige",
        "brown": "brown", "chern": "black", "belyi": "white", "belyy": "white",
    }

    def _url_color(url: str) -> str | None:
        found = set()
        low = url.lower()
        for word, canon in _URL_COLOR_WORDS.items():
            if re.search(rf"(?:^|[^a-z]){word}(?:[^a-z]|$)", low):
                found.add(canon)
        return found.pop() if len(found) == 1 else None

    names: list[str | None] = []
    for data, url in zip(photos, urls):
        name = _url_color(url)
        if name is None:
            rgb = _center_dominant_color(data)
            name = rgb_to_color_name(rgb) if rgb else None
            if name is None and rgb is not None:
                lum = _luminance(rgb)
                name = "black" if lum < 0.32 else "white" if lum > 0.70 else None
        names.append(name)

    known = [n for n in names if n]
    if len(known) < 2:
        return photos, urls  # нечего сравнивать

    counts = Counter(known)
    majority_color, majority_n = counts.most_common(1)[0]
    rest_n = len(known) - majority_n
    if majority_n <= rest_n:
        official_domains = {
            d for u in urls for d in _OFFICIAL_SITEMAP_DOMAINS if d in u
        }
        if len(official_domains) == 1:
            domain = next(iter(official_domains))
            kept = [(d, u) for d, u in zip(photos, urls) if domain in u]
            log.info(f"Color majority: ничья ({dict(counts)}) — доверяем официальному "
                     f"{domain}, оставляем {len(kept)} фото вместо отброса всех")
            return [p for p, _ in kept], [u for _, u in kept]
        # 20.07 (живой батч БП): ничья и без официального источника — раньше
        # выкидывали ВСЁ (карточка без фото). Но этот фильтр вызывается ТОЛЬКО
        # когда цвет в названии НЕ задан (см. вызов: `if not color_en ...`) —
        # продавцу подходит любой вариант, важна лишь согласованность фото
        # между собой. На первых 5 позициях БП 2 потеряли все фото именно на
        # ничьих (blue/black/grey и white/black/grey: у БП тёмный корпус,
        # дешёвый детектор путает black/grey, а White-вариант реально
        # существует у половины моделей). Оставляем группу цвета ГЛАВНОГО
        # (топ-скорового) фото вместо полного отброса.
        main_color = names[0]
        if main_color is not None:
            kept_photos, kept_urls = [], []
            for data, url, name in zip(photos, urls, names):
                if name is None or name == main_color:
                    kept_photos.append(data)
                    kept_urls.append(url)
            log.info(f"Color majority: ничья ({dict(counts)}) — цвет не задан в "
                     f"названии, оставляем группу главного фото {main_color!r}: "
                     f"{len(kept_photos)} из {len(photos)}")
            return kept_photos, kept_urls
        # Цвет главного фото не определился — оставляем только его самого
        # (одно фото лучше, чем ноль; главное = лучший суммарный score).
        log.info(f"Color majority: ничья ({dict(counts)}), цвет главного фото "
                 f"не определён — оставляем только главное")
        return photos[:1], urls[:1]

    kept_photos, kept_urls = [], []
    for data, url, name in zip(photos, urls, names):
        if name is None or name == majority_color:
            kept_photos.append(data)
            kept_urls.append(url)
        else:
            log.info(f"Color majority: отброшено {url} (цвет={name!r}, большинство={majority_color!r})")
    return kept_photos, kept_urls


async def find_product_image(product: str, max_candidates: int = 6,
                              brand: str = "") -> bytes | None:
    """Совместимость: возвращает байты лучшего одного фото."""
    images = await find_product_images(product, n=1,
                                        max_candidates=max_candidates, brand=brand)
    return images[0] if images else None
