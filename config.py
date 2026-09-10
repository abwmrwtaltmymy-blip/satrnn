import os
from dotenv import load_dotenv

load_dotenv()

def _get_int(name, default=0):
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default

def _get_str(name, default=""):
    val = os.getenv(name)
    if val is None:
        return default
    return val

API_ID = _get_int("API_ID", 0)
API_HASH = _get_str("API_HASH", "")
BOT_TOKEN = _get_str("BOT_TOKEN", "")
GEMINI_API_KEY = _get_str("GEMINI_API_KEY", "")
DEV_ID = _get_int("DEV_ID", 0)
ADMIN_ID = DEV_ID
