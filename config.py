import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "36781759"))
API_HASH = os.getenv("API_HASH", "31a2abacece3f047a878d001aa3fbd95")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_ID = int(os.getenv("DEV_ID", "7367921416"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEV_USERNAME = "aakaaz"
DEV_CHANNEL = "sa00cr"
DEV_BIO = "ساترن"
