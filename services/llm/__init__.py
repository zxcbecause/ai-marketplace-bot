from .base import LLMProvider, LLMResponse
from .deepseek import DeepSeekProvider
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider
from database import db_connect

_providers: dict[str, LLMProvider] = {}


def _get_provider(name: str) -> LLMProvider:
    if name not in _providers:
        if name == "openai":
            _providers[name] = OpenAIProvider()
        elif name == "gemini":
            _providers[name] = GeminiProvider()
        else:
            _providers[name] = DeepSeekProvider()
    return _providers[name]


async def get_llm(user_id: int) -> LLMProvider:
    """Возвращает провайдер для пользователя (читает выбор из БД)."""
    async with db_connect() as db:
        cursor = await db.execute(
            "SELECT llm_model FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()

    model = row[0] if row else "deepseek"
    return _get_provider(model)


async def set_llm(user_id: int, model: str):
    """Сохраняет выбор модели пользователя."""
    async with db_connect() as db:
        # INSERT ... ON CONFLICT: admin may have no row in users (access is by ADMIN_ID),
        # and a plain UPDATE would then silently do nothing.
        await db.execute(
            "INSERT INTO users (user_id, llm_model) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET llm_model = excluded.llm_model",
            (user_id, model)
        )
        await db.commit()


__all__ = ["LLMProvider", "LLMResponse", "get_llm", "set_llm"]
