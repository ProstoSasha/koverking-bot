"""Настройки читаются из файла .env (см. .env.example) или из переменных окружения."""
import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Не заполнена настройка {name}. Открой файл .env и впиши её (см. README.md).")
    return value


BOT_TOKEN = _need("BOT_TOKEN")
MANAGER_ID = int(_need("MANAGER_ID"))      # ваш числовой Telegram ID: сюда приходят заявки
SHEET_ID = _need("SHEET_ID")
# Ключ Google: на компьютере — файл credentials.json,
# на хостинге — переменная GOOGLE_CREDS_JSON с содержимым этого файла (её читает sheets.py)
CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Kaliningrad"))
MANAGER_USERNAME = "koverking_manager"
# Адрес студии и текст «Как нас найти» — в texts.py (HOW_TO_FIND)
