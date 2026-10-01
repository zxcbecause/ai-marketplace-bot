"""
Хостинг фото на Cloudflare R2 (S3-совместимое API) — нужен потому что
Ozon (и Excel-шаблоны) принимают только публичную ссылку на картинку,
а сторонние сайты часто либо защищены от хотлинков, либо (через
Playwright-фолбэк kaspi/citilink/itmag/rtings) отдают ссылку на страницу
товара, а не на сам файл. Раз байты картинки у нас уже на руках —
просто перезаливаем их на свой бакет и отдаём свою ссылку.

Имена файлов — случайные (uuid4), без привязки к названию товара:
бакет публичный (Public Development URL), но без листинга — угадать
случайное имя нельзя, так что доступ к файлу есть только у тех, кому
мы сами дали конкретную ссылку.
"""
import asyncio
import logging
import uuid

import boto3
from botocore.config import Config

from config import settings

log = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def _put(data: bytes, key: str, content_type: str) -> None:
    client = _get_client()
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


async def upload_image(data: bytes, ext: str = "jpg") -> str | None:
    """Загружает байты картинки на R2, возвращает публичную ссылку.
    None при ошибке (бот должен продолжать без фото, не падать)."""
    key = f"{uuid.uuid4().hex}.{ext}"
    content_type = "image/png" if ext == "png" else "image/jpeg"
    try:
        await asyncio.to_thread(_put, data, key, content_type)
    except Exception as e:
        log.warning(f"R2 upload failed: {e}")
        return None
    url = f"{settings.r2_public_url.rstrip('/')}/{key}"
    log.info(f"R2 upload OK: {key} ({len(data):,}b) -> {url}")
    return url


async def upload_video(data: bytes, ext: str = "mp4") -> str | None:
    """Как upload_image, но для видео (видеообложка Ozon — атрибут
    принимает только ссылку на файл, не сами байты)."""
    key = f"{uuid.uuid4().hex}.{ext}"
    try:
        await asyncio.to_thread(_put, data, key, "video/mp4")
    except Exception as e:
        log.warning(f"R2 video upload failed: {e}")
        return None
    url = f"{settings.r2_public_url.rstrip('/')}/{key}"
    log.info(f"R2 video upload OK: {key} ({len(data):,}b) -> {url}")
    return url
