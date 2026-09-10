from telethon import TelegramClient, events, Button
import asyncio
import time
import random

from config import API_ID, API_HASH, BOT_TOKEN, DEV_ID
from database import (init_db, add_points, get_top, is_banned, ban_group, unban_group,
                      add_force_sub, remove_force_sub, get_force_subs, get_stats,
                      get_all_groups, get_all_users, register_user, update_win_loss)
from utils import (safe_execute, clean_name, name_has_bad_word, require_subscription,
                   evaluate_answers_with_ai, translate_error)
from game_manager import (internal_games, tournaments, private_sessions, matchmaking_pool,
                          InternalGame, Tournament, get_question)

init_db()

client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

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

async def update_pinned(game_obj, chat_id, text, buttons=None):
    if not hasattr(game_obj, "pin_msgs") or game_obj.pin_msgs is None:
        game_obj.pin_msgs = {}
    existing = game_obj.pin_msgs.get(chat_id)
    if existing:
        try:
            await client.edit_message(chat_id, existing, text, buttons=buttons)
            await asyncio.sleep(0.5)
            return existing
        except Exception:
            pass
    try:
        msg = await client.send_message(chat_id, text, buttons=buttons)
    except Exception as e:
        try:
            await client.send_message(chat_id, "تعذر إرسال رسالة. " + translate_error(e))
        except Exception:
            pass
        return None
    game_obj.pin_msgs[chat_id] = msg.id
    if not hasattr(game_obj, "tracked_messages") or game_obj.tracked_messages is None:
        game_obj.tracked_messages = []
    game_obj.tracked_messages.append((chat_id, msg.id))
    try:
        await client.pin_message(chat_id, msg, notify=False)
    except Exception:
        pass
    await asyncio.sleep(0.7)
    return msg.id

async def unpin_and_keep(game_obj, chat_id):
    if not hasattr(game_obj, "pin_msgs"):
        return
    mid = game_obj.pin_msgs.get(chat_id)
    if not mid:
        return
    try:
        await client.unpin_message(chat_id, mid)
    except Exception:
        pass
    if hasattr(game_obj, "tracked_messages"):
        game_obj.tracked_messages = [(c, m) for c, m in game_obj.tracked_messages if not (c == chat_id and m == mid)]

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

async def send_main_menu(chat_id):
    u = await bot_username()
    text = (
        "مرحبًا بكم في بوت تحدي الثلاثين ثانية.\n\n"
        "اختر أحد الخيارات التالية:\n"
        "1) بدء مبارة مع مجموعة أخرى: يفتح باب الترشيح والتصويت في كروبكم، "
        "وإذا كانت هناك مجموعة تنتظر في قائمة المطابقة سيتم ضمّكم إليها تلقائيًا.\n"
        "2) بدء مبارة داخل الكروب: يحدد قائد التحدي عدد اللاعبين لكل فريق، "
        "وينضم الأعضاء بزر انضمام فقط بدون ترشيح أو تصويت.\n"
        "3) أضف البوت كمشرف: يمنح البوت صلاحية التثبيت والكتابة.\n"
        "4) شرح الأزرار: يعرض هذا الشرح مرة أخرى.\n"
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
    chat_title = getattr(chat, "title", "") or ""
    if name_has_bad_word(chat_title):
        try:
            if event.added_by:
                await client.send_message(event.added_by, "لا يمكنني البقاء في مجموعة تحمل اسمًا مخالفًا للسياسات.")
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
            if event.added_by:
                try:
                    await client.send_message(event.added_by, "لا يمكنني الدخول لمجموعتك (" + chat_title + ") لأن عدد الأعضاء الحقيقيين أقل من 5. العدد الحالي: " + str(humans))
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
        if not getattr(perms, "ban_users", False):
            missing.append("حظر المستخدمين")
        if not getattr(perms, "change_info", False):
            missing.append("تغيير معلومات المجموعة")
    else:
        missing.append("جميع صلاحيات المشرف")
    if (not is_admin) or missing:
        u = await bot_username()
        link = "https://t.me/" + u + "?startgroup=admin"
        text = (
            "مرحبًا، شكرًا لإضافتي إلى مجموعتك (" + chat_title + ").\n\n"
            "لكي أستطيع تنظيم التحديات بشكل كامل، أحتاج إلى صلاحيات المشرف الكاملة في المجموعة، "
            "وتحديدًا:\n"
            "1) حذف الرسائل\n"
            "2) تثبيت الرسائل\n"
            "3) حظر المستخدمين\n"
            "4) تغيير معلومات المجموعة\n\n"
        )
        if missing:
            text += "الصلاحيات الناقصة حاليًا: " + ", ".join(missing) + ".\n\n"
        text += (
            "يرجى إعادة إضافتي كمشرف مع تفعيل كل الصلاحيات. اضغط على الزر أدناه لإعادة الإضافة بشكل صحيح.\n\n"
            "سأغادر المجموعة الآن، وأعود بمجرد إضافتي بالصلاحيات المطلوبة."
        )
        kb = [[Button.url("أعد إضافة البوت كمشرف", link)]]
        target = event.added_by
        if target:
            try:
                await client.send_message(target, text, buttons=kb)
            except Exception:
                pass
        else:
            try:
                await client.send_message(event.chat_id, text, buttons=kb)
                await asyncio.sleep(2)
            except Exception:
                pass
        await client.delete_dialog(event.chat_id)
        return
    try:
        await client.send_message(event.chat_id, "تمت إضافتي بنجاح مع كل الصلاحيات المطلوبة. للبدء أرسل /start_game داخل المجموعة.")
    except Exception:
        pass

@client.on(events.NewMessage(pattern=r"^/start$"))
@safe_execute
async def cmd_start(event):
    if event.is_private:
        user = await event.get_sender()
        register_user(user.id, clean_name(user.first_name))
        u = await bot_username()
        text = (
            "أهلًا بك في بوت تحدي الثلاثين ثانية.\n\n"
            "هذا البوت ينظم تحديات سريعة بين الكروبات والأفراد، مع نظام مزايدة في الخاص، "
            "وعدّاد ثلاثين ثانية للإجابة، وتقييم الإجابات بالذكاء الاصطناعي، "
            "ولوحة متصدرين لأفضل الكروبات واللاعبين.\n\n"
            "خطوات اللعب:\n"
            "1) أضف البوت إلى مجموعتك بصفة مشرف.\n"
            "2) يجب أن تحتوي المجموعة على 5 أعضاء حقيقيين على الأقل.\n"
            "3) اكتب /start_game داخل المجموعة لبدء التحدي.\n"
            "4) اختر نوع اللعب من الأزرار التي تظهر.\n\n"
            "ملاحظة: جميع الأدوار والمزايدات والإجابات تحدث في الخاص، والنتائج تُعلن في المجموعة."
        )
        kb = [
            [Button.url("أضف البوت إلى مجموعتك", "https://t.me/" + u + "?startgroup=admin")],
        ]
        await event.reply(text, buttons=kb)
        return
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await require_subscription(event):
        return
    await send_main_menu(event.chat_id)

@client.on(events.NewMessage(pattern=r"^/start_game(?: (\d+))?$"))
@safe_execute
async def cmd_start_game(event):
    if event.is_private:
        return await event.reply("هذا الأمر مخصص للمجموعات.")
    if is_banned(event.chat_id):
        return await event.reply("لقد تم حظر مجموعتكم من استعمال البوت.")
    if not await require_subscription(event):
        return
    n = event.pattern_match.group(1)
    if n is None:
        kb = []
        for size in (1, 2, 3, 4, 5):
            kb.append([Button.inline(str(size) + " ضد " + str(size), ("internal_size_" + str(size)).encode())])
        return await event.reply("اختر عدد اللاعبين لكل فريق:", buttons=kb)
    team_size = int(n)
    if team_size < 1 or team_size > 10:
        return await event.reply("عدد اللاعبين لكل فريق يجب أن يكون بين 1 و 10.")
    await start_internal(event.chat_id, event.chat.title, team_size)

@client.on(events.CallbackQuery(data=b"explain_buttons"))
@safe_execute
async def cb_explain(event):
    await event.answer()
    text = (
        "شرح الأزرار:\n\n"
        "بدء مبارة مع مجموعة أخرى: يفتح باب الترشيح والتصويت داخل كروبكم، ثم يختار البوت أعلى خمسة مرشحين بحسب التصويت. "
        "إذا كانت هناك مجموعة أخرى في قائمة الانتظار فسيتم دمج الكروبين في تحدي واحد مباشرة.\n\n"
        "أضف البوت كمشرف: يفتح لك نافذة إضافة البوت إلى مجموعتك مع صلاحيات المشرف ليتمكن من التثبيت والكتابة.\n\n"
        "بدء مبارة داخل الكروب: يحدد قائد التحدي عدد اللاعبين لكل فريق، ثم يفتح زر انضمام للجميع بدون ترشيح أو تصويت. "
        "عند اكتمال العدد يُقفل الزر ويبدأ التحدي فورًا."
    )
    await event.reply(text)

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
        await event.edit("تمت إضافة كروبكم إلى قائمة الانتظار. عند دخول كروب آخر للتحدي سيتم فتح باب الترشيح مباشرة في كروباكم.")
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
    title = event.chat.title if hasattr(event.chat, "title") else "مجموعة"
    try:
        await event.delete()
    except Exception:
        pass
    await start_internal(event.chat_id, title, n)

async def start_internal(chat_id, chat_name, team_size):
    if chat_id in internal_games:
        await client.send_message(chat_id, "توجد لعبة جارية بالفعل في هذه المجموعة.")
        return
    g = InternalGame(chat_id, chat_name, team_size)
    internal_games[chat_id] = g
    kb = [[Button.inline("انضمام للتحدي", ("join_int_" + str(chat_id)).encode())]]
    text = ("تحدي داخلي جديد\n"
            "عدد اللاعبين لكل فريق: " + str(team_size) + "\n"
            "المطلوب: " + str(team_size * 2) + " لاعب\n\n"
            "اللاعبون المنضمون (0/" + str(team_size * 2) + "):\n"
            "لا أحد بعد\n\n"
            "اضغط زر الانضمام للمشاركة.")
    await update_pinned(g, chat_id, text, buttons=kb)

@client.on(events.CallbackQuery(pattern=r"^join_int_(-?\d+)$"))
@safe_execute
async def cb_join_internal(event):
    chat_id = int(event.pattern_match.group(1))
    g = internal_games.get(chat_id)
    if not g or g.state != "waiting":
        return await event.answer("لا يمكن الانضمام الآن.", alert=True)
    user = await event.get_sender()
    if user.id in g.players:
        return await event.answer("أنت منضم بالفعل.", alert=True)
    if len(g.players) >= g.required_total:
        return await event.answer("اكتمل العدد.", alert=True)
    users_with_chat = get_all_users()
    if user.id not in users_with_chat:
        return await event.answer("افتح محادثة مع البوت في الخاص واضغط /start ثم عد واضغط انضمام.", alert=True)
    name = clean_name(user.first_name)
    g.players.append(user.id)
    g.names.append(name)
    await event.answer("تم الانضمام.")
    if len(g.players) >= g.required_total:
        g.split_teams()
        g.state = "ready_check"
        t1 = []
        for u in g.team1:
            try:
                ent = await client.get_entity(u)
                t1.append(clean_name(ent.first_name))
            except Exception:
                t1.append("لاعب")
        t2 = []
        for u in g.team2:
            try:
                ent = await client.get_entity(u)
                t2.append(clean_name(ent.first_name))
            except Exception:
                t2.append("لاعب")
        kb = [[Button.inline("جاهز", ("ready_int_" + str(chat_id)).encode())]]
        text = ("اكتمل العدد وتم تقسيم الفريقين\n\n"
                "الفريق الأول:\n- " + "\n- ".join(t1) + "\n\n"
                "الفريق الثاني:\n- " + "\n- ".join(t2) + "\n\n"
                "حالة الجاهزية: 0 من " + str(g.required_total) + "\n\n"
                "على الجميع الضغط على زر جاهز للبدء.")
        await update_pinned(g, chat_id, text, buttons=kb)
    else:
        kb = [[Button.inline("انضمام للتحدي", ("join_int_" + str(chat_id)).encode())]]
        text = ("تحدي داخلي جديد\n"
                "عدد اللاعبين لكل فريق: " + str(g.team_size) + "\n"
                "المطلوب: " + str(g.required_total) + " لاعب\n\n"
                "اللاعبون المنضمون (" + str(len(g.players)) + "/" + str(g.required_total) + "):\n- "
                + "\n- ".join(g.names) + "\n\n"
                "اضغط زر الانضمام للمشاركة.")
        await update_pinned(g, chat_id, text, buttons=kb)

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
    ready_lines = []
    waiting_lines = []
    for p in g.players:
        try:
            pname = clean_name((await client.get_entity(p)).first_name)
        except Exception:
            pname = "لاعب"
        if p in g.ready:
            ready_lines.append(pname)
        else:
            waiting_lines.append(pname)
    t1 = []
    for u in g.team1:
        try:
            t1.append(clean_name((await client.get_entity(u)).first_name))
        except Exception:
            t1.append("لاعب")
    t2 = []
    for u in g.team2:
        try:
            t2.append(clean_name((await client.get_entity(u)).first_name))
        except Exception:
            t2.append("لاعب")
    txt = ("الفريق الأول:\n- " + "\n- ".join(t1) + "\n\n"
           "الفريق الثاني:\n- " + "\n- ".join(t2) + "\n\n"
           "حالة الجاهزية: " + str(len(g.ready)) + " من " + str(g.required_total) + "\n\n")
    if ready_lines:
        txt += "استعدوا:\n- " + "\n- ".join(ready_lines)
    else:
        txt += "استعدوا: لا أحد بعد"
    if waiting_lines:
        txt += "\n\nبالانتظار:\n- " + "\n- ".join(waiting_lines)
    else:
        txt += "\n\nبالانتظار: لا أحد، الجميع جاهز"
    kb = [[Button.inline("جاهز", ("ready_int_" + str(chat_id)).encode())]]
    await update_pinned(g, chat_id, txt, buttons=kb)
    if len(g.ready) >= g.required_total:
        await asyncio.sleep(1)
        await start_round_internal(g)

async def start_round_internal(g):
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
    try:
        bn = clean_name((await client.get_entity(b)).first_name)
    except Exception:
        bn = "لاعب"
    try:
        on = clean_name((await client.get_entity(o)).first_name)
    except Exception:
        on = "لاعب"
    text = ("الجولة " + str(g.round) + "\n\n"
            "السؤال: اذكر أكبر عدد من " + g.question + "\n\n"
            "نقاط الفريق الأول: " + str(g.team1_points) + "\n"
            "نقاط الفريق الثاني: " + str(g.team2_points) + "\n\n"
            "المزايد: " + bn + " من الفريق الأول\n"
            "الخصم: " + on + " من الفريق الثاني\n\n"
            "المزايدة تجري الآن في الخاص، ولدى المزايد 20 ثانية.")
    await update_pinned(g, g.chat_id, text, buttons=None)
    await asyncio.sleep(1)
    try:
        await client.send_message(b, "بدأت المزايدة للجولة " + str(g.round) + ".\nالسؤال: اذكر أكبر عدد من " + g.question + "\nكم رقمًا تستطيع أن تذكر؟ أرسل رقمًا فقط خلال 20 ثانية.")
    except Exception:
        pass
    g.bidding_task = asyncio.create_task(bidding_timeout_internal(g, b))

async def bidding_timeout_internal(g, user_id):
    await asyncio.sleep(20)
    if g.state != "bidding" or g.bidder != user_id:
        return
    g.team1_points -= 20
    g.team2_points += 10
    text = ("انتهت مدة المزايدة دون رد\n\n"
            "تم إقصاء المزايد تلقائيًا\n"
            "خصم 20 نقطة من الفريق الأول\n"
            "إضافة 10 نقاط للفريق الثاني\n\n"
            "نقاط الفريق الأول: " + str(g.team1_points) + "\n"
            "نقاط الفريق الثاني: " + str(g.team2_points))
    await update_pinned(g, g.chat_id, text, buttons=None)
    await asyncio.sleep(1.5)
    await advance_round_internal(g)

async def advance_round_internal(g):
    if g.team1_points <= 0 or g.team2_points <= 0:
        return await finish_internal(g)
    g.round += 1
    await asyncio.sleep(1)
    await start_round_internal(g)

async def finish_internal(g):
    g.state = "done"
    if g.team1_points > g.team2_points:
        winner = "الفريق الأول"
        add_points(g.chat_id, "group", 50, g.chat_name)
        update_win_loss(g.chat_id, "group", True)
    else:
        winner = "الفريق الثاني"
        add_points(g.chat_id, "group", -20, g.chat_name)
        update_win_loss(g.chat_id, "group", False)
    text = ("انتهت المعركة\n\n"
            "الفائز: " + winner + "\n\n"
            "نقاط الفريق الأول: " + str(g.team1_points) + "\n"
            "نقاط الفريق الثاني: " + str(g.team2_points))
    await update_pinned(g, g.chat_id, text, buttons=None)
    await unpin_and_keep(g, g.chat_id)
    for p in g.players:
        private_sessions.pop(p, None)
    internal_games.pop(g.chat_id, None)

async def open_nomination(match):
    kb = [[Button.inline("ترشيح نفسي", ("nom_" + str(match.match_id)).encode())]]
    text = ("فتح باب الترشيح للتحدي\n\n"
            "الخصم: " + match.group2_name + "\n\n"
            "المطلوب من أعضاء الكروب ترشيح أنفسهم، ثم يصوّت الأعضاء على المرشحين.\n"
            "سيتم اختيار أعلى " + str(match.squad) + " مرشحين بحسب التصويت.\n\n"
            "اضغط زر ترشيح نفسي للترشيح.")
    await update_pinned(match, match.group1_id, text, buttons=kb)
    kb2 = [[Button.inline("ترشيح نفسي", ("nom_" + str(match.match_id)).encode())]]
    text2 = ("فتح باب الترشيح للتحدي\n\n"
             "الخصم: " + match.group1_name + "\n\n"
             "المطلوب من أعضاء الكروب ترشيح أنفسهم، ثم يصوّت الأعضاء على المرشحين.\n"
             "سيتم اختيار أعلى " + str(match.squad) + " مرشحين بحسب التصويت.\n\n"
             "اضغط زر ترشيح نفسي للترشيح.")
    await update_pinned(match, match.group2_id, text2, buttons=kb2)

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
    opp = m.group2_name if gid == m.group1_id else m.group1_name
    lines = ["فتح باب الترشيح للتحدي\n",
             "الخصم: " + opp + "\n",
             "المرشحون في " + header + ":"]
    buttons = []
    for uid, d in lst:
        lines.append("- " + d["name"] + " — " + str(len(d["votes"])) + " صوت")
        buttons.append([Button.inline("تصويت لـ " + d["name"], ("vote_" + str(m.match_id) + "_" + str(gid) + "_" + str(uid)).encode())])
    buttons.append([Button.inline("إنهاء الترشيح الآن", ("close_nom_" + str(m.match_id) + "_" + str(gid)).encode())])
    await update_pinned(m, gid, "\n".join(lines), buttons=buttons)

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
        names = "\n- ".join(p["name"] for p in team) if team else "لا يوجد"
        opp = m.group2_name if gid_ == m.group1_id else m.group1_name
        kb = [[Button.inline("جاهز", ("ready_t_" + str(m.match_id)).encode())]]
        txt = ("تم اختيار الفريق الممثل لـ " + gname + "\n\n"
               "الخصم: " + opp + "\n\n"
               "الفريق:\n- " + names + "\n\n"
               "حالة الجاهزية: 0 من " + str(len(team)) + "\n\n"
               "على جميع الممثلين الضغط على زر جاهز للبدء.")
        await update_pinned(m, gid_, txt, buttons=kb)

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
    ready_lines = []
    waiting_lines = []
    for p in team:
        if p["user_id"] in m.ready:
            ready_lines.append(p["name"])
        else:
            waiting_lines.append(p["name"])
    gname = m.group1_name if gid == m.group1_id else m.group2_name
    opp = m.group2_name if gid == m.group1_id else m.group1_name
    txt = ("تم اختيار الفريق الممثل لـ " + gname + "\n\n"
           "الخصم: " + opp + "\n\n"
           "حالة الجاهزية: " + str(len(ready_lines)) + " من " + str(len(team)) + "\n\n")
    if ready_lines:
        txt += "استعدوا:\n- " + "\n- ".join(ready_lines)
    else:
        txt += "استعدوا: لا أحد بعد"
    if waiting_lines:
        txt += "\n\nبالانتظار:\n- " + "\n- ".join(waiting_lines)
    else:
        txt += "\n\nبالانتظار: لا أحد، الجميع جاهز"
    kb = [[Button.inline("جاهز", ("ready_t_" + str(m.match_id)).encode())]]
    await update_pinned(m, gid, txt, buttons=kb)
    needed = len(m.team1) + len(m.team2)
    if needed > 0 and len(m.ready) >= needed:
        await asyncio.sleep(1)
        await start_round_tournament(m)

async def start_round_tournament(m):
    if not m.team1 or not m.team2:
        await client.send_message(m.group1_id, "لا يمكن بدء التحدي: فريق فارغ.")
        tournaments.pop(m.match_id, None)
        return
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
    text1 = ("الجولة " + str(m.round) + "\n\n"
             "السؤال: اذكر أكبر عدد من " + m.question + "\n\n"
             "نقاط " + m.group1_name + ": " + str(m.team1_points) + "\n"
             "نقاط " + m.group2_name + ": " + str(m.team2_points) + "\n\n"
             "المزايد الحالي: " + bname + " من " + m.group1_name + "\n"
             "الخصم: " + oname + " من " + m.group2_name + "\n\n"
             "المزايدة تجري الآن في الخاص، ولدى المزايد 20 ثانية.")
    await update_pinned(m, m.group1_id, text1, buttons=None)
    text2 = ("الجولة " + str(m.round) + "\n\n"
             "السؤال: اذكر أكبر عدد من " + m.question + "\n\n"
             "نقاط " + m.group1_name + ": " + str(m.team1_points) + "\n"
             "نقاط " + m.group2_name + ": " + str(m.team2_points) + "\n\n"
             "المزايد الحالي: " + bname + " من " + m.group1_name + "\n"
             "الخصم: " + oname + " من " + m.group2_name + "\n\n"
             "المزايدة تجري الآن في الخاص، ولدى المزايد 20 ثانية.")
    await update_pinned(m, m.group2_id, text2, buttons=None)
    await asyncio.sleep(1)
    try:
        await client.send_message(b, "بدأت المزايدة للجولة " + str(m.round) + ".\nالسؤال: اذكر أكبر عدد من " + m.question + "\nأرسل رقمًا فقط خلال 20 ثانية.")
    except Exception:
        pass
    m.bidding_task = asyncio.create_task(bidding_timeout_tournament(m, b))

async def bidding_timeout_tournament(m, user_id):
    await asyncio.sleep(20)
    if m.state != "bidding" or m.bidder != user_id:
        return
    failing_team = 1 if any(p["user_id"] == user_id for p in m.team1) else 2
    if failing_team == 1:
        m.team1_points -= 20
        m.team2_points += 10
        fail_name = m.group1_name
        win_name = m.group2_name
    else:
        m.team2_points -= 20
        m.team1_points += 10
        fail_name = m.group2_name
        win_name = m.group1_name
    text = ("انتهت مدة المزايدة دون رد\n\n"
            "تم إقصاء المزايد تلقائيًا\n"
            "خصم 20 نقطة من " + fail_name + "\n"
            "إضافة 10 نقاط لـ " + win_name + "\n\n"
            "نقاط " + m.group1_name + ": " + str(m.team1_points) + "\n"
            "نقاط " + m.group2_name + ": " + str(m.team2_points))
    await update_pinned(m, m.group1_id, text, buttons=None)
    await update_pinned(m, m.group2_id, text, buttons=None)
    await asyncio.sleep(1.5)
    await advance_round_tournament(m)

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
    for gid in m.both_groups():
        text = ("انتهت المعركة الكبرى\n\n"
                "الفائز: " + winner + "\n\n"
                "نقاط " + m.group1_name + ": " + str(m.team1_points) + "\n"
                "نقاط " + m.group2_name + ": " + str(m.team2_points))
        await update_pinned(m, gid, text, buttons=None)
        await unpin_and_keep(m, gid)
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
    sess = private_sessions.get(uid)
    if not sess:
        return
    text = (event.text or "").strip()

    if sess["game_type"] == "internal":
        g = internal_games.get(sess["chat_id"])
        if not g:
            private_sessions.pop(uid, None)
            return await event.reply("انتهت اللعبة.")
        if sess["role"] == "bidder" and g.state == "bidding" and g.bidder == uid:
            if not text.isdigit():
                return await event.reply("أرسل رقمًا فقط.")
            bid = int(text)
            if bid <= 0 or bid > 50:
                return await event.reply("الرقم يجب أن يكون بين 1 و 50.")
            g.current_bid = bid
            try:
                if g.bidding_task:
                    g.bidding_task.cancel()
            except Exception:
                pass
            g.state = "opponent_choice"
            opp = g.opponent
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_" + str(g.chat_id) + "_" + str(uid)).encode())],
                  [Button.inline("مزايدة أعلى", ("outbid_" + str(g.chat_id) + "_" + str(uid)).encode())]]
            await client.send_message(opp, "خصمك قال إنه يستطيع ذكر " + str(bid) + " من " + g.question + ".\nإما أن تزايد برقم أعلى، أو تجبره على الإجابة بالرقم الذي قاله.", buttons=kb)
            await event.reply("تم تسجيل مزايدتك.")
        elif sess["role"] == "opponent" and g.state == "opponent_choice":
            if not text.isdigit():
                return await event.reply("أرسل رقمًا أعلى، أو استخدم الأزرار.")
            newbid = int(text)
            if newbid <= g.current_bid:
                return await event.reply("يجب أن يكون أكبر من " + str(g.current_bid) + ".")
            g.current_bid = newbid
            old_bidder = g.bidder
            old_opponent = g.opponent
            g.bidder = old_opponent
            g.opponent = old_bidder
            private_sessions[g.bidder] = {"game_type": "internal", "chat_id": g.chat_id, "role": "bidder"}
            private_sessions[g.opponent] = {"game_type": "internal", "chat_id": g.chat_id, "role": "opponent"}
            await client.send_message(g.bidder, "تم رفع المزايدة إلى " + str(newbid) + ". أرسل رقمًا أكبر أو انتظر إجبار الخصم.")
            kb = [[Button.inline("إجباره على الإجابة " + str(newbid), ("force_" + str(g.chat_id) + "_" + str(g.opponent)).encode())]]
            await client.send_message(g.opponent, "بانتظار قرار الخصم.", buttons=kb)
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
            if bid <= 0 or bid > 50:
                return await event.reply("الرقم بين 1 و 50.")
            m.current_bid = bid
            try:
                if m.bidding_task:
                    m.bidding_task.cancel()
            except Exception:
                pass
            m.state = "opponent_choice"
            opp = m.opponent
            kb = [[Button.inline("إجباره على الإجابة " + str(bid), ("force_t_" + str(m.match_id) + "_" + str(uid)).encode())],
                  [Button.inline("مزايدة أعلى", ("outbid_t_" + str(m.match_id) + "_" + str(uid)).encode())]]
            await client.send_message(opp, "خصمك قال إنه يذكر " + str(bid) + " من " + m.question + ".", buttons=kb)
            await event.reply("تم تسجيل مزايدتك.")
        elif sess["role"] == "opponent" and m.state == "opponent_choice":
            if not text.isdigit():
                return await event.reply("أرسل رقمًا أعلى.")
            newbid = int(text)
            if newbid <= m.current_bid:
                return await event.reply("يجب أن يكون أكبر من " + str(m.current_bid) + ".")
            m.current_bid = newbid
            old_bidder = m.bidder
            old_opponent = m.opponent
            m.bidder = old_opponent
            m.opponent = old_bidder
            private_sessions[m.bidder] = {"game_type": "tournament", "match_id": m.match_id, "role": "bidder"}
            private_sessions[m.opponent] = {"game_type": "tournament", "match_id": m.match_id, "role": "opponent"}
            await client.send_message(m.bidder, "تم رفع المزايدة إلى " + str(newbid) + ".")
            kb = [[Button.inline("إجباره على الإجابة " + str(newbid), ("force_t_" + str(m.match_id) + "_" + str(m.opponent)).encode())]]
            await client.send_message(m.opponent, "بانتظار قرار الخصم.", buttons=kb)
        elif sess["role"] == "bidder" and m.state == "answering":
            m.answers.append(text)

@client.on(events.CallbackQuery(pattern=r"^outbid_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_outbid_internal(event):
    await event.answer("أرسل الرقم الجديد في الخاص.")

@client.on(events.CallbackQuery(pattern=r"^outbid_t_(\d+)_(\d+)$"))
@safe_execute
async def cb_outbid_t(event):
    await event.answer("أرسل الرقم الجديد في الخاص.")

@client.on(events.CallbackQuery(pattern=r"^force_(-?\d+)_(\d+)$"))
@safe_execute
async def cb_force_internal(event):
    chat_id = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    g = internal_games.get(chat_id)
    if not g or g.bidder != bidder:
        return await event.answer("انتهى الوقت.", alert=True)
    await event.answer("تم بدء التحدي.")
    await begin_answer_internal(g, bidder)

async def begin_answer_internal(g, bidder):
    g.state = "answering"
    g.answers = []
    kb = [[Button.inline("أنهيت الإجابة", ("finish_int_" + str(g.chat_id)).encode())]]
    await client.send_message(bidder, "بدأ الوقت. أرسل " + str(g.current_bid) + " إجابة، كل إجابة في رسالة منفصلة.\nعند الانتهاء اضغط الزر. الوقت: 30 ثانية.", buttons=kb)
    g.answer_task = asyncio.create_task(answer_timeout_internal(g, bidder))

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
            g.team1_points += 10
        else:
            g.team2_points += 10
        add_points(bidder, "player", 10, bn)
        result_line = "نجاح اللاعب " + bn + "\nالسبب: " + reason
    else:
        if team == 1:
            g.team1_points -= 20
            g.team2_points += 10
        else:
            g.team2_points -= 20
            g.team1_points += 10
        add_points(bidder, "player", -20, bn)
        result_line = "فشل اللاعب " + bn + "\nالسبب: " + reason
    text = (result_line + "\n\n"
            "نقاط الفريق الأول: " + str(g.team1_points) + "\n"
            "نقاط الفريق الثاني: " + str(g.team2_points))
    await update_pinned(g, g.chat_id, text, buttons=None)
    try:
        await client.send_message(bidder, "انتهت جولتك، عد إلى المجموعة للنتائج.")
    except Exception:
        pass
    await asyncio.sleep(1.5)
    await advance_round_internal(g)

@client.on(events.CallbackQuery(pattern=r"^force_t_(\d+)_(\d+)$"))
@safe_execute
async def cb_force_t(event):
    mid = int(event.pattern_match.group(1))
    bidder = int(event.pattern_match.group(2))
    m = tournaments.get(mid)
    if not m or m.bidder != bidder:
        return await event.answer("انتهى الوقت.", alert=True)
    await event.answer("تم بدء التحدي.")
    await begin_answer_tournament(m, bidder)

async def begin_answer_tournament(m, bidder):
    m.state = "answering"
    m.answers = []
    kb = [[Button.inline("أنهيت الإجابة", ("finish_t_" + str(m.match_id)).encode())]]
    await client.send_message(bidder, "بدأ الوقت. أرسل " + str(m.current_bid) + " إجابة كل واحدة في رسالة.\nعند الانتهاء اضغط الزر. الوقت: 30 ثانية.", buttons=kb)
    m.answer_task = asyncio.create_task(answer_timeout_tournament(m, bidder))

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
            m.team1_points += 10
        else:
            m.team2_points += 10
        add_points(bidder, "player", 10, bname)
        result_line = "نجاح اللاعب " + bname + "\nالسبب: " + reason
    else:
        if team == 1:
            m.team1_points -= 20
            m.team2_points += 10
        else:
            m.team2_points -= 20
            m.team1_points += 10
        add_points(bidder, "player", -20, bname)
        result_line = "فشل اللاعب " + bname + "\nالسبب: " + reason
    text = (result_line + "\n\n"
            "نقاط " + m.group1_name + ": " + str(m.team1_points) + "\n"
            "نقاط " + m.group2_name + ": " + str(m.team2_points))
    await update_pinned(m, m.group1_id, text, buttons=None)
    await update_pinned(m, m.group2_id, text, buttons=None)
    await asyncio.sleep(1.5)
    await advance_round_tournament(m)

@client.on(events.NewMessage(pattern=r"^/status$"))
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
        return await event.reply("اللعبة الداخلية الجارية:\nالحالة: " + g.state + "\nالجولة: " + str(g.round) + "\nنقاط الفريق الأول: " + str(g.team1_points) + "\nنقاط الفريق الثاني: " + str(g.team2_points) + "\nعدد اللاعبين: " + str(len(g.players)))
    for m in tournaments.values():
        if event.chat_id in m.both_groups():
            return await event.reply("تحدي كروبين جارٍ.\nالحالة: " + m.state + "\nالجولة: " + str(m.round) + "\nنقاط " + m.group1_name + ": " + str(m.team1_points) + "\nنقاط " + m.group2_name + ": " + str(m.team2_points))
    await event.reply("لا توجد ألعاب جارية. أرسل /start_game للبدء.")

@client.on(events.NewMessage(pattern=r"^/end_game$"))
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
        await strip_buttons(g)
        await asyncio.sleep(0.5)
        await delete_tracked(g)
        for p in g.players:
            private_sessions.pop(p, None)
        done = True
    for mid, m in list(tournaments.items()):
        if event.chat_id in m.both_groups():
            await strip_buttons(m)
            await asyncio.sleep(0.5)
            await delete_tracked(m)
            for p in m.team1 + m.team2:
                private_sessions.pop(p["user_id"], None)
            tournaments.pop(mid, None)
            done = True
    if done:
        await event.reply("تم إنهاء اللعبة الحالية وحذف رسائلها.")
    else:
        await event.reply("لا توجد لعبة جارية.")

@client.on(events.NewMessage(pattern=r"^/top$"))
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
        msg += "- " + r["name"] + ": " + str(r["points"]) + " نقطة\n"
    msg += "\nأفضل اللاعبين:\n"
    for r in ptop:
        msg += "- " + r["name"] + ": " + str(r["points"]) + " نقطة\n"
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^/help$"))
@safe_execute
async def cmd_help(event):
    if event.is_private:
        return
    if not await is_group_admin(event):
        return await event.reply("هذا الأمر مخصص للمشرفين فقط.")
    await event.reply("الأوامر المتاحة:\n/start_game عدد - بدء تحدي داخلي\n/end_game - إنهاء اللعبة (للمشرف)\n/status - حالة اللعبة\n/top - لوحة المتصدرين\n/help - هذه القائمة")

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
    await event.reply("الإحصائيات:\nالمجموعات: " + str(s["groups"]) + "\nاللاعبون: " + str(s["players"]) + "\nالمستخدمون المسجلون: " + str(s["users"]) + "\nالمجموعات المحظورة: " + str(s["banned"]) + "\nقنوات الاشتراك الإجباري: " + str(s["subs"]))

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
    await event.reply("تمت إضافة الاشتراك الإجباري: " + final + " (وضع الطلب: " + str(mode) + ").")

@client.on(events.NewMessage(pattern=r"^/remove_sub (\S+)$", from_users=DEV_ID))
@safe_execute
async def cmd_remove_sub(event):
    uname = event.pattern_match.group(1)
    try:
        ent = await client.get_entity(uname)
        remove_force_sub(ent.id)
    except Exception:
        remove_force_sub(0)
    await event.reply("تم حذف القناة " + uname + " من الاشتراك الإجباري.")

@client.on(events.NewMessage(pattern=r"^/list_subs$", from_users=DEV_ID))
@safe_execute
async def cmd_list_subs(event):
    subs = get_force_subs()
    if not subs:
        return await event.reply("لا توجد قنوات اشتراك إجباري.")
    lines = ["قنوات الاشتراك الإجباري:"]
    for s in subs:
        lines.append("- " + s["username"] + " (وضع الطلب: " + str(s["is_request_mode"]) + ")")
    await event.reply("\n".join(lines))

print("Bot is running...")
client.run_until_disconnected()
