import asyncio
import logging

from .base import LLMProvider, LLMResponse
from config import settings

log = logging.getLogger(__name__)

# 17.08.2026: DeepSeek временно без бюджета (402) — нужен текстовый провайдер,
# не завязанный на DeepSeek, чтобы find_product_images() (LLM-валидация
# Playwright-URL) и другие текстовые вызовы могли работать через Gemini —
# он уже используется в проекте для vision (services/image/gemini.py) и
# бюджет на нём есть. Цена ниже официальной (per 1M tokens, gemini-2.5-flash):
# $0.30 / $2.50 — не peak/off-peak, единый тариф.
_PRICE_IN = 0.30 / 1_000_000
_PRICE_OUT = 2.50 / 1_000_000
_MODEL = "gemini-2.5-flash"
# 01.10.2026: «ступени ракеты» — у каждой модели Gemini свой дневной лимит бесплатных запросов.
# Кончился у одной — переходим к следующей; кончился у всех — QUOTA_MARKER в тексте ошибки,
# по нему /wb_create ставит остаток списка в очередь на завтра (handlers/wb.py).
_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
QUOTA_MARKER = "GEMINI_QUOTA_EXHAUSTED"
_exhausted: dict[str, str] = {}  # модель -> дата (UTC), когда кончился дневной лимит


def _today() -> str:
    import datetime
    # лимиты Gemini сбрасываются в полночь по Тихоокеанскому времени (~UTC-8)
    return (datetime.datetime.utcnow() - datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


def quota_exhausted_all() -> bool:
    return all(_exhausted.get(m) == _today() for m in _MODELS)


class GeminiProvider(LLMProvider):
    async def chat(
        self,
        user_message: str,
        system_prompt: str,
        few_shot: list[tuple[str, str]] | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = True,
        thinking_budget: int | None = None,
    ) -> LLMResponse:
        from google import genai as gai
        from google.genai import types as gai_types

        client = gai.Client(api_key=settings.gemini_api_key)

        contents: list = []
        if few_shot:
            for user_ex, assistant_ex in few_shot:
                contents.append(gai_types.Content(role="user", parts=[gai_types.Part.from_text(text=user_ex)]))
                contents.append(gai_types.Content(role="model", parts=[gai_types.Part.from_text(text=assistant_ex)]))
        contents.append(gai_types.Content(role="user", parts=[gai_types.Part.from_text(text=user_message or system_prompt)]))

        config_kwargs = {}
        if system_prompt and user_message:
            config_kwargs["system_instruction"] = system_prompt
        if max_tokens is not None:
            config_kwargs["max_output_tokens"] = max_tokens
        if not enable_thinking:
            config_kwargs["thinking_config"] = gai_types.ThinkingConfig(thinking_budget=0)
        elif thinking_budget is not None:
            # Потолок размышления вместо полного отключения — см. base.py.
            config_kwargs["thinking_config"] = gai_types.ThinkingConfig(
                thinking_budget=thinking_budget
            )

        def _sync(model, cfg):
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=gai_types.GenerateContentConfig(**cfg) if cfg else None,
            )

        last_err = None
        for model in _MODELS:
            if _exhausted.get(model) == _today():
                continue
            cfg = dict(config_kwargs)
            for attempt in range(3):
                try:
                    resp = await asyncio.wait_for(asyncio.to_thread(_sync, model, cfg), timeout=90.0)
                    text = resp.text or ""
                    usage = getattr(resp, "usage_metadata", None)
                    in_tok = getattr(usage, "prompt_token_count", 0) or 0
                    # 25.08.2026: thinking-токены биллятся по цене output вместе с ответом,
                    # но живут в отдельном thoughts_token_count.
                    out_tok = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                    usd = in_tok * _PRICE_IN + out_tok * _PRICE_OUT
                    log.info(f"Gemini ({model}): {in_tok}+{out_tok} tok, ${usd:.5f}")
                    return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok, usd=usd, provider="gemini")
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    if "PerDay" in msg or "per day" in msg.lower():
                        _exhausted[model] = _today()
                        log.warning(f"Gemini: у {model} кончился дневной лимит — следующая модель")
                        break
                    if "404" in msg or "NOT_FOUND" in msg:
                        break  # модели нет — следующая
                    if "thinking" in msg.lower() and "thinking_config" in cfg:
                        cfg.pop("thinking_config")  # модель не умеет управлять размышлением — без него
                        continue
                    log.warning(f"Gemini {model} ошибка (попытка {attempt + 1}/3): {msg[:200]}")
                    if attempt < 2:
                        await asyncio.sleep(10 * (attempt + 1) if ("503" in msg or "429" in msg) else 3)
        if quota_exhausted_all():
            raise RuntimeError(f"{QUOTA_MARKER}: дневной лимит всех моделей Gemini исчерпан, сброс ~в 14:00 по Бишкеку")
        raise RuntimeError(f"Gemini chat failed: {last_err}")

    async def close(self):
        pass
