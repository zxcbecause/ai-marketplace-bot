import logging
import asyncio

from exa_py import Exa
from config import settings
from .common import BaseContextSearch, SearchResult, SearchResponse, BLACKLIST

log = logging.getLogger(__name__)


class ExaSearch(BaseContextSearch):
    def __init__(self):
        self._client = Exa(api_key=settings.exa_api_key)

    async def search_text(
        self,
        query: str,
        max_results: int = 6,
        max_chars: int = 8000,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        """Поиск с полным текстом страниц."""
        if not settings.exa_enabled:
            log.info(f"Exa ОТКЛЮЧЕНА (экономия): пропуск search_text '{query[:50]}'")
            return SearchResponse()
        def _call():
            kwargs: dict = {"num_results": max_results, "text": {"max_characters": max_chars}}
            if include_domains:
                kwargs["include_domains"] = include_domains
            else:
                kwargs["exclude_domains"] = BLACKLIST
            return self._client.search_and_contents(query, **kwargs)

        try:
            raw = await asyncio.to_thread(_call)
        except Exception as e:
            log.warning(f"Exa search_text failed '{query[:60]}': {e}")
            return SearchResponse(request_count=1)

        results = [
            SearchResult(url=r.url or "", title=r.title or "", text=r.text or "")
            for r in raw.results
        ]
        log.info(f"Exa search_text: '{query[:50]}' → {len(results)} results")
        return SearchResponse(results=results, request_count=1)

    async def search_urls(
        self,
        query: str,
        max_results: int = 8,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        """Поиск только URL-ов (без текста страниц)."""
        if not settings.exa_enabled:
            log.info(f"Exa ОТКЛЮЧЕНА (экономия): пропуск search_urls '{query[:50]}'")
            return SearchResponse()
        def _call():
            kwargs: dict = {"num_results": max_results}
            if include_domains:
                kwargs["include_domains"] = include_domains
            else:
                kwargs["exclude_domains"] = BLACKLIST
            return self._client.search(query, **kwargs)

        try:
            raw = await asyncio.to_thread(_call)
        except Exception as e:
            log.warning(f"Exa search_urls failed '{query[:60]}': {e}")
            return SearchResponse(request_count=1)

        results = [SearchResult(url=r.url or "", title=r.title or "") for r in raw.results]
        log.info(f"Exa search_urls: '{query[:50]}' → {len(results)} urls")
        return SearchResponse(results=results, request_count=1)


# Синглтон — один экземпляр на весь процесс
exa_search = ExaSearch()
