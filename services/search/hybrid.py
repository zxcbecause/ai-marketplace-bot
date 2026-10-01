import logging

from .common import BaseContextSearch, SearchResponse
from .searxng import searxng_search
from .exa import exa_search

log = logging.getLogger(__name__)

# Порог "SearXNG нашла достаточно" — ниже него подключаем Exa в помощь (не замену)
_MIN_RESULTS = 2

# as_context() отбрасывает результаты короче этого — считать "достаточно" по
# сырому len(results) неверно: SearXNG может вернуть 2+ коротких SERP-сниппета
# (фетч не удался), порог формально пройден, а as_context() потом всё равно
# всё отфильтрует и контекст останется пустым без единой попытки Exa.
_MIN_TEXT_LEN = 300


class HybridSearch(BaseContextSearch):
    """SearXNG (self-hosted, бесплатно) — основной источник текстового контекста.
    Exa — платный резерв, дергается только если SearXNG вернула пусто/мало
    (и только если settings.exa_enabled — см. config.py)."""

    async def search_text(
        self,
        query: str,
        max_results: int = 6,
        max_chars: int = 8000,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        resp = await searxng_search.search_text(query, max_results, max_chars, include_domains)
        usable = sum(1 for r in resp.results if len(r.text) >= _MIN_TEXT_LEN)
        if usable >= _MIN_RESULTS:
            return resp
        log.info(f"SearXNG дала {len(resp.results)} рез. — добор через Exa: '{query[:50]}'")
        exa_resp = await exa_search.search_text(query, max_results, max_chars, include_domains)
        merged = resp.results + [r for r in exa_resp.results if r.url not in {x.url for x in resp.results}]
        return SearchResponse(results=merged, request_count=resp.request_count + exa_resp.request_count)

    async def search_urls(
        self,
        query: str,
        max_results: int = 8,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        resp = await searxng_search.search_urls(query, max_results, include_domains)
        if len(resp.results) >= _MIN_RESULTS:
            return resp
        log.info(f"SearXNG дала {len(resp.results)} URL — добор через Exa: '{query[:50]}'")
        exa_resp = await exa_search.search_urls(query, max_results, include_domains)
        merged = resp.results + [r for r in exa_resp.results if r.url not in {x.url for x in resp.results}]
        return SearchResponse(results=merged, request_count=resp.request_count + exa_resp.request_count)


# Синглтон — один экземпляр на весь процесс
product_search = HybridSearch()
