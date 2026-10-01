import asyncio
import logging
import re

from .common import SearchResult, SearchResponse, BLACKLIST, BROWSER_SEMAPHORE
from .searxng import _extract_pdf_text

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SKIP_DOMAINS = ("yandex.", "ya.ru", "google.", "googleusercontent", "gstatic.")


async def _extract_links(page, max_links: int) -> list[str]:
    """Достаёт уникальные внешние ссылки с открытой страницы поисковика,
    отфильтрованные от самого поисковика и BLACKLIST."""
    hrefs = await page.eval_on_selector_all(
        "a[href^='http']",
        "els => [...new Set(els.map(e => e.href))]"
    )
    out: list[str] = []
    seen_domains: set[str] = set()
    for u in hrefs:
        try:
            domain = u.split("/")[2]
        except IndexError:
            continue
        if any(s in domain for s in _SKIP_DOMAINS):
            continue
        if any(b in domain for b in BLACKLIST):
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        out.append(u)
        if len(out) >= max_links:
            break
    return out


async def _fetch_page_text(page, url: str, max_chars: int = 6000) -> str:
    # PDF (инструкции/даташиты производителей) через page.goto открывается
    # встроенным PDF-вьювером Chromium — document.body.innerText с него пустой.
    # Тянем как обычный HTTP-запрос и парсим тем же pypdf, что и в searxng.py.
    if url.lower().split("?")[0].endswith(".pdf"):
        try:
            resp = await page.context.request.get(url, timeout=20000)
            if resp.ok:
                data = await resp.body()
                text = await asyncio.to_thread(_extract_pdf_text, data, max_chars)
                log.info(f"web_fallback PDF fetch: {url} ({len(data)}b) → извлечено {len(text)} символов")
                return text
        except Exception as e:
            log.warning(f"web_fallback: PDF не загрузился {url}: {e}")
        return ""
    try:
        await page.goto(url, timeout=20000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        text = await page.evaluate("() => document.body.innerText")
        text = re.sub(r"\s+", " ", text or "").strip()
        return text[:max_chars]
    except Exception as e:
        log.warning(f"web_fallback: страница не загрузилась {url}: {e}")
        return ""


async def search_web_context(query: str, max_pages: int = 4, timeout: float = 120) -> SearchResponse:
    """Обёртка с таймаутом (08.07.2026): своя Playwright-сессия, независимая
    от _playwright_fallback в services/search/images.py — тот же риск
    зависания при «грязном» падении браузера (авария видеодрайвера при
    конкуренции CLIP+Chromium за GPU), но здесь таймаута не было вообще."""
    try:
        return await asyncio.wait_for(_search_web_context_impl(query, max_pages), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(f"web_fallback: таймаут {timeout:.0f}с ('{query[:50]}') — считаем пустым результатом")
        return SearchResponse()


async def _search_web_context_impl(query: str, max_pages: int) -> SearchResponse:
    """Резервный поиск через Google/Yandex напрямую (Playwright), когда Exa
    вернула пусто — спасает новые/нишевые товары, которых ещё нет в индексе Exa.
    Сначала пробует Yandex (русскоязычные магазины), при пустом результате — Google."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright не установлен — web_fallback недоступен")
        return SearchResponse()

    results: list[SearchResult] = []
    try:
        async with BROWSER_SEMAPHORE:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--disable-gpu", "--disable-software-rasterizer"])
                try:
                    page = await browser.new_page(user_agent=UA, locale="ru-RU")

                    links: list[str] = []
                    try:
                        yandex_url = f"https://yandex.ru/search/?text={query.replace(' ', '+')}"
                        await page.goto(yandex_url, timeout=25000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2500)
                        links = await _extract_links(page, max_pages)
                        log.info(f"web_fallback Yandex: '{query[:50]}' → {len(links)} ссылок")
                    except Exception as e:
                        log.warning(f"web_fallback Yandex search error: {e}")

                    if not links:
                        try:
                            google_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&hl=ru&gl=ru"
                            await page.goto(google_url, timeout=25000, wait_until="domcontentloaded")
                            await page.wait_for_timeout(2500)
                            links = await _extract_links(page, max_pages)
                            log.info(f"web_fallback Google: '{query[:50]}' → {len(links)} ссылок")
                        except Exception as e:
                            log.warning(f"web_fallback Google search error: {e}")

                    for url in links:
                        text = await _fetch_page_text(page, url)
                        if len(text) >= 300:
                            results.append(SearchResult(url=url, title="", text=text))
                finally:
                    await browser.close()
    except Exception as e:
        log.warning(f"web_fallback failed: {e}")
        return SearchResponse()

    log.info(f"web_fallback: '{query[:50]}' → {len(results)} страниц с контентом")
    return SearchResponse(results=results, request_count=0)
