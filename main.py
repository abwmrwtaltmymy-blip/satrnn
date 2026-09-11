from telethon import TelegramClient, events, Button
from telethon.tl.types import MessageEntityTextUrl
import asyncio
import time
import random

from config import API_ID, API_HASH, BOT_TOKEN, DEV_ID
from database import (init_db, add_points, get_top, is_banned, ban_group, unban_group,
                      add_force_sub, remove_force_sub, get_force_subs, get_stats,
                      get_all_groups, get_all_users, register_user, update_win_loss)
from utils import (safe_execute, clean_name, name_has_bad_word, require_subscription,
                   evaluate_answers_with_ai, translate_error,
                   ai_generate_answers, ai_generate_bid, safe_str)
from game_manager import (internal_games, tournaments, private_sessions, matchmaking_pool,
                          InternalGame, Tournament, get_question)

init_db()

client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

ai_games = {}

TEAM_NAMES = [
    ("فريق المسلمين", "فريق الكفار"),
  ("فريق المحتوى الهادف", " فريق الشتبوستريه"),
    ("فريق الواعيين", "فريق الترولية"), 
]

def user_link(user_id, name):
    return "[" + name + "](tg://user?id=" + str(user_id) + ")"

def pick_team_names():
    return random.choice(TEAM_NAMES)

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

async def cancel_tasks(game_obj):
    if game_obj is None:
        return
    for attr in ("bidding_task", "answer_task", "opponent_task"):
        t = getattr(game_obj, attr, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

async def send_to_group(game_obj, chat_id, text, buttons=None, round_level=False):
    try:
        msg = await client.send_message(chat_id, text, buttons=buttons)
    except Exception:
        return None
    if game_obj is not None:
        if not hasattr(game_obj, "tracked_messages") or game_obj.tracked_messages is None:
            game_obj.tracked_messages = []
        game_obj.tracked_messages.append((chat_id, msg.id))
        if round_level:
            if not hasattr(game_obj, "round_messages") or game_obj.round_messages is None:
                game_obj.round_messages = []
            game_obj.round_messages.append((chat_id, msg.id))
    await asyncio.sleep(0.6)
    return msg

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
                await asyncio.sleep(0.3)
            except Exception:
                for mid in batch:
                    try:
                        await client.delete_messages(chat_id, mid)
                        await asyncio.sleep(0.15)
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
                await asyncio.sleep(0.3)
            except Exception:
                for mid in batch:
                    try:
                        await client.delete_messages(chat_id, mid)
                        await asyncio.sleep(0.15)
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

async def get_member_names(g, team_ids):
    names = []
    for uid in team_ids:
        try:
            ent = await client.get_entity(uid)
            n = clean_name(ent.first_name)
        except Exception:
            n = "لاعب"
        names.append({"id": uid, "name": n})
    return names

def format_team(g, team_ids, team_label):
    parts = []
    for uid in team_ids:
        try:
            ent_name = g._name_cache.get(uid, "لاعب")
        except Exception:
            ent_name = "لاعب"
        parts.append(user_link(uid, ent_name))
    return team_label + ": " + " ، ".join(parts)

async def cache_names(g, team1, team2):
    if not hasattr(g, "_name_cache") or g._name_cache is None:
        g._name_cache = {}
    for uid in list(team1) + list(team2):
        if uid in g._name_cache:
            continue
        try:
            ent = await client.get_entity(uid)
            g._name_cache[uid] = clean_name(ent.first_name)
        except Exception:
            g._name_cache[uid] = "لاعب"

def team_display(g, which):
    if which == 1:
        label = g.team1_label
        ids = g.team1
    else:
        label = g.team2_label
        ids = g.team2
    parts = []
    for uid in ids:
        name = g._name_cache.get(uid, "لاعب") if hasattr(g, "_name_cache") else "لاعب"
        parts.append(user_link(uid, name))
    return label + ": " + " ، ".join(parts)

def team_label_only(g, which):
    return g.team1_label if which == 1 else g.team2_label

def player_label(g, uid):
    if hasattr(g, "_name_cache") and uid in g._name_cache:
        return user_link(uid, g._name_cache[uid])
    return user_link(uid, "لاعب")

async def send_main_menu(chat_id):
    u = await bot_username()
    text = (
        "مرحبًا بكم في بوت تحدي الثلاثين ثانية.\n\n"
        "اختر أحد الخيارات التالية:\n"
        "1) بدء مبارة مع مجموعة أخرى: ترشيح وتصويت ثم مطابقة.\n"
        "2) بدء مبارة داخل الكروب: عدد اللاعبين لكل فريق ثم انضمام.\n"
        "3) أضف البوت كمشرف: يمنح البوت صلاحية التثبيت والكتابة.\n"
        "4) شرح الأزرار: يعرض هذا الشرح."
    )
    kb = [
        [Button.inline("بدء مبارة مع مجموعة أخرى", b"mode_tournament")],
        [Button.inline("شرح الأزرار", b"explain_buttons")],
        [Button.url("أضف البوت كمشرف", "https://t.me/" + u + "?startgroup=admin")],
        [Button.inline("بدء مبارة داخل الكروب", b"mode_internal")],
    ]
    await client.send_message(chat_id, text, buttons=kb)

@client.on(events.ChatAction)
@safe_execute
async def on_group_join(event):
    me = await client.get_me()
    if not (event.user_added or event.user_joined):
        return
    if event.user_id != me.id:
        return
    chat = await event.get_chat()
    chat_title = safe_str(getattr(chat, "title", ""), "")
    if name_has_bad_word(chat_title):
        try:
            await client.send_message(event.chat_id, "لا يمكنني البقاء في مجموعة تحمل اسمًا مخالفًا للسياسات.")
            await asyncio.sleep(3)
        except Exception:
            pass
        await client.delete_dialog(event.chat_id)
        return
    try:
        parts = await client.get_participants(event.chat_id)
        humans = 0
        for p in parts:
            if not p.bot:
                humans += 1
        if humans < 5:
            try:
                await client.send_message(event.chat_id, "عدد الأعضاء الحقيقيين أقل من 5. لا يمكنني البقاء.")
                await asyncio.sleep(3)
            except Exception:
                pass
            await client.delete_dialog(event.chat_id)
            return
    except Exception:
        pass
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
        return
    try:
        await client.send_message(event.chat_id, "تمت إضافتي بنجاح. للبدء أرسل /start_game")
    except Exception:
        pass

@client.on(events.NewMessage(pattern=r"^/start(?:@\S+)?(?: (.+))?$"))
@safe_execute
async def cmd_start(event):
    payload = safe_str(event.pattern_match.group(1), "")
    if event.is_private:
        user = await event.get_sender()
        register_user(user.id, clean_name(user.first_name))
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
                    name = clean_name(user.first_name)
                    g.players.append(user.id)
                    g.names.append(name)
                    await event.reply("تم تسجيل انضمامك في مجموعة " + safe_str(g.chat_name, "") + "\nانتظر في الكروب.")
                    await refresh_join_pinned(g)
                    if len(g.players) >= g.required_total:
                        g.split_teams()
                        g.state = "ready_check"
                        await cache_names(g, g.team1, g.team2)
                        if g.team_size == 1:
                            t1_name = g._name_cache.get(g.team1[0], "لاعب")
                            t2_name = g._name_cache.get(g.team2[0], "لاعب")
                            g.team1_label = t1_name
                            g.team2_label = t2_name
                        else:
                            labels = pick_team_names()
                            g.team1_label = labels[0]
                            g.team2_label = labels[1]
                        await refresh_ready_pinned(g)
                    return
                else:
                    return await event.reply("لا يوجد تحدٍ مفتوح في هذه المجموعة حاليًا.")
        u = await bot_username()
        text = (
            "أهلًا بك في بوت تحدي الثلاثين ثانية.\n\n"
            "يمكنك:\n"
            "1) إضافة البوت إلى مجموعتك لتنظيم تحديات بين الأعضاء.\n"
            "2) اللعب ضد الذكاء الاصطناعي في الخاص مباشرة.\n\n"
            "اختر ما تريد:"
        )
        kb = [
            [Button.inline("اللعب ضد الذكاء الاصطناعي", b"ai_menu")],
            [Button.url("أضف البوت إلى مجموعتك", "https://t.me/" + u + "?startgroup=admin")],
        ]
        await event.reply(text, buttons=kb)
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    if not await require_subscription(event):
        return
    await send_main_menu(event.chat_id)

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
        for size in (1, 2, 3, 4, 5):
            kb.append([Button.inline(str(size) + " ضد " + str(size), ("internal_size_" + str(size)).encode())])
        return await event.reply("اختر عدد اللاعبين لكل فريق:", buttons=kb)
    try:
        team_size = int(n)
    except Exception:
        return await event.reply("أرسل رقمًا صحيحًا.")
    if team_size < 1 or team_size > 10:
        return await event.reply("عدد اللاعبين لكل فريق يجب أن يكون بين 1 و 10.")
    await start_internal(event.chat_id, safe_str(event.chat.title, "مجموعة"), team_size)

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
        await event.reply("تم إيقاف اللعبة.\n\nنقاطك: " + str(g["player_points"]) + "\nنقاط الذكاء الاصطناعي: " + str(g["ai_points"]))
    else:
        await event.reply("لا توجد لعبة جارية.")

@client.on(events.CallbackQuery(data=b"explain_buttons"))
@safe_execute
async def cb_explain(event):
    await event.answer()
    text = (
        "شرح الأزرار:\n\n"
        "بدء مبارة مع مجموعة أخرى: ترشيح وتصويت في كروبكم، ومطابقة تلقائية مع مجموعة منتظرة.\n\n"
        "أضف البوت كمشرف: يفتح نافذة إضافة البوت مع صلاحيات المشرف.\n\n"
        "بدء مبارة داخل الكروب: عدد اللاعبين لكل فريق، ثم زر انضمام للأعضاء."
    )
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
    ai_games[user_id] = {
        "difficulty": difficulty,
        "player_points": 100,
        "ai_points": 100,
        "round": 1,
        "state": "idle",
        "question": None,
        "player_answers": [],
        "expected_count": 0,
    }
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
    g["question"] = question
    g["state"] = "player_bid"
    text = ("الجولة " + str(g["round"]) + "\n\n"
            "السؤال: " + safe_str(question, "") + "\n\n"
            "نقاطك: " + str(g["player_points"]) + "\n"
            "نقاط الذكاء الاصطناعي: " + str(g["ai_points"]) + "\n\n"
            "أرسل رقمًا يمثل ما تستطيع ذكره من هذا التصنيف.")
    try:
        await event.edit(text)
    except Exception:
        await event.reply(text)

@client.on(events.CallbackQuery(data=b"ai_finish_player"))
@safe_execute
async def cb_ai_finish_player(event):
    await event.answer("جاري التقييم")
    user_id = event.sender_id
    g = ai_games.get(user_id)
    if not g or g["state"] != "player_answering":
        return await event.reply("لا توجد إجابات قيد الانتظار.")
    ok, reason = await evaluate_answers_with_ai(g["question"], g["expected_count"], g["player_answers"])
    if ok:
        g["player_points"] += 10
        result_word = "نجحت"
    else:
        g["player_points"] -= 20
        g["ai_points"] += 10
        result_word = "فشلت"
    combined = (
        "نتيجة دورك\n\n"
        "النتيجة: " + result_word + "\n"
        + reason + "\n\n"
        "نقاطك: " + str(g["player_points"]) + "\n"
        "نقاط الذكاء الاصطناعي: " + str(g["ai_points"])
    )
    await event.reply(combined)
    if g["player_points"] <= 0 or g["ai_points"] <= 0:
        return await finish_ai_game(user_id)
    g["state"] = "ai_turn"
    g["round"] += 1
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
    g["question"] = question
    g["state"] = "player_bid"
    text = ("الجولة " + str(g["round"]) + "\n\n"
            "السؤال: " + safe_str(question, "") + "\n\n"
            "نقاطك: " + str(g["player_points"]) + "\n"
            "نقاط الذكاء الاصطناعي: " + str(g["ai_points"]) + "\n\n"
            "أرسل رقمًا يمثل ما تستطيع ذكره.")
    try:
        await event.edit(text)
    except Exception:
        await event.reply(text)

async def run_ai_turn(user_id):
    g = ai_games.get(user_id)
    if not g:
        return
    await asyncio.sleep(2)
    ai_question = get_question()
    g["ai_question"] = ai_question
    ai_bid = await ai_generate_bid(ai_question, g["difficulty"])
    answers = await ai_generate_answers(ai_question, ai_bid, g["difficulty"])
    ok, reason = await evaluate_answers_with_ai(ai_question, ai_bid, answers)
    if ok:
        g["ai_points"] += 10
        result_word = "نجح"
    else:
        g["ai_points"] -= 20
        g["player_points"] += 10
        result_word = "فشل"
    combined = (
        "دور الذكاء الاصطناعي\n\n"
        "السؤال: " + safe_str(ai_question, "") + "\n"
        "المزايدة: " + str(ai_bid) + " إجابة\n\n"
        "النتيجة: " + result_word + "\n"
        + reason + "\n\n"
        "نقاطك: " + str(g["player_points"]) + "\n"
        "نقاط الذكاء الاصطناعي: " + str(g["ai_points"])
    )
    try:
        await client.send_message(user_id, combined)
    except Exception:
        pass
    if g["player_points"] <= 0 or g["ai_points"] <= 0:
        return await finish_ai_game(user_id)
    g["state"] = "idle"
    kb = [[Button.inline("الجولة التالية", b"ai_next_round")]]
    try:
        await client.send_message(user_id, "اضغط للاستمرار.", buttons=kb)
    except Exception:
        pass

async def finish_ai_game(user_id):
    g = ai_games.pop(user_id, None)
    if not g:
        return
    if g["player_points"] > g["ai_points"]:
        result = "فزت على الذكاء الاصطناعي."
    else:
        result = "خسرت ضد الذكاء الاصطناعي."
    try:
        await client.send_message(user_id, "انتهت اللعبة.\n\n" + result + "\n\nنقاطك: " + str(g["player_points"]) + "\nنقاط الذكاء الاصطناعي: " + str(g["ai_points"]))
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
    return match, "matched"

@client.on(events.CallbackQuery(data=b"mode_tournament"))
@safe_execute
async def cb_mode_tournament(event):
    chat_id = event.chat_id
    if is_banned(chat_id):
        return await event.answer("مجموعتكم محظورة.", alert=True)
    gname = clean_name(event.chat.title if hasattr(event.chat, "title") else "مجموعة")
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
    for n in (1, 2, 3, 4, 5):
        kb.append([Button.inline(str(n) + " ضد " + str(n), ("internal_size_" + str(n)).encode())])
    await event.edit("اختر عدد اللاعبين لكل فريق:", buttons=kb)

@client.on(events.CallbackQuery(pattern=r"^internal_size_(\d+)$"))
@safe_execute
async def cb_internal_size(event):
    n = int(event.pattern_match.group(1))
    await event.answer()
    title = safe_str(getattr(event.chat, "title", ""), "مجموعة")
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
    players_text = "لا أحد بعد" if not g.players else "\n- ".join(g.names)
    text = ("تحدي داخلي\n\n"
            "عدد اللاعبين لكل فريق: " + str(g.team_size) + "\n"
            "المطلوب: " + str(g.required_total) + " لاعب\n\n"
            "المنضمون (" + str(len(g.players)) + "/" + str(g.required_total) + "):\n- " + players_text + "\n\n"
            "اضغط زر الانضمام للمشاركة.")
    kb = [[Button.url("انضمام للتحدي", join_url)]]
    await edit_pinned(g, g.chat_id, text, buttons=kb)

async def refresh_ready_pinned(g):
    if not g.pin_msg_id:
        return
    ready_names = []
    waiting_names = []
    for p in g.players:
        try:
            pname = clean_name((await client.get_entity(p)).first_name)
        except Exception:
            pname = "لاعب"
        if p in g.ready:
            ready_names.append(pname)
        else:
            waiting_names.append(pname)
    teams_info = ""
    if hasattr(g, "team1_label") and g.team1_label:
        teams_info = (team_display(g, 1) + "\n" + team_display(g, 2) + "\n\n")
    text = ("كيف تلعب:\n"
            "1. المزايد يستلم رسالة في الخاص، يحدد رقمًا يمثل ما يستطيع ذكره.\n"
            "2. الخصم يقدر يجبره على الإجابة فقط.\n"
            "3. المجيب يرسل الإجابات في الخاص، كل إجابة في رسالة منفصلة.\n"
            "4. البوت يقيّم الإجابات.\n"
            "5. كل فريق عنده 3 أرواح، أول من يفقدها يخسر.\n\n"
            + teams_info +
            "حالة الجاهزية: " + str(len(g.ready)) + "/" + str(g.required_total) + "\n\n")
    if ready_names:
        text += "استعدوا:\n- " + "\n- ".join(ready_names)
    else:
        text += "استعدوا: لا أحد بعد"
    if waiting_names:
        text += "\n\nبالانتظار:\n- " + "\n- ".join(waiting_names)
    else:
        text += "\n\nبالانتظار: لا أحد، الجميع جاهز"
    kb = [[Button.inline("جاهز", ("ready_int_" + str(g.chat_id)).encode())]]
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
    g._name_cache = {}
    g.team1_label = ""
    g.team2_label = ""
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

async def join_timeout_internal(chat_id):
    await asyncio.sleep(120)
    g = internal_games.get(chat_id)
    if not g or g.state != "waiting":
        return
    internal_games.pop(chat_id, None)
    for p in g.players:
        private_sessions.pop(p, None)
    await delete_pinned(g, chat_id)
    await delete_tracked(g)
    try:
        await client.send_message(chat_id, "تم إلغاء التحدي لعدم اكتمال العدد خلال دقيقتين.")
    except Exception:
        pass

async def bidding_countdown(game_obj, chat_id, user_id, label):
    checkpoints = [15, 10, 5, 3, 2, 1]
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
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثواني" + label)
        except Exception:
            return
        prev = cp

async def answer_countdown(game_obj, chat_id, user_id, label):
    checkpoints = [25, 20, 15, 10, 5, 3, 2, 1]
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
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثانية" + label)
        except Exception:
            return
        prev = cp

async def opponent_countdown(game_obj, chat_id, user_id, label):
    checkpoints = [15, 10, 5, 3, 2, 1]
    prev = 20
    for cp in checkpoints:
        delay = prev - cp
        if delay > 0:
            await asyncio.sleep(delay)
        if game_obj is None:
            return
        if getattr(game_obj, "state", None) != "opponent_choice":
            return
        if getattr(game_obj, "opponent", None) != user_id:
            return
        try:
            await client.send_message(user_id, "بقي " + str(cp) + " ثواني" + label)
        except Exception:
            return
        prev = cp

async def opponent_timeout_internal(g, user_id):
    await asyncio.sleep(20)
    if g.state != "opponent_choice" or g.opponent != user_id:
        return
    await send_to_group(g, g.chat_id, "انتهى وقت الخصم دون قرار.\n\nتم إجبار اللاعب على الإجابة تلقائيًا.", round_level=True)
    await begin_answer_internal(g, g.bidder)

async def opponent_timeout_tournament(m, user_id):
    await asyncio.sleep(20)
    if m.state != "opponent_choice" or m.opponent != user_id:
        return
    for gid in m.both_groups():
        await send_to_group(m, gid, "انتهى وقت الخصم دون قرار.\n\nتم إجبار اللاعب على الإجابة تلقائيًا.", round_level=True)
    await begin_answer_tournament(m, m.bidder)

@client.on(events.CallbackQuery(pattern=r"^ready_int_(-?\d+)$"))
@safe_execute
async def cb_ready_internal(event):
    chat_id = int(event.pattern_match.group(1))
    g = internal_games.get(chat_id)
    if not g or g.state != "ready_check":
        return await event.answer("الوقت انتهى.", alert=True)
    user = await event.get_sender()
    if user.id not in g.players:
        return await event.answer("أنت لست ضمن اللاعبين.", alert=True)
    if user.id in g.ready:
        return await event.answer("أنت مسجل جاهز مسبقًا.", alert=True)
    g.ready.add(user.id)
    await event.answer("تم تسجيل الجاهزية.")
    await refresh_ready_pinned(g)
    if len(g.ready) >= g.required_total:
        try:
            if hasattr(g, "_join_timeout_task") and g._join_timeout_task:
                g._join_timeout_task.cancel()
        except Exception:
            pass
        await asyncio.sleep(1)
        await start_round_internal(g)

async def start_round_internal(g):
    await delete_round_messages(g)
    g.question = get_question()
    g.state = "bidding"
    g.current_bid = 0
    g.answers = []
    b = random.choice(g.team1)
    o = random.choice(g.team2)
    g.bidder = b
    g.opponent = o
    private_sessions[b] = {"game_type": "internal", "chat_id": g.chat_id, "role": "bidder"}
    private_sessions[o] = {"game_type": "internal", "chat_id": g.chat_id, "role": "opponent"}
    await cache_names(g, g.team1, g.team2)
    bn = g._name_cache.get(b, "لاعب")
    on = g._name_cache.get(o, "لاعب")
    await delete_pinned(g, g.chat_id)
    text = ("الجولة " + str(g.round) + "\n\n"
            "السؤال: " + safe_str(g.question, "") + "\n\n"
            "أرواح " + g.team1_label + ": " + str(g.team1_points) + "\n"
            "أرواح " + g.team2_label + ": " + str(g.team2_points) + "\n\n"
            "المزايد: " + user_link(b, bn) + " من " + g.team1_label + "\n"
            "الخصم: " + user_link(o, on) + " من " + g.team2_label + "\n\n"
            "المزايدة تجري في الخاص الآن، ولدى المزايد 20 ثانية.")
    await send_to_group(g, g.chat_id, text, round_level=True)
    kb_withdraw = [[Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
                   [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
    try:
        await client.send_message(b, "بدأت المزايدة للجولة " + str(g.round) + ".\nالسؤال: " + safe_str(g.question, "") + "\nأرسل رقمًا فقط خلال 20 ثانية.", buttons=kb_withdraw)
    except Exception:
        pass
    g.bidding_task = asyncio.create_task(bidding_timeout_internal(g, b))
    asyncio.create_task(bidding_countdown(g, g.chat_id, b, " للمزايدة"))

async def bidding_timeout_internal(g, user_id):
    await asyncio.sleep(20)
    if g.state != "bidding" or g.bidder != user_id:
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
    await asyncio.sleep(1)
    await advance_round_internal(g)

async def end_internal_no_winner(g):
    g.state = "done"
    await cancel_tasks(g)
    await delete_round_messages(g)
    await delete_pinned(g, g.chat_id)
    await strip_buttons(g)
    await asyncio.sleep(0.3)
    await delete_tracked(g)
    for p in g.players:
        private_sessions.pop(p, None)
    internal_games.pop(g.chat_id, None)

async def advance_round_internal(g):
    if g.team1_points <= 0 or g.team2_points <= 0:
        return await finish_internal(g)
    g.round += 1
    await asyncio.sleep(1)
    await start_round_internal(g)

async def finish_internal(g):
    g.state = "done"
    if g.team1_points > g.team2_points:
        winner_label = g.team1_label
        add_points(g.chat_id, "group", 50, g.chat_name)
        update_win_loss(g.chat_id, "group", True)
    else:
        winner_label = g.team2_label
        add_points(g.chat_id, "group", -20, g.chat_name)
        update_win_loss(g.chat_id, "group", False)
    await cancel_tasks(g)
    await delete_round_messages(g)
    await delete_pinned(g, g.chat_id)
    await strip_buttons(g)
    await asyncio.sleep(0.3)
    await delete_tracked(g)
    await asyncio.sleep(0.3)
    try:
        await client.send_message(g.chat_id, "انتهت المباراة.\n\nالفائز: " + winner_label + "\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points))
    except Exception:
        pass
    for p in g.players:
        private_sessions.pop(p, None)
    internal_games.pop(g.chat_id, None)

async def open_nomination(match):
    kb = [[Button.inline("ترشيح نفسي", ("nom_" + str(match.match_id)).encode())]]
    text1 = ("فتح باب الترشيح للتحدي\n\nالخصم: " + safe_str(match.group2_name, "") + "\n\n"
             "من يريد تمثيل الكروب يضغط زر ترشيح نفسي.\nسيتم اختيار أعلى " + str(match.squad) + " بحسب التصويت.")
    text2 = ("فتح باب الترشيح للتحدي\n\nالخصم: " + safe_str(match.group1_name, "") + "\n\n"
             "من يريد تمثيل الكروب يضغط زر ترشيح نفسي.\nسيتم اختيار أعلى " + str(match.squad) + " بحسب التصويت.")
    try:
        msg1 = await client.send_message(match.group1_id, text1, buttons=kb)
        match.tracked_messages.append((match.group1_id, msg1.id))
        try:
            await client.pin_message(match.group1_id, msg1, notify=False)
        except Exception:
            pass
        await asyncio.sleep(0.6)
    except Exception:
        pass
    try:
        msg2 = await client.send_message(match.group2_id, text2, buttons=kb)
        match.tracked_messages.append((match.group2_id, msg2.id))
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
    cand[user.id] = {"name": clean_name(user.first_name), "votes": set(), "ts": time.time()}
    await event.answer("تم ترشيحك.")
    await refresh_nom_msg(m, gid)

async def refresh_nom_msg(m, gid):
    cand = m.candidates[gid]
    lst = sorted(cand.items(), key=lambda x: (-len(x[1]["votes"]), x[1]["ts"]))
    header = m.group1_name if gid == m.group1_id else m.group2_name
    lines = ["المرشحون في " + safe_str(header, "") + ":"]
    buttons = []
    for uid, d in lst:
        lines.append("- " + safe_str(d["name"], "") + " — " + str(len(d["votes"])) + " صوت")
        buttons.append([Button.inline("تصويت لـ " + safe_str(d["name"], ""), ("vote_" + str(m.match_id) + "_" + str(gid) + "_" + str(uid)).encode())])
    buttons.append([Button.inline("إنهاء الترشيح الآن", ("close_nom_" + str(m.match_id) + "_" + str(gid)).encode())])
    try:
        msg = await client.send_message(gid, "\n".join(lines), buttons=buttons)
        m.tracked_messages.append((gid, msg.id))
        await asyncio.sleep(0.4)
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
    m.state = "ready_check"
    await event.answer("تم إغلاق الترشيح.")
    for gid_, team, gname in ((m.group1_id, m.team1, m.group1_name),
                              (m.group2_id, m.team2, m.group2_name)):
        names = "\n- ".join(safe_str(p["name"], "") for p in team) if team else "لا يوجد"
        opp = m.group2_name if gid_ == m.group1_id else m.group1_name
        kb = [[Button.inline("جاهز", ("ready_t_" + str(m.match_id)).encode())]]
        txt = ("كيف تلعب:\n"
               "1. المزايد يستلم رسالة في الخاص، يحدد رقمًا.\n"
               "2. الخصم يقدر يجبره على الإجابة فقط.\n"
               "3. المجيب يرسل الإجابات في الخاص، كل إجابة في رسالة منفصلة.\n"
               "4. البوت يقيّم الإجابات.\n"
               "5. كل فريق عنده 3 أرواح، أول من يفقدها يخسر.\n\n"
               "تم اختيار الفريق الممثل لـ " + safe_str(gname, "") + "\n\n"
               "الخصم: " + safe_str(opp, "") + "\n\n"
               "الفريق:\n- " + names + "\n\n"
               "على جميع الممثلين الضغط على زر جاهز للبدء.")
        try:
            msg = await client.send_message(gid_, txt, buttons=kb)
            m.tracked_messages.append((gid_, msg.id))
            await asyncio.sleep(0.4)
        except Exception:
            pass

@client.on(events.CallbackQuery(pattern=r"^ready_t_(\d+)$"))
@safe_execute
async def cb_ready_t(event):
    mid = int(event.pattern_match.group(1))
    m = tournaments.get(mid)
    if not m or m.state != "ready_check":
        return await event.answer("انتهى الوقت.", alert=True)
    user = await event.get_sender()
    gid = event.chat_id
    team = m.team1 if gid == m.group1_id else m.team2
    if not any(p["user_id"] == user.id for p in team):
        return await event.answer("أنت لست من الممثلين.", alert=True)
    if user.id in m.ready:
        return await event.answer("أنت مسجل جاهز مسبقًا.", alert=True)
    m.ready.add(user.id)
    await event.answer("تم تسجيل جاهزيتك.")
    needed = len(m.team1) + len(m.team2)
    if needed > 0 and len(m.ready) >= needed:
        await asyncio.sleep(1)
        await start_round_tournament(m)

async def start_round_tournament(m):
    if not m.team1 or not m.team2:
        await client.send_message(m.group1_id, "لا يمكن بدء التحدي: فريق فارغ.")
        tournaments.pop(m.match_id, None)
        return
    await delete_round_messages(m)
    m.state = "bidding"
    m.question = get_question()
    m.current_bid = 0
    m.answers = []
    b = random.choice(m.team1)["user_id"]
    o = random.choice(m.team2)["user_id"]
    m.bidder = b
    m.opponent = o
    private_sessions[b] = {"game_type": "tournament", "match_id": m.match_id, "role": "bidder"}
    private_sessions[o] = {"game_type": "tournament", "match_id": m.match_id, "role": "opponent"}
    bname = "لاعب"
    oname = "لاعب"
    for p in m.team1:
        if p["user_id"] == b:
            bname = p["name"]
            break
    for p in m.team2:
        if p["user_id"] == o:
            oname = p["name"]
            break
    for gid in m.both_groups():
        text = ("الجولة " + str(m.round) + "\n\n"
                "السؤال: " + safe_str(m.question, "") + "\n\n"
                "أرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\n"
                "أرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points) + "\n\n"
                "المزايد: " + user_link(b, bname) + " من " + safe_str(m.group1_name, "") + "\n"
                "الخصم: " + user_link(o, oname) + " من " + safe_str(m.group2_name, "") + "\n\n"
                "المزايدة في الخاص الآن، ولدى المزايد 20 ثانية.")
        await send_to_group(m, gid, text, round_level=True)
    kb_withdraw = [[Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
                   [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
    try:
        await client.send_message(b, "بدأت المزايدة للجولة " + str(m.round) + ".\nالسؤال: " + safe_str(m.question, "") + "\nأرسل رقمًا فقط خلال 20 ثانية.", buttons=kb_withdraw)
    except Exception:
        pass
    m.bidding_task = asyncio.create_task(bidding_timeout_tournament(m, b))
    asyncio.create_task(bidding_countdown(m, m.group1_id, b, " للمزايدة"))

async def bidding_timeout_tournament(m, user_id):
    await asyncio.sleep(20)
    if m.state != "bidding" or m.bidder != user_id:
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
            await send_to_group(m, gid, "تم إنهاء التحدي.\n\nأرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\nأرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points), round_level=True)
        await end_tournament_no_winner(m)
        return
    for gid in m.both_groups():
        text = ("انتهت مدة المزايدة دون رد.\n\n"
                + safe_str(fail_name, "") + " فقد روحًا.\n\n"
                "أرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\n"
                "أرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points))
        await send_to_group(m, gid, text, round_level=True)
    await asyncio.sleep(1)
    await advance_round_tournament(m)

async def end_tournament_no_winner(m):
    m.state = "done"
    await cancel_tasks(m)
    await delete_round_messages(m)
    await strip_buttons(m)
    await asyncio.sleep(0.3)
    await delete_tracked(m)
    for p in m.team1 + m.team2:
        private_sessions.pop(p["user_id"], None)
    tournaments.pop(m.match_id, None)

async def advance_round_tournament(m):
    if m.team1_points <= 0 or m.team2_points <= 0:
        return await finish_tournament(m)
    m.round += 1
    await asyncio.sleep(1)
    await start_round_tournament(m)

async def finish_tournament(m):
    m.state = "done"
    if m.team1_points > m.team2_points:
        winner = m.group1_name
        add_points(m.group1_id, "group", 50, m.group1_name)
        add_points(m.group2_id, "group", -20, m.group2_name)
        update_win_loss(m.group1_id, "group", True)
        update_win_loss(m.group2_id, "group", False)
    else:
        winner = m.group2_name
        add_points(m.group2_id, "group", 50, m.group2_name)
        add_points(m.group1_id, "group", -20, m.group1_name)
        update_win_loss(m.group2_id, "group", True)
        update_win_loss(m.group1_id, "group", False)
    await cancel_tasks(m)
    await delete_round_messages(m)
    await strip_buttons(m)
    await asyncio.sleep(0.3)
    await delete_tracked(m)
    await asyncio.sleep(0.3)
    for gid in m.both_groups():
        try:
            await client.send_message(gid, "انتهت المباراة.\n\nالفائز: " + safe_str(winner, "") + "\n\nأرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\nأرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points))
        except Exception:
            pass
    for p in m.team1 + m.team2:
        private_sessions.pop(p["user_id"], None)
    tournaments.pop(m.match_id, None)

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
        if ag["state"] == "player_bid":
            if not txt.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(txt)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            ag["expected_count"] = bid
            ag["player_answers"] = []
            ag["state"] = "player_answering"
            await event.reply("تم تسجيل مزايدتك " + str(bid) + ".\n\nابدأ بإرسال الإجابات، كل إجابة في رسالة منفصلة.\nعند الانتهاء اضغط زر أنهيت الإجابة.")
            kb = [[Button.inline("أنهيت الإجابة", b"ai_finish_player")]]
            await event.reply("جاهز؟", buttons=kb)
            return
        if ag["state"] == "player_answering":
            ag["player_answers"].append(txt)
            return
        if ag["state"] == "ai_turn":
            return await event.reply("انتظر دور الذكاء الاصطناعي.")
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
        if sess["role"] == "bidder" and g.state == "bidding" and g.bidder == uid:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(text)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            g.current_bid = bid
            g.consecutive_timeouts = 0
            try:
                if g.bidding_task:
                    g.bidding_task.cancel()
            except Exception:
                pass
            g.state = "opponent_choice"
            opp = g.opponent
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_" + str(g.chat_id) + "_" + str(uid)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
            await client.send_message(opp, "خصمك قال إنه يستطيع ذكر " + str(bid) + " من " + safe_str(g.question, "") + ".\nهل تجبره على الإجابة؟ لديك 20 ثانية.", buttons=kb)
            await event.reply("تم تسجيل مزايدتك.")
            asyncio.create_task(opponent_countdown(g, g.chat_id, opp, " لاتخاذ القرار"))
            g.opponent_task = asyncio.create_task(opponent_timeout_internal(g, opp))
        elif sess["role"] == "opponent" and g.state == "opponent_choice":
            if not text.isdigit():
                return await event.reply("استخدم الأزرار لاتخاذ القرار.")
            return await event.reply("استخدم الأزرار لاتخاذ القرار.")
        elif sess["role"] == "bidder" and g.state == "answering":
            g.answers.append(text)

    elif sess["game_type"] == "tournament":
        m = tournaments.get(sess["match_id"])
        if not m:
            private_sessions.pop(uid, None)
            return await event.reply("انتهى التحدي.")
        if sess["role"] == "bidder" and m.state == "bidding" and m.bidder == uid:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(text)
            if bid < 1 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            m.current_bid = bid
            m.consecutive_timeouts = 0
            try:
                if m.bidding_task:
                    m.bidding_task.cancel()
            except Exception:
                pass
            m.state = "opponent_choice"
            opp = m.opponent
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_t_" + str(m.match_id) + "_" + str(uid)).encode())],
                  [Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
                  [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
            await client.send_message(opp, "خصمك قال إنه يذكر " + str(bid) + " من " + safe_str(m.question, "") + ".\nهل تجبره على الإجابة؟ لديك 20 ثانية.", buttons=kb)
            await event.reply("تم تسجيل مزايدتك.")
            asyncio.create_task(opponent_countdown(m, m.group1_id, opp, " لاتخاذ القرار"))
            m.opponent_task = asyncio.create_task(opponent_timeout_tournament(m, opp))
        elif sess["role"] == "opponent" and m.state == "opponent_choice":
            if not text.isdigit():
                return await event.reply("استخدم الأزرار لاتخاذ القرار.")
            return await event.reply("استخدم الأزرار لاتخاذ القرار.")
        elif sess["role"] == "bidder" and m.state == "answering":
            m.answers.append(text)

@client.on(events.CallbackQuery(pattern=r"^outbid_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_outbid_internal(event):
    await event.answer("هذا الزر متوقف.", alert=True)

@client.on(events.CallbackQuery(pattern=r"^outbid_t_(\d+)_(\d+)$"))
@safe_execute
async def cb_outbid_t(event):
    await event.answer("هذا الزر متوقف.", alert=True)

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
    try:
        if g.bidding_task:
            g.bidding_task.cancel()
    except Exception:
        pass
    try:
        if g.answer_task:
            g.answer_task.cancel()
    except Exception:
        pass
    try:
        if hasattr(g, "opponent_task") and g.opponent_task:
            g.opponent_task.cancel()
    except Exception:
        pass
    if uid in g.team1:
        g.team1_points -= 1
        fail_name = g.team1_label
    elif uid in g.team2:
        g.team2_points -= 1
        fail_name = g.team2_label
    else:
        fail_name = "الفريق"
    await send_to_group(g, g.chat_id, "انسحب اللاعب.\n\n" + fail_name + " فقد روحًا.\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points), round_level=True)
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
    try:
        if m.bidding_task:
            m.bidding_task.cancel()
    except Exception:
        pass
    try:
        if m.answer_task:
            m.answer_task.cancel()
    except Exception:
        pass
    try:
        if hasattr(m, "opponent_task") and m.opponent_task:
            m.opponent_task.cancel()
    except Exception:
        pass
    if uid in team1_ids:
        m.team1_points -= 1
        fail_name = m.group1_name
    else:
        m.team2_points -= 1
        fail_name = m.group2_name
    for gid in m.both_groups():
        await send_to_group(m, gid, "انسحب اللاعب.\n\n" + safe_str(fail_name, "") + " فقد روحًا.\n\nأرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\nأرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points), round_level=True)
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
    if uid in team1_ids:
        m.team1_points = 0
    else:
        m.team2_points = 0
    await finish_tournament(m)

@client.on(events.CallbackQuery(pattern=r"^force_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_force_internal(event):
    chat_id = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    g = internal_games.get(chat_id)
    if not g or g.bidder != bidder:
        return await event.answer("انتهى الوقت.", alert=True)
    if g.current_bid < 1:
        return await event.answer("انتظر حتى يزايد الخصم.", alert=True)
    if g.state != "opponent_choice":
        return await event.answer("تم اتخاذ القرار مسبقًا.", alert=True)
    await event.answer("تم إجبار الخصم على الإجابة.")
    try:
        if hasattr(g, "opponent_task") and g.opponent_task:
            g.opponent_task.cancel()
    except Exception:
        pass
    await send_to_group(g, g.chat_id, "تم إجبار اللاعب على الإجابة بالرقم " + str(g.current_bid) + ".", round_level=True)
    await begin_answer_internal(g, bidder)

async def begin_answer_internal(g, bidder):
    if g.current_bid < 1:
        return
    g.state = "answering"
    g.answers = []
    kb = [[Button.inline("أنهيت الإجابة", ("finish_int_" + str(g.chat_id)).encode())],
          [Button.inline("انسحاب من الجولة", ("wd_round_int_" + str(g.chat_id)).encode())],
          [Button.inline("انسحاب من المباراة", ("wd_match_int_" + str(g.chat_id)).encode())]]
    await client.send_message(bidder, "بدأ الوقت. أرسل " + str(g.current_bid) + " إجابة، كل إجابة في رسالة منفصلة.\nعند الانتهاء اضغط الزر. الوقت: 30 ثانية.", buttons=kb)
    g.answer_task = asyncio.create_task(answer_timeout_internal(g, bidder))
    asyncio.create_task(answer_countdown(g, g.chat_id, bidder, " للإجابة"))

async def answer_timeout_internal(g, bidder):
    await asyncio.sleep(30)
    if g.state == "answering" and g.bidder == bidder:
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
    await evaluate_internal(g, g.bidder)

async def evaluate_internal(g, bidder):
    g.state = "evaluating"
    ok, reason = await evaluate_answers_with_ai(g.question, g.current_bid, g.answers)
    try:
        bn = clean_name((await client.get_entity(bidder)).first_name)
    except Exception:
        bn = "لاعب"
    team = 1 if bidder in g.team1 else 2
    if ok:
        if team == 1:
            g.team2_points -= 1
            target_name = g.team2_label
        else:
            g.team1_points -= 1
            target_name = g.team1_label
        add_points(bidder, "player", 10, bn)
        result_line = "نجاح اللاعب " + user_link(bidder, bn) + "\n" + reason + "\n\n" + target_name + " فقد روحًا."
    else:
        if team == 1:
            g.team1_points -= 1
        else:
            g.team2_points -= 1
        add_points(bidder, "player", -20, bn)
        result_line = "فشل اللاعب " + user_link(bidder, bn) + "\n" + reason
    text = result_line + "\n\nأرواح " + g.team1_label + ": " + str(g.team1_points) + "\nأرواح " + g.team2_label + ": " + str(g.team2_points)
    await send_to_group(g, g.chat_id, text, round_level=True)
    try:
        await client.send_message(bidder, text)
    except Exception:
        pass
    await asyncio.sleep(1)
    if g.team1_points <= 0 or g.team2_points <= 0:
        await finish_internal(g)
        return
    await advance_round_internal(g)

@client.on(events.CallbackQuery(pattern=r"^force_t_(\d+)_(\d+)$"))
@safe_execute
async def cb_force_t(event):
    mid = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    m = tournaments.get(mid)
    if not m or m.bidder != bidder:
        return await event.answer("انتهى الوقت.", alert=True)
    if m.current_bid < 1:
        return await event.answer("انتظر حتى يزايد الخصم.", alert=True)
    if m.state != "opponent_choice":
        return await event.answer("تم اتخاذ القرار مسبقًا.", alert=True)
    await event.answer("تم إجبار الخصم على الإجابة.")
    try:
        if hasattr(m, "opponent_task") and m.opponent_task:
            m.opponent_task.cancel()
    except Exception:
        pass
    for gid in m.both_groups():
        await send_to_group(m, gid, "تم إجبار اللاعب على الإجابة بالرقم " + str(m.current_bid) + ".", round_level=True)
    await begin_answer_tournament(m, bidder)

async def begin_answer_tournament(m, bidder):
    if m.current_bid < 1:
        return
    m.state = "answering"
    m.answers = []
    kb = [[Button.inline("أنهيت الإجابة", ("finish_t_" + str(m.match_id)).encode())],
          [Button.inline("انسحاب من الجولة", ("wd_round_t_" + str(m.match_id)).encode())],
          [Button.inline("انسحاب من المباراة", ("wd_match_t_" + str(m.match_id)).encode())]]
    await client.send_message(bidder, "بدأ الوقت. أرسل " + str(m.current_bid) + " إجابة كل واحدة في رسالة.\nعند الانتهاء اضغط الزر. الوقت: 30 ثانية.", buttons=kb)
    m.answer_task = asyncio.create_task(answer_timeout_tournament(m, bidder))
    asyncio.create_task(answer_countdown(m, m.group1_id, bidder, " للإجابة"))

async def answer_timeout_tournament(m, bidder):
    await asyncio.sleep(30)
    if m.state == "answering" and m.bidder == bidder:
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
    await evaluate_tournament(m, m.bidder)

async def evaluate_tournament(m, bidder):
    m.state = "evaluating"
    ok, reason = await evaluate_answers_with_ai(m.question, m.current_bid, m.answers)
    team1_ids = [p["user_id"] for p in m.team1]
    team = 1 if bidder in team1_ids else 2
    bname = "لاعب"
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
        add_points(bidder, "player", 10, bname)
        result_line = "نجاح اللاعب " + user_link(bidder, bname) + "\n" + reason + "\n\n" + safe_str(target_name, "") + " فقد روحًا."
    else:
        if team == 1:
            m.team1_points -= 1
        else:
            m.team2_points -= 1
        add_points(bidder, "player", -20, bname)
        result_line = "فشل اللاعب " + user_link(bidder, bname) + "\n" + reason
    for gid in m.both_groups():
        text = result_line + "\n\nأرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\nأرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points)
        await send_to_group(m, gid, text, round_level=True)
    private_text = (result_line + "\n\n" +
                    "أرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\n" +
                    "أرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points))
    try:
        await client.send_message(bidder, private_text)
    except Exception:
        pass
    await asyncio.sleep(1)
    if m.team1_points <= 0 or m.team2_points <= 0:
        await finish_tournament(m)
        return
    await advance_round_tournament(m)

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
            return await event.reply("تحدي كروبين جارٍ.\nالحالة: " + m.state + "\nالجولة: " + str(m.round) + "\nأرواح " + safe_str(m.group1_name, "") + ": " + str(m.team1_points) + "\nأرواح " + safe_str(m.group2_name, "") + ": " + str(m.team2_points))
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
        await cancel_tasks(g)
        await delete_round_messages(g)
        await delete_pinned(g, event.chat_id)
        await strip_buttons(g)
        await asyncio.sleep(0.3)
        await delete_tracked(g)
        for p in g.players:
            private_sessions.pop(p, None)
        done = True
    for mid, m in list(tournaments.items()):
        if event.chat_id in m.both_groups():
            m.state = "done"
            tournaments.pop(mid, None)
            await cancel_tasks(m)
            await delete_round_messages(m)
            await strip_buttons(m)
            await asyncio.sleep(0.3)
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
    gtop = get_top("group")
    ptop = get_top("player")
    msg = "لوحة المتصدرين\n\nأفضل الكروبات:\n"
    for r in gtop:
        msg += "- " + safe_str(r["name"], "") + ": " + str(r["points"]) + " نقطة\n"
    msg += "\nأفضل اللاعبين:\n"
    for r in ptop:
        msg += "- " + safe_str(r["name"], "") + ": " + str(r["points"]) + " نقطة\n"
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^/help(?:@\S+)?$"))
@safe_execute
async def cmd_help(event):
    if event.is_private:
        return
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    await event.reply("الأوامر:\n/start_game عدد\n/end_game\n/status\n/top\n/help")

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

@client.on(events.NewMessage(pattern=r"^/unban_group (-?\d+)$", from_users=DEV_ID))
@safe_execute
async def cmd_unban(event):
    gid = int(event.pattern_match.group(1))
    unban_group(gid)
    await event.reply("تم إلغاء حظر المجموعة " + str(gid) + ".")

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
        lines.append("- " + safe_str(s["username"], "") + " (وضع: " + str(s["is_request_mode"]) + ")")
    await event.reply("\n".join(lines))

print("Bot is running...")
client.run_until_disconnected()
