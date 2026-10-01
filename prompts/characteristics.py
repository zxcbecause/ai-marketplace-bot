"""Промпты скрыты в публичной версии репозитория.

В рабочей версии здесь подробные инструкции для LLM. Ниже заглушки
с теми же именами и плейсхолдерами, чтобы код импортировался и запускался.
"""

CHARACTERISTICS_HEADER = '[скрыто] CHARACTERISTICS_HEADER\n{category}\n{fields}'

CHARACTERISTICS_FALLBACK = '[скрыто] CHARACTERISTICS_FALLBACK'

PACKAGING_PROMPT = '[скрыто] PACKAGING_PROMPT'

KEY_CHARS_PROMPT = '[скрыто] KEY_CHARS_PROMPT'

GAMING_KEY_CHARS_PROMPT = '[скрыто] GAMING_KEY_CHARS_PROMPT'

GAMING_MOUSE_KEY_CHARS_PROMPT = '[скрыто] GAMING_MOUSE_KEY_CHARS_PROMPT'

GAMING_MONITOR_KEY_CHARS_PROMPT = '[скрыто] GAMING_MONITOR_KEY_CHARS_PROMPT'

GAMING_KEY_CHARS_PROMPTS: dict[str, str] = {'Мыши': GAMING_MOUSE_KEY_CHARS_PROMPT, 'Мониторы': GAMING_MONITOR_KEY_CHARS_PROMPT}

SLOGAN_PROMPT = '[скрыто] SLOGAN_PROMPT'

TYPE_MODEL_PROMPT = '[скрыто] TYPE_MODEL_PROMPT'

RICHCONTENT_TIPS_PROMPT = '[скрыто] RICHCONTENT_TIPS_PROMPT'

RICHCONTENT_SLOGAN_PROMPT = '[скрыто] RICHCONTENT_SLOGAN_PROMPT'

WB_NAME_PROMPT = '[скрыто] WB_NAME_PROMPT'

OZON_NAME_PROMPT = '[скрыто] OZON_NAME_PROMPT'

HASHTAG_PROMPT = '[скрыто] HASHTAG_PROMPT\n{product_name}\n{category}\n{key_specs}'
