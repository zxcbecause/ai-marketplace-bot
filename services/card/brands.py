"""Справочник известных брендов — кросс-проверка результата LLM в
parse_product_info (normalize.py). Раньше бренд определялся ЦЕЛИКОМ на
угадывании LLM без какой-либо сверки: LLM могла оставить бренд пустым, хотя
он явно есть в названии, или (реже) выдать бренд, которого в названии вовсе
нет. Здесь — не замена LLM (она всё ещё разбирает BRAND/MODEL/COLOR), а
подстраховка: если бренд из справочника найден в сыром названии, а ответ LLM
с ним расходится (пуст или отсутствует в тексте) — берём бренд из справочника.

Платформенные теги (AMD/Intel/NVIDIA/Radeon/GeForce/Ryzen/Core) сюда
намеренно НЕ включены — для них уже есть отдельная, category-aware проверка
_PLATFORM_TAGS в normalize.py (RAM/SSD/кулер/БП с «for AMD Ryzen» в
названии — это платформа, а не бренд модуля), и обе проверки не должны
конфликтовать.

Список собран по категориям товаров, которые реально продаются через этот
бот (см. category.py CATEGORY_MAP) — не претендует на полноту рынка,
пополняется по мере обнаружения пропусков."""
import re

KNOWN_BRANDS: list[str] = [
    # Собственный бренд
    "магазина",
    # Сеть: роутеры, точки доступа, адаптеры, модемы
    "TP-Link", "Keenetic", "Xiaomi", "ASUS", "D-Link", "Zyxel", "Mercusys",
    "Tenda", "Netis", "Netgear", "Huawei", "MikroTik", "Ubiquiti",
    # Принтеры и картриджи
    "HP", "Canon", "Epson", "Brother", "Xerox", "Pantum", "Kyocera", "Cactus",
    "Ricoh",
    # Периферия: мыши, клавиатуры, коврики, гарнитуры
    "Logitech", "Razer", "SteelSeries", "HyperX", "A4Tech", "Defender",
    "Bloody", "Zowie", "BenQ", "Lamzu", "Pulsar", "Varmilo", "Ducky",
    "Redragon", "Genius", "Oklick", "CBR", "Gembird",
    # Мониторы
    "Samsung", "LG", "AOC", "Acer", "ViewSonic", "Philips", "Iiyama", "MSI",
    # Память/накопители (доп. к списку в normalize.py — не конфликтует)
    "Kingston", "Corsair", "Crucial", "ADATA", "G.Skill", "Patriot",
    "TeamGroup", "Hynix", "Apacer", "GeIL", "Netac", "Kingmax",
    "Silicon Power", "WD", "Western Digital", "Seagate", "Transcend",
    # Видеокарты (бренды-партнёры, не GPU-платформа)
    "Gigabyte", "Palit", "Zotac", "Sapphire", "PowerColor", "Colorful",
    "Inno3D", "Gainward",
    # Ноутбуки/моноблоки/компьютеры
    "Lenovo", "Dell", "Honor", "Irbis", "Tecno", "Infinix", "Apple",
    # Смарт-часы/браслеты
    "Amazfit", "Haylou",
    # Роботы-пылесосы
    "Dreame", "Ecovacs", "Roborock", "Karcher", "Kärcher", "Polaris",
    # Наушники/акустика
    "JBL", "Sony", "Marshall", "Anker", "Baseus", "Hoco", "Edifier",
    "Sennheiser", "Jabra", "QCY", "Soundcore", "F&D",
    # Игровые кресла
    "DXRacer", "Cougar", "AeroCool", "ThunderX3",
    # Обувь
    "Nike", "Adidas", "Puma", "New Balance", "Reebok", "Anta", "Skechers",
    "Under Armour", "Asics",
    # Зарядки/кабели/powerbank
    "Ugreen", "Deppa", "Vention", "Remax", "Borofone", "Usams",
    # IP-камеры/видеонаблюдение
    "Hikvision", "Dahua", "Ezviz", "Imou",
    # Утюги и бытовая техника (по категориям товаров бота — см. session 55+)
    "Gorenje", "Bosch", "Tefal", "Braun", "Scarlett", "Polaris",
    # Процессоры/материнские платы (бренды-партнёры, не платформа)
    "Biostar", "ASRock",
]

# 17.07.2026: раньше сравнение шло по строке с ПОЛНОСТЬЮ удалёнными
# пробелами/дефисами — это склеивало соседние слова названия друг с другом
# и давало ложные срабатывания. Конкретный случай: "Роутер TP-Link Archer
# AX55" после удаления разделителей превращалось в "...tplinkarcherax55...",
# а внутри этой склейки случайно нашлась подстрока "karcher" (конец "Link"
# + начало "Archer") — бренд определялся как "Karcher" (бытовая техника)
# вместо настоящего "TP-Link". Теперь границы слов сохраняются: паттерн
# бренда сам допускает гибкие внутренние разделители («TP-Link» совпадёт
# с «TP Link»/«TPLink»/«TP-Link»), но не может перетечь через границу с
# соседним словом текста — она защищена lookaround на не-буквенно-цифровой
# символ по краям.
_BRAND_PATTERNS: list[tuple[re.Pattern, str]] = []
for _b in KNOWN_BRANDS:
    _parts = re.split(r"[\s\-.]+", _b)
    _pattern = r"[\s\-.]*".join(re.escape(p) for p in _parts)
    _regex = re.compile(
        r"(?<![A-Za-zА-Яа-я0-9])" + _pattern + r"(?![A-Za-zА-Яа-я0-9])",
        re.IGNORECASE,
    )
    _BRAND_PATTERNS.append((_regex, _b))

# Длинные/многословные бренды проверяем раньше коротких — на случай, если
# короткий бренд оказался бы подстрокой более специфичного совпадения.
_BRAND_PATTERNS.sort(key=lambda pair: len(pair[1]), reverse=True)

# 19.07.2026: некоторые бренды — обычные английские слова/материалы, которые
# встречаются в названии совсем в другом смысле (найдено на живом батче:
# "SP7201: ... Sapphire Glass" — сапфировое СТЕКЛО сканера штрихкода,
# граница слова совпала с брендом видеокарт "Sapphire", хотя товар вообще не
# видеокарта). Границы слов тут не спасают — само слово настоящее, просто не
# в роли бренда. Для таких брендов держим доп. регекс обязательного контекста
# (типичные слова/модели ИХ настоящей категории) — совпадение по слову без
# этого контекста не считается. Тот же принцип, что _PLATFORM_TAGS/
# _GPU_CPU_PREFIX_RE в normalize.py, только на уровне конкретного бренда, а
# не общей платформенной группы. Пополнять по мере обнаружения новых коллизий.
_AMBIGUOUS_BRAND_CONTEXT: dict[str, re.Pattern] = {
    "Sapphire": re.compile(r"видеокарта|graphics\s*card|\bRX\s?\d{3,4}\b|\bRadeon\b|\bGPU\b", re.IGNORECASE),
}


def find_known_brand(raw_name: str) -> str | None:
    """Ищет известный бренд в сыром названии товара, не задевая границы
    соседних слов (см. комментарий выше про баг TP-Link/Archer/Karcher).
    Возвращает каноническое имя из KNOWN_BRANDS или None."""
    for regex, canonical in _BRAND_PATTERNS:
        if regex.search(raw_name):
            ctx = _AMBIGUOUS_BRAND_CONTEXT.get(canonical)
            if ctx and not ctx.search(raw_name):
                continue
            return canonical
    return None
