# config.py
import os
from dotenv import load_dotenv
from pytz import timezone

# Загружаем переменные из .env файла
load_dotenv()

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env файле")

# ID авторизованных пользователей (админов)
AUTHORIZED_USER_IDS = {int(x.strip()) for x in os.getenv("AUTHORIZED_USER_IDS", "").split(",") if x.strip()}
if not AUTHORIZED_USER_IDS:
    raise ValueError("AUTHORIZED_USER_IDS не установлены в .env файле")

# Часовой пояс (по умолчанию UTC)
TIMEZONE = timezone(os.getenv("TIMEZONE", "UTC"))

# Путь к базе данных
DATABASE_PATH = os.getenv("DATABASE_PATH", "scheduled_messages.db")
