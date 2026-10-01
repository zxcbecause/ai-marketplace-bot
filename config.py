from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from dotenv import load_dotenv

# Дополнительные переменные (пути, ID склада) читаются через os.getenv в модулях.
load_dotenv(Path(__file__).parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: str
    admin_id: int

    # LLM
    deepseek_api_key: str
    deepseek_model: str = "deepseek-v4-flash"
    openai_api_key: str
    openai_model: str = "gpt-5.4"

    # Image
    gemini_api_key: str

    # Search
    exa_api_key: str
    searxng_url: str = "http://127.0.0.1:8888"

    # Ozon Seller API
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    ozon_api_key_2: str = ""  # второй ключ для хэштегов / параллельных операций

    # Cloudflare R2 (хостинг фото для Ozon API)
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_url: str = ""

    # Wildberries Seller API
    wb_api_key: str = ""
    wb_api_key_readonly: str = ""  # только Content:Read — для массовых проверок без риска задеть write-лимиты основного ключа

    # Hailuo/MiniMax — генерация видеообложек WB
    minimax_api_key: str = ""
    kie_api_key: str = ""  # реселлер, меньший порог пополнения

    # Exa: временный рубильник экономии (07.07.2026). False — все Exa-запросы
    # отключены (возвращают пустой результат, $0), контекст батчей идёт с
    # WB-карточек (WB-first). Вернуть поиск: EXA_ENABLED=true в .env или тут.
    exa_enabled: bool = False

    # Gemini Vision batch-валидация фото кандидатов (03.08.2026): вместо
    # N отдельных вызовов на кандидата (каждый пересылает ~9700-символьный
    # промпт правил заново) — 2 batch-вызова (по половине кандидатов),
    # промпт правил пересылается 1 раз на вызов. Экономия ~60-70% input
    # токенов на выборе главного фото. False — старый надёжный поштучный
    # путь. Включить: GEMINI_VISION_BATCH=true в .env.
    gemini_vision_batch: bool = False

    # Storage
    db_path: str = "./data/bot.db"
    log_path: str = "./data/bot.log"


settings = Settings()
