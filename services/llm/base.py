import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

_BANNED_WORDS_RE = re.compile(
    r"\b(оригинал|оригинальный|оригинальная|оригинальное|оригинальные|"
    r"оригинального|оригинальному|оригинальным|оригинальной|оригинальных|оригинальными)\b",
    re.IGNORECASE,
)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    provider: str = ""

    def __post_init__(self):
        self.text = _BANNED_WORDS_RE.sub("", self.text).strip()


@dataclass
class Message:
    role: str   # "user" | "assistant" | "system"
    content: str


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        user_message: str,
        system_prompt: str,
        few_shot: list[tuple[str, str]] | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = True,
        thinking_budget: int | None = None,
    ) -> LLMResponse:
        """
        few_shot: список пар (user_text, assistant_text) — примеры до основного запроса.

        max_tokens: жёсткий потолок ответа для коротких задач (номер категории,
        ДА/НЕТ, одна строка названия). None — без лимита, для задач с заведомо
        длинным ответом (описание, характеристики).

        enable_thinking: DeepSeek v4 Flash по умолчанию — reasoning-модель,
        рассуждение (reasoning_content) списывается из ТОГО ЖЕ output-бюджета,
        что и финальный ответ (content). Найдено 28.07.2026: wb_create_name
        (просили одну строку до 60 символов) в среднем тратила ~2000
        output-токенов на рассуждение перед ответом. ВАЖНО — max_tokens без
        enable_thinking=False опасен: модель может исчерпать лимит НА
        рассуждении и вернуть пустой content (finish_reason='length',
        content='', проверено живьём). enable_thinking=False убирает
        рассуждение целиком (content приходит сразу, completion_tokens ~1-5
        на простое да/нет) — использовать вместе с небольшим max_tokens
        только для задач с коротким однозначным ответом. Провайдеры, не
        поддерживающие переключение (OpenAI) — просто игнорируют аргумент.

        thinking_budget: ПОТОЛОК размышления в токенах (Gemini) — средний
        вариант между «думай сколько хочешь» (None) и «не думай вовсе»
        (enable_thinking=False). Для задач, где рассуждение реально нужно,
        но разгоняется непропорционально. Замер 01.09.2026 на wb_create_chars
        (сопоставление 56-81 поля справочника с текстом): без лимита
        2138-3629 токенов размышления при 438-627 токенах ответа; при
        budget=512 — те же ключи без брака и в 2.7-4.8x дешевле, а полное
        отключение (0) теряло до 30% заполненных полей и ломало ключи
        («*Описание» вместо «Описание» — такой ключ молча отбрасывается
        при сборке карточки). enable_thinking=False имеет приоритет над
        этим параметром. Провайдеры без поддержки — игнорируют.
        """
        ...
