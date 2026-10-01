import logging
import httpx
from .base import LLMProvider, LLMResponse
from config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://api.openai.com/v1/chat/completions"

_PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.4":      (7.50,  30.00),
    "gpt-4.1":      (2.00,   8.00),
    "gpt-4.1-mini": (0.40,   1.60),
    "gpt-4o":       (2.50,  10.00),
    "gpt-4o-mini":  (0.15,   0.60),
}


class OpenAIProvider(LLMProvider):
    def __init__(self):
        self._client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0),
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
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
        # enable_thinking — специфично для DeepSeek v4 Flash (reasoning по
        # умолчанию), у GPT нет такого переключателя на этом эндпоинте —
        # параметр принимается для единого интерфейса LLMProvider и не влияет
        # на запрос.
        messages = [{"role": "system", "content": system_prompt}]

        if few_shot:
            for user_ex, assistant_ex in few_shot:
                messages.append({"role": "user",      "content": user_ex})
                messages.append({"role": "assistant", "content": assistant_ex})

        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": settings.openai_model,
            "messages": messages,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            resp = await self._client.post(_BASE_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"OpenAI error: {e}")
            raise

        text    = data["choices"][0]["message"]["content"]
        usage   = data.get("usage", {})
        in_tok  = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)

        price_in, price_out = _PRICING.get(settings.openai_model, (2.50, 10.00))
        usd = (in_tok * price_in + out_tok * price_out) / 1_000_000

        log.info(f"OpenAI ({settings.openai_model}): {in_tok}+{out_tok} tok, ${usd:.5f}")
        return LLMResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            usd=usd,
            provider="openai",
        )

    async def close(self):
        await self._client.aclose()
