import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", ""))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_ID = int(os.getenv("DEV_ID", ""))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

_KEYS_STR = os.getenv("GEMINI_KEYS", "")
GEMINI_KEYS = [k.strip() for k in _KEYS_STR.split(",") if k.strip()]

DEV_USERNAME = "aakaaz"
DEV_CHANNEL = "sa00cr"
DEV_BIO = "ساترن"
