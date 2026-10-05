"""Общие настройки тестов: фиктивные ключи, чтобы config.Settings импортировался без .env."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

for key, value in {
    "BOT_TOKEN": "123:test",
    "ADMIN_ID": "1",
    "DEEPSEEK_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "EXA_API_KEY": "",
}.items():
    os.environ.setdefault(key, value)
