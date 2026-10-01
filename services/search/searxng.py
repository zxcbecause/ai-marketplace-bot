import asyncio
import logging
import re
import sys
from pathlib import Path

import httpx
from pypdf import PdfReader
from io import BytesIO

from config import settings
from .common import BaseContextSearch, SearchResult, SearchResponse, BLACKLIST
from . import cache as _cache

log = logging.getLogger(__name__)

# Qrator/WAF блокирует прямой фетч страниц с этой машины (проверено 12.07.2026).
# При include_domains эти домены полностью исключаются из site:-запроса (см.
# _query) — не только из фетча: даже SERP-сниппет по ним обычно короче 300
# символов (порога as_context), так что слот в per_domain-бюджете на них тратить
# не имеет смысла. При обычном (без include_domains) поиске эти домены всё же
# могут попасться в общей выдаче — тогда фетч контента просто пропускается.
FETCH_BLACKLIST = {"dns-shop.ru", "www.wildberries.ru", "wildberries.ru"}

# trafilatura.extract запускаем отдельным процессом (см. _extract_worker.py) —
# lxml может упасть access violation'ом на некоторых страницах (2026-08-17,
# Wharfedale), а это нативный краш, try/except в основном процессе не спасает.
_EXTRACT_WORKER = Path(__file__).with_name("_extract_worker.py")


async def _extract_text(html: str) -> str | None:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_EXTRACT_WORKER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(html.encode("utf-8", "ignore")), timeout=20
        )
    except asyncio.TimeoutError:
        log.warning("trafilatura worker завис — убиваю, бот жив")
        if proc is not None:
            proc.kill()
            await proc.wait()
        return None
    except Exception as e:
        log.warning(f"trafilatura worker: {e}")
        return None
    if proc.returncode != 0:
        log.warning(f"trafilatura worker упал (код {proc.returncode}) — изолировано, бот жив")
        return None
    return out.decode("utf-8", "ignore")


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", text).strip()


def _domain(url: str) -> str:
    return url.split("/")[2].lower() if url.startswith("http") else ""


def _is_pdf(url: str, content_type: str) -> bool:
    return "application/pdf" in content_type.lower() or url.lower().split("?")[0].endswith(".pdf")


def _extract_pdf_text(data: bytes, max_chars: int) -> str:
    """Инструкции/даташиты производителей часто содержат точные габариты и
    характеристики, которых нет в карточках ритейла — но там же лежит и
    мусор (юридические страницы, многоязычные дубли), поэтому режем по
    max_chars как обычную страницу, не пытаясь быть умнее."""
    try:
        reader = PdfReader(BytesIO(data))
        parts = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            if not t:
                continue
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return _WS_RE.sub(" ", "\n".join(parts)).strip()
    except Exception:
        return ""


class SearxngSearch(BaseContextSearch):
    """Бесплатная замена Exa: локальный self-hosted SearXNG (метапоиск по
    Google/Bing/Brave/DuckDuckGo/Startpage) даёт ссылки, полный текст страниц
    докачиваем сами напрямую (httpx)."""

    def __init__(self):
        self._base_url = settings.searxng_url

    async def _raw_query(self, query: str, limit: int) -> list[dict]:
        cache_key = _cache.make_key("searxng_raw", query)
        cached = await _cache.get_cached(cache_key)
        if cached is not None:
            return cached[:limit]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self._base_url}/search",
                    params={"q": query, "format": "json"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning(f"SearXNG query failed '{query[:60]}': {e}")
            return []

        out = []
        for item in data.get("results", []):
            domain = _domain(item.get("url") or "")
            if not domain or any(b in domain for b in BLACKLIST):
                continue
            out.append(item)

        # Кэшируем ПОЛНЫЙ отфильтрованный список (без обрезки по limit) —
        # повторный запрос с другим limit не промахивается мимо кэша.
        await _cache.set_cached(cache_key, out)
        return out[:limit]

    async def _query(
        self, query: str, max_results: int, include_domains: list[str] | None
    ) -> list[dict]:
        if not include_domains:
            return await self._raw_query(query, max_results)

        # Общий запрос + постфильтр по домену почти всегда выдаёт 0-2 результата —
        # SearXNG/поисковики отдают top-N по релевантности БЕЗ учёта домена,
        # узкие ритейл-домены редко попадают в этот top-N. Вместо этого — явные
        # site:-запросы. Домены из FETCH_BLACKLIST (Qrator и т.п. — фетч
        # контента всё равно не удастся) пропускаем, чтобы не тратить слоты.
        #
        # ГРУППИРУЕМ по 4 домена в ОДИН запрос через OR (Google/Bing понимают
        # "site:a.com OR site:b.com") — по-отдельности выходило 13 запросов к
        # движкам на КАЖДЫЙ поиск, и 14.07 в разгар батча все четыре движка
        # (google/brave/ddg/startpage) ушли в suspended за rate limit. С группами
        # тот же охват стоит 3 запроса вместо 13.
        queryable = [d for d in include_domains if d not in FETCH_BLACKLIST] or include_domains
        groups = [queryable[i:i + 4] for i in range(0, len(queryable), 4)]
        per_group = max(3, max_results // len(groups) + 1)
        responses = await asyncio.gather(
            *[self._raw_query(f"{query} site:{' OR site:'.join(g)}", per_group) for g in groups]
        )
        seen: set[str] = set()
        out = []
        for items in responses:
            for item in items:
                url = item.get("url") or ""
                if url in seen:
                    continue
                # Движки иногда трактуют OR нестрого и подмешивают чужие домены —
                # держим только запрошенные (иначе include_domains теряет смысл)
                dom = _domain(url)
                if not any(dom == d or dom.endswith("." + d) for d in include_domains):
                    continue
                seen.add(url)
                out.append(item)
        return out

    async def _fetch_text(self, url: str, max_chars: int) -> str:
        if any(b in _domain(url) for b in FETCH_BLACKLIST):
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    return ""
                content_type = r.headers.get("content-type", "")
                if _is_pdf(url, content_type):
                    pdf_text = await asyncio.to_thread(_extract_pdf_text, r.content, max_chars)
                    log.info(f"PDF fetch: {url} ({len(r.content)}b) → извлечено {len(pdf_text)} символов")
                    return pdf_text
                html = r.text
        except Exception:
            return ""

        # trafilatura вырезает нав/футер/рекламу, оставляет статью + таблицы
        # характеристик — сырой regex-стрип тянул весь текст страницы включая меню
        text = await _extract_text(html)
        if not text:
            text = _html_to_text(html)
        return text[:max_chars]

    async def search_text(
        self,
        query: str,
        max_results: int = 6,
        max_chars: int = 8000,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        items = await self._query(query, max_results, include_domains)
        texts = await asyncio.gather(*[self._fetch_text(i["url"], max_chars) for i in items])
        results = []
        for item, text in zip(items, texts):
            if not text:
                # Qrator/фетч не удался — берём хотя бы сниппет из SERP
                text = item.get("content", "") or ""
            if text:
                results.append(SearchResult(url=item["url"], title=item.get("title", ""), text=text))
        log.info(f"SearXNG search_text: '{query[:50]}' → {len(results)} results")
        return SearchResponse(results=results, request_count=0)

    async def search_urls(
        self,
        query: str,
        max_results: int = 8,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        items = await self._query(query, max_results, include_domains)
        results = [SearchResult(url=i["url"], title=i.get("title", "")) for i in items]
        log.info(f"SearXNG search_urls: '{query[:50]}' → {len(results)} urls")
        return SearchResponse(results=results, request_count=0)

    async def search_images(self, query: str, max_results: int = 20) -> list[tuple[str, str]]:
        """Поиск по image-категории SearXNG (Google/Bing/DDG Images) — прямые
        URL картинок, без резолва og:image. Возвращает [(img_url, page_url)].
        page_url нужен вызывающему коду для брендовых/модельных URL-фильтров."""
        cache_key = _cache.make_key("searxng_images", query)
        cached = await _cache.get_cached(cache_key)
        if cached is not None:
            out = [tuple(pair) for pair in cached]
            log.info(f"SearXNG search_images (кэш): '{query[:50]}' → {len(out)} картинок")
            return out[:max_results]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self._base_url}/search",
                    params={"q": query, "format": "json", "categories": "images"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning(f"SearXNG image query failed '{query[:60]}': {e}")
            return []

        out: list[tuple[str, str]] = []
        for item in data.get("results", []):
            img = item.get("img_src") or ""
            page = item.get("url") or ""
            if not img.startswith("http"):
                continue
            domain = _domain(page)
            if not domain or any(b in domain for b in BLACKLIST):
                continue
            out.append((img, page))

        await _cache.set_cached(cache_key, out)
        log.info(f"SearXNG search_images: '{query[:50]}' → {len(out)} картинок")
        return out[:max_results]


# Синглтон — один экземпляр на весь процесс
searxng_search = SearxngSearch()
