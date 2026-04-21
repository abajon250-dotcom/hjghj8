import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")
DB_NAME = os.getenv("DB_NAME", "bot.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")

os.makedirs(SESSIONS_DIR, exist_ok=True)