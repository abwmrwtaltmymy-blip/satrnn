import re
import json
import google.generativeai as genai
from telethon import errors, functions, Button
from config import GEMINI_API_KEY
from questions import BAD_WORDS, SAFE_FALLBACK

_gemini_ready = False
_gemini_model = None

def _init_gemini():
    global _gemini_ready, _gemini_model
    if _gemini_ready:
        return True
    if not GEMINI_API_KEY or not GEMINI_API_KEY.startswith("AIza") or len(GEMINI_API_KEY) < 30:
        return False
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config={
                "temperature": 0.1,
                "top_p": 0.9,
                "max_output_tokens": 512,
            },
            system_instruction=(
                "أنت حكم عربي صارم لتحديات الألعاب. تتحقق من صحة الإجابات. "
                "ترد بالعربية فقط وبصيغة JSON صارمة كما يُطلب منك."
            ),
        )
        _gemini_ready = True
        return True
    except Exception:
        _gemini_ready = False
        return False

def clean_name(name, default_fallback=SAFE_FALLBACK):
    if not name:
        return default_fallback
    low = name.lower()
    if re.search(r"t\.me|telegram\.me|https?://|\bwww\b|\.com|\.net|\.org|@", low):
        return default_fallback
    for w in BAD_WORDS:
        if w in low:
            return default_fallback
    return name[:32]

def name_has_bad_word(name):
    if not name:
        return False
    low = name.lower()
    if re.search(r"t\.me|telegram\.me|https?://|\bwww\b|\.com|\.net|\.org|@", low):
        return True
    for w in BAD_WORDS:
        if w in low:
            return True
    return False

def _normalize_answers(answers_list):
    uniq = []
    seen = set()
    for a in answers_list:
        s = (a or "").strip()
        if len(s) < 2:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return uniq

def _local_fallback_check(expected_count, answers_list):
    uniq = _normalize_answers(answers_list)
    count = len(uniq)
    if count >= expected_count:
        return True, "تم قبول " + str(count) + " إجابة مختلفة من أصل " + str(expected_count) + " مطلوبة."
    return False, "عدد الإجابات المختلفة " + str(count) + " أقل من المطلوب " + str(expected_count) + "."

def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        return low in ("true", "yes", "نعم", "1", "صح", "صحيح")
    if isinstance(v, (int, float)):
        return v != 0
    return False

def _parse_gemini_json(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return None

async def evaluate_answers_with_ai(question, expected_count, answers_list):
    if not answers_list:
        return False, "لم يتم إرسال أي إجابة."
    uniq = _normalize_answers(answers_list)
    if not uniq:
        return False, "لم يتم إرسال أي إجابة صالحة."

    if len(uniq) < expected_count:
        return False, "عدد الإجابات المختلفة " + str(len(uniq)) + " أقل من المطلوب " + str(expected_count) + "."

    if not _init_gemini():
        return _local_fallback_check(expected_count, uniq)

    prompt = (
        "قيّم إجابات لاعب في تحدي سريع.\n\n"
        "التصنيف المطلوب: " + question + "\n"
        "العدد المطلوب: " + str(expected_count) + "\n"
        "عدد الإجابات المستلمة: " + str(len(uniq)) + "\n"
        "الإجابات:\n"
        + "\n".join("- " + a for a in uniq) + "\n\n"
        "قواعد صارمة يجب تطبيقها:\n"
        "1. احسب فقط الإجابات التي تنتمي فعليًا لتصنيف السؤال.\n"
        "2. اعتبر المكرر بمعنى واحد إجابة واحدة فقط.\n"
        "3. إذا كان عدد الإجابات الصحيحة والمختلفة أقل من " + str(expected_count) + " فالنتيجة فشل مباشرة.\n"
        "4. لا تتهاون ولا تخترع إجابات صحيحة غير موجودة في القائمة.\n"
        "5. إذا الشك موجود في إجابة، اعتبرها خاطئة.\n"
        "6. لا تقبل الإجابات العامة أو الغامضة.\n\n"
        "أعد النتيجة بصيغة JSON فقط بدون أي نص خارجها:\n"
        "{\"correct\": true أو false, \"count\": رقم_الإجابات_الصحيحة_والمختلفة, \"reason\": \"سبب مختصر جدًا بالعربية\"}"
    )

    try:
        resp = await _gemini_model.generate_content_async(prompt)
        text = (resp.text or "").strip()
    except Exception:
        ok, reason = _local_fallback_check(expected_count, uniq)
        return ok, "تعذر تحليل الذكاء الاصطناعي، تم الاعتماد على الفحص المحلي. " + reason

    data = _parse_gemini_json(text)
    if data is None:
        ok, reason = _local_fallback_check(expected_count, uniq)
        return ok, "تعذر قراءة رد الذكاء الاصطناعي، تم الاعتماد على الفحص المحلي. " + reason

    correct = _to_bool(data.get("correct"))
    count_raw = data.get("count")
    reason = data.get("reason") or ""
    try:
        count_int = int(count_raw) if count_raw is not None else len(uniq)
    except Exception:
        count_int = len(uniq)

    if count_int < expected_count:
        correct = False

    if correct:
        return True, "تم قبول " + str(count_int) + " إجابة صحيحة ومختلفة من أصل " + str(expected_count) + " مطلوبة. " + reason
    return False, "عدد الإجابات الصحيحة والمختلفة " + str(count_int) + " أقل من المطلوب " + str(expected_count) + ". " + reason

def translate_error(err):
    low = str(err).lower()
    if "chatwriteforbidden" in low or "not enough rights" in low or "have no rights" in low:
        return "البوت لا يمتلك صلاحية الكتابة في هذه المجموعة. رقّي البوت كمشرف."
    if "chatadminrequired" in low or ("admin" in low and "required" in low):
        return "البوت يحتاج صلاحية مشرف وتثبيت الرسائل."
    if "usernotparticipant" in low:
        return "البوت ليس عضوًا في القناة المطلوبة."
    if "messagenotmodified" in low:
        return "الرسالة لم يتم تعديلها (نفس المحتوى)."
    if "userbanned" in low or "userbannedinchannel" in low:
        return "البوت محظور من قبل تلغرام في هذه المجموعة."
    if "peeridinvalid" in low:
        return "معرّف المجموعة أو المستخدم غير صحيح."
    if "floodwait" in low:
        return "تم تقييد الطلبات مؤقتًا، انتظر قليلًا ثم أعد المحاولة."
    if "button" in low and "invalid" in low:
        return "خطأ في تهيئة أحد الأزرار."
    if "chat not found" in low or "cannot find" in low:
        return "لم يتم العثور على المحادثة المطلوبة."
    if "too many requests" in low:
        return "عدد كبير من الطلبات، انتظر قليلًا."
    if "entity" in low and "bounds" in low:
        return "المعرّف المطلوب غير متاح."
    if "timeout" in low:
        return "انتهت مدة الاتصال بالخادم."
    if "api key" in low or "permission denied" in low or "unauthenticated" in low:
        return "مفتاح الذكاء الاصطناعي غير صالح أو منتهي."
    if "quota" in low or "resource exhausted" in low:
        return "تم استهلاك حصة الذكاء الاصطناعي. انتظر قليلًا ثم أعد المحاولة."
    if "message to delete" in low or "message delete" in low:
        return "تعذر حذف بعض الرسائل."
    if "message not found" in low:
        return "إحدى الرسائل المطلوب حذفها غير موجودة."
    return "حدث خطأ: " + str(err)

def safe_execute(func):
    async def wrapper(event, *args, **kwargs):
        try:
            return await func(event, *args, **kwargs)
        except errors.ChatWriteForbiddenError:
            try:
                await event.reply("البوت لا يمتلك صلاحية الكتابة في هذه المجموعة. رقّي البوت كمشرف.")
            except Exception:
                pass
        except errors.ChatAdminRequiredError:
            try:
                await event.reply("البوت يحتاج صلاحية مشرف وتثبيت الرسائل.")
            except Exception:
                pass
        except errors.UserNotParticipantError:
            try:
                await event.reply("البوت ليس عضوًا في القناة المطلوبة.")
            except Exception:
                pass
        except errors.MessageNotModifiedError:
            pass
        except Exception as e:
            try:
                await event.reply(translate_error(e))
            except Exception:
                pass
    return wrapper

async def _is_member(client, channel_username, user_id):
    try:
        await client(functions.channels.GetParticipantRequest(channel=channel_username, participant=user_id))
        return True
    except errors.UserNotParticipantError:
        return False
    except Exception:
        return False

async def _has_pending_request(client, channel_username, user_id):
    try:
        entity = await client.get_entity(channel_username)
    except Exception:
        return False
    try:
        result = await client(functions.messages.GetChatInviteImportersRequest(
            peer=entity,
            requested=True,
            offset_date=None,
            offset_user=0,
            limit=100,
        ))
        importers = getattr(result, "importers", []) or []
        for imp in importers:
            uid = getattr(imp, "user_id", None)
            if uid == user_id:
                return True
        return False
    except Exception:
        return False

async def check_force_sub(client, user_id):
    from database import get_force_subs
    subs = get_force_subs()
    missing = []
    for sub in subs:
        member = await _is_member(client, sub["username"], user_id)
        if member:
            continue
        if sub["is_request_mode"]:
            pending = await _has_pending_request(client, sub["username"], user_id)
            if pending:
                continue
        missing.append(sub)
    return missing

async def require_subscription(event):
    if event.is_private:
        return True
    missing = await check_force_sub(event.client, event.sender_id)
    if missing:
        buttons = []
        for s in missing:
            uname = s["username"].lstrip("@")
            buttons.append([Button.url("اشترك في " + s["username"], "https://t.me/" + uname)])
        await event.reply("يجب الاشتراك في القنوات التالية أولاً ثم إعادة المحاولة.", buttons=buttons)
        return False
    return True
