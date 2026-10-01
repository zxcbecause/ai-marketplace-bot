"""Сборка минимального Rich-контент JSON для Ozon — один full-width виджет
с уже готовой картинкой (см. services/image/make_richcontent), без ручного
конструктора rich-content.ozon.ru.

Схема виджета "raShowcase"/"billboard" собрана по открытым примерам Ozon —
официальные докс блокируют автоматический парсинг. Если рич-контент не
отрендерится в карточке, нужно один раз собрать такой же блок (одна
картинка на всю ширину) в конструкторе rich-content.ozon.ru, экспортировать
его JSON и сверить с этим точные имена полей.
"""
import json

_RICHCONTENT_W, _RICHCONTENT_H = 1600, 1000


def build_image_rich_content(
    image_url: str, width: int = _RICHCONTENT_W, height: int = _RICHCONTENT_H,
) -> str:
    """JSON-строка для атрибута Ozon "Rich-контент JSON" (id=11254) — один
    блок с готовым изображением на всю ширину карточки."""
    return json.dumps({
        "content": [
            {
                "widgetName": "raShowcase",
                "type": "billboard",
                "blocks": [
                    {
                        "img": {
                            "src": image_url,
                            "srcMobile": image_url,
                            "width": width,
                            "height": height,
                            "widthMobile": width,
                            "heightMobile": height,
                        },
                    },
                ],
            },
        ],
        "version": 0.3,
    }, ensure_ascii=False)
