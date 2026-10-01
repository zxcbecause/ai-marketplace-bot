"""
Команда /video <артикул WB> [кадры|черновик|сборка] — видеообложка через
Hailuo (kie.ai, реселлер MiniMax). Логика (фото с WB → Vision-отбор кадров →
R2 → Hailuo → интро с инфографикой) — в services/video/pipeline.py.

Структура видео (подтверждена пользователем 29.07.2026): инфографика в
начале (3с) → плавный кроссфейд → прокрутка товара, в финале камера
замедляется и останавливается (без обрыва движения).

Подкоманды — инструментарий против косячных видео без лишних трат:
  кадры    — бесплатно показать отобранные опорные кадры и промпт ДО генерации
  черновик — дёшево (512P/6с, БЕЗ конечного кадра) проверить только движение
             камеры/промпт — форма товара тут МОЖЕТ деформироваться, это не
             тест качества, см. build_draft_video
  сборка   — бесплатная пересборка монтажа из кэша последней генерации

29.07.2026: пробовали убрать конечный опорный кадр ради экономии (512P) —
Hailuo без него деформирует форму товара на orbit-повороте (живой баг на
мыши). Вернули конечный кадр обратно — обычная генерация снова 768P/10с,
один клип, с двумя опорными кадрами (старт+финал). CHEAP_MODE в
pipeline.py теперь означает только «не включать дорогую 2-клиповую 16с
версию» (build_wb_video_long), не урезание качества одиночного клипа.

20.07.2026: автозагрузка на WB/Ozon убрана — видео только отправляется
в чат, публикацию на маркетплейсы теперь делают вручную при необходимости.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, InputMediaPhoto

from services.video.pipeline import (
    build_wb_video_long,
    build_draft_video,
    preview_video_frames,
    rebuild_video_from_cache,
)
from handlers.tasks import run_task

log = logging.getLogger(__name__)
router = Router()

_HELP = (
    "Формат: /video <артикул WB> [кадры|черновик|сборка]\n\n"
    "Без подкоманды — генерация: инфографика → прокрутка товара "
    "(Hailuo, 768P/10с, платно, ~$0.4).\n"
    "кадры — бесплатно показать, какие кадры и промпт пойдут в генерацию.\n"
    "черновик — дёшево (512P/6с, без конечного кадра) проверить движение "
    "камеры/промпт — форма товара тут может «поплыть», это не тест качества.\n"
    "сборка — бесплатно пересобрать монтаж из кэша последней генерации."
)


async def _run_video(message: Message, article: str):
    progress = await message.answer(f"Видео для артикула {article}: ищу фото на WB, отбираю кадры...")
    try:
        video_bytes, title = await build_wb_video_long(article)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        log.error(f"video {article}: {e}", exc_info=True)
        await progress.edit_text(f"🔴 Генерация не удалась: {e}")
        return

    doc = BufferedInputFile(video_bytes, filename=f"{article}_video.mp4")
    await message.answer_video(doc, caption=f"Видеообложка: {title}")


async def _run_frames(message: Message, article: str):
    progress = await message.answer(f"Кадры для {article}: отбираю через Vision (бесплатно)...")
    try:
        title, frames, promo, prompt = await preview_video_frames(article)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        log.error(f"video frames {article}: {e}", exc_info=True)
        await progress.edit_text(f"🔴 Не удалось отобрать кадры: {e}")
        return

    if len(frames) == 1:
        # answer_media_group требует минимум 2 элемента — один кадр шлём обычным фото
        await message.answer_photo(
            BufferedInputFile(frames[0], filename="frame1.jpg"),
            caption=f"Опорный кадр для «{title}» (второго не нашлось — риск деформации формы без него)",
        )
    elif frames:
        labels = ["старт", "финал"] if len(frames) == 2 else ["старт", "стык", "финал"]
        media = [
            InputMediaPhoto(
                media=BufferedInputFile(f, filename=f"frame{i}.jpg"),
                caption=(f"Опорные кадры для «{title}» (по порядку: {' → '.join(labels[:len(frames)])})"
                         if i == 1 else None),
            )
            for i, f in enumerate(frames, 1)
        ]
        await message.answer_media_group(media)
    if promo:
        await message.answer_photo(
            BufferedInputFile(promo, filename="promo.jpg"),
            caption="Найдена готовая промо-графика — пойдёт в интро вместо генерации инфографики",
        )
    tail = "" if len(frames) >= 3 else " (меньше 3 — будет один клип вместо 16с)"
    await progress.edit_text(
        f"Отобрано кадров: {len(frames)}"
        + (tail if len(frames) > 1 else "")
        + f"\n\nПромпт:\n<code>{prompt}</code>",
        parse_mode="HTML",
    )


async def _run_draft(message: Message, article: str):
    progress = await message.answer(f"Черновик для {article}: генерю 512P/6с (дёшево)...")
    try:
        video_bytes, title = await build_draft_video(article)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        log.error(f"video draft {article}: {e}", exc_info=True)
        await progress.edit_text(f"🔴 Черновик не удался: {e}")
        return

    doc = BufferedInputFile(video_bytes, filename=f"{article}_draft.mp4")
    await message.answer_video(doc, caption=f"Черновик 512P (только движение/промпт, форма может «поплыть» — без конечного кадра): {title}")


async def _run_rebuild(message: Message, article: str):
    progress = await message.answer(f"Сборка для {article}: пересобираю из кэша (бесплатно)...")
    try:
        video_bytes, title = await rebuild_video_from_cache(article)
    except ValueError as e:
        await progress.edit_text(f"🔴 {e}")
        return
    except Exception as e:
        log.error(f"video rebuild {article}: {e}", exc_info=True)
        await progress.edit_text(f"🔴 Пересборка не удалась: {e}")
        return

    doc = BufferedInputFile(video_bytes, filename=f"{article}_video.mp4")
    await message.answer_video(doc, caption=f"Пересборка из кэша: {title}")


@router.message(Command("video"))
async def cmd_video(message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(_HELP)
        return
    article = parts[1].strip()
    sub = parts[2].strip().lower() if len(parts) > 2 else ""

    if sub in ("кадры", "frames"):
        coro = _run_frames(message, article)
    elif sub in ("черновик", "draft"):
        coro = _run_draft(message, article)
    elif sub in ("сборка", "rebuild"):
        coro = _run_rebuild(message, article)
    elif sub:
        await message.answer(f"Неизвестная подкоманда «{sub}».\n\n{_HELP}")
        return
    else:
        coro = _run_video(message, article)
    run_task(message.from_user.id, coro, message=message)
