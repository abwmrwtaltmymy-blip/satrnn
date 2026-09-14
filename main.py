from telethon import TelegramClient, events, Button, functions
import asyncio
import time
import random
import json
import os
import shutil
import subprocess
import py_compile

from config import API_ID, API_HASH, BOT_TOKEN, DEV_ID
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
EDITABLE_FILES = ["main.py", "utils.py", "questions.py", "answers_bank.py", "game_manager.py", "database.py", "config.py"]
BACKUP_DIR = "file_backups"
EDIT_LOG = "edit_log.json"

os.makedirs(BACKUP_DIR, exist_ok=True)

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
        [Button.inline("الإشعارات", b"dev_menu_notif")],
        [Button.inline("الإحصائيات والفحص", b"dev_menu_stats")],
        [Button.inline("إدارة الملفات", b"dev_menu_files")],
        [Button.inline("قنوات الاشتراك الإجباري", b"dev_menu_subs")],
        [Button.inline("الحظر والإذاعة", b"dev_menu_ban")],
        [Button.inline("التشخيص الذكي", b"dev_menu_ai")],
        [Button.inline("معلومات المطور", b"dev_owner_info")],
    ]


def build_dev_notif_kb():
    return [
        [Button.inline("تشغيل الإشعارات", b"dev_notif_on")],
        [Button.inline("إيقاف الإشعارات", b"dev_notif_off")],
        [Button.inline("اختبار الإشعار", b"dev_notif_test")],
        [Button.inline("رجوع", b"dev_back")],
    ]


def build_dev_stats_kb():
    return [
        [Button.inline("إحصائيات البوت", b"dev_stats")],
        [Button.inline("فحص الأخطاء", b"dev_check_errors")],
        [Button.inline("تشخيص Gemini", b"dev_diag_gemini")],
        [Button.inline("رجوع", b"dev_back")],
    ]


def build_dev_files_kb():
    return [
        [Button.inline("عرض الملفات", b"dev_files_list")],
        [Button.inline("معلومات الملفات", b"dev_files_info")],
        [Button.inline("النسخ الاحتياطية", b"dev_files_backups")],
        [Button.inline("سجل التعديلات", b"dev_files_log")],
        [Button.inline("إعادة تشغيل البوت", b"dev_files_restart")],
        [Button.inline("تشغيل إعادة التشغيل التلقائي", b"dev_files_restart_on")],
        [Button.inline("إيقاف إعادة التشغيل التلقائي", b"dev_files_restart_off")],
        [Button.inline("تعليمات التعديل", b"dev_files_help")],
        [Button.inline("رجوع", b"dev_back")],
    ]


def build_dev_subs_kb():
    return [
        [Button.inline("قائمة القنوات", b"dev_list_subs")],
        [Button.inline("تعليمات الإضافة", b"dev_sub_help")],
        [Button.inline("رجوع", b"dev_back")],
    ]


def build_dev_ban_kb():
    return [
        [Button.inline("قائمة المحظورات", b"dev_list_banned")],
        [Button.inline("تعليمات الحظر", b"dev_ban_help")],
        [Button.inline("تعليمات الإذاعة", b"dev_broadcast_help")],
        [Button.inline("رجوع", b"dev_back")],
    ]


def build_dev_ai_kb():
    return [
        [Button.inline("تشخيص مشكلة", b"dev_ai_diagnose_help")],
        [Button.inline("رجوع", b"dev_back")],
    ]


async def send_dev_panel(user_id):
    val = get_setting("dev_notifications", "1")
    status = "مفعلة" if val == "1" else "متوقفة"
    text = ("لوحة تحكم المطور\n\nحالة الإشعارات: " + status + "\n\nاختر القسم:")
    try:
        await client.send_message(user_id, text, buttons=build_dev_main_kb())
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
        register_user(user.id, clean_name(user.first_name))
        if not payload.startswith("join_"):
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
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
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


@client.on(events.NewMessage(func=lambda e: e.is_private))
@safe_execute
async def private_handler(event):
    uid = event.sender_id
    user = await event.get_sender()
    try:
        register_user(uid, clean_name(user.first_name))
    except Exception:
        pass

    if uid in ai_games:
        ag = ai_games[uid]
        txt = safe_str(event.text, "").strip()
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

    if DEV_STATE.get("ai_fix_waiting") == uid:
        issue_text = safe_str(event.text, "").strip()
        if issue_text.startswith("/"):
            return
        DEV_STATE.pop("ai_fix_waiting", None)
        await event.reply("جاري تحليل المشكلة...")
        analysis, solution, code = await ai_diagnose_issue(issue_text)
        text = ("تحليل المشكلة:\n" + analysis + "\n\nالحل المقترح:\n" + solution)
        if code:
            text += "\n\nالكود المقترح:\n" + code[:3500]
        try:
            await event.reply(text[:4000])
        except Exception:
            pass
        return

    sess = private_sessions.get(uid)
    if not sess:
        return
    text = safe_str(event.text, "").strip()

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


async def answer_watcher_internal(g, bidder):
    bid_at_start = g.current_bid
    while True:
        await asyncio.sleep(1)
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
        await asyncio.sleep(1)
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


@client.on(events.NewMessage(pattern=r"^/top(?:@\S+)?$"))
@safe_execute
async def cmd_top(event):
    if event.is_private:
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


@client.on(events.NewMessage(pattern=r"^/help(?:@\S+)?$"))
@safe_execute
async def cmd_help(event):
    if event.is_private:
        return
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    await event.reply("الأوامر:\n/start_game عدد\n/1v1 بالرد\n/end_game\n/status\n/top\n/help")


@client.on(events.NewMessage(pattern=r"^/dev$", from_users=DEV_ID))
@safe_execute
async def cmd_dev(event):
    await send_dev_panel(event.sender_id)
    try:
        await event.delete()
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"dev_menu_notif"))
@safe_execute
async def cb_dev_menu_notif(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    val = get_setting("dev_notifications", "1")
    status = "مفعلة" if val == "1" else "متوقفة"
    text = ("إعدادات الإشعارات\n\nحالة الإشعارات: " + status)
    try:
        await event.edit(text, buttons=build_dev_notif_kb())
    except Exception:
        await event.reply(text, buttons=build_dev_notif_kb())


@client.on(events.CallbackQuery(data=b"dev_menu_stats"))
@safe_execute
async def cb_dev_menu_stats(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    try:
        await event.edit("الإحصائيات والفحص", buttons=build_dev_stats_kb())
    except Exception:
        await event.reply("الإحصائيات والفحص", buttons=build_dev_stats_kb())


@client.on(events.CallbackQuery(data=b"dev_menu_files"))
@safe_execute
async def cb_dev_menu_files(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    try:
        await event.edit("إدارة الملفات", buttons=build_dev_files_kb())
    except Exception:
        await event.reply("إدارة الملفات", buttons=build_dev_files_kb())


@client.on(events.CallbackQuery(data=b"dev_menu_subs"))
@safe_execute
async def cb_dev_menu_subs(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    try:
        await event.edit("قنوات الاشتراك الإجباري", buttons=build_dev_subs_kb())
    except Exception:
        await event.reply("قنوات الاشتراك الإجباري", buttons=build_dev_subs_kb())


@client.on(events.CallbackQuery(data=b"dev_menu_ban"))
@safe_execute
async def cb_dev_menu_ban(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    try:
        await event.edit("الحظر والإذاعة", buttons=build_dev_ban_kb())
    except Exception:
        await event.reply("الحظر والإذاعة", buttons=build_dev_ban_kb())


@client.on(events.CallbackQuery(data=b"dev_menu_ai"))
@safe_execute
async def cb_dev_menu_ai(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("التشخيص الذكي\n\n"
            "يمكنك إرسال وصف مشكلة، وسيقوم الذكاء الاصطناعي بتحليلها واقتراح حل.\n"
            "استخدم الأمر التالي:\n"
            "/ai_fix")
    try:
        await event.edit(text, buttons=build_dev_ai_kb())
    except Exception:
        await event.reply(text, buttons=build_dev_ai_kb())


@client.on(events.NewMessage(pattern=r"^/ai_fix$", from_users=DEV_ID))
@safe_execute
async def cmd_ai_fix(event):
    DEV_STATE["ai_fix_waiting"] = event.sender_id
    await event.reply("أرسل وصف المشكلة التي تواجهها، وسأحللها بالذكاء الاصطناعي.\n\nللإلغاء أرسل /ai_fix_cancel")


@client.on(events.NewMessage(pattern=r"^/ai_fix_cancel$", from_users=DEV_ID))
@safe_execute
async def cmd_ai_fix_cancel(event):
    DEV_STATE.pop("ai_fix_waiting", None)
    await event.reply("تم الإلغاء.")


@client.on(events.CallbackQuery(data=b"dev_ai_diagnose_help"))
@safe_execute
async def cb_dev_ai_diagnose_help(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("كيفية استخدام التشخيص الذكي:\n\n"
            "1. أرسل الأمر /ai_fix\n"
            "2. أرسل وصف المشكلة بالتفصيل\n"
            "3. سيقوم الذكاء الاصطناعي بتحليلها واقتراح حل مع كود إن أمكن\n\n"
            "ملاحظة: التطبيق التلقائي غير مفعل حالياً، سيتم عرض الحل فقط.")
    try:
        await event.edit(text, buttons=[[Button.inline("رجوع", b"dev_menu_ai")]])
    except Exception:
        await event.reply(text, buttons=[[Button.inline("رجوع", b"dev_menu_ai")]])


@client.on(events.CallbackQuery(data=b"dev_back"))
@safe_execute
async def cb_dev_back(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    val = get_setting("dev_notifications", "1")
    status = "مفعلة" if val == "1" else "متوقفة"
    text = ("لوحة تحكم المطور\n\nحالة الإشعارات: " + status + "\n\nاختر القسم:")
    try:
        await event.edit(text, buttons=build_dev_main_kb())
    except Exception:
        await event.reply(text, buttons=build_dev_main_kb())


@client.on(events.CallbackQuery(data=b"dev_notif_on"))
@safe_execute
async def cb_dev_notif_on(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    set_setting("dev_notifications", "1")
    await event.answer("تم التشغيل.")
    try:
        await event.edit("إعدادات الإشعارات\n\nحالة الإشعارات: مفعلة", buttons=build_dev_notif_kb())
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"dev_notif_off"))
@safe_execute
async def cb_dev_notif_off(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    set_setting("dev_notifications", "0")
    await event.answer("تم الإيقاف.")
    try:
        await event.edit("إعدادات الإشعارات\n\nحالة الإشعارات: متوقفة", buttons=build_dev_notif_kb())
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"dev_notif_test"))
@safe_execute
async def cb_dev_notif_test(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await notify_dev("هذا إشعار تجريبي من البوت.")
    await event.answer("تم إرسال إشعار تجريبي.")


@client.on(events.CallbackQuery(data=b"dev_stats"))
@safe_execute
async def cb_dev_stats(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    s = get_stats()
    text = ("إحصائيات البوت\n\n"
            "المجموعات: " + str(s["groups"]) + "\n"
            "اللاعبون: " + str(s["players"]) + "\n"
            "المستخدمون: " + str(s["users"]) + "\n"
            "المجموعات المحظورة: " + str(s["banned"]) + "\n"
            "قنوات الاشتراك: " + str(s["subs"]))
    kb = [[Button.inline("رجوع", b"dev_menu_stats")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_check_errors"))
@safe_execute
async def cb_dev_check_errors(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer("جاري الفحص...")
    issues = []
    try:
        me = await client.get_me()
        if not me:
            issues.append("تعذر الحصول على بيانات الحساب.")
    except Exception as e:
        issues.append("client.get_me: " + str(e)[:100])
    try:
        from utils import diagnose_gemini
        diag = diagnose_gemini()
        if not diag["working"]:
            if not diag["key_exists"]:
                issues.append("Gemini: المفتاح غير موجود.")
            elif diag["key_length"] < 20:
                issues.append("Gemini: المفتاح قصير.")
            elif not diag["client_created"]:
                issues.append("Gemini: فشل إنشاء client.")
            else:
                issues.append("Gemini: لا يعمل مع أي موديل.")
    except Exception as e:
        issues.append("فحص Gemini: " + str(e)[:150])
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r["name"] for r in c.fetchall()]
        conn.close()
        needed = ["groups", "players", "all_users", "banned_groups", "force_subs", "settings"]
        for t in needed:
            if t not in tables:
                issues.append("جدول ناقص: " + t)
    except Exception as e:
        issues.append("فحص قاعدة البيانات: " + str(e)[:150])
    lines = ["فحص الأخطاء"]
    if not issues:
        lines.append("لا توجد أخطاء ظاهرة.")
    else:
        lines.append("الأخطاء:")
        for i in issues:
            lines.append("- " + i)
    lines.append("")
    lines.append("الألعاب النشطة:")
    lines.append("داخلية: " + str(len(internal_games)))
    lines.append("كروبين: " + str(len(tournaments)))
    lines.append("AI: " + str(len(ai_games)))
    lines.append("جلسات: " + str(len(private_sessions)))
    lines.append("قائمة الانتظار: " + str(len(matchmaking_pool)))
    text = "\n".join(lines)
    kb = [[Button.inline("تشخيص Gemini بالتفصيل", b"dev_diag_gemini")],
          [Button.inline("رجوع", b"dev_menu_stats")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        try:
            await event.reply(text, buttons=kb)
        except Exception:
            pass


@client.on(events.CallbackQuery(data=b"dev_diag_gemini"))
@safe_execute
async def cb_dev_diag_gemini(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer("جاري الفحص...")
    try:
        from utils import diagnose_gemini
        diag = diagnose_gemini()
    except Exception as e:
        try:
            await event.edit("فشل: " + str(e)[:200], buttons=[[Button.inline("رجوع", b"dev_menu_stats")]])
        except Exception:
            pass
        return
    lines = ["تشخيص Gemini", ""]
    if diag["key_exists"]:
        lines.append("المفتاح موجود: نعم")
        lines.append("طول المفتاح: " + str(diag["key_length"]))
        lines.append("بداية المفتاح: " + diag["key_start"])
    else:
        lines.append("المفتاح موجود: لا")
    lines.append("العميل أُنشئ: " + ("نعم" if diag["client_created"] else "لا"))
    lines.append("")
    available = diag.get("models_available", [])
    lines.append("الموديلات المتاحة (" + str(len(available)) + "):")
    if available:
        for name in available[:15]:
            lines.append("- " + name)
    else:
        lines.append("- لم تُجلب القائمة")
    lines.append("")
    lines.append("نتائج الاختبار:")
    tested = diag.get("models_tested", [])
    if tested:
        for m in tested:
            lines.append("- " + m[:80])
    else:
        lines.append("- لم يتم اختبار أي موديل")
    lines.append("")
    lines.append("الخلاصة: " + ("يعمل" if diag["working"] else "لا يعمل"))
    text = "\n".join(lines)[:4000]
    kb = [[Button.inline("رجوع", b"dev_menu_stats")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        try:
            await event.reply(text, buttons=kb)
        except Exception:
            pass


@client.on(events.CallbackQuery(data=b"dev_files_list"))
@safe_execute
async def cb_dev_files_list(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    lines = ["الملفات المتاحة:"]
    for f in EDITABLE_FILES:
        if os.path.exists(f):
            s = os.path.getsize(f)
            if s < 1024:
                sz = str(s) + "B"
            elif s < 1024 * 1024:
                sz = str(round(s / 1024, 1)) + "KB"
            else:
                sz = str(round(s / (1024 * 1024), 2)) + "MB"
            lines.append("- " + f + " (" + sz + ")")
        else:
            lines.append("- " + f + " (غير موجود)")
    lines.append("")
    lines.append("للتعديل: /edit_upload اسم_الملف")
    text = "\n".join(lines)
    kb = [[Button.inline("رجوع", b"dev_menu_files")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_files_info"))
@safe_execute
async def cb_dev_files_info(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    lines = ["معلومات الملفات", ""]
    total_size = 0
    total_lines = 0
    for f in EDITABLE_FILES:
        if not os.path.exists(f):
            lines.append(f + " | غير موجود")
            continue
        try:
            size = os.path.getsize(f)
            total_size += size
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f)))
            with open(f, "r", encoding="utf-8") as fh:
                content = fh.read()
            line_count = content.count("\n") + 1
            total_lines += line_count
            lines.append(f + "\n  الحجم: " + str(size) + "B\n  الأسطر: " + str(line_count) + "\n  آخر تعديل: " + mtime)
        except Exception as e:
            lines.append(f + " | خطأ: " + str(e)[:80])
    lines.append("")
    lines.append("الإجمالي:")
    lines.append("  عدد الملفات: " + str(len([f for f in EDITABLE_FILES if os.path.exists(f)])))
    lines.append("  إجمالي الأسطر: " + str(total_lines))
    text = "\n".join(lines)[:4000]
    kb = [[Button.inline("رجوع", b"dev_menu_files")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_files_backups"))
@safe_execute
async def cb_dev_files_backups(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    lines = ["النسخ الاحتياطية:"]
    for f in EDITABLE_FILES:
        prefix = f + "_"
        try:
            files = [x for x in os.listdir(BACKUP_DIR) if x.startswith(prefix) and x.endswith(".bak")]
            files.sort(reverse=True)
        except Exception:
            files = []
        lines.append("")
        lines.append(f + ":")
        if not files:
            lines.append("  لا يوجد")
        else:
            for b in files[:5]:
                lines.append("  - " + b)
    text = "\n".join(lines)[:4000]
    kb = [[Button.inline("رجوع", b"dev_menu_files")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_files_log"))
@safe_execute
async def cb_dev_files_log(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    if not os.path.exists(EDIT_LOG):
        text = "السجل فارغ."
    else:
        try:
            with open(EDIT_LOG, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []
        if not log:
            text = "السجل فارغ."
        else:
            lines = ["سجل التعديلات (آخر 20):"]
            for entry in log[-20:]:
                lines.append(entry.get("time", "") + " | " + entry.get("action", "") + " | " + entry.get("file", ""))
            text = "\n".join(lines)[:4000]
    kb = [[Button.inline("رجوع", b"dev_menu_files")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_files_restart"))
@safe_execute
async def cb_dev_files_restart(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer("جاري إعادة التشغيل...")
    try:
        await event.edit("جاري إعادة التشغيل...")
    except Exception:
        pass
    await asyncio.sleep(2)
    await _restart_bot()


@client.on(events.CallbackQuery(data=b"dev_files_restart_on"))
@safe_execute
async def cb_dev_files_restart_on(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    RESTART_AUTO["enabled"] = True
    await event.answer("تم التشغيل.")


@client.on(events.CallbackQuery(data=b"dev_files_restart_off"))
@safe_execute
async def cb_dev_files_restart_off(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    RESTART_AUTO["enabled"] = False
    await event.answer("تم الإيقاف.")


@client.on(events.CallbackQuery(data=b"dev_files_help"))
@safe_execute
async def cb_dev_files_help(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("تعليمات إدارة الملفات:\n\n"
            "عرض ملف:\n"
            "/edit اسم_الملف\n\n"
            "رفع محتوى جديد:\n"
            "/edit_upload اسم_الملف\n"
            "ثم أرسل المحتوى كاملاً\n\n"
            "إنشاء نسخة احتياطية:\n"
            "/file_backup اسم_الملف\n\n"
            "استعادة نسخة:\n"
            "/file_restore اسم_النسخة\n\n"
            "عرض السجل:\n"
            "/edit_log\n\n"
            "إعادة تشغيل البوت:\n"
            "/restart")
    kb = [[Button.inline("رجوع", b"dev_menu_files")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


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
    lines = ["المطور"]
    lines.append("الاسم: " + full)
    if username:
        lines.append("اليوزر: @" + username)
    lines.append("الآيدي: " + str(user_id))
    link = "tg://user?id=" + str(user_id)
    kb = [[Button.url("افتح حساب المطور", link)],
          [Button.inline("رجوع", b"dev_back")]]
    try:
        await event.edit("\n".join(lines), buttons=kb)
    except Exception:
        try:
            await event.reply("\n".join(lines), buttons=kb)
        except Exception:
            pass


@client.on(events.CallbackQuery(data=b"dev_list_subs"))
@safe_execute
async def cb_dev_list_subs(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    subs = get_force_subs()
    if not subs:
        text = "لا توجد قنوات اشتراك إجباري."
    else:
        lines = ["قنوات الاشتراك الإجباري:"]
        for s in subs:
            lines.append(safe_str(s["username"], "") + " (وضع الطلب: " + str(s["is_request_mode"]) + ")")
        text = "\n".join(lines)
    kb = [[Button.inline("رجوع", b"dev_menu_subs")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_sub_help"))
@safe_execute
async def cb_dev_sub_help(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("تعليمات قنوات الاشتراك الإجباري:\n\n"
            "إضافة قناة بدون وضع الطلب:\n"
            "/add_sub @channel 0\n\n"
            "إضافة قناة مع وضع الطلب:\n"
            "/add_sub @channel 1\n\n"
            "حذف قناة:\n"
            "/remove_sub @channel\n\n"
            "عرض القائمة:\n"
            "/list_subs")
    kb = [[Button.inline("رجوع", b"dev_menu_subs")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_list_banned"))
@safe_execute
async def cb_dev_list_banned(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM banned_groups")
    rows = c.fetchall()
    conn.close()
    if not rows:
        text = "لا توجد مجموعات محظورة."
    else:
        lines = ["المجموعات المحظورة:"]
        for r in rows:
            lines.append(str(r["chat_id"]))
        text = "\n".join(lines)
    kb = [[Button.inline("رجوع", b"dev_menu_ban")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_ban_help"))
@safe_execute
async def cb_dev_ban_help(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("تعليمات الحظر:\n\n"
            "حظر مجموعة:\n"
            "/ban_group رقم_المجموعة\n\n"
            "إلغاء حظر مجموعة:\n"
            "/unban_group رقم_المجموعة\n\n"
            "مثال:\n"
            "/ban_group -1001234567890")
    kb = [[Button.inline("رجوع", b"dev_menu_ban")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.CallbackQuery(data=b"dev_broadcast_help"))
@safe_execute
async def cb_dev_broadcast_help(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("تعليمات الإذاعة:\n\n"
            "إذاعة للمجموعات:\n"
            "/broadcast_groups نص الرسالة\n\n"
            "إذاعة لمستخدمي الخاص:\n"
            "/broadcast_users نص الرسالة\n\n"
            "إذاعة للجميع:\n"
            "/broadcast_all نص الرسالة")
    kb = [[Button.inline("رجوع", b"dev_menu_ban")]]
    try:
        await event.edit(text, buttons=kb)
    except Exception:
        await event.reply(text, buttons=kb)


@client.on(events.NewMessage(pattern=r"^/broadcast_groups (.+)", from_users=DEV_ID))
@safe_execute
async def cmd_broadcast_groups(event):
    text = event.pattern_match.group(1)
    groups = get_all_groups()
    sent = 0
    for gid in groups:
        if is_banned(gid):
            continue
        try:
            await client.send_message(gid, text)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    await event.reply("تم الإرسال إلى " + str(sent) + " مجموعة.")


@client.on(events.NewMessage(pattern=r"^/broadcast_users (.+)", from_users=DEV_ID))
@safe_execute
async def cmd_broadcast_users(event):
    text = event.pattern_match.group(1)
    users = get_all_users()
    sent = 0
    for uid in users:
        try:
            await client.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    await event.reply("تم الإرسال إلى " + str(sent) + " مستخدم.")


@client.on(events.NewMessage(pattern=r"^/broadcast_all (.+)", from_users=DEV_ID))
@safe_execute
async def cmd_broadcast_all(event):
    text = event.pattern_match.group(1)
    groups = get_all_groups()
    users = get_all_users()
    gs = 0
    us = 0
    for gid in groups:
        if is_banned(gid):
            continue
        try:
            await client.send_message(gid, text)
            gs += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    for uid in users:
        try:
            await client.send_message(uid, text)
            us += 1
            await asyncio.sleep(0.5)
        except Exception:
            pass
    await event.reply("تم الإرسال إلى " + str(gs) + " مجموعة و " + str(us) + " مستخدم.")


@client.on(events.NewMessage(pattern=r"^/ban_group (-?\d+)$", from_users=DEV_ID))
@safe_execute
async def cmd_ban(event):
    gid = int(event.pattern_match.group(1))
    ban_group(gid)
    try:
        await client.send_message(gid, "لقد تم حظر مجموعتكم من استعمال البوت.")
    except Exception:
        pass
    await event.reply("تم حظر المجموعة " + str(gid) + ".")
    await notify_dev("تم حظر مجموعة:\nID: " + str(gid))


@client.on(events.NewMessage(pattern=r"^/unban_group (-?\d+)$", from_users=DEV_ID))
@safe_execute
async def cmd_unban(event):
    gid = int(event.pattern_match.group(1))
    unban_group(gid)
    await event.reply("تم إلغاء حظر المجموعة " + str(gid) + ".")
    await notify_dev("تم إلغاء حظر مجموعة:\nID: " + str(gid))


@client.on(events.NewMessage(pattern=r"^/stats$", from_users=DEV_ID))
@safe_execute
async def cmd_stats(event):
    s = get_stats()
    await event.reply("الإحصائيات:\nالمجموعات: " + str(s["groups"]) + "\nاللاعبون: " + str(s["players"]) + "\nالمستخدمون: " + str(s["users"]) + "\nالمحظورة: " + str(s["banned"]) + "\nقنوات الاشتراك: " + str(s["subs"]))


@client.on(events.NewMessage(pattern=r"^/add_sub (\S+) (\d)$", from_users=DEV_ID))
@safe_execute
async def cmd_add_sub(event):
    uname = event.pattern_match.group(1)
    mode = int(event.pattern_match.group(2))
    try:
        ent = await client.get_entity(uname)
    except Exception as e:
        return await event.reply("تعذر إيجاد القناة: " + str(e))
    final = uname if uname.startswith("@") else "@" + uname
    add_force_sub(ent.id, final, mode)
    await event.reply("تمت الإضافة: " + final)


@client.on(events.NewMessage(pattern=r"^/remove_sub (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_remove_sub(event):
    uname = event.pattern_match.group(1)
    try:
        ent = await client.get_entity(uname)
        remove_force_sub(ent.id)
    except Exception:
        remove_force_sub(0)
    await event.reply("تم الحذف: " + uname)


@client.on(events.NewMessage(pattern=r"^/list_subs$", from_users=DEV_ID))
@safe_execute
async def cmd_list_subs(event):
    subs = get_force_subs()
    if not subs:
        return await event.reply("لا توجد قنوات اشتراك إجباري.")
    lines = ["قنوات الاشتراك:"]
    for s in subs:
        lines.append(safe_str(s["username"], "") + " (وضع: " + str(s["is_request_mode"]) + ")")
    await event.reply("\n".join(lines))


@client.on(events.NewMessage(pattern=r"^/edit (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_edit(event):
    fname = event.pattern_match.group(1)
    if fname not in EDITABLE_FILES:
        return await event.reply("الملف غير مسموح بتعديله.")
    if not os.path.exists(fname):
        return await event.reply("الملف غير موجود.")
    try:
        with open(fname, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return await event.reply("فشل قراءة الملف: " + str(e)[:150])
    if len(content) > 3800:
        await event.reply("الملف كبير (" + str(len(content)) + " حرف). سيتم إرساله مقسم.")
        for i in range(0, len(content), 3500):
            chunk = content[i:i + 3500]
            try:
                await event.reply("[" + str(i) + "-" + str(i + len(chunk)) + "]\n" + chunk)
            except Exception:
                pass
    else:
        await event.reply("محتوى " + fname + ":\n\n" + content)


@client.on(events.NewMessage(pattern=r"^/edit_upload (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_edit_upload(event):
    fname = event.pattern_match.group(1)
    if fname not in EDITABLE_FILES:
        return await event.reply("الملف غير مسموح.")
    DEV_STATE["edit_file"] = fname
    await event.reply("أرسل الآن محتوى " + fname + " كاملاً في رسالة واحدة.\nسيتم فحصه قبل الحفظ.\n\nللإلغاء: /edit_cancel")


@client.on(events.NewMessage(pattern=r"^/edit_cancel$", from_users=DEV_ID))
@safe_execute
async def cmd_edit_cancel(event):
    DEV_STATE.pop("edit_file", None)
    await event.reply("تم الإلغاء.")


@client.on(events.NewMessage(func=lambda e: e.is_private and e.sender_id == DEV_ID and not e.text.startswith("/") and DEV_STATE.get("edit_file")))
@safe_execute
async def cmd_edit_receive(event):
    fname = DEV_STATE.get("edit_file")
    if not fname:
        return
    DEV_STATE.pop("edit_file", None)
    content = event.text
    if not content:
        return await event.reply("المحتوى فارغ.")
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, fname + "_" + ts + ".bak")
        if os.path.exists(fname):
            shutil.copy(fname, dest)
    except Exception:
        pass
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return await event.reply("فشل الحفظ: " + str(e)[:150])
    try:
        py_compile.compile(fname, doraise=True)
    except Exception as e:
        return await event.reply("تم الحفظ لكن يوجد خطأ في الكود:\n" + str(e)[:300])
    try:
        with open(EDIT_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        log = []
    log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "edit", "file": fname, "info": "حفظ جديد"})
    try:
        with open(EDIT_LOG, "w", encoding="utf-8") as f:
            json.dump(log[-200:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    await event.reply("تم حفظ " + fname + " بنجاح.")
    if RESTART_AUTO.get("enabled"):
        await event.reply("جاري إعادة التشغيل التلقائي...")
        await asyncio.sleep(2)
        await _restart_bot()


@client.on(events.NewMessage(pattern=r"^/file_backup (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_file_backup(event):
    fname = event.pattern_match.group(1)
    if fname not in EDITABLE_FILES:
        return await event.reply("الملف غير مسموح.")
    if not os.path.exists(fname):
        return await event.reply("الملف غير موجود.")
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, fname + "_" + ts + ".bak")
        shutil.copy(fname, dest)
    except Exception as e:
        return await event.reply("فشل: " + str(e)[:150])
    await event.reply("تم إنشاء نسخة: " + dest)


@client.on(events.NewMessage(pattern=r"^/file_restore (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_file_restore(event):
    arg = event.pattern_match.group(1)
    if os.path.sep in arg or arg.startswith(".."):
        return await event.reply("اسم غير صالح.")
    src = os.path.join(BACKUP_DIR, arg)
    if not os.path.exists(src):
        return await event.reply("النسخة غير موجودة.")
    original = None
    for f in EDITABLE_FILES:
        if arg.startswith(f + "_"):
            original = f
            break
    if not original:
        return await event.reply("لا يمكن تحديد الملف الأصلي.")
    try:
        shutil.copy(src, original)
    except Exception as e:
        return await event.reply("فشل الاستعادة: " + str(e)[:150])
    await event.reply("تم استعادة " + original + " من " + arg)
    if RESTART_AUTO.get("enabled"):
        await event.reply("جاري إعادة التشغيل...")
        await asyncio.sleep(2)
        await _restart_bot()


@client.on(events.NewMessage(pattern=r"^/edit_log$", from_users=DEV_ID))
@safe_execute
async def cmd_edit_log(event):
    if not os.path.exists(EDIT_LOG):
        return await event.reply("السجل فارغ.")
    try:
        with open(EDIT_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        log = []
    if not log:
        return await event.reply("السجل فارغ.")
    lines = ["سجل التعديلات:"]
    for entry in log[-30:]:
        lines.append(entry.get("time", "") + " | " + entry.get("action", "") + " | " + entry.get("file", ""))
    await event.reply("\n".join(lines)[:4000])


@client.on(events.NewMessage(pattern=r"^/restart$", from_users=DEV_ID))
@safe_execute
async def cmd_restart(event):
    await event.reply("جاري إعادة التشغيل...")
    await asyncio.sleep(2)
    await _restart_bot()


@client.on(events.NewMessage(pattern=r"^/restart_auto_on$", from_users=DEV_ID))
@safe_execute
async def cmd_restart_auto_on(event):
    RESTART_AUTO["enabled"] = True
    await event.reply("تم تشغيل إعادة التشغيل التلقائي.")


@client.on(events.NewMessage(pattern=r"^/restart_auto_off$", from_users=DEV_ID))
@safe_execute
async def cmd_restart_auto_off(event):
    RESTART_AUTO["enabled"] = False
    await event.reply("تم إيقاف إعادة التشغيل التلقائي.")


async def _restart_bot():
    await client.disconnect()
    try:
        subprocess.Popen(["python", "main.py"])
    except Exception:
        pass
    os._exit(0)



AI_ASSIST_STATE = {}


def build_ai_assistant_kb():
    return [
        [Button.inline("أخبرني بالمشكلة", b"ai_assist_start")],
        [Button.inline("رجوع", b"dev_back")],
    ]


@client.on(events.CallbackQuery(data=b"dev_menu_ai"))
@safe_execute
async def cb_dev_menu_ai(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    text = ("المساعد الذكي\n\n"
            "اكتب وصف المشكلة أو التعديل الذي تريده، وسأقرأ الملفات وأقترح الحل.\n\n"
            "أمثلة:\n"
            "- الذكاء الاصطناعي ما يزايد عدل، خليه يقرر بين القبول والرفع والإجبار\n"
            "- عندي خطأ في تسجيل النقاط\n"
            "- أريد تحسين سرعة البوت")
    try:
        await event.edit(text, buttons=build_ai_assistant_kb())
    except Exception:
        await event.reply(text, buttons=build_ai_assistant_kb())


@client.on(events.CallbackQuery(data=b"ai_assist_start"))
@safe_execute
async def cb_ai_assist_start(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    AI_ASSIST_STATE[event.sender_id] = {"mode": "waiting"}
    await event.answer()
    try:
        await event.edit("أرسل الآن وصف المشكلة أو التعديل المطلوب:\n\nللإلغاء: /ai_assist_cancel")
    except Exception:
        await event.reply("أرسل الآن وصف المشكلة:\n\nللإلغاء: /ai_assist_cancel")


@client.on(events.NewMessage(pattern=r"^/ai_assist_cancel$", from_users=DEV_ID))
@safe_execute
async def cmd_ai_assist_cancel(event):
    AI_ASSIST_STATE.pop(event.sender_id, None)
    await event.reply("تم الإلغاء.")


def get_file_summary(content):
    lines = content.split("\n")
    summary = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("class "):
            summary.append(stripped.split("(")[0].strip())
    return summary


def find_relevant_functions(content, keywords):
    if not keywords:
        return content[:25000]
    keywords_lower = [k.lower() for k in keywords.split()]
    blocks = []
    current_block = []
    current_name = None
    in_block = False
    lines = content.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("class "):
            if in_block and current_block:
                blocks.append((current_name, "\n".join(current_block)))
            current_block = [line]
            if stripped.startswith("class "):
                current_name = stripped.split("(")[0].replace("class ", "").strip(":")
            else:
                name_part = stripped.replace("async def ", "").replace("def ", "")
                current_name = name_part.split("(")[0].strip()
            in_block = True
        elif in_block:
            current_block.append(line)
            if stripped and not line.startswith(" ") and not line.startswith("\t") and not stripped.startswith("#"):
                if not (stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("class ") or stripped.startswith("@")):
                    if current_block:
                        blocks.append((current_name, "\n".join(current_block[:-1])))
                        current_block = []
                        in_block = False
                        current_name = None
    if in_block and current_block:
        blocks.append((current_name, "\n".join(current_block)))
    relevant = []
    for name, block in blocks:
        if not name:
            continue
        block_lower = block.lower()
        name_lower = name.lower()
        score = 0
        for kw in keywords_lower:
            if kw in name_lower:
                score += 3
            if kw in block_lower:
                score += 1
        if score > 0:
            relevant.append((score, name, block))
    relevant.sort(key=lambda x: -x[0])
    result = ""
    total = 0
    for score, name, block in relevant:
        if total + len(block) > 22000:
            break
        result += "\n\n=== " + name + " ===\n" + block
        total += len(block)
    if not result:
        result = content[:22000]
    return result


@client.on(events.NewMessage(func=lambda e: e.is_private and e.sender_id == DEV_ID and not e.text.startswith("/") and AI_ASSIST_STATE.get(e.sender_id, {}).get("mode") in ("waiting", "processing")))
async def ai_assist_receive_issue(event):
    state = AI_ASSIST_STATE.get(event.sender_id)
    if not state:
        return
    issue = safe_str(event.text, "").strip()
    if not issue:
        return
    AI_ASSIST_STATE[event.sender_id] = {"mode": "processing"}
    await event.reply("جاري تحليل المشروع... قد يستغرق دقيقة أو دقيقتين.")
    all_files_summary = ""
    all_files_full = ""
    for f in EDITABLE_FILES:
        if f == "config.py":
            continue
        if not os.path.exists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                content = fh.read()
        except Exception:
            continue
        size = len(content)
        summary = get_file_summary(content)
        all_files_summary += "\n\n### " + f + " (" + str(size) + " حرف)\n"
        all_files_summary += "الدوال والكلاسات: " + ", ".join(summary[:40])
        if size <= 25000:
            all_files_full += "\n\n=== " + f + " (كامل) ===\n" + content
        else:
            relevant = find_relevant_functions(content, issue)
            all_files_full += "\n\n=== " + f + " (الدوال ذات الصلة) ===\n" + relevant
    if len(all_files_full) > 25000:
        all_files_full = all_files_full[:25000]
    prompt = (
        "أنت خبير Python وبوتات Telethon محترف.\n"
        "المستخدم يريد تعديلاً أو يعاني من مشكلة في بوت تلغرام.\n\n"
        "وصف المستخدم:\n" + issue + "\n\n"
        "ملخص المشروع (جميع الملفات والدوال):\n" + all_files_summary[:5000] + "\n\n"
        "الكود الكامل للدوال ذات الصلة:\n" + all_files_full + "\n\n"
        "المطلوب:\n"
        "1. حدد الملف والدالة المسؤولة عن المشكلة.\n"
        "2. أعد الكود الجديد للدالة كاملاً من def إلى نهايتها.\n"
        "3. اشرح ما فعلت بشكل مختصر.\n"
        "4. تأكد أن الكود الجديد متوافق مع باقي المشروع (لا يحذف دوالاً ضرورية).\n\n"
        "أعد JSON فقط بهذا الشكل:\n"
        "{\"file\": \"اسم الملف\", \"function\": \"اسم الدالة\", \"new_code\": \"الكود الجديد كاملاً\", \"explanation\": \"شرح مختصر\"}"
    )
    text = await _gemini_generate(prompt)
    if not text:
        AI_ASSIST_STATE.pop(event.sender_id, None)
        return await event.reply("فشل التحليل. حاول مرة أخرى.")
    data = _parse_gemini_json(text)
    if not data:
        AI_ASSIST_STATE.pop(event.sender_id, None)
        return await event.reply("فشل تحليل الرد.\n\nالرد كان:\n" + text[:500])
    fname = safe_str(data.get("file"), "").strip()
    fn = safe_str(data.get("function"), "").strip()
    new_code = safe_str(data.get("new_code"), "").strip()
    explanation = safe_str(data.get("explanation"), "").strip()
    if not fname or not fn or not new_code:
        AI_ASSIST_STATE.pop(event.sender_id, None)
        return await event.reply("لم يتم إرجاع بيانات كافية.\n\n" + text[:500])
    if fname not in EDITABLE_FILES:
        AI_ASSIST_STATE.pop(event.sender_id, None)
        return await event.reply("الملف المقترح غير مسموح: " + fname)
    if not os.path.exists(fname):
        AI_ASSIST_STATE.pop(event.sender_id, None)
        return await event.reply("الملف المقترح غير موجود: " + fname)
    AI_ASSIST_STATE[event.sender_id] = {
        "mode": "preview",
        "file": fname,
        "function": fn,
        "new_code": new_code,
        "explanation": explanation,
    }
    preview = ("الاقتراح:\n\n"
               "الملف: " + fname + "\n"
               "الدالة: " + fn + "\n\n"
               "الشرح: " + explanation + "\n\n"
               "الكود الجديد:\n" + new_code[:2800])
    kb = [
        [Button.inline("تطبيق", b"ai_assist_apply")],
        [Button.inline("عرض الكود كاملاً", b"ai_assist_show_full")],
        [Button.inline("تعديل الوصف", b"ai_assist_edit_desc")],
        [Button.inline("إلغاء", b"ai_assist_cancel_btn")],
    ]
    try:
        await event.reply(preview[:4000], buttons=kb)
    except Exception:
        pass
        
        
@client.on(events.CallbackQuery(data=b"ai_assist_show_full"))
@safe_execute
async def cb_ai_assist_show_full(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    state = AI_ASSIST_STATE.get(event.sender_id)
    if not state or "new_code" not in state:
        return await event.answer("لا يوجد اقتراح.", alert=True)
    await event.answer()
    code = state["new_code"]
    for i in range(0, len(code), 3500):
        chunk = code[i:i + 3500]
        try:
            await client.send_message(event.sender_id, chunk)
        except Exception:
            pass
    kb = [
        [Button.inline("تطبيق", b"ai_assist_apply")],
        [Button.inline("تعديل الوصف", b"ai_assist_edit_desc")],
        [Button.inline("إلغاء", b"ai_assist_cancel_btn")],
    ]
    try:
        await client.send_message(event.sender_id, "اختر:", buttons=kb)
    except Exception:
        pass

@client.on(events.CallbackQuery(data=b"ai_assist_edit_desc"))
@safe_execute
async def cb_ai_assist_edit_desc(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    await event.answer()
    AI_ASSIST_STATE[event.sender_id] = {"mode": "waiting"}
    try:
        await event.edit("أرسل الوصف الجديد للمشكلة:\n\nللإلغاء: /ai_assist_cancel")
    except Exception:
        await event.reply("أرسل الوصف الجديد:\n\nللإلغاء: /ai_assist_cancel")

@client.on(events.CallbackQuery(data=b"ai_assist_cancel_btn"))
@safe_execute
async def cb_ai_assist_cancel_btn(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    AI_ASSIST_STATE.pop(event.sender_id, None)
    await event.answer("تم الإلغاء.")
    try:
        await event.edit("تم إلغاء التعديل.")
    except Exception:
        pass


@client.on(events.CallbackQuery(data=b"ai_assist_apply"))
@safe_execute
async def cb_ai_assist_apply(event):
    if event.sender_id != DEV_ID:
        return await event.answer("للمطور فقط.", alert=True)
    state = AI_ASSIST_STATE.get(event.sender_id)
    if not state or "new_code" not in state:
        return await event.answer("لا يوجد اقتراح.", alert=True)
    fname = state["file"]
    fn = state["function"]
    new_code = state["new_code"]
    await event.answer("جاري التطبيق...")
    try:
        with open(fname, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return await event.reply("تعذر القراءة: " + str(e)[:100])
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, fname + "_" + ts + ".bak")
    try:
        shutil.copy(fname, backup_path)
    except Exception as e:
        return await event.reply("فشل النسخ الاحتياطي: " + str(e)[:100])
    old_code = extract_function_code(content, fn)
    if not old_code:
        return await event.reply("لم يتم العثور على الدالة " + fn + " في " + fname)
    new_content = replace_function_code(content, fn, new_code)
    if not new_content:
        return await event.reply("فشل استبدال الدالة.")
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return await event.reply("فشل الكتابة: " + str(e)[:100])
    try:
        py_compile.compile(fname, doraise=True)
    except Exception as e:
        try:
            shutil.copy(backup_path, fname)
        except Exception:
            pass
        return await event.reply("فشل الفحص. تم استعادة النسخة الأصلية تلقائياً.\n\nالخطأ:\n" + str(e)[:300])
    try:
        with open(EDIT_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        log = []
    log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "ai_edit", "file": fname, "info": fn})
    try:
        with open(EDIT_LOG, "w", encoding="utf-8") as f:
            json.dump(log[-200:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    AI_ASSIST_STATE.pop(event.sender_id, None)
    try:
        await event.edit("تم التطبيق بنجاح.\n\nالملف: " + fname + "\nالدالة: " + fn + "\n\nجاري إعادة التشغيل...")
    except Exception:
        pass
    await asyncio.sleep(2)
    await _restart_bot()


print("Bot is running...")
client.run_until_disconnected()
