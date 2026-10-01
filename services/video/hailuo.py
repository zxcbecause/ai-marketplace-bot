"""
Hailuo (MiniMax) видео через реселлера kie.ai — image-to-video для
видеообложек WB. Модель анимирует реальное фото товара (не рисует
с нуля), опционально с двумя опорными кадрами (start/end frame) —
см. память project_wb_video_cover за историей выбора модели/промпт-стратегии.

Схема API (docs.kie.ai/market/hailuo/02-image-to-video-standard,
docs.kie.ai/market/common/get-task-detail, сверено 13.07.2026):
POST /jobs/createTask -> {"data": {"taskId": ...}}
GET  /jobs/recordInfo?taskId=... -> {"data": {"state": ..., "resultJson": "..."}}
resultJson — JSON-строка вида {"resultUrls": ["https://.../video.mp4"]}.
"""
import asyncio
import json
import logging

import aiohttp

from config import settings

log = logging.getLogger(__name__)

BASE = "https://api.kie.ai/api/v1"
MODEL_STANDARD = "hailuo/02-image-to-video-standard"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.kie_api_key}", "Content-Type": "application/json"}


async def create_task(
    prompt: str,
    image_url: str,
    end_image_url: str | None = None,
    resolution: str = "512P",
    duration: str = "6",
) -> str:
    """Запускает генерацию, возвращает taskId."""
    body = {
        "model": MODEL_STANDARD,
        "input": {
            "prompt": prompt[:1500],
            "image_url": image_url,
            "resolution": resolution,
            "duration": duration,
        },
    }
    if end_image_url:
        body["input"]["end_image_url"] = end_image_url

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE}/jobs/createTask", headers=_headers(), json=body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            if resp.status != 200 or data.get("code") != 200:
                raise RuntimeError(f"kie.ai createTask failed: HTTP {resp.status} {data}")
            task_id = data["data"]["taskId"]
            log.info(f"kie.ai Hailuo task создан: {task_id}")
            return task_id


async def poll_task(task_id: str, interval: float = 10.0, timeout: float = 420.0) -> str:
    """Ждёт завершения задачи, возвращает URL готового видео."""
    elapsed = 0.0
    async with aiohttp.ClientSession() as session:
        while elapsed < timeout:
            async with session.get(
                f"{BASE}/jobs/recordInfo", headers=_headers(),
                params={"taskId": task_id}, timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
            state = (data.get("data") or {}).get("state")
            if state == "success":
                result = json.loads(data["data"]["resultJson"])
                url = result["resultUrls"][0]
                log.info(f"kie.ai Hailuo task {task_id} готово: {url}")
                return url
            if state == "fail":
                raise RuntimeError(f"kie.ai Hailuo task {task_id} упала: {data['data'].get('failMsg')}")
            await asyncio.sleep(interval)
            elapsed += interval
    raise TimeoutError(f"kie.ai Hailuo task {task_id} не завершилась за {timeout:.0f}с")


async def generate_video(
    prompt: str,
    image_url: str,
    end_image_url: str | None = None,
    resolution: str = "512P",
    duration: str = "6",
) -> bytes:
    """Полный цикл: создать задачу → дождаться → скачать видео байтами.
    Hailuo 02 standard в режиме first-last-frame (задан end_image_url)
    поддерживает только 768P — 512P только для одного опорного кадра
    (проверено вживую 13.07.2026, API вернул явную ошибку)."""
    if end_image_url and resolution == "512P":
        resolution = "768P"
    task_id = await create_task(prompt, image_url, end_image_url, resolution, duration)
    video_url = await poll_task(task_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.read()
