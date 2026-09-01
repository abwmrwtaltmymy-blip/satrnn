import asyncio
import os
import random
import re
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, errors, functions, types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    UserStatusOffline,
    UserStatusRecently,
    UserStatusOnline,
    InputReportReasonSpam,
    InputReportReasonViolence,
    InputReportReasonPornography,
    InputReportReasonChildAbuse,
    InputReportReasonOther
)

# 1. إعداد نظام التسجيل (Logging)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("error.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7367921416"))
CHECK_ACCOUNT_ID = OWNER_ID

DB_NAME = "bot_database.db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
MEDIA_DIR = os.path.join(BASE_DIR, "media")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

DEFAULT_KALISHA = "اوكف اوكف.خلحجيلك مميزات كروبي\nاول شي الحروب مو free واليرتبط ينحظر اليفشر ينحظر بدون واسطات وكروب ترول وشتبوست 🙏🏿😭"
MESSAGES_LIMIT = 10000

bot = TelegramClient("makkster_bot", API_ID, API_HASH)

# 2. تهيئة قاعدة البيانات الأحافية (SQLite فقط)
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            name TEXT, 
            username TEXT, 
            join_date TEXT, 
            is_authorized BOOLEAN DEFAULT 0, 
            trial_used BOOLEAN DEFAULT 0)''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY, 
            session_string TEXT NOT NULL, 
            owner_id INTEGER, 
            name TEXT, 
            acc_id INTEGER, 
            type TEXT DEFAULT 'direct')''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, 
            value TEXT)''')
            
        await db.execute('''CREATE TABLE IF NOT EXISTS report_targets (
            target TEXT PRIMARY KEY, 
            msg_ids TEXT DEFAULT '')''')
            
        await db.execute('''CREATE TABLE IF NOT EXISTS owner_chats (
            user_id INTEGER PRIMARY KEY, 
            last_activity REAL)''')
            
        await db.execute('''CREATE TABLE IF NOT EXISTS referrals (
            ref_id INTEGER, 
            user_id INTEGER, 
            PRIMARY KEY(ref_id, user_id))''')
            
        await db.commit()
        
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('kalisha_text', ?)", (DEFAULT_KALISHA,))
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_reason', 'spam')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_text', 'Spam and harmful account')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('speed', '10')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_free_mode', 'False')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mutate_kalisha', 'False')")
        await db.commit()

# --- أدوات التعامل مع قاعدة البيانات ---
async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
        await db.commit()

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

async def is_authorized(user_id: int):
    if user_id == OWNER_ID:
        return True
    if await get_setting('global_free_mode') == 'True':
        return True
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT is_authorized, trial_used FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and (row[0] or not row[1]):
                return True
    return False

# 3. مدير سياق الجلسات الموثوق
@asynccontextmanager
async def get_client_session(session_name):
    session_path = os.path.join(SESSIONS_DIR, session_name)
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()

def clean_account_name(name):
    if not name:
        return "بدون اسم"
    name_lower = name.lower()
    if len(name) > 15 or "t.me" in name_lower or "http" in name_lower or "@" in name_lower:
        return "حساب مساعد"
    return name.strip()

def get_progress_bar(current, total, length=12):
    if total <= 0:
        return "⬛" * length
    progress = min(current / total, 1.0)
    filled = int(progress * length)
    return "🟩" * filled + "⬛" * (length - filled)

# 4. إبقاء الاتصال حياً وحماية الجلسة
async def keep_alive_dots(client):
    try:
        while True:
            for _ in range(3):
                try:
                    msg = await client.send_message(CHECK_ACCOUNT_ID, ".")
                    await client.delete_messages(CHECK_ACCOUNT_ID, [msg.id], revoke=True)
                except Exception:
                    pass
                await asyncio.sleep(1)
            await asyncio.sleep(300)
    except asyncio.CancelledError:
        pass

# 5. دالة الإرسال مع المعالجة الكاملة لـ FloodWaitError
async def send_with_client(client, target_entity, kalisha_data, speed):
    current_sleep = max(1.0, float(speed) + random.uniform(-1.5, 1.5))
    await asyncio.sleep(current_sleep)

    try:
        text = kalisha_data.get("text", "")
        media_path = kalisha_data.get("media")

        if kalisha_data.get("mutate") == 'True':
            suffixes = [f"\n.", f" .", f"\n‌", f" [{random.randint(100, 999)}]", " ✨", " 👋"]
            text = text + random.choice(suffixes)

        if media_path and media_path != 'None' and os.path.exists(media_path):
            await client.send_file(target_entity, media_path, caption=text)
        else:
            await client.send_message(target_entity, text)
        
        try:
            temp = await client.send_message(target_entity, "...")
            await client.delete_messages(target_entity, [temp.id], revoke=False)
        except Exception:
            pass
            
        return True, "success"

    except errors.FloodWaitError as e:
        logger.warning(f"FloodWait encountered: {e.seconds} seconds required.")
        await asyncio.sleep(e.seconds + 2)
        return False, f"flood_{e.seconds}"
    except errors.PeerFloodError:
        return False, "peer_flood"
    except errors.UserPrivacyRestrictedError:
        return False, "privacy_restricted"
    except Exception as e:
        if "ALLOW_PAYMENT_REQUIRED" in str(e):
            return False, "premium_required"
        logger.error(f"Send error to target: {e}")
        return False, "error"

# 6. الأوامر النصية المباشرة
@bot.on(events.NewMessage(pattern=r"^/set_speed (\d+)"))
async def set_speed_cmd(event):
    if not await is_authorized(event.sender_id):
        return
    speed = event.pattern_match.group(1)
    await set_setting('speed', speed)
    await event.reply(f"⚡ **تم تحديث سرعة الإرسال إلى {speed} ثانية.**")

@bot.on(events.NewMessage(pattern=r"^/set_report_text (.*)"))
async def set_report_text_cmd(event):
    if not await is_authorized(event.sender_id):
        return
    text = event.pattern_match.group(1)
    await set_setting('report_text', text)
    await event.reply("📝 **تم تحديث نص التبليغات.**")

@bot.on(events.NewMessage(pattern=r"^/set_kalisha (.*)"))
async def set_kalisha_cmd(event):
    if not await is_authorized(event.sender_id):
        return
    text = event.pattern_match.group(1)
    await set_setting('kalisha_text', text)
    await event.reply("📜 **تم تحديث الكليشة العامة.**")

@bot.on(events.NewMessage(pattern=r"^/set_free (on|off)"))
async def set_free_cmd(event):
    if event.sender_id != OWNER_ID:
        return
    val = 'True' if event.pattern_match.group(1) == 'on' else 'False'
    await set_setting('global_free_mode', val)
    await event.reply(f"🌐 **تم {'تفعيل' if val == 'True' else 'إيقاف'} الوضع المجاني العام.**")

@bot.on(events.NewMessage(pattern=r"^/set_mutate (on|off)"))
async def set_mutate_cmd(event):
    if not await is_authorized(event.sender_id):
        return
    val = 'True' if event.pattern_match.group(1) == 'on' else 'False'
    await set_setting('mutate_kalisha', val)
    await event.reply(f"🔀 **تم {'تفعيل' if val == 'True' else 'إيقاف'} التعديل العشوائي للكليشة.**")

# 7. نظام حماية ومراقبة النشاط
@bot.on(events.NewMessage(incoming=True))
async def protection_handler(event):
    if event.is_private and event.sender_id == OWNER_ID:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute('INSERT OR REPLACE INTO owner_chats (user_id, last_activity) VALUES (?, ?)', (event.chat_id, time.time()))
            await db.commit()

# 8. الواجهة الرئيسية
@bot.on(events.NewMessage(pattern=r"^/start(?: (.*))?$"))
async def start_handler(event):
    user = await event.get_sender()
    user_id = event.sender_id
    ref_id = event.pattern_match.group(1)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,)) as cursor:
            is_new = await cursor.fetchone() is None
            
        if is_new:
            await db.execute('''INSERT INTO users (user_id, name, username, join_date) VALUES (?, ?, ?, ?)''', 
                             (user_id, clean_account_name(user.first_name if user else ""), getattr(user, 'username', 'بدون معرف'), str(datetime.now())))
            if ref_id and ref_id.isdigit() and int(ref_id) != user_id:
                try:
                    await db.execute('INSERT INTO referrals (ref_id, user_id) VALUES (?, ?)', (int(ref_id), user_id))
                    async with db.execute('SELECT COUNT(*) FROM referrals WHERE ref_id = ?', (int(ref_id),)) as ref_cursor:
                        ref_count = (await ref_cursor.fetchone())[0]
                    if ref_count % 5 == 0:
                        await db.execute('UPDATE users SET trial_used = 0 WHERE user_id = ?', (int(ref_id),))
                        await bot.send_message(int(ref_id), "🎉 **مبروك! انضم 5 مستخدمين عن طريق رابطك. تم تجديد التجربة المجانية!**")
                except Exception as e:
                    logger.error(f"Referral logic error: {e}")
            await db.commit()
            
            if user_id != OWNER_ID:
                try:
                    await bot.send_message(OWNER_ID, f"🔔 **مستخدم جديد دخل البوت:**\nالاسم: {user.first_name}\nالأيدي: `{user_id}`")
                except Exception:
                    pass

    if not await is_authorized(user_id):
        bot_info = await bot.get_me()
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute('SELECT COUNT(*) FROM referrals WHERE ref_id = ?', (user_id,)) as cursor:
                my_refs = (await cursor.fetchone())[0]
                
        await event.respond(
            f"🚫 **حسابك غير مفعل.**\n\n🔗 **رابط الدعوة الخاص بك:**\n`https://t.me/{bot_info.username}?start={user_id}`\n"
            f"📊 **عدد الدعوات الحالية:** `{my_refs}/5` (ادعُ 5 أصدقاء للتفعيل المجاني)",
            link_preview=False
        )
        return

    await show_main_menu(event)

async def show_main_menu(event):
    user_id = event.sender_id
    buttons = [
        [Button.inline("🔍 قسم الخمط والترويج", b"main_scrape_menu")],
        [Button.inline("🔥 قسم الشد التلقائي (الريبورتات)", b"main_report_menu")],
        [Button.inline("➕ إضافة حساب مساعد", b"add_acc_direct"), Button.inline("📂 إدارة الحسابات", b"list_accounts")]
    ]
    if user_id == OWNER_ID:
        buttons.append([Button.inline("👑 لوحة تحكم المالك", b"owner_panel")])
        
    text = "🤖 **أهلاً بك في نظام الترويج والشد التلقائي المطور**\nاختر من القائمة أدناه للبدء:"
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)

@bot.on(events.CallbackQuery(data=b"back_start"))
async def back_start_handler(event):
    await show_main_menu(event)

# 9. قائمة الخمط والترويج
@bot.on(events.CallbackQuery(data=b"main_scrape_menu"))
async def main_scrape_menu_handler(event):
    if not await is_authorized(event.sender_id):
        return
    buttons = [
        [Button.inline("🚀 سحب الأعضاء النشطين (الوضع أ)", b"mode_scrape")],
        [Button.inline("📨 الإرسال المباشر للقائمة (الوضع ب)", b"mode_direct")],
        [Button.inline("⚙️ تعديل الكليشة", b"set_kalisha_ui"), Button.inline("🗑️ مسح الميديا", b"del_media")],
        [Button.inline("🔙 رجوع", b"back_start")]
    ]
    await event.edit("🔍 **قسم سحب الأعضاء والإرسال التلقائي:**", buttons=buttons)

# 10. قائمة الشد التلقائي (الريبورتات)
@bot.on(events.CallbackQuery(data=b"main_report_menu"))
async def main_report_menu_handler(event):
    if not await is_authorized(event.sender_id):
        return
        
    reason = await get_setting('report_reason', 'spam')
    text = await get_setting('report_text', 'Spam')
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT COUNT(*) FROM report_targets') as cur:
            target_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id = ? AND type = 'report'", (event.sender_id,)) as cur:
            acc_count = (await cur.fetchone())[0]

    buttons = [
        [Button.inline("🎯 إضافة أهداف (يوزرات/روابط)", b"add_report_target"), Button.inline("🗑️ تنظيف الأهداف", b"clear_report_targets")],
        [Button.inline(f"⚙️ السبب الحالي: [{reason}]", b"change_report_reason")],
        [Button.inline("➕ إضافة حساب شد", b"add_acc_report"), Button.inline("🚀 إطلاق الحملة", b"report_start")],
        [Button.inline("🔙 رجوع", b"back_start")]
    ]
    
    msg_text = (
        f"🔥 **قسم الشد والتطير التلقائي**\n\n"
        f"📊 **الأهداف المحددة:** `{target_count}`\n"
        f"📱 **حسابات الشد المجهزة:** `{acc_count}`\n"
        f"📝 **كليشة البلاغ:** `{text}`"
    )
    await event.edit(msg_text, buttons=buttons)

# 11. إضافة الحسابات الموحدة
@bot.on(events.CallbackQuery(pattern=r"^add_acc_(direct|report)$"))
async def unified_add_account_handler(event):
    if not await is_authorized(event.sender_id):
        return
    acc_type = event.pattern_match.group(1).decode('utf-8')
    await event.delete()
    
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📱 **أرسل رقم هاتف الحساب مع المفتاح الدولي (مثال: +9647800000000):**\n*أرسل /cancel للإلغاء.*")
        try:
            phone_msg = await conv.get_response(timeout=180)
        except asyncio.TimeoutError:
            await conv.send_message("⏰ **انتهت المهلة.**")
            return
            
        if phone_msg.text.strip().startswith('/'):
            await conv.send_message("❌ **تم الإلغاء.**")
            return
            
        phone = "".join(c for c in phone_msg.text if c.isdigit() or c == '+')
        session_name = f"{acc_type}_{phone.replace('+', '')}"
        
        async with get_client_session(session_name) as user_client:
            try:
                send_code = await user_client.send_code_request(phone)
            except Exception as e:
                await conv.send_message(f"❌ **فشل إرسال كود التحقق:** {e}")
                return

            await conv.send_message("📩 **تم إرسال كود التحقق. أرسله الآن مفصولاً بمسافات (مثال: 1 2 3 4 5):**")
            try:
                code_msg = await conv.get_response(timeout=180)
            except asyncio.TimeoutError:
                await conv.send_message("⏰ **انتهت المهلة.**")
                return
                
            if code_msg.text.strip().startswith('/'):
                await conv.send_message("❌ **تم الإلغاء.**")
                return
                
            code = "".join(c for c in code_msg.text if c.isdigit())
            
            try:
                await user_client.sign_in(phone=phone, code=code, phone_code_hash=send_code.phone_code_hash)
            except errors.SessionPasswordNeededError:
                await conv.send_message("🔒 **الحساب محمي بالتحقق بخطوتين. أرسل كلمة المرور مفصولة بمسافات:**")
                try:
                    pass_msg = await conv.get_response(timeout=180)
                except asyncio.TimeoutError:
                    await conv.send_message("⏰ **انتهت المهلة.**")
                    return
                if pass_msg.text.strip().startswith('/'):
                    await conv.send_message("❌ **تم الإلغاء.**")
                    return
                try:
                    await user_client.sign_in(password=pass_msg.text.replace(" ", "").strip())
                except Exception as e:
                    await conv.send_message(f"❌ **فشل التحقق بخطوتين:** {e}")
                    return
            except Exception as e:
                await conv.send_message(f"❌ **فشل تسجيل الدخول:** {e}")
                return
                
            me = await user_client.get_me()
            safe_name = clean_account_name(me.first_name)
            
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO accounts (phone, session_string, owner_id, name, acc_id, type) VALUES (?, ?, ?, ?, ?, ?)', 
                    (phone, session_name, event.sender_id, safe_name, me.id, acc_type)
                )
                await db.commit()
            
            await conv.send_message(f"✅ **تم إضافة الحساب بنجاح!**\n👤 **الاسم:** {safe_name}\n🏷️ **النوع:** {acc_type}")

# 12. عرض وإدارة الحسابات المربوطة
@bot.on(events.CallbackQuery(data=b"list_accounts"))
async def list_accounts_handler(event):
    if not await is_authorized(event.sender_id):
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone, name, type FROM accounts WHERE owner_id = ?", (event.sender_id,)) as cur:
            accounts = await cur.fetchall()
            
    if not accounts:
        buttons = [[Button.inline("🔙 رجوع", b"back_start")]]
        await event.edit("📭 **لا توجد حسابات مربطوة حالياً.**", buttons=buttons)
        return

    msg = "📂 **قائمة الحسابات المربوطة:**\n\n"
    buttons = []
    for phone, name, acc_type in accounts:
        msg += f"• `{phone}` | {name} | [{acc_type}]\n"
        buttons.append([Button.inline(f"❌ حذف {phone}", f"del_acc_{phone}".encode('utf-8'))])
        
    buttons.append([Button.inline("🔙 رجوع", b"back_start")])
    await event.edit(msg, buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^del_acc_(.+)$"))
async def delete_account_handler(event):
    if not await is_authorized(event.sender_id):
        return
    phone = event.pattern_match.group(1).decode('utf-8')
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT session_string FROM accounts WHERE phone = ?", (phone,)) as cur:
            row = await cur.fetchone()
            if row:
                session_path = os.path.join(SESSIONS_DIR, f"{row[0]}.session")
                if os.path.exists(session_path):
                    try:
                        os.remove(session_path)
                    except Exception:
                        pass
        await db.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
        await db.commit()
        
    await event.answer("✅ تم حذف الحساب بنجاح", alert=True)
    await list_accounts_handler(event)

# 13. خوارزمية سحب الأعضاء النشطين والتصفية
@bot.on(events.CallbackQuery(data=b"mode_scrape"))
async def mode_scrape_handler(event):
    if not await is_authorized(event.sender_id):
        return
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT session_string FROM accounts WHERE owner_id = ? AND type = 'direct'", (OWNER_ID,)) as cur:
            owner_accs = await cur.fetchall()
            
    if not owner_accs:
        await event.edit("❌ **لا يوجد حساب مساعد رئيسي للفحص. يرجى إضافة حساب من قبل المالك أولاً.**", buttons=[[Button.inline("🔙 رجوع", b"main_scrape_menu")]])
        return

    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("🎯 **أرسل رابط أو معرف المجموعة المستهدفة (مثال: @group_username أو رابط الدعوة):**")
        try:
            group_msg = await conv.get_response(timeout=180)
        except asyncio.TimeoutError:
            await conv.send_message("⏰ **انتهت المهلة.**")
            return
            
        group_input = group_msg.text.strip()
        progress_msg = await conv.send_message("⏳ **جاري الانضمام وفحص الأعضاء...**")
        
        async with get_client_session(owner_accs[0][0]) as client:
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
            except Exception as e:
                await conv.send_message(f"❌ **فشل الوصول للمجموعة:** {e}")
                return

            active_users = []
            count = 0
            three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

            async for msg in client.iter_messages(target_group, limit=MESSAGES_LIMIT):
                count += 1
                if msg.sender_id and msg.sender_id != target_group.id:
                    try:
                        u = await client.get_entity(msg.sender_id)
                        if isinstance(u, types.User) and not u.deleted and not u.bot:
                            is_active = False
                            if isinstance(u.status, UserStatusOnline) or isinstance(u.status, UserStatusRecently):
                                is_active = True
                            elif isinstance(u.status, UserStatusOffline) and u.status.was_online:
                                if u.status.was_online.replace(tzinfo=timezone.utc) >= three_days_ago:
                                    is_active = True
                            
                            if is_active and u.id not in [x.id for x in active_users]:
                                active_users.append(u)
                    except Exception:
                        pass
                
                if count % 250 == 0:
                    try:
                        await progress_msg.edit(f"⏳ **تم فحص {count} رسالة...**\n👥 **الأعضاء النشطون المكتشفون:** `{len(active_users)}`")
                    except Exception:
                        pass

            if not active_users:
                await conv.send_message("❌ **لم يتم العثور على أعضاء نشطين مطابقين للشروط.**")
                return

            formatted_list = [f"@{u.username}" if u.username else f"tg://user?id={u.id}" for u in active_users]
            
            # حفظ النتائج في ملف وإرساله
            filename = f"members_{target_group.id}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(formatted_list))
                
            await conv.send_message(
                f"✅ **تمت الفلترة بنجاح!**\n"
                f"📊 **إجمالي الرسائل المحللة:** `{count}`\n"
                f"👤 **الأعضاء النشطون خلال آخر 3 أيام:** `{len(active_users)}`",
                file=filename
            )
            if os.path.exists(filename):
                os.remove(filename)

# 14. الترويج والتوجيه المباشر
@bot.on(events.CallbackQuery(data=b"mode_direct"))
async def mode_direct_handler(event):
    if not await is_authorized(event.sender_id):
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT session_string, name FROM accounts WHERE owner_id = ? AND type = 'direct'", (event.sender_id,)) as cur:
            accounts = await cur.fetchall()
            
    if not accounts:
        await event.edit("❌ **لا توجد حسابات ترويج مضافة.**", buttons=[[Button.inline("➕ إضافة حساب", b"add_acc_direct")]])
        return

    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📋 **أرسل قائمة المستهدفين (يوزرات أو معرفات أو أيديات):**")
        try:
            users_msg = await conv.get_response(timeout=300)
        except asyncio.TimeoutError:
            await conv.send_message("⏰ **انتهت المهلة.**")
            return
            
        raw_text = users_msg.text
        targets = list(set(re.findall(r"@([a-zA-Z0-9_]+)", raw_text) + [int(x) for x in re.findall(r"id=(\d+)", raw_text) if x.isdigit()]))
        
        if not targets:
            await conv.send_message("❌ **لم يتم العثور على أهداف صالحة.**")
            return

        kalisha_text = await get_setting('kalisha_text', DEFAULT_KALISHA)
        kalisha_media = await get_setting('kalisha_media')
        mutate = await get_setting('mutate_kalisha')
        speed = float(await get_setting('speed', '10'))
        
        kalisha_data = {"text": kalisha_text, "media": kalisha_media, "mutate": mutate}
        
        status_msg = await conv.send_message(f"🚀 **بدء الحملة...**\n الأهداف: `{len(targets)}`")
        success_count = 0
        fail_count = 0
        
        async with get_client_session(accounts[0][0]) as client:
            keep_alive = asyncio.create_task(keep_alive_dots(client))
            
            for idx, target in enumerate(targets, start=1):
                try:
                    entity = await client.get_entity(target)
                    ok, reason = await send_with_client(client, entity, kalisha_data, speed)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.error(f"Target resolution error: {e}")

                if idx % 3 == 0 or idx == len(targets):
                    bar = get_progress_bar(idx, len(targets))
                    try:
                        await status_msg.edit(
                            f"📊 **حالة الحملة الجارية:**\n"
                            f"{bar} `{idx}/{len(targets)}`\n"
                            f"✅ نجاح: `{success_count}` | ❌ فشل: `{fail_count}`"
                        )
                    except Exception:
                        pass
            
            keep_alive.cancel()
            await conv.send_message(f"🏁 **اكتملت حملة الإرسال!**\n✅ **الناجحة:** `{success_count}`\n❌ **الفاشلة:** `{fail_count}`")

# 15. إدارة كليشة وميديا الإرسال
@bot.on(events.CallbackQuery(data=b"set_kalisha_ui"))
async def set_kalisha_ui_handler(event):
    if not await is_authorized(event.sender_id):
        return
    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📝 **أرسل النص الجديد للكليشة (أو أرسل صورة/فيديو مع شرح):**")
        try:
            msg = await conv.get_response(timeout=180)
        except asyncio.TimeoutError:
            await conv.send_message("⏰ **انتهت المهلة.**")
            return
            
        if msg.media:
            file_path = await msg.download_media(file=MEDIA_DIR + "/")
            await set_setting('kalisha_media', file_path)
            await set_setting('kalisha_text', msg.text or "")
        else:
            await set_setting('kalisha_media', 'None')
            await set_setting('kalisha_text', msg.text or "")
            
        await conv.send_message("✅ **تم تحديث الكليشة والميديا بنجاح.**")

@bot.on(events.CallbackQuery(data=b"del_media"))
async def del_media_handler(event):
    if not await is_authorized(event.sender_id):
        return
    media_path = await get_setting('kalisha_media')
    if media_path and media_path != 'None' and os.path.exists(media_path):
        try:
            os.remove(media_path)
        except Exception:
            pass
    await set_setting('kalisha_media', 'None')
    await event.answer("✅ تم مسح الميديا المرفقة.", alert=True)

# 16. محرك الشد والريبورتات المتوازي
@bot.on(events.CallbackQuery(data=b"add_report_target"))
async def add_report_target_handler(event):
    if not await is_authorized(event.sender_id):
        return
    await event.delete()
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("🎯 **أرسل معرف الحساب/القناة المراد شدها (أو أرسل يوزر مع أيديات رسائل مثلاً: @target 10 11 12):**")
        try:
            msg = await conv.get_response(timeout=180)
        except asyncio.TimeoutError:
            return
            
        parts = msg.text.strip().split()
        target = parts[0]
        msg_ids = ",".join(parts[1:]) if len(parts) > 1 else ""
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO report_targets (target, msg_ids) VALUES (?, ?)", (target, msg_ids))
            await db.commit()
            
        await conv.send_message(f"✅ **تم إضافة الهدف:** `{target}`")

@bot.on(events.CallbackQuery(data=b"clear_report_targets"))
async def clear_report_targets_handler(event):
    if not await is_authorized(event.sender_id):
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM report_targets")
        await db.commit()
    await event.answer("🧹 تم مسح جميع الأهداف.", alert=True)
    await main_report_menu_handler(event)

@bot.on(events.CallbackQuery(data=b"change_report_reason"))
async def change_report_reason_handler(event):
    if not await is_authorized(event.sender_id):
        return
    buttons = [
        [Button.inline("سبام (Spam)", b"set_reason_spam"), Button.inline("عنف (Violence)", b"set_reason_violence")],
        [Button.inline("إباحية (Porn)", b"set_reason_porn"), Button.inline("أطفال (ChildAbuse)", b"set_reason_child")],
        [Button.inline("🔙 رجوع", b"main_report_menu")]
    ]
    await event.edit("⚙️ **اختر سبب التبليغات:**", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=r"^set_reason_(.+)$"))
async def set_reason_pattern_handler(event):
    reason_type = event.pattern_match.group(1).decode('utf-8')
    await set_setting('report_reason', reason_type)
    await event.answer(f"✅ تم ضبط السبب إلى {reason_type}", alert=True)
    await main_report_menu_handler(event)

async def report_worker(session_name, targets, reason_obj, report_text):
    async with get_client_session(session_name) as client:
        for target, msg_ids in targets:
            try:
                entity = await client.get_input_entity(target)
                if msg_ids:
                    ids = [int(x) for x in msg_ids.split(",") if x.isdigit()]
                    await client(functions.messages.ReportRequest(peer=entity, id=ids, reason=reason_obj, message=report_text))
                else:
                    await client(ReportPeerRequest(peer=entity, reason=reason_obj, message=report_text))
                await asyncio.sleep(random.uniform(3.0, 7.0))
            except errors.FloodWaitError as e:
                logger.warning(f"Report worker flood wait: {e.seconds}s")
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                logger.error(f"Report execution failure for {target}: {e}")

@bot.on(events.CallbackQuery(data=b"report_start"))
async def report_start_handler(event):
    if not await is_authorized(event.sender_id):
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT session_string FROM accounts WHERE owner_id = ? AND type = 'report'", (event.sender_id,)) as cur:
            accs = await cur.fetchall()
        async with db.execute("SELECT target, msg_ids FROM report_targets") as cur:
            targets = await cur.fetchall()

    if not accs or not targets:
        await event.answer("❌ تأكد من إضافة حسابات شد وأهداف أولاً!", alert=True)
        return

    await event.edit("🔥 **تم تشغيل حملة الشد التلقائية بنجاح في الخلفية!**")
    
    reason_str = await get_setting('report_reason', 'spam')
    report_text = await get_setting('report_text', 'Violating terms')
    
    reason_map = {
        'spam': InputReportReasonSpam(),
        'violence': InputReportReasonViolence(),
        'porn': InputReportReasonPornography(),
        'child': InputReportReasonChildAbuse()
    }
    reason_obj = reason_map.get(reason_str, InputReportReasonSpam())

    tasks = [report_worker(acc[0], targets, reason_obj, report_text) for acc in accs]
    asyncio.create_task(asyncio.gather(*tasks))

# 17. لوحة تحكم المالك
@bot.on(events.CallbackQuery(data=b"owner_panel"))
async def owner_panel_handler(event):
    if event.sender_id != OWNER_ID:
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts") as cur:
            total_accs = (await cur.fetchone())[0]

    free_mode = await get_setting('global_free_mode', 'False')
    
    buttons = [
        [Button.inline(f"🌐 الوضع المجاني: [{'مفعل' if free_mode == 'True' else 'معطل'}]", b"toggle_free_mode")],
        [Button.inline("➕ إضافة حساب فحص أساسي", b"add_acc_direct")],
        [Button.inline("🔙 رجوع للقائمة الرئيسية", b"back_start")]
    ]
    
    await event.edit(
        f"👑 **لوحة تحكم المالك:**\n\n"
        f"👥 **إجمالي المستخدمين:** `{total_users}`\n"
        f"📱 **إجمالي الحسابات المربوطة:** `{total_accs}`",
        buttons=buttons
    )

@bot.on(events.CallbackQuery(data=b"toggle_free_mode"))
async def toggle_free_mode_handler(event):
    if event.sender_id != OWNER_ID:
        return
    current = await get_setting('global_free_mode', 'False')
    new_val = 'False' if current == 'True' else 'True'
    await set_setting('global_free_mode', new_val)
    await owner_panel_handler(event)

# 18. تشغيل البوت الأساسي
def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    logger.info("Database initialized successfully.")
    bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot started working...")
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
