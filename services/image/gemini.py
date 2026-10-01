import asyncio
import base64
import io
import logging
import re
from itertools import cycle

from PIL import Image
from config import settings

log = logging.getLogger(__name__)

_INFOGRAPHIC_STYLES = [
    "clean white studio background, soft directional light, subtle shadow under product",
    "very light gray gradient (top-left bright, bottom-right slightly darker), minimal feel",
    "soft pastel blue-to-white gradient, airy and fresh",
    "warm cream/beige background with subtle light bloom in center",
    "light lavender-to-white gradient, gentle and modern",
    "pure white with soft mint accent glow behind product",
    "light warm gradient: pale gold top fading to white bottom",
    "frosted glass look: very pale blue-gray, soft texture, light diffuse glow",
]

_RICHCONTENT_STYLES = [
    "vibrant coral-to-peach diagonal gradient with soft glowing light orb in upper-left corner",
    "rich sky-blue to mint-green gradient with subtle abstract geometric shapes",
    "warm sunset gradient: golden yellow to soft pink, with diffused light bloom",
    "saturated lavender-to-magenta gradient with shimmer particles and soft glow",
    "bright turquoise-to-aqua gradient with abstract waves and light reflections",
    "deep cream to amber gradient with golden bokeh dots and warm ambient light",
    "fresh pistachio-to-emerald gradient with delicate light streaks and minimal geometry",
    "playful coral pink to soft orange gradient with rounded shapes and modern aesthetic",
    "premium indigo-to-violet gradient with subtle stars and elegant glow",
    "bold marigold-to-rose gradient with abstract circular accents",
]

_infographic_styles = cycle(_INFOGRAPHIC_STYLES)
_richcontent_styles  = cycle(_RICHCONTENT_STYLES)


def _detect_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


async def _call_gemini(contents: list, retries: int = 3) -> bytes | None:
    from google import genai as gai
    from google.genai import types as gai_types

    client = gai.Client(api_key=settings.gemini_api_key)

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=contents,
            config=gai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )

    for attempt in range(retries):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=150.0)
            candidates = response.candidates or []
            if not candidates:
                log.warning(f"Gemini: empty candidates (attempt {attempt + 1}/{retries})")
                if attempt < retries - 1:
                    await asyncio.sleep(3)
                    continue
                return None
            content = candidates[0].content
            if not content or not content.parts:
                log.warning(f"Gemini: empty content/parts — safety filter? (attempt {attempt + 1}/{retries})")
                if attempt < retries - 1:
                    await asyncio.sleep(3)
                    continue
                return None
            for part in content.parts:
                if part.inline_data is not None:
                    raw = part.inline_data.data
                    return bytes(raw) if isinstance(raw, (bytes, bytearray)) else base64.b64decode(raw)
            log.warning(f"Gemini: no inline_data in response (attempt {attempt + 1}/{retries})")
            if attempt < retries - 1:
                await asyncio.sleep(3)
                continue
            return None
        except asyncio.TimeoutError:
            if attempt < retries - 1:
                log.warning(f"Gemini timeout attempt {attempt + 1}/{retries}")
                continue
            log.error("Gemini: all retries timed out")
            return None
        except Exception as e:
            err = str(e)
            if ("429" in err or "503" in err) and attempt < retries - 1:
                await asyncio.sleep(15 * (attempt + 1))
                continue
            log.error(f"Gemini error: {e}")
            return None
    return None


def _vision_rules_prompt(product: str, color: str = "", allow_dock: bool = False) -> str:
    """Текст правил валидации фото товара (используется одиночной и batch-проверкой)."""
    color_check = (
        f"— цвет товара ЯВНО не соответствует требуемому «{color}» "
        f"(например требуемый чёрный, а на фото белый/синий/зелёный)\n"
    ) if color else ""
    return (
        f"На фото предположительно товар «{product}»"
        f"{' цвета ' + color if color else ''}. "
        f"Можно ли использовать это фото в карточке маркетплейса?\n\n"
        f"Ответь NO если выполняется ХОТЯ БЫ ОДНО из условий:\n"
        f"1. Основной товар НЕ ВИДЕН или плохо виден:\n"
        f"   а) на фото только аксессуары/комплектующие без самого устройства: "
        f"перо/насадки без планшета, чехол/кабель/зарядка без телефона, "
        f"объектив без камеры, мышь/сумка без ноутбука, "
        f"кабели питания/AUX/USB без колонки или монитора, "
        f"пульт/провода/крепление без основного устройства — "
        f"если в кадре только шнуры, провода, разъёмы или мелкие детали без главного товара — это NO\n"
        f"   б) TWS-наушники/вкладыши: зарядный кейс занимает основную часть кадра, "
        f"а сами наушники-вкладыши не видны РЯДОМ С КЕЙСОМ снаружи — "
        f"неважно открытый кейс или закрытый, торчат ли наушники из него или нет: "
        f"если наушники не лежат явно ВНЕ кейса рядом с ним — это NO. "
        f"ИСКЛЮЧЕНИЕ: наушники-клипсы открытого типа (Huawei FreeClip и аналоги) "
        f"крепятся к кейсу через собственный штырь/клипсу и штатно показываются "
        f"висящими/пристёгнутыми НАД открытым кейсом, а не лежащими рядом — "
        f"это нормальное штатное фото для такого форм-фактора, YES.\n"
        f"2. Два и более РАЗНЫХ устройств в кадре: lineup, коллаж, "
        f"сравнение цветов или моделей рядом. "
        f"ИСКЛЮЧЕНИЕ: если сам товар — комплект или пара (стереопара колонок 2.0/2.1, "
        f"набор клавиатура+мышь, комплект из двух модулей памяти, пара TWS-вкладышей), "
        f"то несколько ОДИНАКОВЫХ предметов одного комплекта в кадре — это НОРМАЛЬНО, "
        f"не нарушение. NO только если это разные модели или разные цветовые "
        f"варианты для сравнения.\n"
        f"3. На фото присутствует РОЗНИЧНАЯ УПАКОВКА/КОРОБКА товара — коробка с "
        f"логотипом бренда, штрихкодом, окном для обзора устройства или печатными "
        f"изображениями/характеристиками на ней. Это NO в ЛЮБОМ случае: даже если "
        f"само устройство при этом видно — через прозрачное окно коробки, "
        f"напечатано на коробке, или лежит рядом с коробкой. Нужно фото товара "
        f"БЕЗ упаковки.\n"
        f"4. В кадре есть текстовые наложения НЕ на экране устройства — это NO в любом случае:\n"
        f"   а) рекламные надписи, слоганы, баннеры («WIRELESS MECHANICAL KEYBOARD», «TRUE WIRELESS»),\n"
        f"   б) название/артикул модели крупным шрифтом поверх фото («S75 PRO», «Galaxy S24»),\n"
        f"   в) ТАБЛИЦА или СТРОКА ТЕХНИЧЕСКИХ ХАРАКТЕРИСТИК в любом месте кадра (не на экране): "
        f"объём ОЗУ (например «32 ГБ DDR5», «16GB RAM»), процессор («Core i9», «Ryzen 7»), "
        f"видеокарта («RTX 5070», «RX 7900»), объём накопителя («1024 ГБ», «512 SSD»), "
        f"частота («120Hz», «300 Гц»), ёмкость батареи («6500mAh»). "
        f"Такой текст внизу фото, под товаром, сбоку — тоже NO, не только поверх корпуса.\n"
        f"   г) логотипы спонсоров или партнёров, добавленные поверх снимка магазином/продавцом "
        f"(не производителем) — например водяной знак стороннего сайта, рекламная плашка ритейлера.\n"
        f"   Исключения (это YES, не считать нарушением):\n"
        f"   — текст/цифры, напечатанные на корпусе самого товара (шильдик, гравировка, "
        f"фирменные надписи прямо на устройстве) — часть дизайна товара\n"
        f"   — фирменные бейджи технологий-партнёров производителя на официальных фото "
        f"(«Intel Inside», «AMD Ryzen», «NVIDIA GeForce», «Windows 11», Dolby, Wi-Fi CERTIFIED "
        f"и т.п.) — стандартный элемент заводских промо-фото ноутбуков/ПК/мониторов, "
        f"не является посторонним наложением\n"
        f"   НО: если в названии товара указан конкретный объём/ёмкость/модель-код "
        f"(например «120GB», «512GB», «1TB», артикул с цифрами), а на этикетке/корпусе "
        f"товара ЯВНО читается ДРУГОЕ число объёма — это NO, даже если это надпись "
        f"прямо на устройстве (значит это фото другого SKU той же линейки).\n"
        f"   ТО ЖЕ САМОЕ для буквенного суффикса модели (не только цифр): «5600X» и "
        f"«5600XT», «14400» и «14400F», «i5» и «i5K», «RTX 4070» и «RTX 4070 Ti» — "
        f"РАЗНЫЕ модели, не опечатка и не одно и то же изделие, даже при почти "
        f"идентичном дизайне коробки/чипа у всей линейки. Если в названии товара "
        f"указан код с суффиксом (X/XT/G/F/K/Ti/Pro/Plus/Super и т.п.), а на фото "
        f"чётко читается код БЕЗ этого суффикса или С ДРУГИМ суффиксом — это NO. "
        f"Если текст модели на фото нечитаем/слишком мелкий — не отклоняй по этой "
        f"причине (недостаточно данных ≠ несовпадение), полагайся на остальные правила.\n"
        f"5. Фото явно другого товара — полностью другая категория предмета: "
        f"вместо наушников на фото коврик/мышь/клавиатура/монитор, "
        f"вместо смартфона — планшет или ноутбук, вместо ноутбука — монитор; "
        f"или явно другая модель того же бренда с иным дизайном\n"
        f"6. В РЕАЛЬНОЙ СЦЕНЕ (не на экране устройства) видны части тела человека: "
        f"руки, ладони, пальцы, лицо, тело — человек физически держит товар, "
        f"надевает наушники, прикладывает телефон к уху и т.п. "
        f"НЕ считается нарушением: люди, лица, руки — если это КОНТЕНТ, "
        f"показанный НА ЭКРАНЕ устройства (фото видеозвонка, скриншот, обои, "
        f"демо-изображение) — экран может показывать что угодно, это не реальная сцена.\n"
        f"7. Товар показан «раскладкой» или «вытянутым проводом»:\n"
        f"   а) компоненты разложены отдельными кучками по кадру (россыпь запчастей)\n"
        f"   б) проводные наушники/гарнитура сфотографированы так, что кабель раскинут "
        f"на всю ширину или высоту кадра, соединяя мелкие вкладыши с разъёмом — "
        f"сами наушники занимают малую часть кадра, а провод доминирует\n"
        f"8. Фото уже является готовой карточкой/инфографикой: товар лежит внутри "
        f"видимого прямоугольного или скруглённого блока-подложки (белого, цветного или "
        f"градиентного), который явно является частью дизайна карточки, а не фоном студии — "
        f"такой блок создаёт белый прямоугольник вокруг товара после вырезки фона\n"
        f"9. Товар обрезан по краю кадра — часть устройства выходит за границу фото "
        f"и не видна: срезан угол ноутбука, не виден конец наушника, телефон упирается "
        f"в край кадра, корпус уходит за рамку. Товар должен помещаться целиком.\n"
        f"10. Накладные/полноразмерные наушники или гарнитура сфотографированы плохо:\n"
        f"   а) ПЛОСКИЙ СИЛУЭТ СБОКУ — наушники сняты строго в профиль БЕЗ деталей: "
        f"видна только плоская форма чашки и дужка оголовья, силуэт похож на плоскую "
        f"закорючку, не видно ни логотипа, ни RGB-подсветки, ни текстуры/материала чашки. "
        f"Если же на видимой чашке различимы логотип, RGB-полоса, текстура поролона/сетки "
        f"или другие детали дизайна (диагональный ракурс 3/4, даже если вторая чашка "
        f"скрыта или видна только с ребра) — это YES, такой ракурс нормален для карточки.\n"
        f"   б) СЛОЖЕНЫ/СВЁРНУТЫ — наушники в сложенном положении: чашки прижаты "
        f"друг к другу или к оголовью, конструкция компактно сложена — товар выглядит "
        f"как плоский свёрток, а не раскрытые наушники готовые к использованию.\n"
        f"   в) НАКЛОН К КАМЕРЕ — наушники сильно наклонены: внутренняя поверхность "
        f"амбушюр (подушки/поролон) смотрит прямо в объектив, внешняя сторона чашек "
        f"(логотип, корпус) при этом смотрит вниз или не видна. Наушники как бы "
        f"«падают» на камеру.\n"
        f"   г) ТОЛЬКО ОГОЛОВЬЕ — чашки не видны или скрыты.\n"
        f"   Правило: хорошее фото — на видимой чашке различимы детали дизайна "
        f"(логотип/подсветка/материал), наушники раскрыты и готовы к использованию. "
        f"Всё остальное — NO.\n"
        + (
        f"11. Компьютерная мышь сфотографирована вместе с зарядным хабом/доком/станцией "
        f"(отдельная подставка с кабелем, на которую мышь устанавливается для зарядки) — "
        f"нужна ЧИСТАЯ мышь без хаба в кадре. Если на фото мышь стоит на/рядом "
        f"с дополнительной подставкой-боксом с проводом — это NO.\n"
        if not allow_dock else ""
        )
        +
        f"12. Компьютерная мышь сфотографирована СНИЗУ — кадр снят от днища: видны "
        f"сенсор/датчик (тёмный глазок), ножки-скользители (feet/skates), кнопки или "
        f"переключатели на нижней панели, а верхняя часть корпуса (куда кладётся ладонь) "
        f"не видна или видна лишь узкой полосой сбоку. Такой ракурс годится для карточки "
        f"(информативен), но НЕ для лицевой инфографики/рич-контента — нужен вид СВЕРХУ "
        f"или СБОКУ с видимой верхней поверхностью корпуса. Если днище занимает основную "
        f"часть кадра — это NO.\n"
        f"13. Сетевое оборудование (роутер, точка доступа, антенна, коммутатор) "
        f"сфотографировано со стороны крепления — в кадре доминирует монтажный "
        f"кронштейн/пластина/крепёж для стены или мачты, занимая значительную часть "
        f"кадра. NO в этом случае, даже если сам корпус устройства виден.\n"
        f"14. Клавиатура показана так, что клавиши ПОЛНОСТЬЮ не видны: "
        f"чистый вид на боковой торец (только грань корпуса без единой клавиши), "
        f"вид строго снизу (нижняя панель/ножки), вид строго сзади (только разъёмы). "
        f"Если хотя бы несколько клавиш различимы на фото — это YES.\n"
        f"15. Обувь (кроссовки, кеды, ботинки, сапоги, туфли) сфотографирована "
        f"так, что подошва занимает основную часть кадра: снимок снизу (подошва "
        f"смотрит в объектив), вид строго сбоку с доминированием рельефа подошвы, "
        f"или пара перевёрнута носком вниз. Нужен вид СПЕРЕДИ-СБОКУ (3/4) или "
        f"СБОКУ с видимым верхом обуви (носок, язычок, шнуровка). Если подошва "
        f"занимает более половины площади кадра — это NO.\n"
        f"16. Монитор сфотографирован вместе со СТОРОННИМ кронштейном/креплением, "
        f"которое занимает заметную часть кадра: настольный кронштейн-манипулятор "
        f"(газлифт, струбцина к столешнице), настенное крепление, стойка — "
        f"особенно если монитор снят сбоку/сзади ради демонстрации крепления "
        f"или рука-манипулятор тянется через кадр. Нужен монитор САМ ПО СЕБЕ — "
        f"на своей штатной подставке-ножке (это нормально, штатная подставка "
        f"частью товара) или без подставки. Если в кадре виден кронштейн-"
        f"манипулятор/струбцина/настенный крепёж — это NO.\n"
        f"17. ЭКРАН монитора не виден: монитор снят СЗАДИ (видна задняя панель "
        f"корпуса, VESA-крепление, наклейки), СНИЗУ (панель разъёмов "
        f"HDMI/DP/USB, днище) или строго с ТОРЦА (виден только тонкий профиль "
        f"без экранной поверхности). Для карточки монитора экран должен быть "
        f"виден — фронтально или под углом 3/4. Задняя панель, разъёмы, "
        f"крепёжная площадка в качестве главного содержимого кадра — это NO.\n"
        f"{color_check}"
        f"\nВо всех остальных случаях отвечай YES. Примеры того что точно YES:\n"
        f"— любое изображение на экране устройства (арт, люди, игры, ОС, обои, кино)\n"
        f"— аксессуар (стилус, перо) рядом с устройством, если само устройство видно\n"
        f"— любой ракурс, угол, тень, блик\n"
        f"— мелкий водяной знак магазина в углу фото\n"
        f"— цветной, тёмный, градиентный или фактурный фон\n\n"
    )


async def validate_product_image(img_bytes: bytes, product: str,
                                   color: str = "", allow_dock: bool = False,
                                   check_angle: bool = False) -> bool | None | tuple[bool | None, bool]:
    """
    Спрашивает Gemini-2.5-flash (text out, image in): это одиночное фото
    указанного товара, без коробки/упаковки/аксессуаров/коллажа?
    Если задан color — проверяет соответствие цвета.
    allow_dock=True — отключает правило 11 (мышь+зарядный док = NO),
    используется как запасной проход, если без этого правила не нашлось ни одного фото.
    check_angle=True — дополнительно (без доп. запроса) спрашивает ракурс фото:
    объёмный 3/4 или плоский фас/профиль. Возвращает (verdict, is_3q_angle).
    Возвращает True/False (или (bool, bool) при check_angle=True).
    При ошибке (Vision недоступен) — None / (None, False): вердикт неизвестен,
    не блокируем поток, но и не считаем кандидата подтверждённым.

    Стоимость: ~$0.0001-0.0003 за вызов.
    """
    from google import genai as gai
    from google.genai import types as gai_types

    client = gai.Client(api_key=settings.gemini_api_key)
    mime = _detect_mime(img_bytes)
    prompt = _vision_rules_prompt(product, color, allow_dock) + (
            (
                f"Ответь ДВУМЯ строками.\n"
                f"Первая строка: YES или NO (по правилам выше).\n"
                f"Если YES — вторая строка: ракурс фото:\n"
                f"ANGLE_3Q — товар снят под углом 3/4 (объёмно): видно несколько "
                f"граней одновременно — например верх+перед+бок или перед+бок, "
                f"есть ощущение глубины и формы.\n"
                f"ANGLE_FLAT — товар снят строго прямо: фронтально (плоско, видна "
                f"только передняя панель) или строго в профиль/сбоку (видна только "
                f"одна грань, без объёма).\n"
                f"Если NO — вторая строка не нужна."
            ) if check_angle else
            f"Ответь СТРОГО одним словом: YES или NO."
        )
    contents = [
        gai_types.Part.from_bytes(data=img_bytes, mime_type=mime),
        prompt,
    ]

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=gai_types.GenerateContentConfig(
                thinking_config=gai_types.ThinkingConfig(thinking_budget=0)
            ),
        )

    # 2 ретрая для временных 503/429
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=30.0)
            text = (response.text or "").strip().upper()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            verdict = bool(lines) and lines[0].startswith("YES")
            log.info(f"Gemini Vision validate: {text[:30]!r} → {'OK' if verdict else 'REJECT'}")
            if check_angle:
                is_3q = verdict and len(lines) > 1 and "3Q" in lines[1]
                return verdict, is_3q
            return verdict
        except Exception as e:
            err = str(e)
            retriable = "503" in err or "429" in err or "UNAVAILABLE" in err
            if retriable and attempt == 0:
                log.warning(f"Gemini Vision {err[:60]} — retry через 6s")
                await asyncio.sleep(6)
                continue
            log.warning(f"Gemini Vision validation failed: {e} — неизвестно (None)")
            return (None, False) if check_angle else None
    return (None, False) if check_angle else None


_BATCH_VERDICT_RE = re.compile(r"^\s*(\d+)\s*[:.\)]\s*(YES|NO)\b", re.IGNORECASE)


def _parse_batch_verdicts(text: str, n: int) -> list[bool] | None:
    """Строго парсит построчный ответ batch-проверки ('N: YES'/'N: NO').
    Если найденные номера не покрывают РОВНО множество {1..n} без пропусков
    и дублей — возвращает None (весь батч отбрасывается), а не частичный
    результат: частичное доверие к сбившейся нумерации модели опаснее,
    чем откат на поштучный validate_product_image для всего батча."""
    found: dict[int, bool] = {}
    matched_lines = 0
    for line in text.splitlines():
        m = _BATCH_VERDICT_RE.match(line.strip())
        if not m:
            continue
        matched_lines += 1
        found[int(m.group(1))] = m.group(2).upper() == "YES"
    if matched_lines != n or set(found.keys()) != set(range(1, n + 1)):
        return None
    return [found[i] for i in range(1, n + 1)]


async def validate_images_batch(images: list[bytes], product: str,
                                  color: str = "", allow_dock: bool = False
                                  ) -> list[bool | None]:
    """
    Батч-версия validate_product_image: одним вызовом Gemini проверяет все
    images по тем же правилам — текст правил (~9700 символов) пересылается
    ОДИН раз вместо N раз, экономя input-токены при N>1 кандидатах.
    Не поддерживает check_angle — для ракурса используется поштучный
    validate_product_image.

    Возвращает список bool|None той же длины и в том же порядке, что images.
    None на позиции значит "вердикт для этого фото не получен" (сбой API
    ИЛИ ответ не распарсился построчно на все n фото) — вызывающий код
    обязан докрыть такие позиции поштучным validate_product_image, batch
    здесь — чистая оптимизация поверх старого надёжного пути, а не замена.

    Стоимость батча из N фото — примерно как 1.3-1.5 одиночных вызова
    (за счёт однократной пересылки промпта правил), а не N.
    """
    from google import genai as gai
    from google.genai import types as gai_types

    n = len(images)
    if n == 0:
        return []

    client = gai.Client(api_key=settings.gemini_api_key)
    rules = _vision_rules_prompt(product, color, allow_dock)
    instruction = (
        f"\nНиже {n} фото ПОДРЯД, каждое помечено «Фото N:». Оцени КАЖДОЕ "
        f"фото по правилам выше независимо от остальных — правила и решение "
        f"по одному фото никак не влияют на другие фото.\n"
        f"Ответь РОВНО {n} строками, по одной на каждое фото, строго в "
        f"формате «N: YES» или «N: NO» (N — номер фото), без какого-либо "
        f"другого текста до, между или после строк."
    )
    contents: list = [rules + instruction]
    for i, img in enumerate(images, 1):
        contents.append(f"Фото {i}:")
        contents.append(gai_types.Part.from_bytes(data=img, mime_type=_detect_mime(img)))

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=gai_types.GenerateContentConfig(
                thinking_config=gai_types.ThinkingConfig(thinking_budget=0)
            ),
        )

    # 2 ретрая для временных 503/429 — как у поштучного validate_product_image
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=45.0)
            text = (response.text or "").strip()
            verdicts = _parse_batch_verdicts(text, n)
            if verdicts is None:
                log.warning(
                    f"Gemini Vision batch({n}): ответ не распарсился построчно "
                    f"1..{n} — откат на поштучный путь. Raw: {text[:200]!r}"
                )
                return [None] * n
            ok_count = sum(1 for v in verdicts if v)
            log.info(f"Gemini Vision batch({n}): {ok_count} OK / {n - ok_count} REJECT")
            return verdicts
        except Exception as e:
            err = str(e)
            retriable = "503" in err or "429" in err or "UNAVAILABLE" in err
            if retriable and attempt == 0:
                log.warning(f"Gemini Vision batch {err[:60]} — retry через 6s")
                await asyncio.sleep(6)
                continue
            log.warning(f"Gemini Vision batch({n}) failed: {e} — откат на поштучный путь")
            return [None] * n
    return [None] * n


async def classify_video_frame(img_bytes: bytes, product: str) -> str:
    """Классифицирует фото карточки для видео-пайплайна (build_wb_video) —
    Hailuo анимирует движение камеры вокруг товара, это НЕ карточка
    маркетплейса, поэтому строгие правила validate_product_image (только
    студийный фон, никакого контекста) тут не годятся — из-за них у
    сетевого оборудования (антенны/точки доступа на крыше/стене — обычный
    для категории кадр) отсеивались вообще ВСЕ фото, и в дело шло что
    попало (найдено 13.07.2026 на NanoBeam/CPE610).

    Возвращает одно из:
    PROMO — готовая рекламная/промо-графика (крупный текст названия, лого,
        бейджи фич на чистом фоне) — НЕ для вращения, годится как интро.
    PRODUCT — сам товар чётко виден и узнаваем как главный объект кадра,
        неважно студийный фон или установка на объекте — годится для
        вращения.
    BAD — товар не виден/еле виден (только аксессуар без товара, коллаж
        нескольких разных товаров, сильно обрезан, размыт, не тот товар).
    UNKNOWN — Vision недоступен, не блокируем поток.

    Стоимость: ~$0.0001-0.0003 за вызов."""
    from google import genai as gai
    from google.genai import types as gai_types

    client = gai.Client(api_key=settings.gemini_api_key)
    mime = _detect_mime(img_bytes)
    prompt = (
        f"На фото предположительно товар «{product}». Оцени пригодность фото "
        f"как ОПОРНОГО КАДРА для видео (ИИ-модель анимирует движение камеры "
        f"вокруг товара) — это НЕ карточка маркетплейса, поэтому обычные "
        f"строгие правила (только студийный фон, без контекста использования) "
        f"НЕ применяются.\n\n"
        f"Ответь СТРОГО одним словом:\n"
        f"PROMO — если это готовая рекламная/промо-графика от производителя "
        f"или продавца: крупное название товара текстом, лого бренда, "
        f"короткие бейджи фич (иконка+текст) поверх фото — то, что нельзя "
        f"анимировать, а не обычное фото самого товара.\n"
        f"PRODUCT — если сам товар ЧЁТКО ВИДЕН и узнаваем как главный объект "
        f"кадра — неважно, студийный фон, установлен на объекте (крыша, "
        f"стена, интерьер) или в реальной обстановке. Это ХОРОШИЙ ответ по "
        f"умолчанию, если товар просто виден.\n"
        f"BAD — если товар НЕ виден или еле виден: в кадре только аксессуар/"
        f"комплектующая без самого товара, коллаж нескольких разных товаров, "
        f"товар сильно обрезан по краю кадра, сильно размыт, или это явно "
        f"другой товар."
    )
    contents = [gai_types.Part.from_bytes(data=img_bytes, mime_type=mime), prompt]

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=gai_types.GenerateContentConfig(
                thinking_config=gai_types.ThinkingConfig(thinking_budget=0)
            ),
        )

    for attempt in range(2):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=30.0)
            text = (response.text or "").strip().upper()
            verdict = next((v for v in ("PROMO", "PRODUCT", "BAD") if v in text), "UNKNOWN")
            log.info(f"Gemini video-frame classify: {text[:20]!r} → {verdict}")
            return verdict
        except Exception as e:
            err = str(e)
            if ("503" in err or "429" in err or "UNAVAILABLE" in err) and attempt == 0:
                log.warning(f"Gemini video-frame classify {err[:60]} — retry через 6s")
                await asyncio.sleep(6)
                continue
            log.warning(f"Gemini video-frame classify failed: {e} — UNKNOWN")
            return "UNKNOWN"
    return "UNKNOWN"


async def describe_shape_for_video(img_bytes: bytes, product: str) -> str:
    """Короткое фактическое описание реальной геометрии товара по фото — идёт
    прямо в промпт Hailuo (build_prompt), чтобы модель не фабриковала объём
    там, где его нет. Нужна для плоских панельных товаров (антенны, точки
    доступа на стену/крышу): при orbit-повороте камеры Hailuo раздувает/
    растягивает такие товары, додумывая несуществующую толщину/глубину
    (найдено на CPE210 13.07.2026 — тот же класс проблемы, что раньше на
    NanoBeam/CPE610, см. classify_video_frame). Пустая строка при любой
    проблеме — тогда build_prompt просто не добавляет ограничение.
    Стоимость: ~$0.0001-0.0003 за вызов."""
    from google import genai as gai
    from google.genai import types as gai_types

    client = gai.Client(api_key=settings.gemini_api_key)
    mime = _detect_mime(img_bytes)
    prompt = (
        f"Товар: «{product}». Опиши ОДНОЙ короткой фразой на английском (до "
        f"15 слов) его реальную физическую форму — это пойдёт в промпт для "
        f"ИИ-видео с движением камеры вокруг товара, чтобы модель не "
        f"выдумывала объём/толщину, которых нет на фото.\n\n"
        f"Особо укажи: это ТОНКАЯ ПЛОСКАЯ панель/пластина почти без глубины "
        f"(например, настенная антенна, точка доступа, роутер-панель), или "
        f"полноценный объёмный 3D-корпус с заметной толщиной? Ответь только "
        f"фразой-описанием, без вступлений. Примеры: \"thin flat rectangular "
        f"panel, almost no depth\" или \"compact voluminous body with rounded "
        f"edges and visible depth\".\n\n"
        f"ВАЖНО: если на фото товар показан в разобранном виде, в разрезе, "
        f"со снятой крышкой или с видимой начинкой/платой внутри (рекламный "
        f"инфографический кадр «что внутри») — ИГНОРИРУЙ это и опиши форму "
        f"ТОЛЬКО собранного, закрытого корпуса, каким товар выглядит в "
        f"продаже. Никогда не используй в ответе слова disassembled, "
        f"exploded, cutaway, internal components, teardown, X-ray — фраза "
        f"должна описывать только внешний силуэт цельного закрытого товара."
    )
    contents = [gai_types.Part.from_bytes(data=img_bytes, mime_type=mime), prompt]

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=gai_types.GenerateContentConfig(
                thinking_config=gai_types.ThinkingConfig(thinking_budget=0)
            ),
        )

    for attempt in range(2):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=30.0)
            text = (response.text or "").strip().strip('"')
            log.info(f"Gemini shape-for-video: {text[:80]!r}")
            return text[:150]
        except Exception as e:
            err = str(e)
            if ("503" in err or "429" in err or "UNAVAILABLE" in err) and attempt == 0:
                log.warning(f"Gemini shape-for-video {err[:60]} — retry через 6s")
                await asyncio.sleep(6)
                continue
            log.warning(f"Gemini shape-for-video failed: {e} — пропуск")
            return ""
    return ""


async def generate_infographic_bg(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    bg_style: str | None = None,
) -> bytes | None:
    # Если стиль не передан — берём из ротации
    if not bg_style:
        bg_style = next(_infographic_styles)

    prompt = f"""Generate a PLAIN BACKGROUND IMAGE only. Portrait orientation 3:4 (768x1024).

This is a background for a product card. All elements (product photo, text, blocks) will be added programmatically on top.

REQUIREMENTS:
- Pure background only — NO product photos, NO text, NO logos, NO UI elements
- Completely clean surface from edge to edge
- Style: {bg_style}
- ALWAYS light: white, cream, pastel, soft gradient, light gray
- NEVER: dark, black, heavy shadows, neon, realistic objects

Think of it as a studio backdrop or a smooth gradient wallpaper."""

    contents = [prompt]

    raw = await _call_gemini(contents)
    if raw:
        target_w, target_h = 768, 1024  # стандартный 3:4 Gemini
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if img.size != (target_w, target_h):
            log.info(f"Gemini infographic crop+resize: {img.size} → {target_w}×{target_h}")
            src_w, src_h = img.size
            scale = max(target_w / src_w, target_h / src_h)
            new_w, new_h = int(src_w * scale), int(src_h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - target_w) // 2
            top  = (new_h - target_h) // 2
            img  = img.crop((left, top, left + target_w, top + target_h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        log.info(f"Gemini infographic OK: {product}, {img.size}")
        return buf.getvalue()
    return None


def _crop_white_borders(img: Image.Image,
                        white_thresh: int = 238,
                        max_frac: float = 0.30,
                        min_frac: float = 0.04) -> Image.Image:
    """
    Срезает почти-белые полосы сверху и снизу (виньетка / затухание в белый
    от Gemini). Находит границы по средней яркости строк, поэтому ширина
    реза подстраивается под конкретный фон.

    white_thresh — строка считается «белой», если её средняя яркость ≥ порога.
    max_frac     — не срезать больше этой доли высоты с каждого края (защита
                   от полностью светлого пастельного фона).
    min_frac     — всегда срезать хотя бы столько (мягкий градиентный фронт
                   у самого края, который ещё не дотянул до white_thresh).
    """
    try:
        src_w, src_h = img.size
        # Средняя яркость каждой строки: ужимаем ширину до 1px боксовым ресайзом.
        gray = img.convert("L").resize((1, src_h), Image.BOX)
        rows = list(gray.getdata())  # длина src_h, значения 0..255

        max_cut = int(src_h * max_frac)
        min_cut = int(src_h * min_frac)

        top = 0
        while top < max_cut and rows[top] >= white_thresh:
            top += 1
        bottom = 0
        while bottom < max_cut and rows[src_h - 1 - bottom] >= white_thresh:
            bottom += 1

        # Гарантированный минимум — снимает тонкий градиентный фронт у края.
        top = max(top, min_cut)
        bottom = max(bottom, min_cut)

        if top + bottom >= src_h:
            return img
        if top or bottom:
            cropped = img.crop((0, top, src_w, src_h - bottom))
            log.debug(f"Gemini richcontent white-border crop: "
                      f"top={top}px bottom={bottom}px → {cropped.size}")
            return cropped
        return img
    except Exception as e:
        log.warning(f"_crop_white_borders failed: {e} — фон без обрезки")
        return img


async def generate_richcontent_bg(
    product: str,
    img_bytes: bytes | None,
) -> bytes | None:
    bg_style = next(_richcontent_styles)

    prompt = f"""Generate a PLAIN BACKGROUND IMAGE for a marketplace rich-content card.
REQUIRED FORMAT: wide landscape rectangle, aspect ratio 17:10 (width is 1.7x the height). NOT square.

This is ONLY a background. Real product photos will be placed on top programmatically.

STRICT RULES — entire image:
- NO products, NO photos, NO objects of any kind
- NO text, letters, digits, watermarks, logos
- Uniform style edge-to-edge: same gradient/texture across the WHOLE image
- No darker/lighter "zones" — the background must look identical on the left and on the right
- NO white borders, NO white edges, NO vignette, NO fade-to-white at top/bottom/sides
- The color/gradient must extend all the way to every edge of the canvas

BACKGROUND STYLE:
- THIS CARD: {bg_style}
- Use RICH, SATURATED colors but keep the image BRIGHT (overall luminance > 60%, no dark areas)
- Vivid gradients, subtle abstract shapes, soft glow are encouraged
- NEVER: pure white only, near-white, dull gray, black, deep dark gradient
- The card should look COLORFUL and eye-catching, not bland
- Modern marketplace aesthetic — premium and visually engaging

Output ONLY the clean background (no objects, no products, no text). Nothing else."""

    # Фото товара НЕ передаём — Gemini-фон должен быть чистым, без объектов.
    # Реальные фото накладываются поверх в make_richcontent.
    contents = [prompt]

    raw = await _call_gemini(contents)
    if raw:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Gemini часто кладёт сверху/снизу «затухание в белый» (виньетку
        # studio backdrop / light bloom). Раньше резали фиксированные 10% —
        # если белая полоса шире, её остаток вылезал на левой зоне фона.
        # Теперь определяем высоту белых полос адаптивно и срезаем именно их.
        img = _crop_white_borders(img)
        target_w, target_h = 1700, 1000
        if img.size != (target_w, target_h):
            log.info(f"Gemini richcontent stretch: {img.size} → {target_w}×{target_h}")
            img = img.resize((target_w, target_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        log.info(f"Gemini richcontent OK: {product}, {img.size}")
        return buf.getvalue()
    return None
