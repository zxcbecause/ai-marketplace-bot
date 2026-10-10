from .base import LLMProvider, LLMResponse
from .deepseek import DeepSeekProvider
from .openai_provider import OpenAIProvider
from .gemini_provider import GeminiProvider
from database import UsersRepository, db_connect

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
        model = await UsersRepository(db).get_llm_model(user_id)
    return _get_provider(model)


async def set_llm(user_id: int, model: str):
    """Сохраняет выбор модели пользователя."""
    async with db_connect() as db:
        await UsersRepository(db).set_llm_model(user_id, model)


__all__ = ["LLMProvider", "LLMResponse", "get_llm", "set_llm"]
