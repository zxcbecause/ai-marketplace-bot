import io
import logging
import os
import threading
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import numpy as np
import torch
from PIL import Image

log = logging.getLogger(__name__)

_MODEL_ID = "openai/clip-vit-base-patch32"
_model = None
_processor = None
_device = "cuda" if torch.cuda.is_available() else "cpu"
# 20.07.2026: _load() зовётся параллельно из нескольких to_thread (run_gpu) —
# без лока модель грузилась ДВАЖДЫ (2× VRAM), а одновременный ленивый импорт
# transformers из двух потоков давал "cannot import name 'CLIPModel'"
# (наблюдалось живьём: лог 00:46:02.106 и .108, две загрузки с разницей 2 мс).
_load_lock = threading.Lock()


def _from_pretrained(cls):
    # Сначала локальный кэш без походов на HF Hub: from_pretrained по
    # умолчанию делает десятки HEAD/GET к huggingface.co даже когда модель
    # давно скачана — секунды на каждом холодном старте и точка отказа,
    # если HF лежит. Онлайн — только если кэша реально нет.
    try:
        return cls.from_pretrained(_MODEL_ID, local_files_only=True)
    except OSError:
        log.info(f"CLIP: нет локального кэша {_MODEL_ID} — качаем с HF Hub")
        return cls.from_pretrained(_MODEL_ID)


def _load():
    global _model, _processor
    if _model is None:
        with _load_lock:
            if _model is None:
                from transformers import CLIPModel, CLIPProcessor
                log.info(f"Loading CLIP on {_device}...")
                processor = _from_pretrained(CLIPProcessor)
                model = _from_pretrained(CLIPModel).to(_device)
                # _model присваиваем последним: быстрый путь наверху проверяет
                # именно его, _processor к этому моменту уже должен быть готов.
                _processor = processor
                _model = model
                log.info("CLIP ready")
    return _model, _processor


# Русская категория → английская фраза для CLIP (текст-энкодер знает
# категории на английском, но НЕ знает бренд-модели вида "Edifier G1000").
_CATEGORY_EN: dict[str, str] = {
    "Акустика": "computer speakers or a stereo speaker set",
    "Наушники": "headphones or wireless earbuds",
    "Мыши": "a computer mouse",
    "Клавиатуры": "a computer keyboard",
    "Комплект клавиатура и мышь": "a keyboard and mouse set",
    "Смартфоны": "a smartphone",
    "Планшеты": "a tablet computer",
    "Ноутбуки": "a laptop computer",
    "Мониторы": "a computer monitor viewed from the front with visible screen",
    "Моноблоки": "an all-in-one desktop computer",
    "Видеокарты": "a graphics card",
    "Материнские платы": "a computer motherboard",
    "Процессоры": "a CPU processor chip in retail packaging or bare",
    "Оперативная память": "RAM memory modules",
    "SSD накопители": "an SSD solid state drive",
    "Внешние жёсткие диски": "an external hard drive",
    "Блоки питания": "a power supply unit",
    "Зарядные устройства и блоки питания": "a charger or power adapter",
    "Корпуса для ПК": "a PC computer case",
    "Охлаждение": "a CPU cooler or PC fan",
    "Сетевое оборудование": "a wifi router or network device",
    "Камеры видеонаблюдения": "a surveillance camera",
    "Принтеры": "a printer",
    "Смарт-часы": "a smartwatch or fitness band",
    "Игровые кресла": "a gaming chair",
    "Графические планшеты": "a graphics drawing tablet",
    "Кроссовки": "a sneaker shoe",
    "Кабели и аксессуары": "a cable or adapter",
    "Кронштейны для мониторов": "a monitor mount arm",
    "Модемы": "a modem or network device",
}

# Точечные негативы для категорий с известными проблемами подбора —
# добавляются К общему набору только для своей категории, не размывая
# softmax для всех остальных.
_CATEGORY_NEGATIVES: dict[str, list[str]] = {
    "Наушники": [
        "headphones folded or collapsed, ear cups pressed together",
        "headphones showing inner cushion padding facing up",
    ],
    "Смартфоны": [
        "extreme close-up of camera module or lens only, no full phone body",
        "smartphone next to its retail box with charger and accessories",
    ],
    # 05.07: на лицевую инфографику мониторов попадали фото задней панели —
    # WB-фотосеты содержат тыльные ракурсы, обычный clip_score их не отличал.
    "Мониторы": [
        "the back side of a monitor, rear panel with ports and mounting stand",
        "rear view of a display showing its back cover, no screen visible",
    ],
}


def clip_score(img_bytes: bytes, product: str, category: str = "") -> float:
    """
    Возвращает score 0..1 — сумма softmax-вероятностей позитивных меток.

    Переработан 03.07.2026: раньше было 2 позитива с бренд-моделью (которую
    CLIP не знает) против 22 генерических негативов — softmax размазывался,
    живые фото (включая официальные сайты) получали 0.00x и резались порогом
    0.40. Подтверждено экспериментом: фото пары колонок Edifier G1000 с WB —
    старый скорер 0.004, сбалансированный 0.999. Пары товаров (акустика 2.0,
    комплекты, RAM-киты) дополнительно ловились негативом «multiple products».
    Теперь: позитивы строятся из КАТЕГОРИИ (англ.), негативов немного и они
    сфокусированы; категорийные добавки — точечно из _CATEGORY_NEGATIVES.
    """
    try:
        model, processor = _load()
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        cat_en = _CATEGORY_EN.get(category, "")
        subject = cat_en or "a consumer electronics product"
        texts = [
            # Позитивные (индексы 0-2)
            f"a clean studio product photo of {subject}",
            f"a professional ecommerce photo of {subject} on a plain background",
            f"a photo of {product}",
            # Негативные (индексы 3+) — сфокусированный набор
            "an advertisement banner with large marketing text",
            "a company logo or icon only, no physical product",
            "a catalog page or collage with many different products",
            "a product shown next to its retail packaging box",
            "a hand holding an object, person using a device",
            "a blurry low quality photo",
            "a screenshot of a website with text and buttons",
            "a photo of a completely different unrelated product",
        ]
        n_pos = 3
        texts.extend(_CATEGORY_NEGATIVES.get(category, []))

        inputs = processor(
            text=texts, images=img,
            return_tensors="pt", padding=True
        ).to(_device)

        with torch.no_grad():
            outputs = model(**inputs)
            probs = outputs.logits_per_image.softmax(dim=1)[0]

        positive_score = float(probs[:n_pos].sum())
        log.debug(f"CLIP score: {positive_score:.3f} for {product[:40]} (cat={category or '-'})")
        return positive_score

    except Exception as e:
        log.warning(f"CLIP scoring failed: {e}")
        return 0.5


def clip_image_embedding(img_bytes: bytes):
    """Нормализованный image embedding для cosine similarity."""
    model, processor = _load()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    inputs = processor(images=img, return_tensors="pt").to(_device)
    with torch.no_grad():
        vision_outputs = model.vision_model(pixel_values=inputs["pixel_values"])
        pooled = vision_outputs.pooler_output
        feats = model.visual_projection(pooled)
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats[0].cpu()


def image_similarity(emb1, emb2) -> float:
    """Cosine similarity (0..1) между двумя нормализованными embeddings."""
    return float((emb1 * emb2).sum())


def get_image_embedding(img_bytes: bytes) -> np.ndarray | None:
    """L2-нормированный CLIP image-эмбеддинг (512-мерный, patch32) — для
    оценки визуальной ПОХОЖЕСТИ/РАЗНИЦЫ кадров между собой (косинус =
    скалярное произведение нормированных векторов), в отличие от clip_score
    (похожесть картинки на ТЕКСТОВОЕ описание категории). Нужен для отбора
    визуально разных ракурсов товара под видео-прокрутку
    (services/video/pipeline.py). None при любой ошибке — вызывающий код
    должен иметь фолбэк."""
    try:
        model, processor = _load()
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        inputs = processor(images=img, return_tensors="pt").to(_device)
        with torch.no_grad():
            out = model.get_image_features(**inputs)
            # transformers 5.x: get_image_features возвращает
            # BaseModelOutputWithPooling (эмбеддинг в .pooler_output), а не
            # голый тензор, как в старых версиях/примерах из документации HF.
            feat = out.pooler_output if hasattr(out, "pooler_output") else out
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat[0].cpu().numpy()
    except Exception as e:
        log.warning(f"CLIP embedding failed: {e}")
        return None
