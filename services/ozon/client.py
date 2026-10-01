"""
Тонкий клиент Ozon Seller API — категории/атрибуты/импорт товаров.

Имена атрибутов, которые отдаёт /v1/description-category/attribute,
совпадают буква-в-букву с заголовками колонок официального Ozon-шаблона
Excel (см. utils/ozon_filler.py) — поэтому характеристики, собранные
для Excel через OzonExcelFiller.get_chars_prompt(), можно напрямую
сопоставить с ID атрибутов и отправить через API, без отдельного промпта.
"""
import os
import asyncio
import logging
import re

import requests

from config import settings

log = logging.getLogger(__name__)

BASE = "https://api-seller.ozon.ru"


def _headers(api_key: str | None = None) -> dict:
    return {
        "Client-Id": settings.ozon_client_id,
        "Api-Key": api_key or settings.ozon_api_key,
        "Content-Type": "application/json",
    }


def _post(path: str, body: dict, api_key: str | None = None) -> dict:
    r = requests.post(f"{BASE}{path}", headers=_headers(api_key), json=body, timeout=30)
    if not r.ok:
        log.error(f"Ozon API {path} -> {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


async def get_category_attributes(description_category_id: int, type_id: int) -> list[dict]:
    """Список атрибутов категории+типа: id, name, dictionary_id, is_collection, is_required."""
    def _call():
        return _post("/v1/description-category/attribute", {
            "description_category_id": description_category_id,
            "type_id": type_id,
            "language": "RU",
        })
    data = await asyncio.to_thread(_call)
    return data.get("result", [])


async def search_attribute_value(
    attribute_id: int, description_category_id: int, type_id: int,
    value: str, limit: int = 5,
) -> int | None:
    """Ищет dictionary_value_id для текстового значения в справочнике атрибута.
    Возвращает точное совпадение если есть, иначе первый результат, иначе None."""
    def _call():
        return _post("/v1/description-category/attribute/values/search", {
            "attribute_id": attribute_id,
            "description_category_id": description_category_id,
            "type_id": type_id,
            "language": "RU",
            "value": value,
            "limit": limit,
        })
    try:
        data = await asyncio.to_thread(_call)
    except Exception as e:
        log.warning(f"attribute/values/search failed (attr={attribute_id}, value={value!r}): {e}")
        return None
    results = data.get("result", [])
    if not results:
        return None
    value_low = value.strip().lower()
    for r in results:
        if r["value"].strip().lower() == value_low:
            return r["id"]
    return results[0]["id"]


async def build_attributes_payload(
    attrs_meta: list[dict], values: dict[str, str],
    description_category_id: int, type_id: int,
) -> list[dict]:
    """values — {имя атрибута (как в Excel-шаблоне/API): текстовое значение}.
    Для словарных атрибутов резолвит dictionary_value_id через поиск,
    для текстовых — отправляет значение как есть. Для is_collection-атрибутов
    значение режется по ',' или ';' — LLM-промпт (get_chars_prompt) просит
    "через запятую", а OzonExcelFiller._normalize_value тоже принимает оба
    разделителя; раньше резалось только по ';' — значения вида "SBC, AAC,
    L2HC" уходили целиком и не находились в справочнике."""
    name_to_attr = {a["name"]: a for a in attrs_meta}
    # Нечёткий фолбэк: LLM-вариации имени («страна изготовитель» без дефиса,
    # другой регистр) не должны терять атрибут при живом импорте
    def _norm(n: str) -> str:
        return re.sub(r"[\s\-–—*]+", "", n).lower().replace("ё", "е")
    norm_to_attr = {_norm(a["name"]): a for a in attrs_meta}
    plan = []
    for name, value in values.items():
        attr = name_to_attr.get(name) or norm_to_attr.get(_norm(name))
        if not attr or not value:
            continue
        plan.append((attr["id"], attr.get("is_collection", False), str(value), attr.get("dictionary_id", 0)))

    async def resolve(attr_id, is_collection, value, dict_id):
        if dict_id == 0:
            return attr_id, [{"value": value}]
        parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()] if is_collection else [value]
        values_out = []
        for p in parts:
            vid = await search_attribute_value(attr_id, description_category_id, type_id, p)
            if vid:
                values_out.append({"dictionary_value_id": vid})
            else:
                log.warning(f"Ozon attribute {attr_id}: значение не найдено в справочнике: {p!r}")
        return attr_id, values_out

    resolved = await asyncio.gather(*[resolve(*p) for p in plan])
    return [{"id": attr_id, "values": vals} for attr_id, vals in resolved if vals]


async def import_product(item: dict) -> int:
    """Отправляет товар на создание/обновление, возвращает task_id."""
    def _call():
        return _post("/v3/product/import", {"items": [item]})
    data = await asyncio.to_thread(_call)
    return data["result"]["task_id"]


async def update_prices(items: list[dict]) -> dict:
    """Обновляет цену уже существующих товаров по offer_id (без полного
    реимпорта). items: [{"offer_id": "...", "price": "12345"}]."""
    body = {
        "prices": [
            {
                "offer_id": it["offer_id"],
                "price": str(it["price"]),
                "old_price": "0",
                "currency_code": "KZT",
            }
            for it in items
        ]
    }
    def _call():
        return _post("/v1/product/import/prices", body)
    return await asyncio.to_thread(_call)


async def update_product_attributes(offer_id: str, attributes: list[dict]) -> int:
    """Частичное обновление атрибутов товара без полного реимпорта.
    attributes: [{"id": attr_id, "complex_id": 0, "values": [{"value": "..."}]}]
    Использует второй API-ключ если задан (ozon_api_key_2) — для параллельных операций.
    Возвращает task_id."""
    key = settings.ozon_api_key_2 or None
    def _call():
        return _post("/v1/product/attributes/update", {"items": [{"offer_id": offer_id, "attributes": attributes}]}, api_key=key)
    data = await asyncio.to_thread(_call)
    return data.get("task_id", 0)


async def get_import_info(task_id: int) -> dict:
    """Статус обработки задачи импорта: items[].status / errors."""
    def _call():
        return _post("/v1/product/import/info", {"task_id": task_id})
    data = await asyncio.to_thread(_call)
    return data["result"]


async def get_products_info(offer_ids: list[str]) -> dict[str, dict]:
    """Карточки по списку offer_id: errors, warnings, images, price и т.д.
    Возвращает {offer_id: item}. Поля errors и warnings — списки замечаний
    от модерации (errors блокируют публикацию, warnings — доработки)."""
    def _call():
        return _post("/v3/product/info/list", {"offer_id": offer_ids})
    data = await asyncio.to_thread(_call)
    items = data.get("items", [])
    return {item["offer_id"]: item for item in items}


async def get_products_attributes(offer_ids: list[str]) -> dict[str, dict]:
    """Полные данные карточки (атрибуты, габариты, баркод, фото) по offer_id —
    то, что нужно для полного переимпорта через import_product."""
    def _call():
        return _post("/v4/product/info/attributes", {
            "filter": {"offer_id": offer_ids}, "limit": len(offer_ids),
        })
    data = await asyncio.to_thread(_call)
    items = data.get("result", [])
    return {item["offer_id"]: item for item in items}


async def get_all_products_list(visibility: str = "", with_stock: bool = False) -> list[str]:
    """Все offer_id из кабинета Ozon (пагинация через last_id).
    visibility: "" = все, "VISIBLE" = опубликованные, "ARCHIVATED" = архив и т.д.
    with_stock=True: только товары с остатком (has_fbo_stocks OR has_fbs_stocks) — т.е. «в продаже»."""
    offer_ids: list[str] = []
    last_id = ""
    flt = {"visibility": visibility} if visibility else {}
    while True:
        def _call(lid=last_id):
            return _post("/v3/product/list", {"limit": 100, "last_id": lid, "filter": flt})
        data = await asyncio.to_thread(_call)
        result = data.get("result", {})
        items = result.get("items", [])
        for it in items:
            if not it.get("offer_id"):
                continue
            if with_stock and not (it.get("has_fbo_stocks") or it.get("has_fbs_stocks")):
                continue
            offer_ids.append(it["offer_id"])
        last_id = result.get("last_id", "")
        if not items or not last_id:
            break
    return offer_ids


async def update_pictures(
    product_id: int, images: list[str],
    images360: list[str] | None = None, color_image: str = "",
) -> dict:
    """Полностью переопределяет список картинок товара (нужен product_id, не offer_id)."""
    def _call():
        return _post("/v1/product/pictures/import", {
            "product_id": product_id,
            "images": images,
            "images360": images360 or [],
            "color_image": color_image,
        })
    data = await asyncio.to_thread(_call)
    return data.get("result", {})


async def unarchive_products(product_ids: list[int]) -> dict:
    """Снимает товары с архива Ozon (нужны product_id, не offer_id —
    см. get_products_info, поле "product_id"). Поле запроса — item_ids,
    не product_ids (несмотря на название эндпоинта/остальных методов)."""
    def _call():
        return _post("/v1/product/unarchive", {"item_ids": product_ids})
    return await asyncio.to_thread(_call)


WAREHOUSE_ID = int(os.getenv("OZON_WAREHOUSE_ID", "0"))  # ID склада продавца на Ozon (из .env)


async def update_stocks(offer_id: str, stock: int) -> dict:
    """Выставляет остаток товара (offer_id) напрямую, без полного реимпорта."""
    def _call():
        return _post("/v2/products/stocks", {
            "stocks": [{"offer_id": offer_id, "stock": stock, "warehouse_id": WAREHOUSE_ID}],
        })
    return await asyncio.to_thread(_call)
