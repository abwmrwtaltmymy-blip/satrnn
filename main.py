from telethon import TelegramClient, events, Button, functions
import asyncio
import time
import random
import json
import os
import shutil
import subprocess
import py_compile

from config import (API_ID, API_HASH, BOT_TOKEN, DEV_ID,
                    DEV_USERNAME, DEV_CHANNEL, DEV_BIO)
from database import (init_db, add_points, get_top, is_banned, ban_group, unban_group,
                      add_force_sub, remove_force_sub, get_force_subs, get_stats,
                      get_all_groups, get_all_users, register_user, update_win_loss,
                      get_setting, set_setting, get_connection)
from utils import (safe_execute, clean_name, clean_name_with_id, name_has_bad_word,
                   require_subscription, evaluate_answers_with_ai, evaluate_winner_points,
                   translate_error, ai_generate_answers, ai_generate_bid, safe_str,
                   safe_display_name, ai_smart_bid, ai_react_to_bid, ai_human_like_delay,
                   ai_comment_on_round, ai_diagnose_issue,
                   extract_function_code, replace_function_code,
                   _parse_gemini_json, _gemini_generate)
from game_manager import (internal_games, tournaments, private_sessions, matchmaking_pool,
                          InternalGame, Tournament, DuelGame, AIGame, get_question)

init_db()

client = TelegramClient("botion", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

ai_games = {}
_last_join_event = {}
DEV_STATE = {}
RESTART_AUTO = {"enabled": False}
AI_CHAT_MODE = {}
AI_AGENT_STATE = {}
AI_AGENT_LOG = []
AI_ASSIST_STATE = {}

EDITABLE_FILES = ["main.py", "utils.py", "questions.py", "answers_bank.py", "game_manager.py", "database.py", "config.py"]
BACKUP_DIR = "file_backups"
EDIT_LOG = "edit_log.json"

os.makedirs(BACKUP_DIR, exist_ok=True)

from config import GEMINI_KEYS, GEMINI_API_KEY
print("GEMINI_KEYS loaded:", len(GEMINI_KEYS), "keys")
print("GEMINI_API_KEY:", GEMINI_API_KEY[:15] + "..." if GEMINI_API_KEY else "EMPTY")

TEAM_NAMES = [
    ("فريق MBC3", "فريق سبيستون"),
    ("فريق المحتوى الهادف", "فريق الشتبوستريه"),
    ("فريق الواعيين", "فريق الترولرية"),
    ("فريق ماين كرافت", "فريق رزدنت ايفل"),
]


def user_link(user_id, name):
    return "[" + name + "](tg://user?id=" + str(user_id) + ")"


def pick_team_names():
    return random.choice(TEAM_NAMES)


def display_group_name(name):
    return safe_display_name(name, "المجموعة")


async def bot_username():
    me = await client.get_me()
    return me.username or ""


async def is_group_admin(event):
    if event.sender_id == DEV_ID:
        return True
    try:
        perms = await client.get_permissions(event.chat_id, event.sender_id)
        return bool(getattr(perms, "is_admin", False))
    except Exception:
        return False


async def notify_dev(text):
    try:
        enabled = get_setting("dev_notifications", "1")
        if enabled != "1":
            return
    except Exception:
        pass
    if not DEV_ID:
        return
    try:
        await client.send_message(DEV_ID, "[إشعار]\n" + text)
    except Exception:
        pass


async def notify_dev_user_start(user):
    try:
        enabled = get_setting("dev_notifications", "1")
        if enabled != "1":
            return
    except Exception:
        pass
    if not DEV_ID:
        return
    uid = getattr(user, "id", None)
    if uid is None:
        return
    if uid == DEV_ID:
        return
    name = safe_str(getattr(user, "first_name", ""), "")
    uname = getattr(user, "username", None)
    uname_str = ("@" + uname) if uname else "بدون يوزر"
    last = getattr(user, "last_name", None)
    full = name + (" " + last if last else "")
    text = ("[إشعار]\nمستخدم جديد فتح البوت.\nالاسم: " + full + "\nاليوزر: " + uname_str + "\nالآيدي: " + str(uid))
    try:
        await client.send_message(DEV_ID, text)
    except Exception:
        pass


async def cancel_round_tasks(game_obj):
    if game_obj is None:
        return
    current = asyncio.current_task()
    for attr in ("bidding_task", "answer_task", "opponent_timeout_task", "answer_watcher_task", "opponent_task"):
        t = getattr(game_obj, attr, None)
        if t and t != current:
            try:
                t.cancel()
            except Exception:
                pass
            try:
                setattr(game_obj, attr, None)
            except Exception:
                pass
    try:
        await asyncio.sleep(0)
    except Exception:
        pass


async def send_to_group(game_obj, chat_id, text, round_level=False, buttons=None):
    try:
        msg = await client.send_message(chat_id, text, buttons=buttons)
        if round_level:
            if not hasattr(game_obj, "round_messages") or game_obj.round_messages is None:
                game_obj.round_messages = []
            game_obj.round_messages.append((chat_id, msg.id))
        return msg
    except Exception as e:
        print("send_to_group error:", str(e)[:200])
        return None


async def delete_round_messages(game_obj):
    if game_obj is None or not hasattr(game_obj, "round_messages"):
        return
    msgs = list(game_obj.round_messages)
    game_obj.round_messages = []
    if not msgs:
        return
    ids_by_chat = {}
    for chat_id, msg_id in msgs:
        ids_by_chat.setdefault(chat_id, []).append(msg_id)
    for chat_id, ids in ids_by_chat.items():
        unique_ids = list(set(ids))
        for i in range(0, len(unique_ids), 100):
            batch = unique_ids[i:i + 100]
            try:
                await client.delete_messages(chat_id, batch)
                await asyncio.sleep(0.2)
            except Exception:
                for mid in batch:
                    try:
                        await client.delete_messages(chat_id, mid)
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass


async def edit_pinned(game_obj, chat_id, text, buttons=None):
    if game_obj is None or not hasattr(game_obj, "pin_msg_id") or not game_obj.pin_msg_id:
        return
    try:
        await client.edit_message(chat_id, game_obj.pin_msg_id, text, buttons=buttons)
    except Exception:
        pass


async def delete_pinned(game_obj, chat_id):
    if game_obj is None or not hasattr(game_obj, "pin_msg_id") or not game_obj.pin_msg_id:
        return
    pid = game_obj.pin_msg_id
    try:
        await client.unpin_message(chat_id, pid)
    except Exception:
        pass
    try:
        await client.delete_messages(chat_id, pid)
    except Exception:
        pass
    game_obj.pin_msg_id = None


async def get_full_bot_context():
    info = []
    try:
        from config import GEMINI_KEYS, GEMINI_API_KEY
        from utils import _gemini_models_working, _gemini_keys_working
        total_keys = len([k for k in GEMINI_KEYS if k and len(k) > 20])
        working = len(_gemini_models_working)
        current = _gemini_models_working[0] if _gemini_models_working else "لا يوجد"
        info.append("=== مفاتيح Gemini ===")
        info.append("عدد المفاتيح الكلي: " + str(total_keys))
        info.append("عدد المفاتيح الشغالة: " + str(working))
        info.append("الموديل المستخدم: " + current)
    except Exception as e:
        info.append("خطأ في جلب المفاتيح: " + str(e)[:100])

    try:
        s = get_stats()
        info.append("")
        info.append("=== الإحصائيات ===")
        info.append("المجموعات: " + str(s["groups"]))
        info.append("اللاعبون: " + str(s["players"]))
        info.append("المستخدمون: " + str(s["users"]))
        info.append("المحظورة: " + str(s["banned"]))
        info.append("قنوات الاشتراك: " + str(s["subs"]))
    except Exception as e:
        info.append("خطأ في الإحصائيات: " + str(e)[:100])

    try:
        info.append("")
        info.append("=== الألعاب النشطة ===")
        info.append("ألعاب داخلية: " + str(len(internal_games)))
        info.append("بطولات: " + str(len(tournaments)))
        info.append("ألعاب AI: " + str(len(ai_games)))
        info.append("جلسات خاصة: " + str(len(private_sessions)))
        info.append("قائمة الانتظار: " + str(len(matchmaking_pool)))
    except Exception as e:
        info.append("خطأ في الألعاب: " + str(e)[:100])

    try:
        info.append("")
        info.append("=== الملفات ===")
        for f in EDITABLE_FILES:
            if os.path.exists(f):
                size = os.path.getsize(f)
                info.append("- " + f + ": " + str(size) + " بايت")
            else:
                info.append("- " + f + ": غير موجود")
    except Exception as e:
        info.append("خطأ في الملفات: " + str(e)[:100])

    try:
        info.append("")
        info.append("=== الإعدادات ===")
        info.append("الإشعارات: " + get_setting("dev_notifications", "1"))
        info.append("التشغيل التلقائي: " + str(RESTART_AUTO.get("enabled", False)))
    except Exception as e:
        info.append("خطأ في الإعدادات: " + str(e)[:100])

    try:
        info.append("")
        info.append("=== القنوات الإجبارية ===")
        subs = get_force_subs()
        if not subs:
            info.append("لا يوجد")
        else:
            for sub in subs:
                info.append("- " + safe_str(sub["username"], "") + " (طلب: " + str(sub["is_request_mode"]) + ")")
    except Exception as e:
        info.append("خطأ في القنوات: " + str(e)[:100])

    try:
        info.append("")
        info.append("=== آخر 5 تعديلات ===")
        if os.path.exists(EDIT_LOG):
            with open(EDIT_LOG, "r", encoding="utf-8") as f:
                log = json.load(f)
            for entry in log[-5:]:
                info.append("- " + entry.get("time", "") + " | " + entry.get("action", "") + " | " + entry.get("file", ""))
        else:
            info.append("لا يوجد")
    except Exception as e:
        info.append("خطأ في السجل: " + str(e)[:100])

    return "\n".join(info)


async def delete_tracked(game_obj):
    if game_obj is None or not hasattr(game_obj, "tracked_messages"):
        return
    tracked = list(game_obj.tracked_messages)
    game_obj.tracked_messages = []
    if not tracked:
        return
    ids_by_chat = {}
    for chat_id, msg_id in tracked:
        ids_by_chat.setdefault(chat_id, []).append(msg_id)
    for chat_id, ids in ids_by_chat.items():
        unique_ids = list(set(ids))
        for i in range(0, len(unique_ids), 100):
            batch = unique_ids[i:i + 100]
            try:
                await client.delete_messages(chat_id, batch)
                await asyncio.sleep(0.2)
            except Exception:
                for mid in batch:
                    try:
                        await client.delete_messages(chat_id, mid)
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass


async def strip_buttons(game_obj):
    if game_obj is None or not hasattr(game_obj, "tracked_messages"):
        return
    for chat_id, msg_id in list(game_obj.tracked_messages):
        try:
            await client.edit_message(chat_id, msg_id, None, buttons=None)
        except Exception:
            pass


async def cache_names(g, team1, team2):
    if not hasattr(g, "_name_cache") or g._name_cache is None:
        g._name_cache = {}
    for uid in list(team1) + list(team2):
        if uid in g._name_cache:
            continue
        try:
            ent = await client.get_entity(uid)
            raw = ent.first_name or ""
            g._name_cache[uid] = clean_name_with_id(raw, uid, "لاعب")
        except Exception:
            g._name_cache[uid] = "لاعب " + str(uid)


def pick_next_player(g, team_num):
    if team_num == 1:
        team = g.team1
        played = g.team1_played
    else:
        team = g.team2
        played = g.team2_played
    remaining = [uid for uid in team if uid not in played]
    if not remaining:
        if team_num == 1:
            g.team1_played = []
        else:
            g.team2_played = []
        remaining = team[:]
    chosen = random.choice(remaining)
    if team_num == 1:
        g.team1_played.append(chosen)
    else:
        g.team2_played.append(chosen)
    return chosen


async def send_main_menu(chat_id):
    u = await bot_username()
    text = ("مرحبًا بكم في بوت تحدي الثلاثين ثانية.\n\n"
            "اختر أحد الخيارات التالية:\n"
            "1) بدء مبارة مع مجموعة أخرى: ترشيح وتصويت ثم مطابقة.\n"
            "2) بدء مبارة داخل الكروب: عدد اللاعبين لكل فريق ثم انضمام.\n"
            "3) أضف البوت كمشرف: يمنح البوت صلاحية التثبيت والكتابة.\n"
            "4) شرح الأزرار: يعرض هذا الشرح.")
    kb = [
        [Button.inline("بدء مبارة مع مجموعة أخرى", b"mode_tournament")],
        [Button.inline("شرح الأزرار", b"explain_buttons")],
        [Button.url("أضف البوت كمشرف", "https://t.me/" + u + "?startgroup=admin")],
        [Button.inline("بدء مبارة داخل الكروب", b"mode_internal")],
    ]
    await client.send_message(chat_id, text, buttons=kb)


def build_dev_main_kb():
    return [
        [Button.inline("محادثة المطور", b"dev_chat_mode")],
        [Button.inline("معلومات المطور", b"dev_owner_info")],
    ]


async def send_dev_panel(user_id):
    val = get_setting("dev_notifications", "1")
    status = "مفعلة" if val == "1" else "متوقفة"
    text = ("لوحة تحكم المطور\n\nحالة الإشعارات: " + status + "\n\nاختر القسم:")
    try:
        await client.send_message(user_id, text, buttons=build_dev_main_kb())
    except Exception:
        pass


async def find_group_id_by_name(name):
    if not name:
        return None
    name = name.strip()
    try:
        if name.lstrip("-").isdigit():
            return int(name)
    except Exception:
        pass
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id, name FROM groups")
    rows = c.fetchall()
    conn.close()
    name_lower = name.lower()
    for r in rows:
        gname = safe_str(r["name"], "").lower()
        if name_lower == gname:
            return r["chat_id"]
    for r in rows:
        gname = safe_str(r["name"], "").lower()
        if name_lower in gname or gname in name_lower:
            return r["chat_id"]
    return None


async def find_user_id_by_name(name):
    if not name:
        return None
    name = name.strip()
    try:
        if name.lstrip("-").isdigit():
            return int(name)
    except Exception:
        pass
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, name FROM all_users")
    rows = c.fetchall()
    conn.close()
    name_lower = name.lower()
    for r in rows:
        uname = safe_str(r["name"], "").lower()
        if name_lower in uname or uname in name_lower:
            return r["user_id"]
    return None


def log_ai_action(action, target, message, status):
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "target": target,
        "message": message[:100],
        "status": status,
    }
    AI_AGENT_LOG.append(entry)
    if len(AI_AGENT_LOG) > 100:
        AI_AGENT_LOG.pop(0)
        
async def execute_ai_action(event, action, target, message):
    try:
        if action == "stats":
            s = get_stats()
            text = ("إحصائيات البوت\n\n"
                    "المجموعات: " + str(s["groups"]) + "\n"
                    "اللاعبون: " + str(s["players"]) + "\n"
                    "المستخدمون: " + str(s["users"]) + "\n"
                    "المحظورة: " + str(s["banned"]) + "\n"
                    "قنوات الاشتراك: " + str(s["subs"]))
            log_ai_action(action, "", "", "success")
            return await event.reply(text)
        if action == "bot_full_info":
            info = await get_full_bot_context()
            log_ai_action(action, "", "", "success")
            return await event.reply(info[:4000])
    
        if action == "list_groups":
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT chat_id, name FROM groups ORDER BY name LIMIT 50")
            rows = c.fetchall()
            conn.close()
            if not rows:
                return await event.reply("لا توجد مجموعات.")
            lines = ["المجموعات (" + str(len(rows)) + "):"]
            for r in rows:
                lines.append("- " + safe_str(r["name"], "") + " | " + str(r["chat_id"]))
            log_ai_action(action, "", "", "success")
            return await event.reply("\n".join(lines)[:4000])
        if action == "list_users":
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT user_id, name FROM all_users ORDER BY name LIMIT 50")
            rows = c.fetchall()
            conn.close()
            if not rows:
                return await event.reply("لا يوجد مستخدمون.")
            lines = ["المستخدمون (" + str(len(rows)) + "):"]
            for r in rows:
                lines.append("- " + safe_str(r["name"], "") + " | " + str(r["user_id"]))
            log_ai_action(action, "", "", "success")
            return await event.reply("\n".join(lines)[:4000])
        if action == "game_status":
            gid = await find_group_id_by_name(target)
            if not gid:
                return await event.reply("لم أجد المجموعة: " + target)
            g = internal_games.get(gid)
            if not g:
                return await event.reply("لا توجد لعبة جارية في هذه المجموعة.")
            text = ("حالة اللعبة في المجموعة " + str(gid) + ":\n"
                    "الحالة: " + g.state + "\n"
                    "الجولة: " + str(g.round) + "\n"
                    "أرواح الفريق الأول: " + str(g.team1_points) + "\n"
                    "أرواح الفريق الثاني: " + str(g.team2_points))
            log_ai_action(action, target, "", "success")
            return await event.reply(text)
        if action == "user_info":
            uid = await find_user_id_by_name(target)
            if not uid:
                return await event.reply("لم أجد المستخدم: " + target)
            try:
                ent = await client.get_entity(uid)
                first = safe_str(getattr(ent, "first_name", ""), "")
                last = safe_str(getattr(ent, "last_name", ""), "")
                full = (first + " " + last).strip() or "غير معروف"
                uname = getattr(ent, "username", None)
                lines = ["معلومات المستخدم:", "الاسم: " + full]
                if uname:
                    lines.append("اليوزر: @" + uname)
                lines.append("الآيدي: " + str(uid))
                log_ai_action(action, target, "", "success")
                return await event.reply("\n".join(lines))
            except Exception:
                return await event.reply("تعذر جلب بيانات المستخدم.")
        if action == "edit_log":
            if not os.path.exists(EDIT_LOG):
                return await event.reply("السجل فارغ.")
            try:
                with open(EDIT_LOG, "r", encoding="utf-8") as f:
                    log = json.load(f)
            except Exception:
                log = []
            if not log:
                return await event.reply("السجل فارغ.")
            lines = ["سجل التعديلات (آخر 20):"]
            for entry in log[-20:]:
                lines.append(entry.get("time", "") + " | " + entry.get("action", "") + " | " + entry.get("file", ""))
            log_ai_action(action, "", "", "success")
            return await event.reply("\n".join(lines)[:4000])
        if action == "agent_log":
            if not AI_AGENT_LOG:
                return await event.reply("سجل المساعد فارغ.")
            lines = ["سجل المساعد (آخر 20):"]
            for entry in AI_AGENT_LOG[-20:]:
                lines.append(entry["time"] + " | " + entry["action"] + " | " + entry["status"])
            return await event.reply("\n".join(lines)[:4000])
        if action == "chat":
            if not message:
                return await event.reply("لم يتم إرجاع رد نصي.")
            return await event.reply(message)
        if action == "ban_group":
            gid = await find_group_id_by_name(target)
            if not gid:
                log_ai_action(action, target, "", "target_not_found")
                return await event.reply("لم أجد المجموعة: " + target)
            ban_group(gid)
            log_ai_action(action, target, "", "success")
            return await event.reply("تم حظر المجموعة: " + target + " (" + str(gid) + ")")
        if action == "unban_group":
            gid = await find_group_id_by_name(target)
            if not gid:
                log_ai_action(action, target, "", "target_not_found")
                return await event.reply("لم أجد المجموعة: " + target)
            unban_group(gid)
            log_ai_action(action, target, "", "success")
            return await event.reply("تم إلغاء حظر المجموعة: " + target + " (" + str(gid) + ")")
        if action == "end_game":
            gid = await find_group_id_by_name(target)
            if not gid:
                log_ai_action(action, target, "", "target_not_found")
                return await event.reply("لم أجد المجموعة: " + target)
            if gid not in internal_games:
                return await event.reply("لا توجد لعبة جارية في هذه المجموعة.")
            g = internal_games.pop(gid)
            g.state = "done"
            await cancel_round_tasks(g)
            await delete_round_messages(g)
            await delete_pinned(g, gid)
            await strip_buttons(g)
            await delete_tracked(g)
            log_ai_action(action, target, "", "success")
            return await event.reply("تم إيقاف اللعبة في: " + target)
        if action == "broadcast_groups":
            if not message:
                return await event.reply("لم يتم تحديد نص الإعلان.")
            groups = get_all_groups()
            sent = 0
            for gid in groups:
                if is_banned(gid):
                    continue
                try:
                    await client.send_message(gid, message)
                    sent += 1
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            log_ai_action(action, "", message, "sent_" + str(sent))
            return await event.reply("تم الإرسال إلى " + str(sent) + " مجموعة.")
        if action == "broadcast_users":
            if not message:
                return await event.reply("لم يتم تحديد نص الإعلان.")
            users = get_all_users()
            sent = 0
            for uid in users:
                try:
                    await client.send_message(uid, message)
                    sent += 1
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            log_ai_action(action, "", message, "sent_" + str(sent))
            return await event.reply("تم الإرسال إلى " + str(sent) + " مستخدم.")
        if action == "broadcast_all":
            if not message:
                return await event.reply("لم يتم تحديد نص الإعلان.")
            groups = get_all_groups()
            users = get_all_users()
            gs = 0
            us = 0
            for gid in groups:
                if is_banned(gid):
                    continue
                try:
                    await client.send_message(gid, message)
                    gs += 1
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            for uid in users:
                try:
                    await client.send_message(uid, message)
                    us += 1
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            log_ai_action(action, "", message, "groups_" + str(gs) + "_users_" + str(us))
            return await event.reply("تم الإرسال إلى " + str(gs) + " مجموعة و " + str(us) + " مستخدم.")
        if action == "send_message":
            if not target or not message:
                return await event.reply("يجب تحديد المجموعة والرسالة.")
            gid = await find_group_id_by_name(target)
            if not gid:
                return await event.reply("لم أجد المجموعة: " + target)
            try:
                await client.send_message(gid, message)
                log_ai_action(action, target, message, "success")
                return await event.reply("تم إرسال الرسالة إلى: " + target)
            except Exception as e:
                return await event.reply("فشل الإرسال: " + str(e)[:150])
        if action == "restart_bot":
            log_ai_action(action, "", "", "success")
            await event.reply("جاري إعادة التشغيل...")
            await asyncio.sleep(2)
            await _restart_bot()
        return await event.reply("لم أتمكن من تنفيذ: " + action)
    except Exception as e:
        log_ai_action(action, target, message, "error_" + str(e)[:50])
        return await event.reply("حدث خطأ أثناء التنفيذ: " + str(e)[:150])


@client.on(events.NewMessage(func=lambda e: e.is_private))
@safe_execute
async def private_handler(event):
    uid = event.sender_id
    user = await event.get_sender()
    try:
        register_user(uid, clean_name(user.first_name))
    except Exception:
        pass

    text = safe_str(event.text, "").strip()

    if uid == DEV_ID and AI_CHAT_MODE.get(uid) and not text.startswith("/"):
        await handle_dev_chat(event, text)
        return

    if uid in ai_games:
        ag = ai_games[uid]
        txt = text
        if txt.startswith("/"):
            return
        if ag.state == "player_bid":
            if not txt.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(txt)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            ag.player_bid = bid
            ag.player_answers = []
            ag.state = "player_answering"
            await event.reply("تم تسجيل مزايدتك " + str(bid) + ".\n\nأرسل الإجابات، كل إجابة في رسالة منفصلة، ثم اضغط زر الإنهاء.")
            kb = [[Button.inline("إنهاء الإجابات", b"ai_finish_player")]]
            await event.reply("أرسل الإجابات الآن.", buttons=kb)
            return
        if ag.state == "player_answering":
            ag.player_answers.append(txt)
            return
        if ag.state == "ai_turn":
            return await event.reply("انتظر دور الذكاء الاصطناعي.")
        return

    sess = private_sessions.get(uid)
    if not sess:
        return

    if sess["game_type"] == "internal":
        g = internal_games.get(sess["chat_id"])
        if not g:
            private_sessions.pop(uid, None)
            return await event.reply("انتهت اللعبة.")
        is_current_bidder = (g.state == "bidding" and g.bidder == uid)
        is_current_opponent = (g.state == "opponent_choice" and g.opponent == uid and not getattr(g, "opponent_resolved", False))
        if is_current_bidder:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(text)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            await cancel_round_tasks(g)
            g.current_bid = bid
            g.consecutive_timeouts = 0
            g.state = "opponent_choice"
            g.opponent_resolved = False
            opp = g.opponent
            bname = g._name_cache.get(uid, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.respond("تم اتخاذ القرار.\n\nمزايدتك: " + str(bid) + "\n\nسيتم إبلاغ الخصم الآن.")
            except Exception:
                pass
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_" + str(g.chat_id) + "_" + str(uid)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
            await client.send_message(opp, "خصمك " + user_link(uid, bname) + " قال إنه يستطيع ذكر " + str(bid) + " من " + safe_str(g.question, "") + ".\nهل تجبره على الإجابة، أو تزايد برقم أعلى؟ لديك 20 ثانية.", buttons=kb)
            g.opponent_timeout_task = asyncio.create_task(opponent_timeout_internal(g, opp))
        elif is_current_opponent:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا أعلى من " + str(g.current_bid) + "، أو استخدم الأزرار.")
            newbid = int(text)
            if newbid <= g.current_bid:
                return await event.reply("يجب أن يكون أكبر من " + str(g.current_bid) + ".")
            if newbid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            await cancel_round_tasks(g)
            g.current_bid = newbid
            old_bidder = g.bidder
            old_opponent = g.opponent
            g.bidder = old_opponent
            g.opponent = old_bidder
            private_sessions[g.bidder] = {"game_type": "internal", "chat_id": g.chat_id, "role": "bidder"}
            private_sessions[g.opponent] = {"game_type": "internal", "chat_id": g.chat_id, "role": "opponent"}
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.respond("تم اتخاذ القرار.\n\nمزايدتك الجديدة: " + str(newbid) + "\n\nسيتم إبلاغ الخصم الآن.")
            except Exception:
                pass
            kb = [[Button.inline("إجباره على الإجابة " + str(newbid), ("force_" + str(g.chat_id) + "_" + str(g.opponent)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
            await client.send_message(g.opponent, "الخصم رفع المزايدة إلى " + str(newbid) + ".\nهل تجبره على الإجابة، أو تزايد برقم أعلى؟ لديك 20 ثانية.", buttons=kb)
            g.state = "opponent_choice"
            g.opponent_resolved = False
            g.opponent_timeout_task = asyncio.create_task(opponent_timeout_internal(g, g.opponent))
        elif sess["role"] == "bidder" and g.state == "answering":
            g.answers.append(text)
            try:
                await event.delete()
            except Exception:
                pass
        else:
            return
    elif sess["game_type"] == "tournament":
        m = tournaments.get(sess["match_id"])
        if not m:
            private_sessions.pop(uid, None)
            return await event.reply("انتهى التحدي.")
        is_current_bidder = (m.state == "bidding" and m.bidder == uid)
        is_current_opponent = (m.state == "opponent_choice" and m.opponent == uid and not getattr(m, "opponent_resolved", False))
        if is_current_bidder:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(text)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            await cancel_round_tasks(m)
            m.current_bid = bid
            m.consecutive_timeouts = 0
            m.state = "opponent_choice"
            m.opponent_resolved = False
            opp = m.opponent
            bname = "لاعب غير معروف"
            for p in m.team1 + m.team2:
                if p["user_id"] == uid:
                    bname = p["name"]
                    break
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.respond("تم اتخاذ القرار.\n\nمزايدتك: " + str(bid) + "\n\nسيتم إبلاغ الخصم الآن.")
            except Exception:
                pass
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_t_" + str(m.match_id) + "_" + str(uid)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
            await client.send_message(opp, "خصمك " + user_link(uid, bname) + " قال إنه يذكر " + str(bid) + " من " + safe_str(m.question, "") + ".\nهل تجبره على الإجابة، أو تزايد برقم أعلى؟ لديك 20 ثانية.", buttons=kb)
            m.opponent_timeout_task = asyncio.create_task(opponent_timeout_tournament(m, opp))
        elif is_current_opponent:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا أعلى من " + str(m.current_bid) + "، أو استخدم الأزرار.")
            newbid = int(text)
            if newbid <= m.current_bid:
                return await event.reply("يجب أن يكون أكبر من " + str(m.current_bid) + ".")
            if newbid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            await cancel_round_tasks(m)
            m.current_bid = newbid
            old_bidder = m.bidder
            old_opponent = m.opponent
            m.bidder = old_opponent
            m.opponent = old_bidder
            private_sessions[m.bidder] = {"game_type": "tournament", "match_id": m.match_id, "role": "bidder"}
            private_sessions[m.opponent] = {"game_type": "tournament", "match_id": m.match_id, "role": "opponent"}
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.respond("تم اتخاذ القرار.\n\nمزايدتك الجديدة: " + str(newbid) + "\n\nسيتم إبلاغ الخصم الآن.")
            except Exception:
                pass
            kb = [[Button.inline("إجباره على الإجابة " + str(newbid), ("force_t_" + str(m.match_id) + "_" + str(m.opponent)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
            await client.send_message(m.opponent, "الخصم رفع المزايدة إلى " + str(newbid) + ".\nهل تجبره على الإجابة، أو تزايد برقم أعلى؟ لديك 20 ثانية.", buttons=kb)
            m.state = "opponent_choice"
            m.opponent_resolved = False
            m.opponent_timeout_task = asyncio.create_task(opponent_timeout_tournament(m, m.opponent))
        elif sess["role"] == "bidder" and m.state == "answering":
            m.answers.append(text)
            try:
                await event.delete()
            except Exception:
                pass
        else:
            return
            
@client.on(events.CallbackQuery(data=b"ai_agent_confirm"))
@safe_execute
async def cb_ai_agent_confirm(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    state = AI_AGENT_STATE.pop(event.sender_id, None)
    if not state:
        return await event.answer("انتهت الجلسة.", alert=True)
    await event.answer("جاري التنفيذ...")
    await execute_ai_action(event, state["action"], state["target"], state["message"])


@client.on(events.CallbackQuery(data=b"ai_agent_cancel"))
@safe_execute
async def cb_ai_agent_cancel(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    AI_AGENT_STATE.pop(event.sender_id, None)
    await event.answer("تم الإلغاء.")
    try:
        await event.edit("تم إلغاء الطلب.")
    except Exception:
        pass


@client.on(events.ChatAction)
@safe_execute
async def on_group_join(event):
    me = await client.get_me()
    if not (event.user_added or event.user_joined):
        return
    if event.user_id != me.id:
        return
    now = time.time()
    key = (event.chat_id, me.id)
    last = _last_join_event.get(key, 0)
    if now - last < 10:
        return
    _last_join_event[key] = now
    chat = await event.get_chat()
    chat_title = safe_str(getattr(chat, "title", ""), "")
    if name_has_bad_word(chat_title):
        await notify_dev("البوت في مجموعة باسم مخالف:\nID: " + str(event.chat_id))
    perms = None
    try:
        perms = await client.get_permissions(event.chat_id, me.id)
    except Exception:
        perms = None
    is_admin = False
    missing = []
    if perms is not None:
        is_admin = bool(getattr(perms, "is_admin", False))
        if not getattr(perms, "delete_messages", False):
            missing.append("حذف الرسائل")
        if not getattr(perms, "pin_messages", False):
            missing.append("تثبيت الرسائل")
    else:
        missing.append("جميع صلاحيات المشرف")
    if (not is_admin) or missing:
        u = await bot_username()
        link = "https://t.me/" + u + "?startgroup=admin"
        text = "أحتاج صلاحيتين لأعمل: حذف الرسائل وتثبيت الرسائل.\n"
        if missing:
            text += "الناقص: " + ", ".join(missing) + ".\n"
        text += "رقّوني وأنا أرجع."
        kb = [[Button.url("أعد إضافة البوت كمشرف", link)]]
        try:
            await client.send_message(event.chat_id, text, buttons=kb)
            await asyncio.sleep(3)
        except Exception:
            pass
        await client.delete_dialog(event.chat_id)
        await notify_dev("خرج البوت من مجموعة (صلاحيات ناقصة):\n" + display_group_name(chat_title) + "\nID: " + str(event.chat_id) + "\nالناقص: " + ", ".join(missing))
        return
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO groups (chat_id, name) VALUES (?, ?)", (event.chat_id, safe_str(chat_title, "المجموعة")))
        conn.commit()
        conn.close()
    except Exception as e:
        print("add group error:", str(e)[:150])
    try:
        await client.send_message(event.chat_id, "تمت إضافتي بنجاح. للبدء أرسل /start_game")
    except Exception:
        pass
    await notify_dev("أضيف البوت إلى مجموعة:\n" + display_group_name(chat_title) + "\nID: " + str(event.chat_id))


@client.on(events.NewMessage(pattern=r"^/start(?:@\S+)?(?: (.+))?$"))
@safe_execute
async def cmd_start(event):
    payload = safe_str(event.pattern_match.group(1), "")
    if event.is_private:
        user = await event.get_sender()
        from database import is_new_user
        is_new = is_new_user(user.id)
        register_user(user.id, clean_name(user.first_name))
        if not payload.startswith("join_") and is_new:
            asyncio.create_task(notify_dev_user_start(user))
        if payload.startswith("join_"):
            try:
                gid = int(payload[5:])
            except Exception:
                gid = None
            if gid is not None:
                g = internal_games.get(gid)
                if g and g.state == "waiting":
                    if user.id in g.players:
                        return await event.reply("أنت منضم بالفعل في هذه المجموعة.")
                    if len(g.players) >= g.required_total:
                        return await event.reply("اكتمل العدد في هذه المجموعة.")
                    name = clean_name_with_id(user.first_name, user.id, "لاعب")
                    g.players.append(user.id)
                    g.names.append(name)
                    if not hasattr(g, "_name_cache") or g._name_cache is None:
                        g._name_cache = {}
                    g._name_cache[user.id] = name
                    await event.reply("تم تسجيل انضمامك في " + display_group_name(getattr(g, "chat_name_display", None) or g.chat_name) + "\nانتظر في الكروب.")
                    await refresh_join_pinned(g)
                    if len(g.players) >= g.required_total:
                        g.split_teams()
                        g.state = "playing"
                        await cache_names(g, g.team1, g.team2)
                        if g.team_size == 1:
                            t1_name = g._name_cache.get(g.team1[0]) or ("لاعب " + str(g.team1[0]))
                            t2_name = g._name_cache.get(g.team2[0]) or ("لاعب " + str(g.team2[0]))
                            g.team1_label = t1_name
                            g.team2_label = t2_name
                        else:
                            labels = pick_team_names()
                            g.team1_label = labels[0] if labels[0] else "الفريق الأول"
                            g.team2_label = labels[1] if labels[1] else "الفريق الثاني"
                        if not g.team1_label:
                            g.team1_label = "الفريق الأول"
                        if not g.team2_label:
                            g.team2_label = "الفريق الثاني"
                        await send_teams_intro_internal(g)
                        await asyncio.sleep(4)
                        await start_round_internal(g)
                    return
                else:
                    return await event.reply("لا يوجد تحدٍ مفتوح في هذه المجموعة حاليًا.")
        u = await bot_username()
        text = ("أهلًا بك في بوت تحدي الثلاثين ثانية.\n\n"
                "يمكنك:\n"
                "1) إضافة البوت إلى مجموعتك لتنظيم تحديات بين الأعضاء.\n"
                "2) اللعب ضد الذكاء الاصطناعي في الخاص مباشرة.\n\n"
                "اختر ما تريد:")
        kb = [
            [Button.inline("اللعب ضد الذكاء الاصطناعي", b"ai_menu")],
            [Button.url("أضف البوت إلى مجموعتك", "https://t.me/" + u + "?startgroup=admin")],
        ]
        await event.reply(text, buttons=kb)
        if user.id == DEV_ID:
            await send_dev_panel(user.id)
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    if not await require_subscription(event):
        return
    await send_main_menu(event.chat_id)


@client.on(events.NewMessage(pattern=r"^@(\S+)"))
@safe_execute
async def on_bot_mention(event):
    me = await client.get_me()
    my_username = (me.username or "").lower()
    if not my_username:
        return
    full_text = safe_str(event.text, "")
    mentioned = None
    for word in full_text.split():
        if word.startswith("@"):
            mentioned = word[1:].lower().strip()
            break
    if mentioned is None or mentioned != my_username:
        return
    if event.is_private:
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    u = me.username or ""
    text = ("تحدي الثلاثين ثانية\n\n"
            "لعبة سريعة وحلوة تخلي الكروب كله يشارك.\n\n"
            "شلون تلعب:\n"
            "فريقين يتقابلون، وكل جولة يطلع موضوع معين.\n"
            "لاعب من الفريق الأول يزايد برقم، يعني يكول أني أذكر هذا العدد.\n"
            "لاعب الفريق الثاني يكدر يقبل أو يرفع المزايدة.\n"
            "اللي تثبت عنده المزايدة عنده ثلاثين ثانية يذكر الإجابات.\n"
            "كل جولة فريق يخسر روح، وأول فريق تخلص أرواحه يخسر.\n\n"
            "دز البوت لكروبك وابدأ التحدي.\n"
            "@" + u)
    kb = [[Button.inline("شنو هذا البوت", b"promo_info")]]
    try:
        await event.reply(text, buttons=kb)
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"promo_info"))
@safe_execute
async def cb_promo_info(event):
    await event.answer()
    me = await client.get_me()
    u = me.username or ""
    text = ("تحدي الثلاثين ثانية\n\n"
            "لعبة جماعية سريعة، فريقين يتقابلون، وكل جولة يطلع موضوع.\n"
            "المزايد يكول جم لاعب يذكر، والخصم يقبل أو يرفع.\n"
            "اللي تثبت عنده المزايدة عنده ثلاثين ثانية يذكر الإجابات.\n"
            "كل جولة فريق يخسر روح، وأول فريق تخلص أرواحه يخسر.\n\n"
            "أضف البوت لمجموعتك وابدأ التحدي.\n"
            "@" + u)
    kb = [
        [Button.url("افتح البوت في الخاص", "https://t.me/" + u)],
        [Button.url("أضف البوت لمجموعتك", "https://t.me/" + u + "?startgroup=admin")],
    ]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.NewMessage(pattern=r"^/start_game(?:@\S+)?(?:\s+(\d+))?\s*$"))
@safe_execute
async def cmd_start_game(event):
    if event.is_private:
        return await event.reply("هذا الأمر مخصص للمجموعات.")
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await require_subscription(event):
        return
    if event.chat_id in internal_games:
        existing = internal_games[event.chat_id]
        if getattr(existing, "state", "") in ("done", "finished"):
            internal_games.pop(event.chat_id, None)
        else:
            return await event.reply("توجد لعبة جارية بالفعل في هذه المجموعة.")
    n = event.pattern_match.group(1)
    if n is None:
        raw = safe_str(event.text, "").strip()
        parts = raw.split()
        if len(parts) >= 2:
            try:
                n = int(parts[1])
            except Exception:
                n = None
    if n is None:
        kb = []
        for size in (1, 2, 3, 4, 5, 6, 7):
            kb.append([Button.inline(str(size) + " ضد " + str(size), ("internal_size_" + str(size)).encode())])
        return await event.reply("اختر عدد اللاعبين لكل فريق:", buttons=kb)
    try:
        team_size = int(n)
    except Exception:
        return await event.reply("أرسل رقمًا صحيحًا.")
    if team_size < 1 or team_size > 7:
        return await event.reply("عدد اللاعبين لكل فريق يجب أن يكون بين 1 و 7.")
    await start_internal(event.chat_id, safe_str(event.chat.title, "المجموعة"), team_size)


@client.on(events.NewMessage(pattern=r"^/1v1$"))
@safe_execute
async def cmd_1v1(event):
    if event.is_private:
        return await event.reply("هذا الأمر مخصص للمجموعات.")
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    if not event.reply_to_msg_id:
        return await event.reply("قم بالرد على رسالة الشخص الذي تريد تحديه ثم أرسل /1v1")
    try:
        target_msg = await event.get_reply_message()
        target = await target_msg.get_sender()
    except Exception:
        return await event.reply("تعذر تحديد الشخص.")
    if target is None:
        return await event.reply("تعذر تحديد الشخص.")
    if target.id == event.sender_id:
        return await event.reply("لا يمكنك تحدي نفسك.")
    if target.bot:
        return await event.reply("لا يمكنك تحدي بوت.")
    challenger = await event.get_sender()
    if event.chat_id in internal_games:
        return await event.reply("توجد لعبة جارية بالفعل في هذه المجموعة.")
    p1_name = clean_name_with_id(challenger.first_name, challenger.id, "لاعب")
    p2_name = clean_name_with_id(target.first_name, target.id, "لاعب")
    g = DuelGame(event.chat_id, safe_str(event.chat.title, "المجموعة"),
                 challenger.id, p1_name, target.id, p2_name)
    internal_games[event.chat_id] = g
    text = ("تحدي 1 ضد 1\n\n"
            "اللاعب الأول: " + user_link(challenger.id, p1_name) + "\n"
            "اللاعب الثاني: " + user_link(target.id, p2_name) + "\n\n"
            "الأرواح: 3 لكل لاعب.\n"
            "سيتم إرسال تفاصيل الجولة الأولى الآن.")
    await event.reply(text)
    await asyncio.sleep(2)
    await start_round_internal(g)


@client.on(events.NewMessage(pattern=r"^/ai_play$"))
@safe_execute
async def cmd_ai_play(event):
    if not event.is_private:
        return await event.reply("هذا الأمر مخصص للخاص فقط.")
    user = await event.get_sender()
    register_user(user.id, clean_name(user.first_name))
    text = ("اللعب ضد الذكاء الاصطناعي\n\n"
            "اختر مستوى الصعوبة:\n"
            "سهل: الذكاء الاصطناعي يخطئ كثيرًا.\n"
            "متوسط: توازن بين الصواب والخطأ.\n"
            "صعب: الذكاء الاصطناعي دقيق لكن ليس مثاليًا.")
    kb = [
        [Button.inline("سهل", b"ai_diff_easy")],
        [Button.inline("متوسط", b"ai_diff_medium")],
        [Button.inline("صعب", b"ai_diff_hard")],
    ]
    await event.reply(text, buttons=kb)


@client.on(events.NewMessage(pattern=r"^/ai_stop$"))
@safe_execute
async def cmd_ai_stop(event):
    if not event.is_private:
        return
    user_id = event.sender_id
    g = ai_games.pop(user_id, None)
    if g:
        await event.reply("تم إيقاف اللعبة.\n\nنقاطك: " + str(g.player_points) + "\nنقاط الذكاء الاصطناعي: " + str(g.ai_points))
    else:
        await event.reply("لا توجد لعبة جارية.")


@client.on(events.CallbackQuery(data=b"explain_buttons"))
@safe_execute
async def cb_explain(event):
    await event.answer()
    text = ("شرح الأزرار:\n\n"
            "بدء مبارة مع مجموعة أخرى: ترشيح وتصويت في كروبكم، ومطابقة تلقائية مع مجموعة منتظرة.\n\n"
            "أضف البوت كمشرف: يفتح نافذة إضافة البوت مع صلاحيات المشرف.\n\n"
            "بدء مبارة داخل الكروب: عدد اللاعبين لكل فريق، ثم زر انضمام للأعضاء.")
    await event.reply(text)


@client.on(events.CallbackQuery(data=b"ai_menu"))
@safe_execute
async def cb_ai_menu(event):
    await event.answer()
    text = ("اللعب ضد الذكاء الاصطناعي\n\n"
            "اختر مستوى الصعوبة:\n"
            "سهل: الذكاء الاصطناعي يخطئ كثيرًا.\n"
            "متوسط: توازن بين الصواب والخطأ.\n"
            "صعب: الذكاء الاصطناعي دقيق لكن ليس مثاليًا.")
    kb = [
        [Button.inline("سهل", b"ai_diff_easy")],
        [Button.inline("متوسط", b"ai_diff_medium")],
        [Button.inline("صعب", b"ai_diff_hard")],
    ]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(pattern=r"^ai_diff_(easy|medium|hard)$"))
@safe_execute
async def cb_ai_difficulty(event):
    difficulty = safe_str(event.pattern_match.group(1), "medium").strip().lower()
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    user_id = event.sender_id
    await event.answer("بدء اللعبة")
    ai_games[user_id] = AIGame(user_id, difficulty)
    labels = {"easy": "سهل", "medium": "متوسط", "hard": "صعب"}
    label = labels.get(difficulty, "متوسط")
    text = ("بدأت اللعبة ضد الذكاء الاصطناعي\n\n"
            "مستوى الصعوبة: " + label + "\n"
            "نقاطك: 100\n"
            "نقاط الذكاء الاصطناعي: 100")
    kb = [[Button.inline("ابدأ الجولة الأولى", b"ai_start_round")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"ai_start_round"))
@safe_execute
async def cb_ai_start_round(event):
    await event.answer()
    user_id = event.sender_id
    g = ai_games.get(user_id)
    if not g:
        return await event.reply("لا توجد لعبة. أرسل /ai_play لبدء لعبة جديدة.")
    question = get_question()
    g.question = question
    g.state = "player_bid"
    g.player_bid = 0
    g.ai_bid = 0
    g.player_answers = []
    g.ai_answers = []
    g.turn = "player"
    text = ("الجولة " + str(g.round) + "\n\n"
            "السؤال: " + safe_str(question, "") + "\n\n"
            "نقاطك: " + str(g.player_points) + "\n"
            "نقاط الذكاء الاصطناعي: " + str(g.ai_points) + "\n\n"
            "أرسل رقمًا يمثل ما تستطيع ذكره من هذا التصنيف.")
    try:
        await event.edit(text)
    except Exception:
        await event.reply(text)


@client.on(events.CallbackQuery(data=b"ai_finish_player"))
@safe_execute
async def cb_ai_finish_player(event):
    await event.answer()
    user_id = event.sender_id
    g = ai_games.get(user_id)
    if not g or g.state != "player_answering":
        return await event.reply("لا توجد إجابات قيد الانتظار.")
    ok, reason = await evaluate_answers_with_ai(g.question, g.player_bid, g.player_answers)
    if ok:
        g.player_points += 10
        result_word = "نجحت"
    else:
        g.player_points -= 20
        g.ai_points += 10
        result_word = "فشلت"
    player_comment = await ai_comment_on_round(g.difficulty, success=ok, is_player=True)
    combined = ("نتيجة دورك\n\n"
                "النتيجة: " + result_word + "\n"
                + reason + "\n\n"
                "نقاطك: " + str(g.player_points) + "\n"
                "نقاط الذكاء الاصطناعي: " + str(g.ai_points))
    await event.reply(combined)
    if g.player_points <= 0 or g.ai_points <= 0:
        return await finish_ai_game(user_id)
    g.state = "ai_turn"
    g.round += 1
    asyncio.create_task(run_ai_turn(user_id))


@client.on(events.CallbackQuery(data=b"ai_next_round"))
@safe_execute
async def cb_ai_next_round(event):
    await event.answer()
    user_id = event.sender_id
    g = ai_games.get(user_id)
    if not g:
        return await event.reply("لا توجد لعبة.")
    question = get_question()
    g.question = question
    g.state = "player_bid"
    g.player_bid = 0
    g.ai_bid = 0
    g.player_answers = []
    g.ai_answers = []
    g.turn = "player"
    text = ("الجولة " + str(g.round) + "\n\n"
            "السؤال: " + safe_str(question, "") + "\n\n"
            "نقاطك: " + str(g.player_points) + "\n"
            "نقاط الذكاء الاصطناعي: " + str(g.ai_points) + "\n\n"
            "أرسل رقمًا يمثل ما تستطيع ذكره.")
    try:
        await event.edit(text)
    except Exception:
        await event.reply(text)


async def run_ai_turn(user_id):
    g = ai_games.get(user_id)
    if not g:
        return
    await ai_human_like_delay(g.difficulty)
    ai_question = get_question()
    g.question = ai_question
    mode, ai_bid = await ai_smart_bid(ai_question, g.difficulty, opponent_bid=0, is_response=False)
    g.ai_bid = ai_bid
    try:
        await client.send_message(user_id, "الذكاء الاصطناعي يزايد بـ " + str(ai_bid) + " إجابة على السؤال:\n" + safe_str(ai_question, ""))
    except Exception:
        pass
    await asyncio.sleep(1.5)
    answers = await ai_generate_answers(ai_question, ai_bid, g.difficulty)
    g.ai_answers = answers
    ok, reason = await evaluate_answers_with_ai(ai_question, ai_bid, answers)
    if ok:
        g.ai_points += 10
        result_word = "نجح"
    else:
        g.ai_points -= 20
        g.player_points += 10
        result_word = "فشل"
    ai_comment = await ai_comment_on_round(g.difficulty, success=ok, is_player=False)
    combined = ("دور الذكاء الاصطناعي\n\n"
                "السؤال: " + safe_str(ai_question, "") + "\n"
                "المزايدة: " + str(ai_bid) + " إجابة\n\n"
                "النتيجة: " + result_word + "\n"
                + reason + "\n\n"
                "الذكاء الاصطناعي يقول: " + ai_comment + "\n\n"
                "نقاطك: " + str(g.player_points) + "\n"
                "نقاط الذكاء الاصطناعي: " + str(g.ai_points))
    try:
        await client.send_message(user_id, combined)
    except Exception:
        pass
    if g.player_points <= 0 or g.ai_points <= 0:
        return await finish_ai_game(user_id)
    g.state = "idle"
    kb = [[Button.inline("الجولة التالية", b"ai_next_round")]]
    try:
        await client.send_message(user_id, "اضغط للاستمرار.", buttons=kb)
    except Exception:
        pass


async def finish_ai_game(user_id):
    g = ai_games.pop(user_id, None)
    if not g:
        return
    if g.player_points > g.ai_points:
        result = "فزت على الذكاء الاصطناعي."
    else:
        result = "خسرت ضد الذكاء الاصطناعي."
    try:
        await client.send_message(user_id, "انتهت اللعبة.\n\n" + result + "\n\nنقاطك: " + str(g.player_points) + "\nنقاط الذكاء الاصطناعي: " + str(g.ai_points))
        kb = [[Button.inline("العب مرة أخرى", b"ai_menu")]]
        await client.send_message(user_id, "تريد جولة جديدة؟", buttons=kb)
    except Exception:
        pass
        
async def try_match_group(chat_id, gname):
    for t in tournaments.values():
        if t.group1_id == chat_id or t.group2_id == chat_id:
            return None, "already_in_tournament"
    for entry in matchmaking_pool:
        if entry["chat_id"] == chat_id:
            return None, "already_in_pool"
    if not matchmaking_pool:
        matchmaking_pool.append({"chat_id": chat_id, "name": gname, "ts": time.time()})
        return None, "pooled"
    random.shuffle(matchmaking_pool)
    other = None
    while matchmaking_pool:
        candidate = matchmaking_pool.pop(0)
        if candidate["chat_id"] != chat_id:
            other = candidate
            break
    if not other:
        matchmaking_pool.append({"chat_id": chat_id, "name": gname, "ts": time.time()})
        return None, "pooled"
    match = Tournament(other["chat_id"], other["name"], chat_id, gname)
    tournaments[match.match_id] = match
    await open_nomination(match)
    await notify_dev("مطابقة كروبين:\n" + display_group_name(other["name"]) + " ضد " + display_group_name(gname) + "\nID1: " + str(other["chat_id"]) + "\nID2: " + str(chat_id))
    return match, "matched"


@client.on(events.CallbackQuery(data=b"mode_tournament"))
@safe_execute
async def cb_mode_tournament(event):
    chat_id = event.chat_id
    if is_banned(chat_id):
        return await event.answer("مجموعتكم محظورة.", alert=True)
    raw_name = event.chat.title if hasattr(event.chat, "title") else "المجموعة"
    gname = clean_name(raw_name, "المجموعة")
    match, status = await try_match_group(chat_id, gname)
    if status == "matched":
        await event.answer("تمت المطابقة وبدأ الترشيح.")
    elif status == "pooled":
        await event.answer("تمت الإضافة لقائمة الانتظار.")
        await event.edit("تمت إضافة كروبكم إلى قائمة الانتظار. سيتم فتح باب الترشيح عند دخول كروب آخر للتحدي.")
    elif status == "already_in_tournament":
        await event.answer("أنتم في تحدي بالفعل.", alert=True)
    elif status == "already_in_pool":
        await event.answer("أنتم بالفعل في قائمة الانتظار.", alert=True)


@client.on(events.CallbackQuery(data=b"mode_internal"))
@safe_execute
async def cb_mode_internal(event):
    kb = []
    for n in (1, 2, 3, 4, 5, 6, 7):
        kb.append([Button.inline(str(n) + " ضد " + str(n), ("internal_size_" + str(n)).encode())])
    await event.edit("اختر عدد اللاعبين لكل فريق:", buttons=kb)


@client.on(events.CallbackQuery(pattern=r"^internal_size_(\d+)$"))
@safe_execute
async def cb_internal_size(event):
    n = int(event.pattern_match.group(1))
    await event.answer()
    raw_title = safe_str(getattr(event.chat, "title", ""), "المجموعة")
    title = clean_name(raw_title, "المجموعة")
    try:
        await event.delete()
    except Exception:
        pass
    await start_internal(event.chat_id, title, n)


async def refresh_join_pinned(g):
    if not g.pin_msg_id:
        return
    u = await bot_username()
    join_url = "https://t.me/" + u + "?start=join_" + str(g.chat_id)
    if not g.players:
        players_text = "لا أحد بعد"
    else:
        lines = []
        for uid in g.players:
            name = g._name_cache.get(uid, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
            lines.append(user_link(uid, name))
        players_text = "\n- ".join(lines)
    text = ("تحدي داخلي\n\n"
            "عدد اللاعبين لكل فريق: " + str(g.team_size) + "\n"
            "المطلوب: " + str(g.required_total) + " لاعب\n\n"
            "المنضمون (" + str(len(g.players)) + "/" + str(g.required_total) + "):\n- " + players_text + "\n\n"
            "اضغط زر الانضمام للمشاركة.")
    kb = [[Button.url("انضمام للتحدي", join_url)]]
    await edit_pinned(g, g.chat_id, text, buttons=kb)


async def start_internal(chat_id, chat_name, team_size):
    if chat_id in internal_games:
        existing = internal_games[chat_id]
        if getattr(existing, "state", "") in ("done", "finished"):
            internal_games.pop(chat_id, None)
        else:
            await client.send_message(chat_id, "توجد لعبة جارية بالفعل في هذه المجموعة.")
            return
    g = InternalGame(chat_id, chat_name, team_size)
    g.chat_name_display = display_group_name(chat_name)
    g._name_cache = {}
    g.team1_label = ""
    g.team2_label = ""
    g.start_time = time.time()
    g.consecutive_timeouts = 0
    internal_games[chat_id] = g
    u = await bot_username()
    join_url = "https://t.me/" + u + "?start=join_" + str(chat_id)
    kb = [[Button.url("انضمام للتحدي", join_url)]]
    text = ("تحدي داخلي جديد\n\n"
            "عدد اللاعبين لكل فريق: " + str(team_size) + "\n"
            "المطلوب: " + str(team_size * 2) + " لاعب\n\n"
            "المنضمون (0/" + str(team_size * 2) + "):\n- لا أحد بعد\n\n"
            "اضغط زر الانضمام للمشاركة.")
    try:
        msg = await client.send_message(chat_id, text, buttons=kb)
        g.pin_msg_id = msg.id
        if not hasattr(g, "tracked_messages") or g.tracked_messages is None:
            g.tracked_messages = []
        g.tracked_messages.append((chat_id, msg.id))
        try:
            await client.pin_message(chat_id, msg, notify=False)
        except Exception:
            pass
    except Exception:
        pass
    g._join_timeout_task = asyncio.create_task(join_timeout_internal(chat_id))
    await notify_dev("بدء تحدي داخلي:\n" + display_group_name(chat_name) + "\nID: " + str(chat_id) + "\nحجم الفريق: " + str(team_size) + " ضد " + str(team_size))


async def join_timeout_internal(chat_id):
    await asyncio.sleep(120)
    g = internal_games.get(chat_id)
    if not g or g.state != "waiting":
        return
    internal_games.pop(chat_id, None)
    for p in g.players:
        private_sessions.pop(p, None)
    try:
        if g.pin_msg_id:
            await client.unpin_message(chat_id, g.pin_msg_id)
    except Exception:
        pass
    await delete_pinned(g, chat_id)
    await strip_buttons(g)
    await asyncio.sleep(0.2)
    await delete_tracked(g)
    try:
        await client.send_message(chat_id, "تم إلغاء التحدي لعدم اكتمال العدد خلال دقيقتين.")
    except Exception:
        pass


async def bidding_countdown(game_obj, chat_id, user_id, label):
    bid_at_start = getattr(game_obj, "current_bid", 0)
    checkpoints = [10, 5, 3, 1]
    prev = 20
    for cp in checkpoints:
        delay = prev - cp
        if delay > 0:
            await asyncio.sleep(delay)
        if game_obj is None:
            return
        if getattr(game_obj, "state", None) != "bidding":
            return
        if getattr(game_obj, "bidder", None) != user_id:
            return
        if getattr(game_obj, "current_bid", 0) != bid_at_start:
            return
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثواني" + label)
        except Exception:
            return
        prev = cp


async def answer_countdown(game_obj, chat_id, user_id, label):
    bid_at_start = getattr(game_obj, "current_bid", 0)
    checkpoints = [20, 10, 5, 3, 1]
    prev = 30
    for cp in checkpoints:
        delay = prev - cp
        if delay > 0:
            await asyncio.sleep(delay)
        if game_obj is None:
            return
        if getattr(game_obj, "state", None) != "answering":
            return
        if getattr(game_obj, "bidder", None) != user_id:
            return
        if getattr(game_obj, "current_bid", 0) != bid_at_start:
            return
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثانية" + label)
        except Exception:
            return
        prev = cp


async def opponent_timeout_internal(g, user_id):
    bid_at_start = g.current_bid
    checkpoints = [10, 5, 3, 1]
    prev = 20
    for cp in checkpoints:
        delay = prev - cp
        if delay > 0:
            await asyncio.sleep(delay)
        if g.state != "opponent_choice" or g.opponent != user_id:
            return
        if getattr(g, "opponent_resolved", False):
            return
        if g.current_bid != bid_at_start:
            return
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثواني لاتخاذ القرار")
        except Exception:
            pass
        prev = cp
    await asyncio.sleep(1)
    if g.state != "opponent_choice" or g.opponent != user_id:
        return
    if getattr(g, "opponent_resolved", False):
        return
    if g.current_bid != bid_at_start:
        return
    g.opponent_resolved = True
    if user_id in g.team1:
        g.team1_points -= 1
        fail_name = g.team1_label
    else:
        g.team2_points -= 1
        fail_name = g.team2_label
    uname = g._name_cache.get(user_id, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
    other_id = g.bidder
    await send_to_group(g, g.chat_id, "الخصم " + user_link(user_id, uname) + " تأخر في اتخاذ القرار.\n\n" + fail_name + " فقد روحًا.\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points), round_level=True)
    try:
        await client.send_message(other_id, "خصمك " + user_link(user_id, uname) + " تأخر في اتخاذ القرار، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
    except Exception:
        pass
    if g.team1_points <= 0 or g.team2_points <= 0:
        await finish_internal(g)
        return
    await asyncio.sleep(1)
    await advance_round_internal(g)


async def opponent_timeout_tournament(m, user_id):
    bid_at_start = m.current_bid
    checkpoints = [10, 5, 3, 1]
    prev = 20
    for cp in checkpoints:
        delay = prev - cp
        if delay > 0:
            await asyncio.sleep(delay)
        if m.state != "opponent_choice" or m.opponent != user_id:
            return
        if getattr(m, "opponent_resolved", False):
            return
        if m.current_bid != bid_at_start:
            return
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثواني لاتخاذ القرار")
        except Exception:
            pass
        prev = cp
    await asyncio.sleep(1)
    if m.state != "opponent_choice" or m.opponent != user_id:
        return
    if getattr(m, "opponent_resolved", False):
        return
    if m.current_bid != bid_at_start:
        return
    m.opponent_resolved = True
    if any(p["user_id"] == user_id for p in m.team1):
        m.team1_points -= 1
        fail_name = m.group1_name
    else:
        m.team2_points -= 1
        fail_name = m.group2_name
    uname = "لاعب غير معروف"
    for p in m.team1 + m.team2:
        if p["user_id"] == user_id:
            uname = p["name"]
            break
    other_id = m.bidder
    for gid in m.both_groups():
        await send_to_group(m, gid, "الخصم " + user_link(user_id, uname) + " تأخر في اتخاذ القرار.\n\n" + display_group_name(fail_name) + " فقد روحًا.\n\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points), round_level=True)
    try:
        await client.send_message(other_id, "خصمك " + user_link(user_id, uname) + " تأخر في اتخاذ القرار، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
    except Exception:
        pass
    if m.team1_points <= 0 or m.team2_points <= 0:
        await finish_tournament(m)
        return
    await asyncio.sleep(1)
    await advance_round_tournament(m)


async def send_teams_intro_internal(g):
    t1_names = []
    for uid in g.team1:
        t1_names.append(user_link(uid, g._name_cache.get(uid, "لاعب غير معروف")))
    t2_names = []
    for uid in g.team2:
        t2_names.append(user_link(uid, g._name_cache.get(uid, "لاعب غير معروف")))
    text = ("بدء المباراة\n\n"
            + g.team1_label + ":\n- " + "\n- ".join(t1_names) + "\n\n"
            + g.team2_label + ":\n- " + "\n- ".join(t2_names) + "\n\n"
            "أرواح كل فريق: " + str(g.team1_points))
    await send_to_group(g, g.chat_id, text, round_level=True)


def decide_bidder_teams(g):
    if g.round % 2 == 1:
        first_team = g.first_bidder_team
    else:
        first_team = 2 if g.first_bidder_team == 1 else 1
    if first_team == 1:
        b = pick_next_player(g, 1)
        o = pick_next_player(g, 2)
        bteam = 1
    else:
        b = pick_next_player(g, 2)
        o = pick_next_player(g, 1)
        bteam = 2
    return b, o, bteam


async def start_round_internal(g):
    try:
        await cancel_round_tasks(g)
        await delete_round_messages(g)
        g.question = get_question()
        g.state = "bidding"
        g.current_bid = 0
        g.answers = []
        g.opponent_resolved = False
        g.forced = False
        g.consecutive_timeouts = 0
        g.answer_watcher_task = None
        if not g.team1_label:
            g.team1_label = "الفريق الأول"
        if not g.team2_label:
            g.team2_label = "الفريق الثاني"
        b, o, bteam = decide_bidder_teams(g)
        g.bidder = b
        g.opponent = o
        for old_uid in list(private_sessions.keys()):
            s = private_sessions.get(old_uid)
            if s and s.get("game_type") == "internal" and s.get("chat_id") == g.chat_id:
                private_sessions.pop(old_uid, None)
        private_sessions[b] = {"game_type": "internal", "chat_id": g.chat_id, "role": "bidder"}
        private_sessions[o] = {"game_type": "internal", "chat_id": g.chat_id, "role": "opponent"}
        await cache_names(g, g.team1, g.team2)
        for uid in g.players:
            if uid not in g._name_cache:
                try:
                    ent = await client.get_entity(uid)
                    g._name_cache[uid] = clean_name_with_id(ent.first_name, uid, "لاعب")
                except Exception:
                    g._name_cache[uid] = "لاعب " + str(uid)
        bn = g._name_cache.get(b, "لاعب غير معروف")
        on = g._name_cache.get(o, "لاعب غير معروف")
        b_team_label = g.team1_label if bteam == 1 else g.team2_label
        o_team_label = g.team2_label if bteam == 1 else g.team1_label
        await delete_pinned(g, g.chat_id)
        text = ("الجولة " + str(g.round) + "\n\n"
                "السؤال: " + safe_str(g.question, "") + "\n\n"
                "أرواح " + g.team1_label + ": " + str(g.team1_points) + "\n"
                "أرواح " + g.team2_label + ": " + str(g.team2_points) + "\n\n"
                "المزايد: " + user_link(b, bn) + " من " + b_team_label + "\n"
                "الخصم: " + user_link(o, on) + " من " + o_team_label + "\n\n"
                "المزايدة تجري في الخاص الآن، ولدى المزايد 20 ثانية.")
        await send_to_group(g, g.chat_id, text, round_level=True)
        kb_withdraw = [[Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
                       [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
        try:
            await client.send_message(b, "بدأت المزايدة للجولة " + str(g.round) + ".\nالسؤال: " + safe_str(g.question, "") + "\nأرسل رقمًا فقط خلال 20 ثانية.", buttons=kb_withdraw)
        except Exception:
            pass
        try:
            await client.send_message(o, "أنت الخصم في الجولة " + str(g.round) + ".\nالسؤال: " + safe_str(g.question, "") + "\nانتظر مزايدة خصمك ثم قرر.", buttons=kb_withdraw)
        except Exception:
            pass
        g.bidding_task = asyncio.create_task(bidding_timeout_internal(g, b))
        asyncio.create_task(bidding_countdown(g, g.chat_id, b, " للمزايدة"))
    except Exception as e:
        print("start_round_internal error:", str(e)[:300])


async def bidding_timeout_internal(g, user_id):
    bid_at_start = g.current_bid
    await asyncio.sleep(20)
    if g.state != "bidding" or g.bidder != user_id:
        return
    if g.current_bid != bid_at_start:
        return
    g.consecutive_timeouts = getattr(g, "consecutive_timeouts", 0) + 1
    if user_id in g.team1:
        g.team1_points -= 1
        fail_name = g.team1_label
    else:
        g.team2_points -= 1
        fail_name = g.team2_label
    if g.consecutive_timeouts >= 2 or g.team1_points <= 0 or g.team2_points <= 0:
        await send_to_group(g, g.chat_id, "تم إنهاء التحدي.\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points), round_level=True)
        await end_internal_no_winner(g)
        return
    await send_to_group(g, g.chat_id, "انتهت مدة المزايدة دون رد.\n\n" + fail_name + " فقد روحًا.\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points), round_level=True)
    try:
        if g.opponent and g.opponent != user_id:
            await client.send_message(g.opponent, "انتهت مدة مزايدة خصمك دون رد، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
    except Exception:
        pass
    await asyncio.sleep(1)
    await advance_round_internal(g)


async def end_internal_no_winner(g):
    g.state = "done"
    await cancel_round_tasks(g)
    await delete_round_messages(g)
    await delete_pinned(g, g.chat_id)
    await strip_buttons(g)
    await asyncio.sleep(0.2)
    await delete_tracked(g)
    for p in g.players:
        private_sessions.pop(p, None)
    internal_games.pop(g.chat_id, None)


async def advance_round_internal(g):
    if g.team1_points <= 0 or g.team2_points <= 0:
        return await finish_internal(g)
    g.round += 1
    await asyncio.sleep(1)
    try:
        await start_round_internal(g)
    except Exception as e:
        print("advance_round_internal error:", str(e)[:300])
        try:
            await send_to_group(g, g.chat_id, "حدث خطأ أثناء بدء الجولة التالية.", round_level=True)
        except Exception:
            pass
        await finish_internal(g)


async def finish_internal(g):
    g.state = "done"
    g.end_time = time.time()
    if g.team_size == 1:
        t1_label = user_link(g.team1[0], g.team1_label) if g.team1 else g.team1_label
        t2_label = user_link(g.team2[0], g.team2_label) if g.team2 else g.team2_label
    else:
        t1_label = g.team1_label
        t2_label = g.team2_label
    if g.team1_points > g.team2_points:
        winner_label = t1_label
        try:
            winner_pts, reason = await evaluate_winner_points(g.round_stats, g.team1_label, g.team2_label, g.team_size)
        except Exception:
            winner_pts, reason = 400, "أداء جيد."
        try:
            add_points(g.chat_id, "group", winner_pts, g.chat_name)
            update_win_loss(g.chat_id, "group", True)
        except Exception as e:
            print("finish_internal add_points error:", str(e)[:200])
        result_text = "الفائز: " + winner_label + "\nالنقاط: " + str(winner_pts) + "\n" + safe_str(reason, "")
    elif g.team2_points > g.team1_points:
        winner_label = t2_label
        try:
            winner_pts, reason = await evaluate_winner_points(g.round_stats, g.team2_label, g.team1_label, g.team_size)
        except Exception:
            winner_pts, reason = 400, "أداء جيد."
        try:
            add_points(g.chat_id, "group", winner_pts, g.chat_name)
            update_win_loss(g.chat_id, "group", True)
        except Exception as e:
            print("finish_internal add_points error:", str(e)[:200])
        result_text = "الفائز: " + winner_label + "\nالنقاط: " + str(winner_pts) + "\n" + safe_str(reason, "")
    else:
        try:
            add_points(g.chat_id, "group", 0, g.chat_name)
        except Exception:
            pass
        result_text = "انتهت المباراة بالتعادل."
    await cancel_round_tasks(g)
    await delete_round_messages(g)
    await delete_pinned(g, g.chat_id)
    await strip_buttons(g)
    await asyncio.sleep(0.2)
    await delete_tracked(g)
    await asyncio.sleep(0.2)
    try:
        await client.send_message(g.chat_id, "انتهت المباراة.\n\n" + result_text + "\n\nأرواح " + t1_label + ": " + str(g.team1_points) + "\nأرواح " + t2_label + ": " + str(g.team2_points))
    except Exception:
        pass
    for p in g.players:
        private_sessions.pop(p, None)
    internal_games.pop(g.chat_id, None)
    await notify_dev("انتهاء تحدي داخلي:\n" + display_group_name(g.chat_name) + "\nID: " + str(g.chat_id))


async def open_nomination(match):
    kb = [[Button.inline("ترشيح نفسي", ("nom_" + str(match.match_id)).encode())]]
    text1 = ("فتح باب الترشيح للتحدي\n\nالخصم: " + display_group_name(match.group2_name) + "\n\nمن يريد تمثيل الكروب يضغط زر ترشيح نفسي.\nسيتم اختيار أعلى " + str(match.squad) + " بحسب التصويت.")
    text2 = ("فتح باب الترشيح للتحدي\n\nالخصم: " + display_group_name(match.group1_name) + "\n\nمن يريد تمثيل الكروب يضغط زر ترشيح نفسي.\nسيتم اختيار أعلى " + str(match.squad) + " بحسب التصويت.")
    try:
        msg1 = await client.send_message(match.group1_id, text1, buttons=kb)
        match.tracked_messages.append((match.group1_id, msg1.id))
        match.nom_msg_ids[match.group1_id] = msg1.id
        try:
            await client.pin_message(match.group1_id, msg1, notify=False)
        except Exception:
            pass
        await asyncio.sleep(0.2)
    except Exception:
        pass
    try:
        msg2 = await client.send_message(match.group2_id, text2, buttons=kb)
        match.tracked_messages.append((match.group2_id, msg2.id))
        match.nom_msg_ids[match.group2_id] = msg2.id
        try:
            await client.pin_message(match.group2_id, msg2, notify=False)
        except Exception:
            pass
    except Exception:
        pass


@client.on(events.CallbackQuery(pattern=r"^nom_(\d+)$"))
@safe_execute
async def cb_nominate(event):
    mid = int(event.pattern_match.group(1))
    m = tournaments.get(mid)
    if not m or m.state != "nominating":
        return await event.answer("انتهى الترشيح.", alert=True)
    user = await event.get_sender()
    gid = event.chat_id
    if gid not in (m.group1_id, m.group2_id):
        return await event.answer("هذه المجموعة ليست في التحدي.", alert=True)
    cand = m.candidates[gid]
    if user.id in cand:
        return await event.answer("أنت مرشّح بالفعل.", alert=True)
    raw_name = clean_name_with_id(user.first_name, user.id, "لاعب")
    cand[user.id] = {"name": raw_name, "votes": set(), "ts": time.time()}
    await event.answer("تم ترشيحك.")
    await refresh_nom_msg(m, gid)


async def refresh_nom_msg(m, gid):
    cand = m.candidates[gid]
    lst = sorted(cand.items(), key=lambda x: (-len(x[1]["votes"]), x[1]["ts"]))
    header = m.group1_name if gid == m.group1_id else m.group2_name
    lines = ["المرشحون في " + display_group_name(header) + ":"]
    buttons = []
    for uid, d in lst:
        lines.append(user_link(uid, safe_str(d["name"], "")) + " — " + str(len(d["votes"])) + " صوت")
        buttons.append([Button.inline("تصويت لـ " + safe_str(d["name"], ""), ("vote_" + str(m.match_id) + "_" + str(gid) + "_" + str(uid)).encode())])
    buttons.append([Button.inline("إنهاء الترشيح الآن", ("close_nom_" + str(m.match_id) + "_" + str(gid)).encode())])
    new_text = "\n".join(lines)
    old_id = m.nom_msg_ids.get(gid)
    if old_id:
        try:
            await client.edit_message(gid, old_id, new_text, buttons=buttons)
            return
        except Exception:
            pass
    try:
        msg = await client.send_message(gid, new_text, buttons=buttons)
        m.nom_msg_ids[gid] = msg.id
        m.tracked_messages.append((gid, msg.id))
        await asyncio.sleep(0.2)
    except Exception:
        pass


@client.on(events.CallbackQuery(pattern=r"^vote_(\d+)_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_vote(event):
    mid = int(event.pattern_match.group(1))
    gid = int(event.pattern_match.group(2))
    uid = int(event.pattern_match.group(3))
    m = tournaments.get(mid)
    if not m or m.state != "nominating":
        return await event.answer("انتهى التصويت.", alert=True)
    voter = await event.get_sender()
    if voter.id == uid:
        return await event.answer("لا يمكنك التصويت لنفسك.", alert=True)
    if gid not in m.candidates or uid not in m.candidates[gid]:
        return await event.answer("المرشح غير موجود.", alert=True)
    for c in m.candidates[gid].values():
        c["votes"].discard(voter.id)
    m.candidates[gid][uid]["votes"].add(voter.id)
    await event.answer("تم التصويت.")
    await refresh_nom_msg(m, gid)


@client.on(events.CallbackQuery(pattern=r"^close_nom_(\d+)_(-?\d+)$"))
@safe_execute
async def cb_close_nom(event):
    mid = int(event.pattern_match.group(1))
    gid = int(event.pattern_match.group(2))
    m = tournaments.get(mid)
    if not m or m.state != "nominating":
        return await event.answer("لا يوجد ترشيح قائم.", alert=True)
    if event.sender_id != DEV_ID:
        try:
            perms = await client.get_permissions(gid, event.sender_id)
            if not perms.is_admin:
                return await event.answer("فقط المشرفون.", alert=True)
        except Exception:
            return await event.answer("تعذر التحقق.", alert=True)
    m.team1 = m.resolve_top(m.group1_id)
    m.team2 = m.resolve_top(m.group2_id)
    m.state = "playing"
    m.start_time = time.time()
    await event.answer("تم إغلاق الترشيح.")
    for gid_, team, gname in ((m.group1_id, m.team1, m.group1_name),
                              (m.group2_id, m.team2, m.group2_name)):
        names_list = []
        for p in team:
            names_list.append(user_link(p["user_id"], safe_str(p["name"], "")))
        names = "\n- ".join(names_list) if names_list else "لا يوجد"
        opp = m.group2_name if gid_ == m.group1_id else m.group1_name
        txt = ("الفريق الممثل لـ " + display_group_name(gname) + "\n\n"
               "الخصم: " + display_group_name(opp) + "\n\n"
               "الفريق:\n- " + names + "\n\n"
               "سيتم إرسال تفاصيل الجولة الأولى إليكم.")
        try:
            msg = await client.send_message(gid_, txt)
            m.tracked_messages.append((gid_, msg.id))
            await asyncio.sleep(0.2)
        except Exception:
            pass
    await send_teams_intro_tournament(m)
    await asyncio.sleep(4)
    await start_round_tournament(m)


async def send_teams_intro_tournament(m):
    t1_names = []
    for p in m.team1:
        t1_names.append(user_link(p["user_id"], safe_str(p["name"], "")))
    t2_names = []
    for p in m.team2:
        t2_names.append(user_link(p["user_id"], safe_str(p["name"], "")))
    text = ("بدء المباراة\n\n"
            + display_group_name(m.group1_name) + ":\n- " + "\n- ".join(t1_names) + "\n\n"
            + display_group_name(m.group2_name) + ":\n- " + "\n- ".join(t2_names) + "\n\n"
            "أرواح كل فريق: " + str(m.team1_points))
    for gid in m.both_groups():
        await send_to_group(m, gid, text, round_level=True)


def decide_bidder_tournament(m):
    if m.round % 2 == 1:
        first_team = m.first_bidder_team
    else:
        first_team = 2 if m.first_bidder_team == 1 else 1
    if first_team == 1:
        b = pick_next_player(m, 1)
        o = pick_next_player(m, 2)
        bteam = 1
    else:
        b = pick_next_player(m, 2)
        o = pick_next_player(m, 1)
        bteam = 2
    return b, o, bteam


async def start_round_tournament(m):
    try:
        if not m.team1 or not m.team2:
            try:
                await client.send_message(m.group1_id, "لا يمكن بدء التحدي: فريق فارغ.")
            except Exception:
                pass
            tournaments.pop(m.match_id, None)
            return
        await cancel_round_tasks(m)
        await delete_round_messages(m)
        m.state = "bidding"
        m.question = get_question()
        m.current_bid = 0
        m.answers = []
        m.opponent_resolved = False
        m.forced = False
        m.consecutive_timeouts = 0
        m.answer_watcher_task = None
        b, o, bteam = decide_bidder_tournament(m)
        m.bidder = b
        m.opponent = o
        for old_uid in list(private_sessions.keys()):
            s = private_sessions.get(old_uid)
            if s and s.get("game_type") == "tournament" and s.get("match_id") == m.match_id:
                private_sessions.pop(old_uid, None)
        private_sessions[b] = {"game_type": "tournament", "match_id": m.match_id, "role": "bidder"}
        private_sessions[o] = {"game_type": "tournament", "match_id": m.match_id, "role": "opponent"}
        bname = "لاعب غير معروف"
        oname = "لاعب غير معروف"
        for p in m.team1 + m.team2:
            if p["user_id"] == b:
                bname = p["name"]
            if p["user_id"] == o:
                oname = p["name"]
        b_team_label = m.group1_name if bteam == 1 else m.group2_name
        o_team_label = m.group2_name if bteam == 1 else m.group1_name
        for gid in m.both_groups():
            text = ("الجولة " + str(m.round) + "\n\n"
                    "السؤال: " + safe_str(m.question, "") + "\n\n"
                    "أرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\n"
                    "أرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points) + "\n\n"
                    "المزايد: " + user_link(b, bname) + " من " + display_group_name(b_team_label) + "\n"
                    "الخصم: " + user_link(o, oname) + " من " + display_group_name(o_team_label) + "\n\n"
                    "المزايدة في الخاص الآن، ولدى المزايد 20 ثانية.")
            await send_to_group(m, gid, text, round_level=True)
        kb_withdraw = [[Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
                       [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
        try:
            await client.send_message(b, "بدأت المزايدة للجولة " + str(m.round) + ".\nالسؤال: " + safe_str(m.question, "") + "\nأرسل رقمًا فقط خلال 20 ثانية.", buttons=kb_withdraw)
        except Exception:
            pass
        try:
            await client.send_message(o, "أنت الخصم في الجولة " + str(m.round) + ".\nالسؤال: " + safe_str(m.question, "") + "\nانتظر مزايدة خصمك ثم قرر.", buttons=kb_withdraw)
        except Exception:
            pass
        m.bidding_task = asyncio.create_task(bidding_timeout_tournament(m, b))
        asyncio.create_task(bidding_countdown(m, m.group1_id, b, " للمزايدة"))
    except Exception as e:
        print("start_round_tournament error:", str(e)[:300])


async def bidding_timeout_tournament(m, user_id):
    bid_at_start = m.current_bid
    await asyncio.sleep(20)
    if m.state != "bidding" or m.bidder != user_id:
        return
    if m.current_bid != bid_at_start:
        return
    m.consecutive_timeouts = getattr(m, "consecutive_timeouts", 0) + 1
    failing_team = 1 if any(p["user_id"] == user_id for p in m.team1) else 2
    if failing_team == 1:
        m.team1_points -= 1
        fail_name = m.group1_name
    else:
        m.team2_points -= 1
        fail_name = m.group2_name
    if m.consecutive_timeouts >= 2 or m.team1_points <= 0 or m.team2_points <= 0:
        for gid in m.both_groups():
            await send_to_group(m, gid, "تم إنهاء التحدي.\n\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points), round_level=True)
        await end_tournament_no_winner(m)
        return
    for gid in m.both_groups():
        text = ("انتهت مدة المزايدة دون رد.\n\n"
                + display_group_name(fail_name) + " فقد روحًا.\n\n"
                "أرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\n"
                "أرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points))
        await send_to_group(m, gid, text, round_level=True)
    try:
        if m.opponent and m.opponent != user_id:
            await client.send_message(m.opponent, "انتهت مدة مزايدة خصمك دون رد، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
    except Exception:
        pass
    await asyncio.sleep(1)
    await advance_round_tournament(m)


async def end_tournament_no_winner(m):
    m.state = "done"
    await cancel_round_tasks(m)
    await delete_round_messages(m)
    await strip_buttons(m)
    await asyncio.sleep(0.2)
    await delete_tracked(m)
    for p in m.team1 + m.team2:
        private_sessions.pop(p["user_id"], None)
    tournaments.pop(m.match_id, None)


async def advance_round_tournament(m):
    if m.team1_points <= 0 or m.team2_points <= 0:
        return await finish_tournament(m)
    m.round += 1
    await asyncio.sleep(1)
    try:
        await start_round_tournament(m)
    except Exception as e:
        print("advance_round_tournament error:", str(e)[:300])
        for gid in m.both_groups():
            try:
                await client.send_message(gid, "حدث خطأ أثناء بدء الجولة التالية.")
            except Exception:
                pass
        await finish_tournament(m)


async def finish_tournament(m):
    m.state = "done"
    m.end_time = time.time()
    if m.team1_points > m.team2_points:
        winner = m.group1_name
        try:
            winner_pts, reason = await evaluate_winner_points(m.round_stats, m.group1_name, m.group2_name, m.squad)
        except Exception:
            winner_pts, reason = 400, "أداء جيد."
        try:
            add_points(m.group1_id, "group", winner_pts, m.group1_name)
            add_points(m.group2_id, "group", -20, m.group2_name)
            update_win_loss(m.group1_id, "group", True)
            update_win_loss(m.group2_id, "group", False)
        except Exception as e:
            print("finish_tournament add_points error:", str(e)[:200])
        result_text = "الفائز: " + display_group_name(winner) + "\nالنقاط: " + str(winner_pts) + "\n" + safe_str(reason, "")
    elif m.team2_points > m.team1_points:
        winner = m.group2_name
        try:
            winner_pts, reason = await evaluate_winner_points(m.round_stats, m.group2_name, m.group1_name, m.squad)
        except Exception:
            winner_pts, reason = 400, "أداء جيد."
        try:
            add_points(m.group2_id, "group", winner_pts, m.group2_name)
            add_points(m.group1_id, "group", -20, m.group1_name)
            update_win_loss(m.group2_id, "group", True)
            update_win_loss(m.group1_id, "group", False)
        except Exception as e:
            print("finish_tournament add_points error:", str(e)[:200])
        result_text = "الفائز: " + display_group_name(winner) + "\nالنقاط: " + str(winner_pts) + "\n" + safe_str(reason, "")
    else:
        try:
            add_points(m.group1_id, "group", 0, m.group1_name)
            add_points(m.group2_id, "group", 0, m.group2_name)
        except Exception:
            pass
        result_text = "انتهت المباراة بالتعادل."
    await cancel_round_tasks(m)
    await delete_round_messages(m)
    await strip_buttons(m)
    await asyncio.sleep(0.2)
    await delete_tracked(m)
    await asyncio.sleep(0.2)
    for gid in m.both_groups():
        try:
            await client.send_message(gid, "انتهت المباراة.\n\n" + result_text + "\n\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points))
        except Exception:
            pass
    for p in m.team1 + m.team2:
        private_sessions.pop(p["user_id"], None)
    tournaments.pop(m.match_id, None)
    await notify_dev("انتهاء تحدي كروبين:\n" + display_group_name(m.group1_name) + " ضد " + display_group_name(m.group2_name) + "\n" + result_text)


async def answer_watcher_internal(g, bidder):
    bid_at_start = g.current_bid
    while True:
        await asyncio.sleep(0.2)
        if g.state != "answering" or g.bidder != bidder:
            return
        if g.current_bid != bid_at_start:
            return
        if len(g.answers) >= g.current_bid:
            break
    if g.state != "answering" or g.bidder != bidder:
        return
    if g.current_bid != bid_at_start:
        return
    try:
        if g.answer_task:
            g.answer_task.cancel()
    except Exception:
        pass
    await evaluate_internal(g, bidder)


async def answer_watcher_tournament(m, bidder):
    bid_at_start = m.current_bid
    while True:
        await asyncio.sleep(0.2)
        if m.state != "answering" or m.bidder != bidder:
            return
        if m.current_bid != bid_at_start:
            return
        if len(m.answers) >= m.current_bid:
            break
    if m.state != "answering" or m.bidder != bidder:
        return
    if m.current_bid != bid_at_start:
        return
    try:
        if m.answer_task:
            m.answer_task.cancel()
    except Exception:
        pass
    await evaluate_tournament(m, bidder)


@client.on(events.CallbackQuery(pattern=r"^force_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_force_internal(event):
    chat_id = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    uid = event.sender_id
    g = internal_games.get(chat_id)
    if not g:
        return await event.answer("انتهت اللعبة.", alert=True)
    if g.state != "opponent_choice":
        return await event.answer("الوقت انتهى أو تم اتخاذ القرار.", alert=True)
    if getattr(g, "opponent_resolved", False):
        return await event.answer("انتهى وقت القرار.", alert=True)
    if g.opponent != uid:
        return await event.answer("هذا الزر ليس لك.", alert=True)
    if g.current_bid < 1:
        return await event.answer("انتظر حتى يزايد الخصم.", alert=True)
    g.opponent_resolved = True
    g.forced = True
    await event.answer("تم إجبار الخصم على الإجابة.")
    try:
        if hasattr(g, "opponent_timeout_task") and g.opponent_timeout_task:
            g.opponent_timeout_task.cancel()
            g.opponent_timeout_task = None
    except Exception:
        pass
    try:
        await event.delete()
    except Exception:
        pass
    try:
        await event.respond("تم اتخاذ القرار.\n\nقرارك: إجبار الخصم على الإجابة بالرقم " + str(g.current_bid) + "\n\nسيتم إبلاغ الخصم الآن.")
    except Exception:
        pass
    bid = g.current_bid
    bname = g._name_cache.get(bidder, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
    oname = g._name_cache.get(uid, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
    text = ("تم إجبار اللاعب " + user_link(bidder, bname) + " على الإجابة بالرقم " + str(bid) + ".\n"
            "الخصم " + user_link(uid, oname) + " اتخذ القرار.\n"
            "على " + user_link(bidder, bname) + " أن يذكر " + str(bid) + " إجابة خلال 30 ثانية.")
    await send_to_group(g, g.chat_id, text, round_level=True)
    await begin_answer_internal(g, g.bidder)


async def begin_answer_internal(g, bidder):
    if g.current_bid < 1:
        return
    g.state = "answering"
    g.answers = []
    g.answer_start_time = time.time()
    kb = [[Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
          [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
    await client.send_message(bidder, "ابدأ بإرسال " + str(g.current_bid) + " إجابة، كل إجابة في رسالة منفصلة. الوقت: 30 ثانية.", buttons=kb)
    g.answer_task = asyncio.create_task(answer_timeout_internal(g, bidder))
    g.answer_watcher_task = asyncio.create_task(answer_watcher_internal(g, bidder))
    asyncio.create_task(answer_countdown(g, g.chat_id, bidder, " للإجابة"))


async def answer_timeout_internal(g, bidder):
    bid_at_start = g.current_bid
    await asyncio.sleep(30)
    if g.state != "answering" or g.bidder != bidder:
        return
    if g.current_bid != bid_at_start:
        return
    await evaluate_internal(g, bidder)


@client.on(events.CallbackQuery(pattern=r"^finish_int_(-?\d+)$"))
@safe_execute
async def cb_finish_internal(event):
    chat_id = int(event.pattern_match.group(1))
    g = internal_games.get(chat_id)
    if not g or g.state != "answering":
        return await event.answer("لا توجد إجابات قيد الانتظار.", alert=True)
    await event.answer("جاري التقييم.")
    try:
        if g.answer_task:
            g.answer_task.cancel()
    except Exception:
        pass
    try:
        if getattr(g, "answer_watcher_task", None):
            g.answer_watcher_task.cancel()
    except Exception:
        pass
    await evaluate_internal(g, g.bidder)


async def evaluate_internal(g, bidder):
    try:
        g.state = "evaluating"
        duration = time.time() - getattr(g, "answer_start_time", time.time())
        ok, reason = await evaluate_answers_with_ai(g.question, g.current_bid, g.answers)
        if not reason:
            reason = "نجح اللاعب." if ok else "لم يجمع العدد المطلوب."
        try:
            ent = await client.get_entity(bidder)
            bn = clean_name_with_id(ent.first_name, bidder, "لاعب")
        except Exception:
            bn = "لاعب " + str(bidder)
        team = 1 if bidder in g.team1 else 2
        if not g.team1_label:
            g.team1_label = "الفريق الأول"
        if not g.team2_label:
            g.team2_label = "الفريق الثاني"
        if ok:
            if team == 1:
                g.team2_points -= 1
                target_name = g.team2_label
            else:
                g.team1_points -= 1
                target_name = g.team1_label
            try:
                add_points(bidder, "player", 10, bn)
            except Exception as e:
                print("add_points error:", str(e)[:200])
            result_line = "نجاح اللاعب " + user_link(bidder, bn) + "\n" + reason + "\n\n" + target_name + " فقد روحًا."
        else:
            if team == 1:
                g.team1_points -= 1
            else:
                g.team2_points -= 1
            try:
                add_points(bidder, "player", -20, bn)
            except Exception as e:
                print("add_points error:", str(e)[:200])
            result_line = "فشل اللاعب " + user_link(bidder, bn) + "\n" + reason
        try:
            g.record_round(g.round, bidder, team, g.current_bid, ok, len(g.answers), duration, g.forced)
        except Exception as e:
            print("record_round error:", str(e)[:200])
        text = result_line + "\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points)
        await send_to_group(g, g.chat_id, text, round_level=True)
        try:
            await client.send_message(bidder, text)
        except Exception:
            pass
        other_id = g.opponent if bidder == g.bidder else g.bidder
        if other_id and other_id != bidder:
            try:
                summary = ("نتيجة جولة خصمك:\n" + result_line + "\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points))
                await client.send_message(other_id, summary)
            except Exception:
                pass
        await asyncio.sleep(1)
        if g.team1_points <= 0 or g.team2_points <= 0:
            await finish_internal(g)
            return
        await cancel_round_tasks(g)
        await advance_round_internal(g)
    except Exception as e:
        print("evaluate_internal error:", str(e)[:300])
        try:
            await send_to_group(g, g.chat_id, "حدث خطأ أثناء التقييم.", round_level=True)
        except Exception:
            pass
        await finish_internal(g)


@client.on(events.CallbackQuery(pattern=r"^force_t_(\d+)_(\d+)$"))
@safe_execute
async def cb_force_t(event):
    mid = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    uid = event.sender_id
    m = tournaments.get(mid)
    if not m:
        return await event.answer("انتهى التحدي.", alert=True)
    if m.state != "opponent_choice":
        return await event.answer("الوقت انتهى أو تم اتخاذ القرار.", alert=True)
    if getattr(m, "opponent_resolved", False):
        return await event.answer("انتهى وقت القرار.", alert=True)
    if m.opponent != uid:
        return await event.answer("هذا الزر ليس لك.", alert=True)
    if m.current_bid < 1:
        return await event.answer("انتظر حتى يزايد الخصم.", alert=True)
    m.opponent_resolved = True
    m.forced = True
    await event.answer("تم إجبار الخصم على الإجابة.")
    try:
        if hasattr(m, "opponent_timeout_task") and m.opponent_timeout_task:
            m.opponent_timeout_task.cancel()
            m.opponent_timeout_task = None
    except Exception:
        pass
    try:
        await event.delete()
    except Exception:
        pass
    try:
        await event.respond("تم اتخاذ القرار.\n\nقرارك: إجبار الخصم على الإجابة بالرقم " + str(m.current_bid) + "\n\nسيتم إبلاغ الخصم الآن.")
    except Exception:
        pass
    bid = m.current_bid
    bname = "لاعب غير معروف"
    oname = "لاعب غير معروف"
    for p in m.team1 + m.team2:
        if p["user_id"] == bidder:
            bname = p["name"]
        if p["user_id"] == uid:
            oname = p["name"]
    text = ("تم إجبار اللاعب " + user_link(bidder, bname) + " على الإجابة بالرقم " + str(bid) + ".\n"
            "الخصم " + user_link(uid, oname) + " اتخذ القرار.\n"
            "على " + user_link(bidder, bname) + " أن يذكر " + str(bid) + " إجابة خلال 30 ثانية.")
    for gid in m.both_groups():
        await send_to_group(m, gid, text, round_level=True)
    await begin_answer_tournament(m, m.bidder)


async def begin_answer_tournament(m, bidder):
    if m.current_bid < 1:
        return
    m.state = "answering"
    m.answers = []
    m.answer_start_time = time.time()
    kb = [[Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
          [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
    await client.send_message(bidder, "ابدأ بإرسال " + str(m.current_bid) + " إجابة، كل إجابة في رسالة منفصلة. الوقت: 30 ثانية.", buttons=kb)
    m.answer_task = asyncio.create_task(answer_timeout_tournament(m, bidder))
    m.answer_watcher_task = asyncio.create_task(answer_watcher_tournament(m, bidder))
    asyncio.create_task(answer_countdown(m, m.group1_id, bidder, " للإجابة"))


async def answer_timeout_tournament(m, bidder):
    bid_at_start = m.current_bid
    await asyncio.sleep(30)
    if m.state != "answering" or m.bidder != bidder:
        return
    if m.current_bid != bid_at_start:
        return
    await evaluate_tournament(m, bidder)


@client.on(events.CallbackQuery(pattern=r"^finish_t_(\d+)$"))
@safe_execute
async def cb_finish_t(event):
    mid = int(event.pattern_match.group(1))
    m = tournaments.get(mid)
    if not m or m.state != "answering":
        return await event.answer("لا توجد إجابات.", alert=True)
    await event.answer("جاري التقييم.")
    try:
        if m.answer_task:
            m.answer_task.cancel()
    except Exception:
        pass
    try:
        if getattr(m, "answer_watcher_task", None):
            m.answer_watcher_task.cancel()
    except Exception:
        pass
    await evaluate_tournament(m, m.bidder)


async def evaluate_tournament(m, bidder):
    try:
        m.state = "evaluating"
        duration = time.time() - getattr(m, "answer_start_time", time.time())
        ok, reason = await evaluate_answers_with_ai(m.question, m.current_bid, m.answers)
        if not reason:
            reason = "نجح اللاعب." if ok else "لم يجمع العدد المطلوب."
        team1_ids = [p["user_id"] for p in m.team1]
        team = 1 if bidder in team1_ids else 2
        bname = "لاعب غير معروف"
        for p in m.team1 + m.team2:
            if p["user_id"] == bidder:
                bname = p["name"]
                break
        if ok:
            if team == 1:
                m.team2_points -= 1
                target_name = m.group2_name
            else:
                m.team1_points -= 1
                target_name = m.group1_name
            try:
                add_points(bidder, "player", 10, bname)
            except Exception as e:
                print("add_points error:", str(e)[:200])
            result_line = "نجاح اللاعب " + user_link(bidder, bname) + "\n" + reason + "\n\n" + display_group_name(target_name) + " فقد روحًا."
        else:
            if team == 1:
                m.team1_points -= 1
            else:
                m.team2_points -= 1
            try:
                add_points(bidder, "player", -20, bname)
            except Exception as e:
                print("add_points error:", str(e)[:200])
            result_line = "فشل اللاعب " + user_link(bidder, bname) + "\n" + reason
        try:
            m.record_round(m.round, bidder, team, m.current_bid, ok, len(m.answers), duration, m.forced)
        except Exception as e:
            print("record_round error:", str(e)[:200])
        for gid in m.both_groups():
            text = result_line + "\n\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points)
            await send_to_group(m, gid, text, round_level=True)
        private_text = (result_line + "\n\n" +
                        "أرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\n" +
                        "أرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points))
        try:
            await client.send_message(bidder, private_text)
        except Exception:
            pass
        other_id = m.opponent if bidder == m.bidder else m.bidder
        if other_id and other_id != bidder:
            try:
                await client.send_message(other_id, "نتيجة جولة خصمك:\n" + private_text)
            except Exception:
                pass
        await asyncio.sleep(1)
        if m.team1_points <= 0 or m.team2_points <= 0:
            await finish_tournament(m)
            return
        await cancel_round_tasks(m)
        await advance_round_tournament(m)
    except Exception as e:
        print("evaluate_tournament error:", str(e)[:300])
        for gid in m.both_groups():
            try:
                await client.send_message(gid, "حدث خطأ أثناء التقييم.")
            except Exception:
                pass
        await finish_tournament(m)


@client.on(events.CallbackQuery(pattern=r"^wd_round_int_(-?\d+)$"))
@safe_execute
async def cb_wd_round_internal(event):
    chat_id = int(event.pattern_match.group(1))
    uid = event.sender_id
    g = internal_games.get(chat_id)
    if not g:
        return await event.answer("انتهت اللعبة.", alert=True)
    if uid not in g.players:
        return await event.answer("أنت لست لاعبًا.", alert=True)
    await event.answer("تم الانسحاب من الجولة.")
    await cancel_round_tasks(g)
    if uid in g.team1:
        g.team1_points -= 1
        fail_name = g.team1_label
    elif uid in g.team2:
        g.team2_points -= 1
        fail_name = g.team2_label
    else:
        fail_name = "الفريق"
    uname = g._name_cache.get(uid, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
    await send_to_group(g, g.chat_id, "انسحب اللاعب " + user_link(uid, uname) + ".\n\n" + fail_name + " فقد روحًا.\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points), round_level=True)
    other_id = g.opponent if uid == g.bidder else g.bidder
    if other_id and other_id != uid:
        try:
            await client.send_message(other_id, "خصمك " + user_link(uid, uname) + " انسحب من الجولة، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
        except Exception:
            pass
    if g.team1_points <= 0 or g.team2_points <= 0:
        await finish_internal(g)
        return
    await asyncio.sleep(1)
    await advance_round_internal(g)


@client.on(events.CallbackQuery(pattern=r"^wd_match_int_(-?\d+)$"))
@safe_execute
async def cb_wd_match_internal(event):
    chat_id = int(event.pattern_match.group(1))
    uid = event.sender_id
    g = internal_games.get(chat_id)
    if not g:
        return await event.answer("انتهت اللعبة.", alert=True)
    if uid not in g.players:
        return await event.answer("أنت لست لاعبًا.", alert=True)
    await event.answer("تم الانسحاب من المباراة.")
    uname = g._name_cache.get(uid, "لاعب غير معروف") if hasattr(g, "_name_cache") else "لاعب غير معروف"
    other_id = g.opponent if uid == g.bidder else g.bidder
    if other_id and other_id != uid:
        try:
            await client.send_message(other_id, "خصمك " + user_link(uid, uname) + " انسحب من المباراة كاملة.\n\nستنتهي المباراة لصالحك.")
        except Exception:
            pass
    if uid in g.team1:
        g.team1_points = 0
    elif uid in g.team2:
        g.team2_points = 0
    await finish_internal(g)


@client.on(events.CallbackQuery(pattern=r"^wd_round_t_(\d+)$"))
@safe_execute
async def cb_wd_round_tournament(event):
    mid = int(event.pattern_match.group(1))
    uid = event.sender_id
    m = tournaments.get(mid)
    if not m:
        return await event.answer("انتهى التحدي.", alert=True)
    team1_ids = [p["user_id"] for p in m.team1]
    team2_ids = [p["user_id"] for p in m.team2]
    if uid not in team1_ids and uid not in team2_ids:
        return await event.answer("أنت لست لاعبًا.", alert=True)
    await event.answer("تم الانسحاب من الجولة.")
    await cancel_round_tasks(m)
    if uid in team1_ids:
        m.team1_points -= 1
        fail_name = m.group1_name
    else:
        m.team2_points -= 1
        fail_name = m.group2_name
    uname = "لاعب غير معروف"
    for p in m.team1 + m.team2:
        if p["user_id"] == uid:
            uname = p["name"]
            break
    for gid in m.both_groups():
        await send_to_group(m, gid, "انسحب اللاعب " + user_link(uid, uname) + ".\n\n" + display_group_name(fail_name) + " فقد روحًا.\n\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points), round_level=True)
    other_id = m.opponent if uid == m.bidder else m.bidder
    if other_id and other_id != uid:
        try:
            await client.send_message(other_id, "خصمك " + user_link(uid, uname) + " انسحب من الجولة، وفريقه فقد روحًا.\n\nانتظر الجولة التالية.")
        except Exception:
            pass
    if m.team1_points <= 0 or m.team2_points <= 0:
        await finish_tournament(m)
        return
    await asyncio.sleep(1)
    await advance_round_tournament(m)


@client.on(events.CallbackQuery(pattern=r"^wd_match_t_(\d+)$"))
@safe_execute
async def cb_wd_match_tournament(event):
    mid = int(event.pattern_match.group(1))
    uid = event.sender_id
    m = tournaments.get(mid)
    if not m:
        return await event.answer("انتهى التحدي.", alert=True)
    team1_ids = [p["user_id"] for p in m.team1]
    team2_ids = [p["user_id"] for p in m.team2]
    if uid not in team1_ids and uid not in team2_ids:
        return await event.answer("أنت لست لاعبًا.", alert=True)
    await event.answer("تم الانسحاب من المباراة.")
    uname = "لاعب غير معروف"
    for p in m.team1 + m.team2:
        if p["user_id"] == uid:
            uname = p["name"]
            break
    other_id = m.opponent if uid == m.bidder else m.bidder
    if other_id and other_id != uid:
        try:
            await client.send_message(other_id, "خصمك " + user_link(uid, uname) + " انسحب من المباراة كاملة.\n\nستنتهي المباراة لصالحك.")
        except Exception:
            pass
    if uid in team1_ids:
        m.team1_points = 0
    else:
        m.team2_points = 0
    await finish_tournament(m)


@client.on(events.NewMessage(pattern=r"^/status(?:@\S+)?$"))
@safe_execute
async def cmd_status(event):
    if event.is_private:
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    if not await require_subscription(event):
        return
    g = internal_games.get(event.chat_id)
    if g:
        return await event.reply("اللعبة الداخلية الجارية:\nالحالة: " + g.state + "\nالجولة: " + str(g.round) + "\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points) + "\nعدد اللاعبين: " + str(len(g.players)))
    for m in tournaments.values():
        if event.chat_id in m.both_groups():
            return await event.reply("تحدي كروبين جارٍ.\nالحالة: " + m.state + "\nالجولة: " + str(m.round) + "\nأرواح " + display_group_name(m.group1_name) + ": " + str(m.team1_points) + "\nأرواح " + display_group_name(m.group2_name) + ": " + str(m.team2_points))
    await event.reply("لا توجد ألعاب جارية. أرسل /start_game للبدء.")


@client.on(events.NewMessage(pattern=r"^/end_game(?:@\S+)?$"))
@safe_execute
async def cmd_end(event):
    if event.is_private:
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    done = False
    if event.chat_id in internal_games:
        g = internal_games.pop(event.chat_id)
        g.state = "done"
        await cancel_round_tasks(g)
        await delete_round_messages(g)
        await delete_pinned(g, event.chat_id)
        await strip_buttons(g)
        await asyncio.sleep(0.2)
        await delete_tracked(g)
        for p in g.players:
            private_sessions.pop(p, None)
        done = True
    for mid, m in list(tournaments.items()):
        if event.chat_id in m.both_groups():
            m.state = "done"
            tournaments.pop(mid, None)
            await cancel_round_tasks(m)
            await delete_round_messages(m)
            await strip_buttons(m)
            await asyncio.sleep(0.2)
            await delete_tracked(m)
            for p in m.team1 + m.team2:
                private_sessions.pop(p["user_id"], None)
            done = True
    if done:
        await event.reply("تم إنهاء اللعبة الحالية.")
    else:
        await event.reply("لا توجد لعبة جارية.")


@client.on(events.NewMessage(pattern=r"^/top"))
@safe_execute
async def cmd_top(event):
    if event.is_private:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT name, points FROM groups ORDER BY points DESC LIMIT 5")
        gtop = c.fetchall()
        c.execute("SELECT user_id, name, points FROM players ORDER BY points DESC LIMIT 5")
        ptop = c.fetchall()
        conn.close()
        msg = "لوحة المتصدرين\n\nأفضل الكروبات:\n"
        if not gtop:
            msg += "لا يوجد\n"
        for r in gtop:
            msg += display_group_name(safe_str(r["name"], "")) + ": " + str(r["points"]) + " نقطة\n"
        msg += "\nأفضل اللاعبين:\n"
        if not ptop:
            msg += "لا يوجد\n"
        for r in ptop:
            uid = r["user_id"]
            name = clean_name_with_id(safe_str(r["name"], ""), uid, "لاعب")
            msg += user_link(uid, name) + ": " + str(r["points"]) + " نقطة\n"
        await event.reply(msg)
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    if not await require_subscription(event):
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name, points FROM groups ORDER BY points DESC LIMIT 5")
    gtop = c.fetchall()
    c.execute("SELECT user_id, name, points FROM players ORDER BY points DESC LIMIT 5")
    ptop = c.fetchall()
    conn.close()
    msg = "لوحة المتصدرين\n\nأفضل الكروبات:\n"
    if not gtop:
        msg += "لا يوجد\n"
    for r in gtop:
        msg += display_group_name(safe_str(r["name"], "")) + ": " + str(r["points"]) + " نقطة\n"
    msg += "\nأفضل اللاعبين:\n"
    if not ptop:
        msg += "لا يوجد\n"
    for r in ptop:
        uid = r["user_id"]
        name = clean_name_with_id(safe_str(r["name"], ""), uid, "لاعب")
        msg += user_link(uid, name) + ": " + str(r["points"]) + " نقطة\n"
    await event.reply(msg)


@client.on(events.NewMessage(pattern=r"^/help"))
@safe_execute
async def cmd_help(event):
    if event.is_private:
        await event.reply(
            "أهلاً بك في بوت المسابقات!\n\n"
            "الأوامر المتاحة:\n"
            "/start_game - بدء لعبة جديدة في المجموعات\n"
            "/1v1 - تحدي ثنائي بالرد\n"
            "/status - حالة اللعبة\n"
            "/top - عرض لوحة المتصدرين\n"
            "/ai_play - اللعب ضد الذكاء الاصطناعي في الخاص\n"
            "/ai_stop - إيقاف اللعبة ضد الذكاء الاصطناعي"
        )
        return
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    await event.reply(
        "الأوامر:\n"
        "/start_game عدد\n"
        "/1v1 بالرد\n"
        "/end_game\n"
        "/status\n"
        "/top\n"
        "/help"
    )


@client.on(events.NewMessage(pattern=r"^المطور$"))
@safe_execute
async def cmd_dev_announce(event):
    if event.is_private:
        return
    try:
        me = await client.get_entity(DEV_ID)
    except Exception:
        return
    first = safe_str(getattr(me, "first_name", ""), "")
    last = safe_str(getattr(me, "last_name", ""), "")
    full = (first + " " + last).strip() or "المطور"
    user_id = getattr(me, "id", DEV_ID)
    name_link = "[" + full + "](tg://user?id=" + str(user_id) + ")"
    text = name_link
    kb = [
        [Button.url("• " + DEV_BIO, "https://t.me/" + DEV_USERNAME)],
    ]
    photo = None
    try:
        photo = await client.download_profile_photo(me, file=bytes)
    except Exception:
        photo = None
    try:
        if photo:
            import io
            bio = io.BytesIO(photo)
            bio.name = "dev.jpg"
            await client.send_file(event.chat_id, bio, caption=text, buttons=kb, parse_mode="md")
        else:
            await event.reply(text, buttons=kb, parse_mode="md")
    except Exception:
        try:
            await event.reply(text, buttons=kb)
        except Exception:
            pass


@client.on(events.NewMessage(pattern=r"^/dev$", from_users=DEV_ID))
@safe_execute
async def cmd_dev(event):
    await send_dev_panel(event.sender_id)
    try:
        await event.delete()
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"dev_chat_mode"))
@safe_execute
async def cb_dev_chat_mode(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer("تم فتح المحادثة.")
    AI_CHAT_MODE[event.sender_id] = True
    try:
        await event.edit(
            "محادثة المطور مفتوحة\n\n"
            "اكتب أي شي:\n"
            "- سؤال → أرد عليك\n"
            "- أمر → أنفذه (مع تأكيد إذا خطير)\n\n"
            "لإغلاق المحادثة:\n"
            "/end_chat"
        )
    except Exception:
        await event.reply(
            "محادثة المطور مفتوحة\n\n"
            "اكتب أي شي:\n"
            "- سؤال → أرد عليك\n"
            "- أمر → أنفذه (مع تأكيد إذا خطير)\n\n"
            "لإغلاق المحادثة:\n"
            "/end_chat"
        )


@client.on(events.NewMessage(pattern=r"^/end_chat$", from_users=DEV_ID))
@safe_execute
async def cmd_end_chat(event):
    if not event.is_private:
        return
    AI_CHAT_MODE.pop(event.sender_id, None)
    await event.reply("تم إنهاء المحادثة.\n\nللعودة إلى لوحة المطور: /dev")


AI_CHAT_HISTORY = {}

async def handle_dev_chat(event, text):
    if not text:
        return
    uid = event.sender_id
    if uid not in AI_CHAT_HISTORY:
        AI_CHAT_HISTORY[uid] = []
    bot_context = await get_full_bot_context()
    tools_desc = (
        "الأدوات المتاحة (ارجع JSON فقط):\n\n"
        "1. حظر مجموعة: {\"action\": \"ban_group\", \"target\": \"اسم أو ID\"}\n"
        "2. إلغاء حظر مجموعة: {\"action\": \"unban_group\", \"target\": \"اسم أو ID\"}\n"
        "3. إيقاف لعبة: {\"action\": \"end_game\", \"target\": \"اسم أو ID\"}\n"
        "4. إعلان للكروبات: {\"action\": \"broadcast_groups\", \"message\": \"النص\"}\n"
        "5. إعلان للمستخدمين: {\"action\": \"broadcast_users\", \"message\": \"النص\"}\n"
        "6. إعلان للجميع: {\"action\": \"broadcast_all\", \"message\": \"النص\"}\n"
        "7. إحصائيات: {\"action\": \"stats\"}\n"
        "8. قائمة الكروبات: {\"action\": \"list_groups\"}\n"
        "9. قائمة المستخدمين: {\"action\": \"list_users\"}\n"
        "10. حالة لعبة: {\"action\": \"game_status\", \"target\": \"اسم أو ID\"}\n"
        "11. إرسال رسالة لمجموعة: {\"action\": \"send_message\", \"target\": \"اسم أو ID\", \"message\": \"النص\"}\n"
        "12. إعادة تشغيل البوت: {\"action\": \"restart_bot\"}\n"
        "13. معلومات مستخدم: {\"action\": \"user_info\", \"target\": \"اسم أو ID\"}\n"
        "14. محادثة حرة: {\"action\": \"chat\", \"message\": \"ردك باللهجة العراقية\"}\n"
        "15. معلومات البوت الكاملة: {\"action\": \"bot_full_info\"}\n\n"
        "أعد JSON فقط."
    )
    history = AI_CHAT_HISTORY[uid][-16:]
    history_text = ""
    for h in history:
        history_text += "" + h["user"] + "\nالبوت: " + h["bot"] + "\n\n"
    prompt = (
        "أنت مساعد ذكي يتحكم في بوت تلغرام، وتتكلم باللهجة العراقية.\n"
        "أنت تتحدث مع المطور، وردودك يجب أن تكون مترابطة مع المحادثة السابقة.\n\n"
        "معلومات حقيقية عن البوت الحالي:\n"
        + bot_context + "\n\n"
        "المحادثة السابقة:\n" + history_text + "\n"
        "المطور قال الآن: " + text + "\n\n"
        "إذا كان سؤال → استخدم action=chat ورد بالعراقي مستخدماً المعلومات أعلاه.\n"
        "إذا كان أمر → استخدم الأداة المناسبة.\n"
        "إذا سأل عن حالة البوت → استخدم action=bot_full_info.\n\n"
        + tools_desc
    )
    response = await _gemini_generate(prompt)
    if not response:
        return await event.reply("تعذر الاتصال بالذكاء الاصطناعي.")
    data = _parse_gemini_json(response)
    if not data:
        return await event.reply("ما فهمت طلبك. جرب مرة ثانية.")
    action = safe_str(data.get("action"), "")
    target = safe_str(data.get("target"), "")
    message = safe_str(data.get("message"), "")
    if action == "chat" and message:
        AI_CHAT_HISTORY[uid].append({"user": text, "bot": message})
        if len(AI_CHAT_HISTORY[uid]) > 20:
            AI_CHAT_HISTORY[uid] = AI_CHAT_HISTORY[uid][-20:]
    safe_actions = ("stats", "list_groups", "list_users", "game_status", "user_info", "chat", "bot_full_info")
    confirm_actions = ("ban_group", "unban_group", "end_game", "broadcast_groups", "broadcast_users", "broadcast_all", "send_message", "restart_bot")
    if action in safe_actions:
        await execute_ai_action(event, action, target, message)
        return
    if action in confirm_actions:
        AI_AGENT_STATE[event.sender_id] = {"action": action, "target": target, "message": message}
        preview = "تريدني أنفذ: " + action + "\n"
        if target:
            preview += "الهدف: " + target + "\n"
        if message:
            preview += "الرسالة: " + message[:100] + "\n"
        preview += "\nتأكد؟"
        kb = [[Button.inline("اي نفذ", b"ai_agent_confirm")], [Button.inline("لا", b"ai_agent_cancel")]]
        await event.reply(preview, buttons=kb)
        return
    await event.reply("ما فهمت شنو تريد.")


@client.on(events.CallbackQuery(data=b"dev_owner_info"))
@safe_execute
async def cb_dev_owner_info(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    if not DEV_ID:
        return
    try:
        ent = await client.get_entity(DEV_ID)
    except Exception:
        return
    first = safe_str(getattr(ent, "first_name", ""), "")
    last = safe_str(getattr(ent, "last_name", ""), "")
    full = (first + " " + last).strip() or "المطور"
    username = getattr(ent, "username", None)
    user_id = getattr(ent, "id", DEV_ID)
    lines = ["المطور", "الاسم: " + full]
    if username:
        lines.append("اليوزر: @" + username)
    lines.append("الآيدي: " + str(user_id))
    link = "tg://user?id=" + str(user_id)
    kb = [[Button.url("افتح حساب المطور", link)]]
    try:
        await event.edit("\n".join(lines), buttons=kb)
    except Exception:
        try:
            await event.reply("\n".join(lines), buttons=kb)
        except Exception:
            pass


@client.on(events.CallbackQuery(data=b"dev_back"))
@safe_execute
async def cb_dev_back(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    await send_dev_panel(event.sender_id)


async def _restart_bot():
    await client.disconnect()
    try:
        subprocess.Popen(["python", "main.py"])
    except Exception:
        pass
    os._exit(0)


print("Bot is running...")
client.run_until_disconnected()
