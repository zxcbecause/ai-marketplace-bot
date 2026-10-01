"""Пул фоновых шаблонов /rarity — координаты вымеряны программно (детект
связных тёмно-синих плашек RGB(80,87,141) на фоне data/backgrounds/rarity/
purple_wave_01_clean.jpg, 900×1200) по референсу пользователя 17.07.2026.
Плашки (заголовок + 3 характеристики) и ватермарка магазина уже впечатаны в
саму картинку фона — рендерер только кладёт вырезанное фото товара в
product_box и текст поверх плашек. Добавление нового фона = новый dict
в RARITY_TEMPLATES с теми же ключами."""
import random
from pathlib import Path

RARITY_BG_DIR = Path(r"C:\AI-Bot-V2\data\backgrounds\rarity")

RARITY_TEMPLATES: list[dict] = [
    {
        "key": "purple_wave_01",
        "bg": str(RARITY_BG_DIR / "purple_wave_01_clean.jpg"),
        "canvas": (900, 1200),
        "title_box": (118, 0, 782, 265),
        "product_box": (10, 296, 569, 1003),
        "char_boxes": [
            (578, 343, 899, 469),
            (578, 501, 899, 627),
            (578, 658, 899, 784),
        ],
        "has_watermark": True,
        "title_color": (255, 255, 255),
        "char_color": (255, 255, 255),
    },
]


def pick_random_rarity_template() -> dict:
    return random.choice(RARITY_TEMPLATES)
