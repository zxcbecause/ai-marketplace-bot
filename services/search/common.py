import asyncio
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Ограничивает число одновременных headless-браузеров (Playwright) на процесс,
# независимо от того, сколько товаров обрабатывается параллельно вызывающим
# кодом. Добавлено после инцидента 12.07.2026: wb_live_create.py с
# CONCURRENCY=4 после исчерпания баланса Exa молча ушёл на веб-фолбэк,
# и 4 воркера одновременно открыли по браузеру — комп встал колом.
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

BLACKLIST = [
    # Новости и обзоры — не дают точных спеков
    "gagadget.com", "ixbt.com", "3dnews.ru", "ferra.ru", "hi-tech.mail.ru",
    "overclockers.ru", "settings.menu", "androidinsider.ru",
    "trustedreviews.com", "techradar.com", "tomsguide.com", "notebookcheck.net",
    # Форумы и UGC
    "4pda.ru", "4pda.to", "habr.com", "pikabu.ru", "reddit.com",
    # Агрегаторы отзывов
    "otzovik.com", "irecommend.ru", "yell.ru",
    # Видео / соцсети
    "youtube.com", "youtu.be", "vk.com", "t.me", "instagram.com", "tiktok.com",
    # 21.07.2026: китайские маркетплейсы — часто машинный/кривой перевод
    # текста карточки, живой случай на кроссовках Adidas (Duramo RC2):
    # характеристика "Особенности обуви" пришла иероглифами прямо с
    # AliExpress ("超轻, 缓震, 透气"), LLM не перевела и не отфильтровала.
    "aliexpress.com", "aliexpress.ru",
    # Прайс-агрегаторы без контента
    "market.yandex.ru", "price.ru", "sravni.ru",
    # Вотермарки на фото
    "aktek.ru",
    # Наш R2-хостинг — чтобы не брать собственные инфографики как исходное фото
    "pub-e637cbc0b4694c849eb6b20f67d4b28a.r2.dev",
]


@dataclass
class SearchResult:
    url: str
    title: str = ""
    text: str = ""


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    request_count: int = 0

    def as_context(self, model_id: str = "", min_score: float = 0.75) -> str:
        """Собирает результаты в строку контекста для LLM."""
        parts = []
        seen_domains: set[str] = set()

        # Взвешенные токены: числа и коды модели (T606, RTX4090) → вес 2, слова → вес 1
        filter_tokens: list[tuple[str, float]] = []
        if model_id:
            for w in model_id.split():
                # Убираем скобки и знаки препинания — "(2.0)" → "2.0", "(ARGB)" → "argb"
                wl = re.sub(r'[()[\]{},;:!?"\']', '', w).lower().strip('.')
                if len(wl) < 2:
                    continue
                weight = 2.0 if re.search(r'\d', wl) else 1.0
                filter_tokens.append((wl, weight))
        max_score = sum(w for _, w in filter_tokens)

        for r in self.results:
            if len(r.text) < 300:
                continue
            domain = r.url.split("/")[2] if r.url.startswith("http") else r.url
            if domain in seen_domains:
                continue
            # Взвешенный фильтр: числа важнее слов, порог min_score суммарного веса
            if filter_tokens and max_score > 0:
                text_lower = r.text.lower()
                score = sum(w for t, w in filter_tokens if t in text_lower)
                if score / max_score < min_score:
                    continue
            # Домен помечаем занятым ТОЛЬКО когда страница реально взята —
            # иначе мусорная страница каталога сжигала слот домена, и настоящая
            # карточка товара с того же сайта отбрасывалась (site:-поиск часто
            # даёт листинг + карточку с одного домена).
            seen_domains.add(domain)
            parts.append(f"=== Страница: {r.url} ===\n{r.title}\n{r.text}")
        return "\n\n".join(parts)


# Базовые розничные домены со структурированными таблицами характеристик.
# al-style.kz — наш поставщик, всегда первый. shop.kz/forcecom.kz — KZ-ритейл,
# проверены на фетч с этой машины 14.07.2026 (без Qrator-блока).
_SPEC_DOMAINS = [
    "al-style.kz", "shop.kz", "forcecom.kz",
    "dns-shop.ru", "citilink.ru", "mvideo.ru", "eldorado.ru",
    "kaspi.kz", "sulpak.kz", "technodom.kz", "ozon.ru",
]

_PACK_DOMAINS = list(_SPEC_DOMAINS)

# Приоритетные спец-базы для СЛОЖНЫХ товаров — у ритейла по ним таблицы бедные
# или кривые, а у этих сайтов структурированные полные спеки. Ключ — подстрока
# категории (lowercase). Доступность проверена 14.07.2026 (e-katalog, gigabyte,
# msi — заблокированы с этой машины, НЕ добавлять без повторной проверки).
_COMPLEX_SPEC_DOMAINS: dict[str, list[str]] = {
    "ноутбук": ["nanoreview.net", "laptopmedia.com"],
    "моноблок": ["nanoreview.net"],
    "материнск": ["techpowerup.com"],
    "видеокарт": ["techpowerup.com", "nanoreview.net"],
    "процессор": ["techpowerup.com", "nanoreview.net"],
    "ssd": ["techpowerup.com"],
    "накопител": ["techpowerup.com"],
    "оперативн": ["techpowerup.com"],
    "смартфон": ["nanoreview.net"],
    "планшет": ["nanoreview.net"],
    # 21.07.2026: батч клиента — 100% Adidas, поэтому пока жёстко один бренд.
    # Если появятся кроссовки других брендов — переделать на подбор домена
    # по распознанному бренду, а не хардкодить adidas.com для всей категории.
    "кроссов": ["adidas.com"],
}


def _domains_for_category(category: str, base: list[str]) -> list[str]:
    """Спец-домены категории идут ПЕРВЫМИ — им приоритет в per_domain-бюджете
    SearXNG (site:-запрос на каждый домен, см. searxng._query)."""
    cat = (category or "").lower()
    extra = [d for key, doms in _COMPLEX_SPEC_DOMAINS.items() if key in cat for d in doms]
    out: list[str] = []
    for d in extra + base:
        if d not in out:
            out.append(d)
    return out


class BaseContextSearch:
    """Логика построения контекста карточки товара поверх search_text/search_urls.
    Общая для любого провайдера (Exa, SearXNG, гибрид) — конкретный провайдер
    реализует только search_text/search_urls."""

    async def search_text(
        self,
        query: str,
        max_results: int = 6,
        max_chars: int = 8000,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        raise NotImplementedError

    async def search_urls(
        self,
        query: str,
        max_results: int = 8,
        include_domains: list[str] | None = None,
    ) -> SearchResponse:
        raise NotImplementedError

    async def search_specs_context(self, product: str, category: str = "") -> tuple[str, int]:
        """Целевой поиск технических характеристик на розничных сайтах.
        DNS/Citilink/Ozon имеют структурированные таблицы спеков — точнее обзоров.
        category — включает приоритетные спец-базы для сложных товаров
        (ноутбуки, платы, видеокарты...), см. _COMPLEX_SPEC_DOMAINS."""
        domains = _domains_for_category(category, _SPEC_DOMAINS)
        extras = [d for d in domains if d not in _SPEC_DOMAINS]
        # Кавычки вокруг модели — не путаем Artist 12 с Artist 12 Pro и т.п.
        tasks = [self.search_text(
            f'"{product}" характеристики',
            max_results=5,
            max_chars=6000,
            include_domains=domains,
        )]
        if extras:
            # Спец-базы сложных категорий — англоязычные (techpowerup,
            # nanoreview...): русское «характеристики» в site:-запросе к ним
            # почти всегда даёт 0. Дублируем запрос по-английски только по ним.
            tasks.append(self.search_text(
                f'"{product}" specifications',
                max_results=4,
                max_chars=6000,
                include_domains=extras,
            ))
        responses = await asyncio.gather(*tasks)
        resp = SearchResponse(
            results=[r for rr in responses for r in rr.results],
            request_count=sum(rr.request_count for rr in responses),
        )
        context = resp.as_context(model_id=product)
        if not context and resp.results:
            # Строгий фильтр (75%) убил всё — мягкий проход по уже скачанным
            # страницам (как в search_product_context), без новых запросов
            context = resp.as_context(model_id=product, min_score=0.4)
            if context:
                log.info(f"Specs context recovered with relaxed filter (0.4) for: {product}")
        return context, resp.request_count

    async def search_packaging_context(self, product: str, category: str = "") -> tuple[str, int]:
        """Целевой поиск габаритов и веса В УПАКОВКЕ на маркетплейсах.
        Маркетплейсы указывают упакованный вес и размеры в карточке товара."""
        resp = await self.search_text(
            f"{product} вес с упаковкой габариты упаковки",
            max_results=4,
            max_chars=4000,
            include_domains=_domains_for_category(category, _PACK_DOMAINS),
        )
        context = resp.as_context(model_id=product)
        if not context and resp.results:
            context = resp.as_context(model_id=product, min_score=0.4)
            if context:
                log.info(f"Packaging context recovered with relaxed filter (0.4) for: {product}")
        return context, resp.request_count

    async def search_product_context(
        self,
        product: str,
        query_en: str,
        query_other: str = "",
    ) -> tuple[str, int]:
        """
        Несколько параллельных запросов для контекста карточки товара.
        Возвращает (context_str, request_count).
        """
        import asyncio

        tasks = [
            self.search_text(f"{product} характеристики", max_results=6, max_chars=10000),
            self.search_text(query_en, max_results=6, max_chars=8000),
        ]
        if query_other:
            tasks.append(self.search_text(query_other, max_results=4, max_chars=8000))

        responses = await asyncio.gather(*tasks)
        total_requests = sum(r.request_count for r in responses)

        parts = [r.as_context(model_id=product) for r in responses]
        context = "\n\n".join(p for p in parts if p)

        if not context:
            # Строгий фильтр (75%) ничего не оставил — нишевый товар.
            # Пере-фильтруем уже полученные страницы мягче (40%), без новых
            # запросов: лучше живой веб-контекст не по точной модели, чем
            # полное его отсутствие.
            parts = [r.as_context(model_id=product, min_score=0.4) for r in responses]
            context = "\n\n".join(p for p in parts if p)
            if context:
                log.info(f"Product context recovered with relaxed filter (0.4) for: {product}")

        return context, total_requests

    async def search_product_context_lite(self, product: str) -> tuple[str, int]:
        """Экономный добор контекста для WB-first режима: максимум 2 запроса,
        без LLM-генерации запросов. Используется, только когда собственная
        WB-карточка дала слишком мало характеристик."""
        import asyncio

        tasks = [
            self.search_text(f"{product} характеристики", max_results=6, max_chars=8000),
            self.search_text(f"{product} specifications", max_results=6, max_chars=8000),
        ]
        responses = await asyncio.gather(*tasks)
        total_requests = sum(r.request_count for r in responses)
        parts = [r.as_context(model_id=product) for r in responses]
        context = "\n\n".join(p for p in parts if p)
        if not context:
            parts = [r.as_context(model_id=product, min_score=0.4) for r in responses]
            context = "\n\n".join(p for p in parts if p)
        return context, total_requests
