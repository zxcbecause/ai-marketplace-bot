import datetime
import logging
import httpx
from .base import LLMProvider, LLMResponse
from config import settings

log = logging.getLogger(__name__)

# 17.08.2026: DeepSeek перешёл на peak/off-peak тарификацию (пик 01:00-04:00
# и 06:00-10:00 UTC — офф-пик ровно вдвое дешевле), подтверждено live через
# api-docs.deepseek.com. Старая плоская цена $0.14/$0.28 за 1M токенов,
# которая тут была, — это ни кэш-хит, ни кэш-мисс, ни офф-пик, ни пик, а
# что-то устаревшее ещё с флэт-тарифа: логи считали батч сегодня в ~$0.49,
# а реальный баланс DeepSeek за то же время просел на $3.32 (расхождение
# ~6.8x, часть батча пришлась на пиковые часы 06:00-10:00 UTC = 11:00-15:00
# Алматы, где output стоит $1.32 вместо предполагавшихся $0.28 — 4.7x).
# Живая цена (per 1M tokens), see api-docs.deepseek.com/quick_start/pricing:
_PRICE_IN_HIT_OFFPEAK  = 0.007 / 1_000_000
_PRICE_IN_HIT_PEAK     = 0.014 / 1_000_000
_PRICE_IN_MISS_OFFPEAK = 0.22  / 1_000_000
_PRICE_IN_MISS_PEAK    = 0.44  / 1_000_000
_PRICE_OUT_OFFPEAK     = 0.66  / 1_000_000
_PRICE_OUT_PEAK        = 1.32  / 1_000_000


def _is_peak_utc() -> bool:
    h = datetime.datetime.now(datetime.timezone.utc).hour
    return 1 <= h < 4 or 6 <= h < 10

_BASE_URL = "https://api.deepseek.com/v1/chat/completions"


class DeepSeekProvider(LLMProvider):
    def __init__(self):
        self._client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0),
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        )

    async def chat(
        self,
        user_message: str,
        system_prompt: str,
        few_shot: list[tuple[str, str]] | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = True,
        thinking_budget: int | None = None,  # Gemini-специфично, здесь не поддержано
    ) -> LLMResponse:
        messages = [{"role": "system", "content": system_prompt}]

        if few_shot:
            for user_ex, assistant_ex in few_shot:
                messages.append({"role": "user",      "content": user_ex})
                messages.append({"role": "assistant", "content": assistant_ex})

        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": settings.deepseek_model,
            "messages": messages,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if not enable_thinking:
            # 28.07.2026: deepseek-v4-flash по умолчанию reasoning — без этого
            # флага model тратит output-токены на reasoning_content ДО
            # финального content, и max_tokens может обрезать ответ ПУСТЫМ
            # (finish_reason='length', content=''), см. base.py::chat.
            payload["thinking"] = {"type": "disabled"}

        import asyncio as _aio
        for attempt in range(3):
            try:
                resp = await self._client.post(_BASE_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
                # 04.08.2026: raise_for_status() пропускает HTTP 200 с телом
                # без "choices" (видели на живом батче Ozon — редкий сбой на
                # стороне DeepSeek, тело неизвестного формата на 200 OK).
                # Без этой проверки KeyError('choices') летел из-под цикла
                # ретраев необработанным и ронял ОДИН товар батча. Логируем
                # само тело — иначе непонятно, что вообще вернул API.
                if "choices" not in data:
                    raise ValueError(f"ответ DeepSeek без 'choices': {data!r}"[:500])
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                # 4xx (кроме 429) не лечатся повтором: 402 — нет денег, 401 — ключ
                if 400 <= code < 500 and code != 429:
                    log.error(f"DeepSeek fatal {code} ({type(e).__name__}): {e}")
                    raise
                log.error(f"DeepSeek error ({type(e).__name__}): {e}")
                if attempt < 2:
                    log.info(f"DeepSeek retry {attempt + 1}/2...")
                    await _aio.sleep(3)
                else:
                    raise
            except Exception as e:
                # 03.08.2026: голое str(e) иногда пусто (httpx-таймаут/сетевая
                # ошибка без текста) — тип исключения обязателен для диагностики,
                # без него в логе оставалось "DeepSeek error: " пустотой.
                log.error(f"DeepSeek error ({type(e).__name__}): {e}")
                if attempt < 2:
                    log.info(f"DeepSeek retry {attempt + 1}/2...")
                    await _aio.sleep(3)
                else:
                    raise

        text       = data["choices"][0]["message"]["content"]
        usage      = data.get("usage", {})
        in_tok     = usage.get("prompt_tokens", 0)
        out_tok    = usage.get("completion_tokens", 0)
        hit_tok    = usage.get("prompt_cache_hit_tokens", 0)
        miss_tok   = usage.get("prompt_cache_miss_tokens", max(0, in_tok - hit_tok))
        peak       = _is_peak_utc()
        usd        = (
            hit_tok * (_PRICE_IN_HIT_PEAK if peak else _PRICE_IN_HIT_OFFPEAK)
            + miss_tok * (_PRICE_IN_MISS_PEAK if peak else _PRICE_IN_MISS_OFFPEAK)
            + out_tok * (_PRICE_OUT_PEAK if peak else _PRICE_OUT_OFFPEAK)
        )

        log.info(f"DeepSeek: {in_tok}+{out_tok} tok, ${usd:.5f}")
        return LLMResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            usd=usd,
            provider="deepseek",
        )

    async def close(self):
        await self._client.aclose()
