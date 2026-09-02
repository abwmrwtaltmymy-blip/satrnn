Enterimport asyncio
import os
import random
import telebot
import re
import time
import logging
import sqlite3
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from telethon import TelegramClient, events, Button, errors, functions, types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import ChannelParticipantsAdmins
from dotenv import load_dotenv
import aiosqlite

# إعداد السجلات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("error.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()


bot_token = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7367921416"))


# استخدام os.getenv مع قيمك الحقيقية كبديل في حال فشل قراءة المتغيرات
api_id_str = os.getenv("API_ID", "12345678") # امسح الأرقام وضع الـ API ID الخاص بك
api_id = int(api_id_str)
import sqlite3

api_hash = os.getenv("API_HASH", "abcdef1234567890") # امسح النص وضع الـ API HASH الخاص بك

bot = TelegramClient("makkster_bot", api_id, api_hash)


# المسارات والأساسيات
DB_NAME = "bot_database.db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
MEDIA_DIR = os.path.join(BASE_DIR, "media") 
MEMORY_FILE = os.path.join(BASE_DIR, "processed_targets.txt")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True) 

DEFAULT_KALISHA = "اوكف اوكف.خلحجيلك مميزات كروبي\nاول شي الحروب مو fري واليرتبط ينحظر اليفشر ينحظر بدون واسطات وكروب ترول وشتبوست 🙏🏿😭"
MESSAGES_LIMIT = 10000
CHECK_ACCOUNT_ID = OWNER_ID

# فئة استخراج API لتيليجرام تلقائياً
class TelegramAPIExtractor:
    def __init__(self, phone):
        self.phone = phone
        self.session = aiohttp.ClientSession()
        self.base_url = "https://my.telegram.org"
        self.random_hash = ""

    async def request_code(self):
        async with self.session.post(f"{self.base_url}/auth/send_password", data={"phone": self.phone}) as resp:
            data = await resp.json()
            self.random_hash = data.get("random_hash")
            return self.random_hash is not None

    async def login(self, code):
        data = {"phone": self.phone, "random_hash": self.random_hash, "password": code}
        async with self.session.post(f"{self.base_url}/auth/login", data=data) as resp:
            return await resp.text() == "true"

    async def extract_api_keys(self):
        async with self.session.get(f"{self.base_url}/apps") as resp:
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            
            api_id_input = soup.find('input', {'name': 'api_id'})
            if api_id_input:
                api_id_val = api_id_input.get('value')
                api_hash_val = soup.find('input', {'name': 'api_hash'}).get('value')
                return int(api_id_val), api_hash_val
            else:
                hash_val = soup.find('input', {'name': 'hash'}).get('value')
                create_data = {
                    "hash": hash_val,
                    "app_title": "BotManagerApp",
                    "app_shortname": f"app{self.phone.replace('+', '')}",
                    "app_url": "",
                    "app_platform": "android",
                    "app_desc": ""
                }
                await self.session.post(f"{self.base_url}/apps/create", data=create_data)
                return await self.extract_api_keys()

    async def close(self):
        await self.session.close()

# إعداد قاعدة البيانات
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'user',
                balance INTEGER DEFAULT 0,
                is_referred BOOLEAN DEFAULT FALSE,
                name TEXT,
                username TEXT,
                date TEXT
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT,
                session_string TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                account_type TEXT DEFAULT 'sender',
                user_id INTEGER,
                name TEXT,
                account_id INTEGER,
                PRIMARY KEY (phone, user_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS blacklisted_groups (
                identifier TEXT PRIMARY KEY
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS report_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT,
                report_type TEXT,
                report_text TEXT
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS owner_chats (
                chat_id INTEGER PRIMARY KEY,
                last_interaction TIMESTAMP
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS progress_tracking (
                task_id TEXT PRIMARY KEY,
                target_entity TEXT,
                last_processed_id INTEGER,
                total_processed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                updated_at TIMESTAMP
            )
        ''')
        await db.commit()

@asynccontextmanager
async def managed_client(session_path, api_id_param, api_hash_param):
    client = TelegramClient(session_path, api_id_param, api_hash_param)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()

# التعامل مع قاعدة البيانات
async def add_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT balance, is_referred FROM users WHERE user_id = ?", (user_id,)) as cursor:
            result = await cursor.fetchone()
            if result:
                return {"balance": result[0], "is_referred": result[1]}
            return None

async def update_balance(user_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ''', (key, str(value)))
        await db.commit()

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

async def add_account_unified(phone: str, session_string: str, account_type: str, user_id: int, name: str, account_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO accounts (phone, session_string, status, account_type, user_id, name, account_id)
            VALUES (?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(phone, user_id) DO UPDATE SET session_string=excluded.session_string, status=excluded.status, name=excluded.name, account_id=excluded.account_id
        ''', (phone, session_string, account_type, user_id, name, account_id))
        await db.commit()

async def get_all_accounts(user_id: int, account_type: str = 'sender'):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT phone, session_string, name, account_id FROM accounts WHERE user_id = ? AND account_type = ? AND status="active"', (user_id, account_type)) as cursor:
            return await cursor.fetchall()

async def delete_account(phone: str, user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('DELETE FROM accounts WHERE phone = ? AND user_id = ?', (phone, user_id))
        await db.commit()

async def set_user_role(user_id: int, role: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO users (user_id, role) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role
        ''', (user_id, role))
        await db.commit()

async def get_user_role(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT role FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 'user'

def clean_account_name(name):
    if not name:
        return "بدون اسم"
    name_lower = name.lower()
    if len(name) > 15 or "t.me" in name_lower or "http" in name_lower or "@" in name_lower:
        return "حساب مساعد (مشبوه)"
    return name.strip()

async def is_authorized(user_id):
    if user_id == OWNER_ID:
        return True
    
    global_free = await get_setting("global_free_mode", "False")
    if global_free == "True":
        return True
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 'trial':
                return True
                
    return False

async def consume_trial(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 'trial':
                await db.execute("UPDATE users SET role = 'user' WHERE user_id = ?", (user_id,))
                await db.commit()
                return True
    return False

async def send_user_list_batches(client, bot_client, chat_id, user_entities, title):
    if not user_entities:
        return
    
    users_list = list(user_entities)
    for i in range(0, len(users_list), 100):
        chunk = users_list[i:i+100]
        lines = ["@sa22cr"]
        
        for u in chunk:
            lines.append(str(u.id))
        
        message_text = "\n".join(lines)
        
        try:
            await client.send_message('me', message_text, parse_mode='md')
        except Exception as e:
            logger.error(f"خطأ أثناء الإرسال للمحفوظات: {e}")
            
        try:
            await bot_client.send_message(chat_id, message_text, parse_mode='md')
        except Exception as e:
            logger.error(f"خطأ أثناء الإرسال للمستخدم: {e}")

def get_progress_bar(current, total, length=15):
    progress = min(current / total, 1.0) if total > 0 else 1.0
    filled = int(progress * length)
    empty = length - filled
    return "⬜" * filled + "⬛" * empty

async def send_with_client(client, target_entity, kalisha_data):
    current_sleep = random.randint(5, 12)
    speed_setting = await get_setting("send_speed")
    if speed_setting and speed_setting.isdigit():
        current_sleep = int(speed_setting)
    await asyncio.sleep(current_sleep)

    try:
        text = kalisha_data.get("text", "")
        media_path = kalisha_data.get("media")

        if kalisha_data.get("mutate", False):
            mutations = [
                f"{text}\n.",
                f"{text} .",
                f"{text}\n‌",  
                f"{text} [{random.randint(100, 999)}]"
            ]
            text = random.choice(mutations)

        if media_path and os.path.exists(media_path):
            await client.send_file(target_entity, media_path, caption=text)
        else:
            await client.send_message(target_entity, text)
        
        temp_dots = await client.send_message(target_entity, "...")
        await asyncio.sleep(1)
        await client.delete_messages(target_entity, [temp_dots.id], revoke=False)
        
        try:
            await client.send_message(CHECK_ACCOUNT_ID, ".")
        except Exception as e:
            logger.error(f"خطأ في حساب الفحص: {e}")
        return True, "success"

    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds + 2)
        return False, f"flood_wait_{e.seconds}"

    except errors.PeerFloodError:
        return False, "peer_flood"

    except errors.UserPrivacyRestrictedError:
        return False, "privacy_closed"

    except Exception as e:
        if "ALLOW_PAYMENT_REQUIRED" in str(e):
            return False, "premium_required"
        logger.error(f"خطأ غير متوقع في الإرسال: {e}")
        return False, "error"

# --- أوامر المالك والتحكم النصية كاملة ---
@bot.on(events.NewMessage(pattern=r"^/set_speed (\d+)$"))
async def set_speed_cmd(event):
    if event.sender_id != OWNER_ID: return
    speed = event.pattern_match.group(1)
    await set_setting("send_speed", speed)
    await event.respond(f"✅ تم تعديل سرعة الإرسال إلى `{speed}` ثانية.")

@bot.on(events.NewMessage(pattern=r"^/set_report_text (.+)$"))
async def set_report_text_cmd(event):
    if event.sender_id != OWNER_ID: return
    text = event.pattern_match.group(1)
    await set_setting("report_text", text)
    await event.respond("✅ تم تعديل كليشة التبليغ بنجاح.")

@bot.on(events.NewMessage(pattern=r"^/set_kalisha$"))
async def set_kalisha_cmd(event):
    if event.sender_id != OWNER_ID: return
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        text = reply_msg.text or reply_msg.message or ""
        await set_setting("kalisha_text", text)
        if reply_msg.media:
            file_path = await reply_msg.download_media(MEDIA_DIR + "/")
            await set_setting("kalisha_media", file_path)
        await event.respond("✅ تم تحديث الكليشة العامة بنجاح.")
    else:
        await event.respond("⚠️ قم بالرد على الرسالة التي تريد اعتمادها كليشة عامة مع الأمر /set_kalisha.")

@bot.on(events.NewMessage(pattern=r"^/set_free (on|off)$"))
async def set_free_cmd(event):
    if event.sender_id != OWNER_ID: return
    status = event.pattern_match.group(1)
    await set_setting("global_free_mode", "True" if status == "on" else "False")
    await event.respond(f"✅ تم تغيير وضع المجاني إلى: `{status}`.")

@bot.on(events.NewMessage(pattern=r"^/set_mutate (on|off)$"))
async def set_mutate_cmd(event):
    if event.sender_id != OWNER_ID: return
    status = event.pattern_match.group(1)
    await set_setting("mutate_kalisha", "True" if status == "on" else "False")
    await event.respond(f"✅ تم تغيير وضع التعديل الطفيف إلى: `{status}`.")

@bot.on(events.NewMessage(incoming=True))
async def ownership_protection_handler(event):
    sender_id = event.sender_id
    if sender_id:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute('''
                INSERT INTO owner_chats (chat_id, last_interaction) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_interaction=excluded.last_interaction
            ''', (sender_id, datetime.now(timezone.utc).isoformat()))
            await db.commit()

    if sender_id == OWNER_ID and event.is_private:
        text = event.raw_text or ""
        if "تحويل ملكية" in text or "transfer ownership" in text.lower():
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT last_interaction FROM owner_chats WHERE chat_id = ?", (OWNER_ID,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        last_time = datetime.fromisoformat(row[0])
                        if datetime.now(timezone.utc) - last_time > timedelta(minutes=15):
                            await event.reply("🚨 تحذير: تم رصد محاولة تحويل ملكية مشبوهة ولم يتم التفاعل مع المالك خلال آخر 15 دقيقة! تم إلغاء العملية تلقائياً.")
                            raise events.StopPropagation

# أمر البدء الرئيسي (/start)
@bot.on(events.NewMessage(pattern=r"^/start(?: (.*))?$"))
async def start_handler(event):
    user = await event.get_sender()
    ref_id = event.pattern_match.group(1)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (event.sender_id,)) as cursor:
            is_new_user = await cursor.fetchone() is None

        if is_new_user:
            await db.execute(
                "INSERT INTO users (user_id, name, username, date) VALUES (?, ?, ?, ?)",
                (
                    event.sender_id,
                    clean_account_name(user.first_name if user else ""),
                    getattr(user, "username", None) or "بدون معرف",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
            )
            await db.commit()

            if ref_id and ref_id.isdigit() and int(ref_id) != event.sender_id:
                ref_id_int = int(ref_id)
                async with db.execute("SELECT balance FROM users WHERE user_id = ?", (ref_id_int,)) as cursor:
                    ref_row = await cursor.fetchone()

                if ref_row:
                    await db.execute("UPDATE users SET balance = balance + 1 WHERE user_id = ?", (ref_id_int,))
                    await db.commit()

                    async with db.execute("SELECT balance FROM users WHERE user_id = ?", (ref_id_int,)) as cursor:
                        new_bal = (await cursor.fetchone())[0]

                    if new_bal % 5 == 0:
                        await db.execute("UPDATE users SET role = 'trial' WHERE user_id = ?", (ref_id_int,))
                        await db.commit()
                        try:
                            await bot.send_message(
                                ref_id_int,
                                "🎉 **مبروك!** لقد قام 5 أشخاص بالدخول للبوت عبر رابط الإحالة الخاص بك.\n\n"
                                "🎁 **تم منحك تجربة مجانية تلقائياً!** يمكنك الآن استخدام البوت لمرة واحدة مجاناً. أرسل /start للبدء."
                            )
                        except Exception as e:
                            logger.error(f"خطأ في إرسال التجربة المجانية: {e}")

            if event.sender_id != OWNER_ID:
                try:
                    await bot.send_message(
                        OWNER_ID,
                        f"🚨 **إشعار: مستخدم جديد قام بتشغيل البوت!**\n\n"
                        f"👤 الاسم: `{clean_account_name(user.first_name if user else '')}`\n"
                        f"🆔 الأيدي: `{event.sender_id}`\n"
                        f"🌐 المعرف: @{getattr(user, 'username', 'لا يوجد')}\n"
                        f"⏰ الوقت: `{datetime.now().strftime('%Y-%m-%d %I:%M %p')}`"
                    )
                except Exception as e:
                    logger.error(f"خطأ في الإشعار: {e}")

    if not await is_authorized(event.sender_id):
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={event.sender_id}"

        await event.respond(
            "❌ **عذراً، أنت لا تملك صلاحية استخدام هذا البوت.**\n\n"
            "💎 **للحصول على الصلاحية لديك خياران:**\n\n"
            "1️⃣ **الاشتراك المدفوع:**\n"
            "راسل المطور للااشتراك وتفعيل البوت في حسابك: @sa22cr\n\n"
            "2️⃣ **التجربة المجانية (نظام الدعوات):**\n"
            "قم بدعوة **5** من أصدقائك لبدء البوت عبر رابطك الخاص. ستحصل على تجربة مجانية تلقائياً عند اكتمال العدد!\n\n"
            f"🔗 **رابط الدعوة الخاص بك:**\n`{ref_link}`\n\n",
            link_preview=False
        )
        return

    buttons = [
        [Button.inline("🔍 خمط الأعضاء (جمع وتصفية)", b"main_scrape_menu")],
        [Button.inline("🔥 الشد التلقائي (الريبورتات)", b"main_report_menu")],
        [Button.inline("➕ إضافة حساب مساعد", b"add_account"), Button.inline("📂 إدارة الحسابات", b"page_accounts_0")]
    ]

    if event.sender_id == OWNER_ID:
        buttons.append([Button.inline("👑 لوحة تحكم المالك", b"owner_panel")])

    await event.respond(
        "👋 **أهلاً بك في بوت الترويج التلقائي المطور**\n\n"
        "▫️ اختر أحد الأوضاع من القائمة أدناه:",
        buttons=buttons
    )

# --- معالجات القوائم والأزرار ---
@bot.on(events.CallbackQuery(data=b"main_scrape_menu"))
async def main_scrape_menu_handler(event):
    if not await is_authorized(event.sender_id): return
    buttons = [
        [Button.inline("🚀 سحب الأعضاء (الوضع أ)", b"mode_scrape")],
        [Button.inline("📨 الإرسال المباشر (الوضع ب)", b"mode_direct")],
        [Button.inline("⚙️ تعديل الكليشة", b"set_kalisha"), Button.inline("🗑️ مسح الميديا", b"del_media")],
        [Button.inline("🔙 رجوع", b"back_start")]
    ]
    await event.edit("🔍 **قسم خمط الأعضاء والترويج**\n\nاختر الوظيفة المطلوبة:", buttons=buttons)

@bot.on(events.CallbackQuery(data=b"main_report_menu"))
async def main_report_menu_handler(event):
    if not await is_authorized(event.sender_id): return
    buttons = [
        [Button.inline("🔗 أضف كروب/قناة للشد", b"report_add_target")],
        [Button.inline("📝 أضف كليشة للبلاغ", b"report_add_text")],
        [Button.inline("⚠️ نوع البلاغ", b"report_set_type")],
        [Button.inline("📩 أضف رابط رسائل للشد عليها", b"report_add_msgs")],
        [Button.inline("▶️ بدء الشد", b"report_start"), Button.inline("⏸️ إيقاف الشد", b"report_stop")],
        [Button.inline("📊 حالة الشد", b"report_status")],
        [Button.inline("🔙 رجوع", b"back_start")]
    ]
    await event.edit("🔥 **قسم الشد التلقائي**\n\nاختر من القائمة أدناه لإعداد الحملة:", buttons=buttons)

@bot.on(events.CallbackQuery(data=b"list_accounts"))
async def list_accounts_handler(event):
    # تحويل مسار إدارة الحسابات القديم إلى الصفحة الأولى
    await paginated_accounts_handler(event)

@bot.on(events.CallbackQuery(data=b"report_set_type"))
async def report_set_type_handler(event):
    if not await is_authorized(event.sender_id): return
    buttons = [
        [Button.inline("🗑 مزعج (سبام)", b"rtype_spam"), Button.inline("🔞 محتوى غير لائق", b"rtype_pornography")],
        [Button.inline("🚨 عنف أو أذى", b"rtype_violence"), Button.inline("👤 حساب مزيف", b"rtype_fake")],
        [Button.inline("👶 إساءة للأطفال", b"rtype_childabuse"), Button.inline("💊 مخدرات", b"rtype_illegal_drugs")],
        [Button.inline("🕵️ تفاصيل شخصية", b"rtype_personal"), Button.inline("©️ حقوق النشر", b"rtype_copyright")],
        [Button.inline("📍 موقع غير ملائم", b"rtype_geo"), Button.inline("❓ أخرى", b"rtype_other")],
        [Button.inline("🔙 رجوع للقائمة", b"main_report_menu")]
    ]
    await event.edit("⚠️ **اختر نوع المخالفة الذي سيتم التبليغ عنه:**", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^rtype_(.*)$"))
async def report_save_type_handler(event):
    if not await is_authorized(event.sender_id): return
    rtype = event.pattern_match.group(1).decode('utf-8')
    await set_setting("report_type", rtype)
    await event.edit(f"✅ **تم تحديد نوع البلاغ بنجاح.**\nالنوع المختار: `{rtype}`", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"add_account"))
async def add_account_handler(event):
    if not await is_authorized(event.sender_id): return
    await event.delete()
    
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📱 **أدخل رقم هاتف الحساب مع رمز الدولة (+964...):**")
        try:
            phone_msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError:
            await conv.send_message("⏳ انتهى وقت الانتظار.")
            return
            
        if phone_msg.text.strip().startswith('/'):
            await conv.send_message("❌ تم الإلغاء.")
            return
            
        phone = "".join(c for c in phone_msg.text if c.isdigit() or c == '+')
        
        await conv.send_message("⏳ جاري إنشاء/استخراج مفاتيح API الخاصة بحسابك...")
        extractor = TelegramAPIExtractor(phone)
        try:
            req_ok = await extractor.request_code()
            if not req_ok:
                await conv.send_message("❌ فشل طلب الكود من my.telegram.org. تأكد من الرقم.")
                await extractor.close()
                return
        except Exception as e:
            await conv.send_message(f"❌ حدث خطأ أثناء الاتصال بموقع تيليجرام: {e}")
            await extractor.close()
            return

        await conv.send_message("📩 **أدخل كود الدخول المرسل لحسابك (الخاص بـ my.telegram.org):**")
        try:
            web_code_msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError:
            await extractor.close()
            return
            
        try:
            login_ok = await extractor.login(web_code_msg.text.strip())
            if not login_ok:
                await conv.send_message("❌ كود الدخول غير صحيح.")
                await extractor.close()
                return
            user_api_id, user_api_hash = await extractor.extract_api_keys()
        except Exception as e:
            await conv.send_message(f"❌ فشل استخراج المفاتيح: {e}")
            await extractor.close()
            return
        finally:
            await extractor.close()
        
        session_name = f"acc_{phone.replace('+', '')}"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        async with managed_client(session_path, user_api_id, user_api_hash) as user_client:
            try:
                send_code = await user_client.send_code_request(phone)
            except Exception as e:
                await conv.send_message(f"❌ حدث خطأ أثناء إرسال الكود: {e}")
                return

            await conv.send_message("📩 **تم إرسال كود التحقق إلى حسابك في تيليجرام. أرسل الكود مع مسافات بين الأرقام:**")
            try:
                code_msg = await conv.get_response(timeout=300)
            except asyncio.TimeoutError:
                return
                
            code = "".join(c for c in code_msg.text if c.isdigit())
            
            try:
                await user_client.sign_in(phone=phone, code=code, phone_code_hash=send_code.phone_code_hash)
            except errors.SessionPasswordNeededError:
                await conv.send_message("🔒 **أدخل كلمة المرور (التحقق بخطوتين):**")
                try:
                    pass_msg = await conv.get_response(timeout=300)
                    password = pass_msg.text.replace(" ", "").strip()
                    await user_client.sign_in(password=password)
                except Exception as e:
                    await conv.send_message(f"❌ فشل تسجيل الدخول بكلمة المرور: {e}")
                    return
            except Exception as e:
                await conv.send_message(f"❌ فشل تسجيل الدخول: {e}")
                return
                
            me = await user_client.get_me()
            safe_name = clean_account_name(me.first_name)
            
            await add_account_unified(phone, session_name, 'sender', event.sender_id, safe_name, me.id)
            await conv.send_message(f"✅ **تم تسجيل دخول الحساب بنجاح!**\n👤 الاسم: {safe_name}\n🆔 الأيدي: `{me.id}`", buttons=[[Button.inline("🔙 رجوع", b"back_start")]])

@bot.on(events.CallbackQuery(data=b"set_kalisha"))
async def set_kalisha_handler(event):
    if not await is_authorized(event.sender_id): return
    await event.delete()
    
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message(
            "📝 **أرسل الكليشة الجديدة الآن:**\n\n"
            "▫️ إذا كنت تريد نصاً فقط، أرسل النص.\n"
            "▫️ إذا كنت تريد إرسال صورة أو فيديو، أرسل الصورة/الفيديو واكتب الكليشة في (الوصف / Caption) الخاص بها.\n\n"
            "*لإلغاء العملية أرسل /cancel*"
        )
        try:
            msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError:
            await conv.send_message("⏳ انتهى وقت الانتظار (5 دقائق). يرجى المحاولة مرة أخرى.")
            return
        
        if msg.text and msg.text.strip().startswith('/'):
            await conv.send_message("❌ تم إلغاء العملية.", buttons=[[Button.inline("🔙 رجوع", b"back_start")]])
            return

        old_media = await get_setting("kalisha_media")
        if old_media and os.path.exists(old_media):
            try: os.remove(old_media)
            except Exception as e: logger.error(f"خطأ في حذف الميديا: {e}")
        
        if msg.media:
            status_msg = await conv.send_message("⏳ جاري حفظ الوسائط، يرجى الانتظار...")
            file_path = await msg.download_media(MEDIA_DIR + "/")
            await set_setting("kalisha_media", file_path)
            await set_setting("kalisha_text", msg.text or "")
            await status_msg.delete()
        else:
            await set_setting("kalisha_media", "")
            await set_setting("kalisha_text", msg.text or "")
            
        current_text = await get_setting("kalisha_text", DEFAULT_KALISHA)
        media_status = "مع وسائط 🖼️/🎥" if msg.media else "نص فقط 📝"
        
        await conv.send_message(
            f"✅ **تم حفظ الكليشة بنجاح!**\n\n"
            f"نوع الكليشة: {media_status}\n"
            f"النص الحالي:\n{current_text}\n\n"
            f"🛡️ **نظام الحماية ضد الحظر التلقائي:**\n"
            f"هل تريد تفعيل ميزة التعديل الطفيف تلقائياً؟",
            buttons=[
                [Button.inline("🟢 تفعيل ميزة التعديل الطفيف", b"mutate_on")],
                [Button.inline("🔴 إرسال النص الأصلي بدون تغيير", b"mutate_off")]
            ]
        )

@bot.on(events.CallbackQuery(data=b"report_add_target"))
async def report_add_target_handler(event):
    if not await is_authorized(event.sender_id): return
    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message(
            "🔗 **أرسل الرابط أو اليوزر للمجموعة أو القناة أو الحساب المستهدف.**\n\n"
            "*لإلغاء العملية أرسل /cancel*"
        )
        try: msg = await conv.get_response(timeout=120)
        except asyncio.TimeoutError: return
        
        if msg.text.strip().startswith('/'):
            await conv.send_message("❌ تم الإلغاء.", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])
            return
            
        await set_setting("report_target", msg.text.strip())
        await conv.send_message("✅ **تم حفظ الهدف بنجاح وسرية.**", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"report_add_text"))
async def report_add_text_handler(event):
    if not await is_authorized(event.sender_id): return
    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📝 **أرسل كليشة البلاغ التي ستستخدمها الحسابات داخلياً:**\n\n*لإلغاء العملية أرسل /cancel*")
        try: msg = await conv.get_response(timeout=120)
        except asyncio.TimeoutError: return
        
        if msg.text.strip().startswith('/'): return
            
        await set_setting("report_text", msg.text.strip())
        await conv.send_message("✅ **تم حفظ كليشة البلاغ بسريّة تامة.**", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"report_add_msgs"))
async def report_add_msgs_handler(event):
    if not await is_authorized(event.sender_id): return
    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message(
            "📩 **أرسل روابط الرسائل التي تريد الشد عليها مباشرة (رابط في كل سطر):**\n"
            "*لإلغاء العملية أرسل /cancel*"
        )
        try: msg = await conv.get_response(timeout=120)
        except asyncio.TimeoutError: return
        
        if msg.text.strip().startswith('/'):
            await conv.send_message("❌ تم الإلغاء.", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])
            return
            
        messages_list = [line.strip() for line in msg.text.splitlines() if line.strip().startswith("http")]
        await set_setting("report_messages", "\n".join(messages_list))
        await conv.send_message(f"✅ **تم حفظ {len(messages_list)} رسالة للشد عليها.**", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"owner_add_rep_acc"))
async def owner_add_rep_acc_handler(event):
    if event.sender_id != OWNER_ID: return
    await event.delete()
    
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📱 **أدخل رقم هاتف حساب الشد مع رمز الدولة (+964...):**")
        try:
            phone_msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError:
            await conv.send_message("⏳ انتهى وقت الانتظار.")
            return

        if phone_msg.text.strip().startswith('/'):
            await conv.send_message("❌ تم الإلغاء.")
            return
            
        phone = "".join(c for c in phone_msg.text if c.isdigit() or c == '+')
        session_name = f"rep_acc_{phone.replace('+', '')}"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        async with managed_client(session_path, api_id, api_hash) as user_client:
            try:
                send_code = await user_client.send_code_request(phone)
            except Exception as e:
                await conv.send_message(f"❌ حدث خطأ أثناء إرسال الكود: {e}")
                return

            await conv.send_message("📩 **تم إرسال كود التحقق إلى حسابك. أرسل الكود مع مسافات بين الأرقام:**")
            try:
                code_msg = await conv.get_response(timeout=300)
            except asyncio.TimeoutError:
                await conv.send_message("⏳ انتهى وقت الانتظار.")
                return
                
            if code_msg.text.strip().startswith('/'):
                await conv.send_message("❌ تم الإلغاء.")
                return
                
            code = "".join(c for c in code_msg.text if c.isdigit())
            
            try:
                await user_client.sign_in(phone=phone, code=code, phone_code_hash=send_code.phone_code_hash)
            except errors.SessionPasswordNeededError:
                await conv.send_message("🔒 **أدخل كلمة المرور (التحقق بخطوتين):**")
                try:
                    pass_msg = await conv.get_response(timeout=300)
                except asyncio.TimeoutError:
                    await conv.send_message("⏳ انتهى وقت الانتظار.")
                    return
                password = pass_msg.text.replace(" ", "").strip()
                try:
                    await user_client.sign_in(password=password)
                except Exception as e:
                    await conv.send_message(f"❌ فشل تسجيل الدخول بكلمة المرور: {e}")
                    return
            except Exception as e:
                await conv.send_message(f"❌ فشل تسجيل الدخول: {e}")
                return
                
            me = await user_client.get_me()
            safe_name = clean_account_name(me.first_name)
            
            await add_account_unified(phone, session_name, 'report', event.sender_id, safe_name, me.id)
            await conv.send_message(f"✅ **تم تسجيل دخول حساب الشد بنجاح!**\n👤 الاسم: {safe_name}\n🆔 الأيدي: `{me.id}`", buttons=[[Button.inline("🔙 رجوع لللوحة", b"owner_panel")]])

@bot.on(events.CallbackQuery(data=b"owner_list_rep_acc"))
async def owner_list_rep_acc_handler(event):
    if event.sender_id != OWNER_ID: return
    rep_accs = await get_all_accounts(event.sender_id, 'report')
    await event.edit(f"📂 **عدد حسابات الشد المتوفرة حالياً:** `{len(rep_accs)}` حساب.", buttons=[[Button.inline("🔙 رجوع", b"owner_panel")]])

@bot.on(events.CallbackQuery(data=b"report_status"))
async def report_status_handler(event):
    is_rep = await get_setting("is_reporting", "False")
    status = "🟢 فعّال (يتم الشد حالياً)" if is_rep == 'True' else "🔴 متوقف"
    count = await get_setting("report_count", "0")
    await event.edit(f"📊 **حالة الشد التلقائي:**\n\nالحالة: {status}\nعدد البلاغات المرسلة حتى الآن: `{count}`", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"report_stop"))
async def report_stop_handler(event):
    await set_setting("is_reporting", "False")
    await event.edit("⏸️ **تم إرسال أمر إيقاف الشد. ستتوقف الحسابات تدريجياً.**", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])

@bot.on(events.CallbackQuery(data=b"report_start"))
async def report_start_handler(event):
    target = await get_setting("report_target")
    msgs = await get_setting("report_messages")
    if not target and not msgs:
        await event.answer("⚠️ لم تقم بإضافة هدف أو روابط رسائل للشد!", alert=True)
        return
        
    await set_setting("is_reporting", "True")
    await event.edit("▶️ **بدأت عملية الشد التلقائي في الخلفية...**", buttons=[[Button.inline("🔙 رجوع", b"main_report_menu")]])
    
    asyncio.create_task(run_reporting_loop(event.sender_id))

async def report_single_client(client, target_entity, report_reason, report_text, is_message=False, msg_ids=None):
    try:
        if is_message and msg_ids:
            await client(functions.messages.ReportRequest(
                target_entity,
                msg_ids,
                report_reason,
                report_text
            ))
            return len(msg_ids)
        else:
            await client(ReportPeerRequest(
                target_entity,
                report_reason,
                report_text
            ))
            return 1
    except errors.FloodWaitError as e:
        logger.warning(f"FloodWaitError على حساب {getattr(client, 'account_name', '')}: {e.seconds} ثانية")
        await asyncio.sleep(e.seconds + 2)
        return 0
    except Exception as e:
        logger.error(f"خطأ في البلاغ من حساب {getattr(client, 'account_name', '')}: {e}")
        return 0

async def run_reporting_loop(user_id):
    rep_accs = await get_all_accounts(user_id, 'report')
    if not rep_accs:
        rep_accs = await get_all_accounts(OWNER_ID, 'report')
        
    if not rep_accs:
        await set_setting("is_reporting", "False")
        logger.warning("لا توجد حسابات مضافة للشد. تم الإيقاف.")
        return

    rtype = await get_setting("report_type", "spam")
    reasons = {
        "violence": types.InputReportReasonViolence(),
        "pornography": types.InputReportReasonPornography(),
        "fake": types.InputReportReasonFake(),
        "childabuse": types.InputReportReasonChildAbuse(),
        "illegal_drugs": types.InputReportReasonIllegalDrugs(),
        "personal": types.InputReportReasonPersonalDetails(),
        "copyright": types.InputReportReasonCopyright(),
        "geo": types.InputReportReasonGeoIrrelevant(),
        "other": types.InputReportReasonOther()
    }
    report_reason = reasons.get(rtype, types.InputReportReasonSpam())

    while True:
        is_rep = await get_setting("is_reporting", "False")
        if is_rep != 'True':
            break
            
        report_text = await get_setting("report_text", "")
        raw_msgs = await get_setting("report_messages", "")
        report_target = await get_setting("report_target", "")
        
        clients = []
        for phone, session_string, name, account_id in rep_accs:
            session_path = os.path.join(SESSIONS_DIR, session_string)
            try:
                client = TelegramClient(session_path, api_id, api_hash)
                await client.connect()
                if await client.is_user_authorized():
                    client.account_name = name or 'حساب بدون اسم'
                    clients.append(client)
            except Exception as e:
                logger.error(f"خطأ بتشغيل حساب الشد ({name}): {e}")

        if not clients:
            await set_setting("is_reporting", "False")
            logger.error("كل حسابات الشد فشل الاتصال بها، تم إيقاف الحملة.")
            break

        tasks = []
        if raw_msgs:
            lines = raw_msgs.splitlines()
            targets_dict = {}
            for link in lines:
                match_pub = re.search(r"t\.me/([^/]+)/(\d+)", link)
                match_priv = re.search(r"t\.me/c/(\d+)/(\d+)", link)
                if match_pub and match_pub.group(1) != 'c':
                    peer = match_pub.group(1)
                    msg_id = int(match_pub.group(2))
                    targets_dict.setdefault(peer, []).append(msg_id)
                elif match_priv:
                    peer = int("-100" + match_priv.group(1))
                    msg_id = int(match_priv.group(2))
                    targets_dict.setdefault(peer, []).append(msg_id)

            for client in clients:
                for peer, msg_ids in targets_dict.items():
                    try:
                        entity = await client.get_input_entity(peer)
                        tasks.append(report_single_client(client, entity, report_reason, report_text, is_message=True, msg_ids=msg_ids))
                    except Exception as e:
                        logger.error(f"خطأ في استخراج الكيان للرسائل: {e}")
        elif report_target:
            for client in clients:
                try:
                    entity = await client.get_input_entity(report_target)
                    tasks.append(report_single_client(client, entity, report_reason, report_text, is_message=False))
                except Exception as e:
                    logger.error(f"خطأ في استخراج الكيان للهدف: {e}")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total_added = sum(r for r in results if isinstance(r, int))
            if total_added > 0:
                curr = int(await get_setting("report_count", "0"))
                await set_setting("report_count", str(curr + total_added))

        for c in clients:
            try: await c.disconnect()
            except Exception: pass

        await asyncio.sleep(random.randint(15, 30))

@bot.on(events.CallbackQuery(pattern=r"^mutate_(on|off)$"))
async def toggle_mutate_callback(event):
    if not await is_authorized(event.sender_id): return
    choice = event.data.decode().split("_")[-1]
    await set_setting("mutate_kalisha", "True" if choice == "on" else "False")
    
    status_msg = "🟢 مفعّلة (سيتم حماية الرسائل عبر التعديل الطفيف)" if choice == "on" else "🔴 معطّلة (سيتم إرسال الرسائل متطابقة تماماً)"
    await event.edit(
        f"⚙️ **تم تحديث إعدادات الكليشة بنجاح!**\n\n"
        f"الحماية ضد التكرار المتطابق: {status_msg}",
        buttons=[[Button.inline("🔙 القائمة الرئيسية", b"back_start")]]
    )

@bot.on(events.CallbackQuery(data=b"del_media"))
async def del_media_handler(event):
    if not await is_authorized(event.sender_id): return
    old_media = await get_setting("kalisha_media")
    if old_media and os.path.exists(old_media):
        try: os.remove(old_media)
        except Exception as e: logger.error(f"خطأ في حذف الميديا: {e}")
    await set_setting("kalisha_media", "")
    await event.answer("✅ تم مسح الصورة/الفيديو بنجاح. سيتم إرسال النص فقط.", alert=True)

# دالة التحكم بالصفحات لقائمة الحسابات المضافة
def generate_pagination_buttons(data_list, current_page, items_per_page, callback_prefix):
    total_pages = (len(data_list) + items_per_page - 1) // items_per_page
    start_idx = current_page * items_per_page
    end_idx = start_idx + items_per_page
    
    buttons = []
    for item in data_list[start_idx:end_idx]:
        buttons.append([Button.inline(f"❌ {item['name']}", f"{callback_prefix}_{item['phone']}")])
        
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(Button.inline("⬅️ السابق", f"page_accounts_{current_page - 1}"))
    if current_page < total_pages - 1:
        nav_buttons.append(Button.inline("التالي ➡️", f"page_accounts_{current_page + 1}"))
        
    if nav_buttons:
        buttons.append(nav_buttons)
        
    return buttons

@bot.on(events.CallbackQuery(pattern=r"^page_accounts_(\d+)$"))
async def paginated_accounts_handler(event):
    if not await is_authorized(event.sender_id): return
    page = int(event.pattern_match.group(1))
    accounts = await get_all_accounts(event.sender_id, 'sender')
    
    formatted_accounts = [{"name": name, "phone": phone} for phone, _, name, _ in accounts]
    buttons = generate_pagination_buttons(formatted_accounts, page, 5, "del_acc")
    buttons.append([Button.inline("🔙 رجوع", b"back_start")])
    
    await event.edit(f"📂 **قائمة الحسابات (صفحة {page + 1}):**", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^del_acc_.*$"))
async def delete_account_handler(event):
    if not await is_authorized(event.sender_id): return
    phone = event.data.decode().replace("del_acc_", "")
    buttons = [
        [Button.inline("✅ نعم، متأكد من الحذف", f"confirm_del_{phone}".encode())],
        [Button.inline("❌ إلغاء والتراجع", b"page_accounts_0")]
    ]
    await event.edit(f"⚠️ **هل أنت متأكد أنك تريد حذف هذا الحساب؟**", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^confirm_del_.*$"))
async def confirm_delete_account_callback(event):
    if not await is_authorized(event.sender_id): return
    phone = event.data.decode().replace("confirm_del_", "")
    await delete_account(phone, event.sender_id)
    await event.edit(f"✅ تم حذف الحساب بنجاح من قائمتك.", buttons=[[Button.inline("📂 رجوع للقائمة", b"page_accounts_0")]])

@bot.on(events.CallbackQuery(data=b"mode_scrape"))
async def mode_scrape_handler(event):
    if not await is_authorized(event.sender_id): return
    owner_accounts = await get_all_accounts(OWNER_ID, 'sender')
    if not owner_accounts:
        await event.edit("❌ يجب إضافة حساب فحص ثابت من قبل المطور في البوت.", buttons=[[Button.inline("🔙 رجوع", b"back_start")]])
        return

    selected_acc = owner_accounts[0]
    await event.delete()
    
    async with bot.conversation(event.chat_id) as conv:
        session_path = os.path.join(SESSIONS_DIR, selected_acc[1])
        async with managed_client(session_path, api_id, api_hash) as client:
            blocked_users = set()
            status_msg = await conv.send_message("⏳ **جاري قراءة المحادثات السابقة لحساب الفحص لمنع التكرار...**")
            try:
                async for d in client.iter_dialogs():
                    if d.is_user and d.entity:
                        blocked_users.add(d.entity.id)
            except Exception as e:
                logger.error(f"خطأ في قراءة المحادثات: {e}")
            
            await status_msg.edit("🤔 **هل تريد استثناء محادثات حساب آخر مضاف في البوت؟ (نعم/لا)**")
            try: ex_choice = await conv.get_response(timeout=300)
            except asyncio.TimeoutError: return
                
            if ex_choice.text.strip() == "نعم":
                accounts = await get_all_accounts(event.sender_id, 'sender')
                if len(accounts) > 0:
                    msg_acc = "🔢 **اختر الحساب الذي تريد استخدامه للجمع:**\n\n"
                    for idx, (p, s, name, aid) in enumerate(accounts):
                        if aid: msg_acc += f"**{idx+1}.** [{name}](tg://user?id={aid})\n"
                        else: msg_acc += f"**{idx+1}.** {name}\n"

                    await conv.send_message(msg_acc)
                    try:
                        ex_num = await conv.get_response(timeout=300)
                        ex_acc = accounts[int(ex_num.text.strip()) - 1]
                        ex_path = os.path.join(SESSIONS_DIR, ex_acc[1])
                        async with managed_client(ex_path, api_id, api_hash) as ex_client:
                            async for d in ex_client.iter_dialogs():
                                if d.is_user and d.entity: blocked_users.add(d.entity.id)
                        await conv.send_message("✅ تم دمج محادثات الحساب الإضافي في قائمة التجاهل.")
                    except Exception as e:
                        logger.error(f"خطأ جلب محادثات الحساب الإضافي: {e}")

            await conv.send_message("🎯 **أرسل الآن رابط أو يوزر المجموعة المستهدفة لجمع الأعضاء منها:**")
            try: group_msg = await conv.get_response(timeout=300)
            except asyncio.TimeoutError: return
                
            group_input = group_msg.text.strip()

            try:
                if "+" in group_input or "joinchat" in group_input:
                    hash_val = group_input.split("/")[-1].replace("+", "").replace("joinchat/", "")
                    await client(ImportChatInviteRequest(hash_val))
                else:
                    await client(JoinChannelRequest(group_input))
            except Exception:
                pass

            try:
                target_group = await client.get_entity(group_input)
                group_title = getattr(target_group, 'title', 'المجموعة المستهدفة')
            except Exception as e:
                await conv.send_message(f"❌ فشل الوصول للمجموعة: {e}")
                return

            admins = set()
            try:
                async for admin in client.iter_participants(target_group, filter=ChannelParticipantsAdmins):
                    admins.add(admin.id)
            except Exception:
                pass

            senders = {}
            count = 0
            progress_msg = await conv.send_message("⏳ **جاري البدء بجمع الأعضاء المتفاعلين...**")
            three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

            async for msg in client.iter_messages(target_group, limit=MESSAGES_LIMIT):
                count += 1
                if msg and msg.sender_id:
                    uid = msg.sender_id
                    if uid == target_group.id: continue
                        
                    if uid not in admins and uid not in blocked_users:
                        if uid not in senders:
                            try:
                                u_entity = await client.get_entity(uid)
                                if not getattr(u_entity, 'deleted', False) and not getattr(u_entity, 'bot', False):
                                    status = getattr(u_entity, 'status', None)
                                    if isinstance(status, types.UserStatusOffline):
                                        if status.was_online and status.was_online.replace(tzinfo=timezone.utc) >= three_days_ago:
                                            senders[uid] = u_entity
                                    elif status and not isinstance(status, types.UserStatusEmpty):
                                        senders[uid] = u_entity
                            except Exception:
                                pass

                if count % 100 == 0 or count == MESSAGES_LIMIT:
                    bar = get_progress_bar(count, MESSAGES_LIMIT)
                    try:
                        await progress_msg.edit(
                            f"⚡ **جاري فحص رسائل المجموعة المستهدفة**\n\n"
                            f"{bar} ({int((count/MESSAGES_LIMIT)*100)}%)\n\n"
                            f"📥 تم فحص: `{count}` / `{MESSAGES_LIMIT}` رسالة\n"
                            f"👥 تم صيد: `{len(senders)}` عضو متفاعل ونشط"
                        )
                    except Exception: pass

            if not senders:
                await conv.send_message("❌ لم يتم العثور على أعضاء مطابقين للشروط في هذه المجموعة.")
                return

            await conv.send_message(f"✅ **تم جمع {len(senders)} عضو بنجاح!**\nجاري إرسال القوائم مقسمة...")
            await send_user_list_batches(client, bot, event.chat_id, senders.values(), f"أعضاء {group_title}")
            await consume_trial(event.sender_id)
            await conv.send_message("✨ **انتهت عملية التصفية والأرشفة.**")

@bot.on(events.CallbackQuery(data=b"mode_direct"))
async def mode_direct_handler(event):
    if not await is_authorized(event.sender_id): return
    accounts = await get_all_accounts(event.sender_id, 'sender')
    if not accounts:
        await event.edit("❌ لا توجد حسابات مضافة خاصة بك للإرسال.", buttons=[[Button.inline("➕ إضافة حساب", b"add_account")]])
        return

    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        msg_acc = "🔢 <b>اختر رقم الحساب الذي سينفذ الإرسال المباشر:</b>\n\n"
        for idx, (p, s, name, aid) in enumerate(accounts):
            if aid: msg_acc += f"<b>{idx+1}.</b> <a href='tg://user?id={aid}'>{name}</a>\n"
            else: msg_acc += f"<b>{idx+1}.</b> {name}\n"
                
        await conv.send_message(msg_acc, parse_mode='html')
        try: acc_choice = await conv.get_response(timeout=300)
        except asyncio.TimeoutError: return
            
        try: sender_acc = accounts[int(acc_choice.text.strip()) - 1]
        except (ValueError, IndexError):
            await conv.send_message("❌ اختيار غير صحيح.")
            return

        await conv.send_message("📋 **أرسل الآن قائمة اليوزرات أو الايديات:**")
        try: users_msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError: return
        
        raw_text = users_msg.text
        target_ids = set()
        for username in re.findall(r"@([a-zA-Z0-9_]{5,32})", raw_text): target_ids.add(username)
        for uid in re.findall(r"id=(\d+)", raw_text): target_ids.add(int(uid))
        for line in raw_text.splitlines():
            line_clean = line.strip()
            if line_clean.isdigit() and len(line_clean) > 5: target_ids.add(int(line_clean))

        if not target_ids:
            await conv.send_message("❌ لم يتم التعرف على أي يوزرات صالحة.")
            return

        await conv.send_message(f"🚀 **تم رصد `{len(target_ids)}` هدف.** بدء حملة الإرسال...")
        
        session_path = os.path.join(SESSIONS_DIR, sender_acc[1])
        async with managed_client(session_path, api_id, api_hash) as client:
            kalisha_data = {
                "text": await get_setting("kalisha_text", DEFAULT_KALISHA),
                "media": await get_setting("kalisha_media"),
                "mutate": (await get_setting("mutate_kalisha", "False") == "True")
            }

            success_count = 0
            error_count = 0
            
            for idx, target in enumerate(list(target_ids)):
                try: entity = await client.get_entity(target)
                except Exception:
                    error_count += 1
                    continue

                success, status = await send_with_client(client, entity, kalisha_data)
                if success: success_count += 1
                else: error_count += 1

            await consume_trial(event.sender_id)
            await conv.send_message(f"🏁 **انتهت حملة الإرسال المباشر بنجاح!**\n✅ ناجح: `{success_count}` | ❌ أخطاء: `{error_count}`")

# لوحة المالك الأساسية
@bot.on(events.CallbackQuery(data=b"owner_panel"))
async def owner_panel_handler(event):
    if event.sender_id != OWNER_ID: return
    is_free_mode = (await get_setting("global_free_mode", "False") == "True")
    free_mode_status = "مفعل 🟢" if is_free_mode else "معطل 🔴"
    
    buttons = [
        [Button.inline(f"🔓 البوت للجميع ({free_mode_status})", b"owner_toggle_free")],
        [Button.inline("➕ إضافة حساب للشد", b"owner_add_rep_acc"), Button.inline("📂 حسابات الشد الحالية", b"owner_list_rep_acc")],
        [Button.inline("🔙 رجوع للقائمة الرئيسية", b"back_start")]
    ]
    
    await event.edit(f"👑 **لوحة تحكم المالك الأساسي**\n\n🌐 وضع المجاني للكل: `{free_mode_status}`", buttons=buttons)

@bot.on(events.CallbackQuery(data=b"owner_toggle_free"))
async def owner_toggle_free_handler(event):
    if event.sender_id != OWNER_ID: return
    current_status = (await get_setting("global_free_mode", "False") == "True")
    new_status_str = "False" if current_status else "True"
    await set_setting("global_free_mode", new_status_str)
    
    new_status_txt = "مفتوح مجاناً للجميع 🟢" if new_status_str == "True" else "مغلق (بالاشتراك فقط) 🔴"
    await event.edit(f"✅ **تم تغيير حالة البوت بنجاح!**\n\nالوضع الحالي: {new_status_txt}", buttons=[[Button.inline("🔙 رجوع للوحة المالك", b"owner_panel")]])

# تتبع التقدم وإدارة الاستئناف
async def save_progress(task_id: str, target: str, last_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO progress_tracking (task_id, target_entity, last_processed_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET last_processed_id=excluded.last_processed_id, updated_at=excluded.updated_at
        ''', (task_id, target, last_id, datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_progress(task_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT last_processed_id FROM progress_tracking WHERE task_id = ?", (task_id,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

# معالج الأخطاء الشامل لـ Telethon
def handle_telethon_errors(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except errors.FloodWaitError as e:
            logger.warning(f"تم تفعيل حظر Flood. الانتظار لـ {e.seconds} ثانية.")
            await asyncio.sleep(e.seconds)
            return await wrapper(*args, **kwargs)
        except errors.AuthKeyUnregisteredError:
            logger.error("الجلسة تالفة أو تم تسجيل الخروج منها.")
            return None
        except Exception as e:
            logger.error(f"خطأ غير متوقع في {func.__name__}: {e}")
            return None
    return wrapper

@bot.on(events.CallbackQuery(data=b"back_start"))
async def back_start_handler(event):
    await start_handler(event)


from telethon import events, Button

# =========================================================
# 👑 كود أوامر ولوحة تحكم المالك الأساسي (Telethon)
# =========================================================

# تأكد من تعريف OWNER_ID في بداية الملف أو استدعائه من متغيرات البيئة:
# OWNER_ID = int(os.getenv("OWNER_ID", 123456789))

# متغيرات افتراضية للحفظ (يمكنك ربطها بملف json أو داتابيز لديك)
bot_settings = {
    "speed": 1,
    "report_text": "بلاغ",
    "kalisha": "",
    "free_mode": False,
    "mutate_mode": False
}


# 1️⃣ أمر تحديد سرعة الإرسال (/set_speed)
@bot.on(events.NewMessage(pattern=r'^/set_speed (\d+)'))
async def set_speed_handler(event):
    if event.sender_id != OWNER_ID:
        return
    speed = event.pattern_match.group(1)
    bot_settings["speed"] = int(speed)
    await event.reply(f"✅ تم تعديل سرعة الإرسال إلى `{speed}` ثانية.")


# 2️⃣ أمر تعيين نص التبليغ (/set_report_text)
@bot.on(events.NewMessage(pattern=r'^/set_report_text (.+)'))
async def set_report_text_handler(event):
    if event.sender_id != OWNER_ID:
        return
    text = event.pattern_match.group(1)
    bot_settings["report_text"] = text
    await event.reply("✅ تم تعديل كليشة التبليغ بنجاح.")


# 3️⃣ أمر تعيين الكليشة العامة (/set_kalisha)
@bot.on(events.NewMessage(pattern=r'^/set_kalisha'))
async def set_kalisha_handler(event):
    if event.sender_id != OWNER_ID:
        return
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        bot_settings["kalisha"] = reply_msg.text
        await event.reply("✅ تم تحديث الكليشة العامة بنجاح.")
    else:
        await event.reply("⚠️ قم بالرد على الرسالة التي تريد اعتمادها كليشة عامة مع الأمر /set_kalisha.")


# 4️⃣ أمر تفعيل/إلغاء الوضع المجاني للكل (/set_free)
@bot.on(events.NewMessage(pattern=r'^/set_free (on|off)'))
async def set_free_handler(event):
    if event.sender_id != OWNER_ID:
        return
    status = event.pattern_match.group(1).lower()
    bot_settings["free_mode"] = (status == "on")
    await event.reply(f"✅ تم تغيير وضع المجاني إلى: `{status}`.")


# 5️⃣ أمر تفعيل/إلغاء التعديل الطفيف (/set_mutate)
@bot.on(events.NewMessage(pattern=r'^/set_mutate (on|off)'))
async def set_mutate_handler(event):
    if event.sender_id != OWNER_ID:
        return
    status = event.pattern_match.group(1).lower()
    bot_settings["mutate_mode"] = (status == "on")
    await event.reply(f"✅ تم تغيير وضع التعديل الطفيف إلى: `{status}`.")


# 6️⃣ نظام حماية الملكية التلقائي عند رصد كلمة تحويل ملكية بالخاص
@bot.on(events.NewMessage(pattern=r'(?i)(تحويل ملكية|transfer ownership)'))
async def transfer_ownership_protection(event):
    if event.is_private and event.sender_id != OWNER_ID:
        await event.reply("🚨 تحذير: تم رصد محاولة تحويل ملكية مشبوهة ولم يتم التفاعل مع المالك خلال آخر 15 دقيقة! تم إلغاء العملية تلقائياً.")


# 7️⃣ فتح لوحة تحكم المالك عند الضغط على زر اللوحة
@bot.on(events.CallbackQuery(data=b'owner_panel'))
async def owner_panel_handler(event):
    if event.sender_id != OWNER_ID:
        await event.answer("⚠️ هذه اللوحة مخصصة للمالك فقط!", alert=True)
        return
    
    free_status_text = "مفعل 🟢" if bot_settings.get("free_mode") else "معطل 🔴"
    text = f"👑 **لوحة تحكم المالك الأساسي**\n\n🌐 وضع المجاني للكل: `{free_status_text}`"
    
    buttons = [
        [Button.inline("🔄 تغيير وضع المجاني", data=b'owner_toggle_free')],
        [Button.inline("🔙 العودة للقائمة", data=b'main_menu')]
    ]
    await event.edit(text, buttons=buttons)


# 8️⃣ زر تبديل الوضع المجاني مباشرة من لوحة التحكم
@bot.on(events.CallbackQuery(data=b'owner_toggle_free'))
async def owner_toggle_free_handler(event):
    if event.sender_id != OWNER_ID:
        await event.answer("⚠️ مخصص للمالك فقط!", alert=True)
        return
    
    # عكس حالة الوضع المجاني
    bot_settings["free_mode"] = not bot_settings.get("free_mode", False)
    
    if bot_settings["free_mode"]:
        new_status_txt = "مفتوح مجاناً للجميع 🟢"
    else:
        new_status_txt = "مغلق (بالاشتراك فقط) 🔴"
        
    await event.answer("تم تغيير الحالة بنجاح!")
    await event.edit(f"✅ **تم تغيير حالة البوت بنجاح!**\n\nالوضع الحالي: {new_status_txt}", buttons=[
        [Button.inline("🔙 رجوع للوحة المالك", data=b'owner_panel')]
    ])

from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# جلب الإعدادات والتوكنات مع قيم افتراضية احتياطية
BOT_TOKEN = os.getenv("BOT_TOKEN", "8912932417:AAEFhUSx6xQ_LappuPA3fGYytOKY0FDdEpQ")
OWNER_ID = int(os.getenv("OWNER_ID", 7367921416)) # ضع آيدي المالك الخاص بك هنا

bot = telebot.TeleBot(BOT_TOKEN)
DB_NAME = "bot_database.db"

# تهيئة قاعدة البيانات والجداول المطلوبة
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # جدول المستخدمين
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            phone_number TEXT,
            api_id TEXT,
            api_hash TEXT,
            role TEXT DEFAULT 'user'
        )
    ''')
    # جدول الاشتراك الإجباري
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS forced_subs (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT
        )
    ''')
    conn.commit()
    conn.close()

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    user_exists = cursor.fetchone()
    
    if not user_exists:
        cursor.execute('INSERT INTO users (user_id, role) VALUES (?, ?)', (user_id, 'user'))
        conn.commit()
        
        try:
            bot.send_message(OWNER_ID, f"🚨 **مستخدم جديد دخل للبوت!**\n\n👤 الاسم: {username}\n🆔 الآيدي: `{user_id}`", parse_mode="Markdown")
        except Exception as e:
            pass
            
    conn.close()

    # 1. إنشاء لوحة المفاتيح الرئيسية للخدمات
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("بدء الاستخراج 🚀", "خدمات البوت ⚙️")
    
    # 2. إظهار زر لوحة الأدمن فقط إذا كان المستخدم هو المالك
    if user_id == OWNER_ID:
        markup.add("👑 لوحة التحكم")

    bot.reply_to(message, "أهلاً بك في البوت! 🌟\nاختر من الأزرار التالية للبدء:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "خدمات البوت ⚙️" or message.text == "بدء الاستخراج")
def restricted_service(message):
    # الكود سيعمل للجميع مباشرة بدون المطالبة بأي رقم
    bot.reply_to(message, "مرحباً بك في الخدمة المطلوبة جاري التنفيذ...")


# 4. أوامر المالك: إدارة الاشتراك الإجباري
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != OWNER_ID:
        return
    
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ اضافة قناة/مجموعة", "➖ حذف قناة/مجموعة")
    markup.add("📋 القنوات المضافة", "رجوع")
    bot.reply_to(message, "مرحباً بك في لوحة تحكم المالك:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "📋 القنوات المضافة" and message.from_user.id == OWNER_ID)
def list_forced_subs(message):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, chat_name FROM forced_subs')
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        bot.reply_to(message, "لا توجد أي قنوات أو مجموعات مضافة حالياً للاشتراك الإجباري.")
        return
        
    text = "📋 **قنوات ومجموعات الاشتراك الإجباري:**\n\n"
    for r in rows:
        text += f"📌 الاسم: {r[1]} \n🆔 المعرف: `{r[0]}`\n-------------------\n"
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "➕ اضافة قناة/مجموعة" and message.from_user.id == OWNER_ID)
def add_sub_step(message):
    msg = bot.reply_to(message, "أرسل معرف القناة أو المجموعة (مثال: `@ChannelUsername` أو الآيدي) مع اسم لها بهذا الشكل:\n`ID OR Username | Name`", parse_mode="Markdown")
    bot.register_next_step_handler(msg, save_new_sub)

def save_new_sub(message):
    if message.from_user.id != OWNER_ID:
        return
    try:
        parts = message.text.split('|')
        chat_id = parts[0].strip()
        chat_name = parts[1].strip() if len(parts) > 1 else "قناة/مجموعة"
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO forced_subs (chat_id, chat_name) VALUES (?, ?)', (chat_id, chat_name))
        conn.commit()
        conn.close()
        
        bot.reply_to(message, f"✅ تمت الإضافة بنجاح:\n{chat_name} ({chat_id})")
    except Exception as e:
        bot.reply_to(message, f"❌ حدث خطأ في الصيغة. تأكد من استخدام الرمز `|`\n الخطأ: {e}", parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "➖ حذف قناة/مجموعة" and message.from_user.id == OWNER_ID)
def remove_sub_step(message):
    msg = bot.reply_to(message, "أرسل معرف القناة أو المجموعة المراد حذفها فقط (مثال: `@ChannelUsername`):")
    bot.register_next_step_handler(msg, delete_sub)

def delete_sub(message):
    if message.from_user.id != OWNER_ID:
        return
    chat_id = message.text.strip()
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM forced_subs WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    
    bot.reply_to(message, f"🗑️ تم حذف المعرف `{chat_id}` من قائمة الاشتراك الإجباري.", parse_mode="Markdown")

if __name__ == '__main__':
    init_db()
    print("البوت يعمل الآن بكفاءة... 🚀")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
