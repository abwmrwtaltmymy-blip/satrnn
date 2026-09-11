import re
import json
import random
import asyncio
from telethon import errors, functions, Button
from config import GEMINI_API_KEY
from questions import BAD_WORDS, SAFE_FALLBACK

try:
    from google import genai
    _genai_available = True
except Exception:
    _genai_available = False

_gemini_client = None
_gemini_models_working = []
_gemini_lock = None

def _get_lock():
    global _gemini_lock
    if _gemini_lock is None:
        _gemini_lock = asyncio.Lock()
    return _gemini_lock

def safe_str(v, default=""):
    if v is None:
        return default
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="ignore")
        except Exception:
            return default
    if not isinstance(v, str):
        try:
            return str(v)
        except Exception:
            return default
    return v

def _init_gemini():
    global _gemini_client, _gemini_models_working
    if _gemini_client is not None and _gemini_models_working:
        return True
    if not _genai_available:
        print("google-genai not installed")
        return False
    if not GEMINI_API_KEY or len(GEMINI_API_KEY) < 20:
        print("Gemini key missing")
        return False
    models_to_try = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash-latest",
        "gemini-flash-latest",
        "gemini-2.5-pro",
        "gemini-pro-latest",
    ]
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print("Gemini client failed:", str(e)[:200])
        return False
    working = []
    for model_name in models_to_try:
        try:
            test = client.models.generate_content(
                model=model_name,
                contents="قل مرحبا"
            )
            if test and test.text:
                working.append(model_name)
                print("Gemini OK:", model_name)
        except Exception as e:
            err = str(e)[:150]
            if "404" in err or "NOT_FOUND" in err:
                print("Gemini skip (not available):", model_name)
            elif "503" in err or "UNAVAILABLE" in err:
                print("Gemini busy but usable:", model_name)
                working.append(model_name)
            else:
                print("Gemini failed:", model_name, "->", err)
            continue
    if not working:
        print("No working Gemini model found")
        return False
    _gemini_client = client
    _gemini_models_working = working
    print("Gemini ready with:", working[0], "total:", len(working))
    return True

async def _gemini_generate(prompt, max_retries=3):
    if not _init_gemini():
        return None
    async with _get_lock():
        models = list(_gemini_models_working)
    for model_name in models:
        for attempt in range(max_retries):
            try:
                response = _gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = safe_str(response.text, "").strip()
                if text:
                    return text
            except Exception as e:
                err = str(e)
                if "503" in err or "UNAVAILABLE" in err or "overloaded" in err.lower():
                    wait = (attempt + 1) * 1.5
                    await asyncio.sleep(wait)
                    continue
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    await asyncio.sleep(2)
                    continue
                print("Gemini generate failed:", model_name, "->", err[:150])
                break
    return None

def clean_name(name, default_fallback=SAFE_FALLBACK):
    if name is None:
        return default_fallback
    if isinstance(name, bytes):
        try:
            name = name.decode("utf-8", errors="ignore")
        except Exception:
            return default_fallback
    if not isinstance(name, str):
        try:
            name = str(name)
        except Exception:
            return default_fallback
    name = name.strip()
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
    if isinstance(name, bytes):
        try:
            name = name.decode("utf-8", errors="ignore")
        except Exception:
            return True
    if not isinstance(name, str):
        try:
            name = str(name)
        except Exception:
            return True
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
        if a is None:
            continue
        if isinstance(a, bytes):
            try:
                a = a.decode("utf-8", errors="ignore")
            except Exception:
                continue
        if not isinstance(a, str):
            try:
                a = str(a)
            except Exception:
                continue
        s = a.strip()
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
        return True, "تم قبول " + str(count) + " إجابة مختلفة."
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

def _normalize_ar(s):
    s = safe_str(s, "").strip().lower()
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ة", "ه")
    s = s.replace("ى", "ي")
    s = s.replace("ؤ", "و").replace("ئ", "ي")
    s = s.replace("ء", "")
    s = re.sub(r"[\u064B-\u0652]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    if s.startswith("ال") and len(s) > 3:
        s = s[2:]
    s = s.strip()
    return s

def _levenshtein(a, b):
    if a == b:
        return 0
    if len(a) == 0:
        return len(b)
    if len(b) == 0:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            insert = curr[j - 1] + 1
            delete = prev[j] + 1
            substitute = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(insert, delete, substitute))
        prev = curr
    return prev[-1]

def _allowed_errors(word):
    n = len(word)
    if n <= 3:
        return 0
    if n <= 5:
        return 1
    if n <= 8:
        return 2
    return 3

def _word_match(answer_norm, bank_norm):
    a_words = answer_norm.split()
    b_words = bank_norm.split()
    if len(b_words) == 1 and len(b_words[0]) >= 4:
        for aw in a_words:
            if aw == b_words[0]:
                return True
            allowed = _allowed_errors(b_words[0])
            if allowed > 0 and abs(len(aw) - len(b_words[0])) <= allowed:
                if _levenshtein(aw, b_words[0]) <= allowed:
                    return True
        return False
    if len(a_words) == len(b_words):
        total_diff = 0
        for wa, wb in zip(a_words, b_words):
            allowed_w = _allowed_errors(wb)
            d = _levenshtein(wa, wb)
            if d > allowed_w:
                return False
            total_diff += d
        return total_diff <= 2
    if len(b_words) > 1:
        b_joined = " ".join(b_words)
        for aw in a_words:
            if len(aw) >= 4 and aw in b_joined:
                return True
    return False

def _fuzzy_match(answer_norm, bank_norm):
    if not answer_norm or not bank_norm:
        return False
    if answer_norm == bank_norm:
        return True
    if answer_norm in bank_norm or bank_norm in answer_norm:
        if abs(len(answer_norm) - len(bank_norm)) <= 2:
            return True
    allowed = _allowed_errors(bank_norm)
    if allowed > 0:
        if abs(len(answer_norm) - len(bank_norm)) <= allowed:
            if _levenshtein(answer_norm, bank_norm) <= allowed:
                return True
    if _word_match(answer_norm, bank_norm):
        return True
    return False

def _check_against_bank(question, answers_list):
    try:
        from answers_bank import ANSWERS_BANK
    except Exception:
        return None, None, False
    raw = ANSWERS_BANK.get(question)
    if not raw:
        return None, None, False
    bank = [b.strip() for b in raw.split(",") if b.strip()]
    bank_norm = [_normalize_ar(b) for b in bank]
    correct_answers = []
    unknown_answers = []
    for a in answers_list:
        n = _normalize_ar(a)
        if len(n) < 2:
            continue
        matched = False
        for b in bank_norm:
            if _fuzzy_match(n, b):
                matched = True
                break
        if matched:
            correct_answers.append(a)
        else:
            unknown_answers.append(a)
    return correct_answers, unknown_answers, True

async def _verify_unknown_with_ai(question, unknown_answers):
    if not unknown_answers:
        return {}
    if not _init_gemini():
        return {a: False for a in unknown_answers}
    prompt = (
        "أنت حكم صارم تتحقق من انتماء الإجابات لتصنيف معين.\n\n"
        "التصنيف المطلوب: " + str(question) + "\n\n"
        "الإجابات للفحص:\n"
        + "\n".join("- " + a for a in unknown_answers) + "\n\n"
        "لكل إجابة أجب بصيغة سطر واحد:\n"
        "<الإجابة>|<نعم أو لا>\n\n"
        "قواعد:\n"
        "1. نعم فقط إذا الإجابة تنتمي فعليًا للتصنيف.\n"
        "2. لا لغير ذلك.\n"
        "3. أي شك = لا.\n"
        "4. لا تكتب أي شيء آخر غير الأسطر المطلوبة."
    )
    text = await _gemini_generate(prompt)
    if not text:
        return {a: False for a in unknown_answers}
    result = {}
    for line in text.split("\n"):
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        key = _normalize_ar(parts[0])
        val = parts[1].strip().lower()
        is_yes = val in ("نعم", "yes", "true", "1", "صح", "صحيح")
        for a in unknown_answers:
            if _normalize_ar(a) == key:
                result[a] = is_yes
                break
    for a in unknown_answers:
        if a not in result:
            result[a] = False
    return result

async def evaluate_answers_with_ai(question, expected_count, answers_list):
    if not answers_list:
        return False, "لم يتم إرسال أي إجابة."
    uniq = _normalize_answers(answers_list)
    if not uniq:
        return False, "لم يتم إرسال أي إجابة صالحة."

    correct_answers, unknown_answers, is_bank_question = _check_against_bank(question, uniq)

    if is_bank_question:
        ai_verified_correct = 0
        if unknown_answers:
            verified = await _verify_unknown_with_ai(question, unknown_answers)
            ai_verified_correct = sum(1 for v in verified.values() if v)
        total_correct = len(correct_answers) + ai_verified_correct
        if total_correct >= expected_count:
            return True, "تم قبول " + str(total_correct) + " إجابة صحيحة من أصل " + str(expected_count) + " مطلوبة."
        rejected = [a for a in unknown_answers]
        extra = ""
        if rejected:
            extra = " إجابات مرفوضة: " + ", ".join(rejected[:5])
        return False, "عدد الإجابات الصحيحة " + str(total_correct) + " أقل من المطلوب " + str(expected_count) + "." + extra

    if not _init_gemini():
        return _local_fallback_check(expected_count, uniq)

    prompt = (
        "قيّم إجابات لاعب في تحدي سريع.\n\n"
        "التصنيف: " + str(question) + "\n"
        "العدد المطلوب: " + str(expected_count) + "\n"
        "الإجابات:\n"
        + "\n".join("- " + a for a in uniq) + "\n\n"
        "قواعد صارمة:\n"
        "1. احسب فقط الإجابات المنتمية فعليًا للتصنيف.\n"
        "2. أي إجابة من تصنيف آخر تعتبر خاطئة.\n"
        "3. المكرر بمعنى واحد إجابة واحدة.\n"
        "4. الشك يعتبر خطأ.\n"
        "5. إذا عدد الإجابات الصحيحة أقل من " + str(expected_count) + " فالنتيجة فشل.\n\n"
        "أعد JSON فقط:\n"
        "{\"correct\": true أو false, \"count\": رقم, \"reason\": \"سبب مختصر بالعربية\"}"
    )
    text = await _gemini_generate(prompt)
    if not text:
        return _local_fallback_check(expected_count, uniq)
    data = _parse_gemini_json(text)
    if data is None:
        return _local_fallback_check(expected_count, uniq)
    correct = _to_bool(data.get("correct"))
    count_raw = data.get("count")
    reason = safe_str(data.get("reason"), "")
    try:
        count_int = int(count_raw) if count_raw is not None else len(uniq)
    except Exception:
        count_int = len(uniq)
    if count_int < expected_count:
        correct = False
    if correct:
        return True, "تم قبول " + str(count_int) + " إجابة صحيحة من أصل " + str(expected_count) + ". " + reason
    return False, "عدد الإجابات الصحيحة " + str(count_int) + " أقل من المطلوب " + str(expected_count) + ". " + reason

async def ai_generate_answers(question, target_count, difficulty="medium"):
    question = safe_str(question, "")
    accuracy_map = {"easy": 0.55, "medium": 0.75, "hard": 0.9}
    accuracy = accuracy_map.get(difficulty, 0.75)
    actual_count = max(1, int(round(target_count * (accuracy + random.uniform(-0.15, 0.15)))))
    prompt = (
        "أنت لاعب عربي في تحدي ألعاب.\n"
        "التصنيف: " + question + "\n"
        "اطلب منك ذكر " + str(actual_count) + " إجابة.\n"
        "أعد قائمة عربية بالإجابات فقط، كل إجابة في سطر، بدون ترقيم ولا شرح."
    )
    text = await _gemini_generate(prompt)
    if not text:
        return _local_generate_answers(question, target_count, difficulty)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cleaned = []
    for l in lines:
        l = re.sub(r"^[\-\*\d\.\)\s]+", "", l).strip()
        if l and len(l) < 60:
            cleaned.append(l)
    if not cleaned:
        return _local_generate_answers(question, target_count, difficulty)
    return cleaned[:actual_count]

def _local_generate_answers(question, target_count, difficulty="medium"):
    try:
        from answers_bank import ANSWERS_BANK
        raw = ANSWERS_BANK.get(question)
        if raw:
            base = [b.strip() for b in raw.split(",") if b.strip()]
        else:
            base = []
    except Exception:
        base = []
    if not base:
        base = ["جواب" + str(i) for i in range(1, 15)]
    accuracy_map = {"easy": 0.55, "medium": 0.75, "hard": 0.9}
    accuracy = accuracy_map.get(difficulty, 0.75)
    target = max(1, int(round(target_count * (accuracy + random.uniform(-0.15, 0.15)))))
    random.shuffle(base)
    picked = base[:min(target, len(base))]
    return picked

async def ai_generate_bid(question, difficulty="medium"):
    question = safe_str(question, "")
    ranges = {"easy": (3, 6), "medium": (5, 9), "hard": (8, 14)}
    low, high = ranges.get(difficulty, (5, 9))
    prompt = (
        "أنت لاعب عربي في تحدي ألعاب.\n"
        "السؤال: اذكر أكبر عدد من " + question + ".\n"
        "أعد رقمًا فقط بين " + str(low) + " و " + str(high) + ". لا تكتب أي نص آخر."
    )
    text = await _gemini_generate(prompt)
    if not text:
        return _local_generate_bid(question, difficulty)
    digits = re.findall(r"\d+", text)
    if digits:
        n = int(digits[0])
        return max(1, min(n, 50))
    return _local_generate_bid(question, difficulty)

def _local_generate_bid(question, difficulty="medium"):
    ranges = {"easy": (3, 6), "medium": (5, 9), "hard": (8, 14)}
    low, high = ranges.get(difficulty, (5, 9))
    return random.randint(low, high)

def translate_error(err):
    low = str(err).lower()
    if "chatwriteforbidden" in low or "not enough rights" in low or "have no rights" in low:
        return "البوت لا يمتلك صلاحية الكتابة في هذه المجموعة. رقّي البوت كمشرف."
    if "chatadminrequired" in low or ("admin" in low and "required" in low):
        return "البوت يحتاج صلاحية مشرف وتثبيت الرسائل."
    if "usernotparticipant" in low:
        return "البوت ليس عضوًا في القناة المطلوبة."
    if "messagenotmodified" in low:
        return "الرسالة لم يتم تعديلها."
    if "userbanned" in low or "userbannedinchannel" in low:
        return "البوت محظور من قبل تلغرام في هذه المجموعة."
    if "peeridinvalid" in low:
        return "معرّف المجموعة أو المستخدم غير صحيح."
    if "floodwait" in low:
        return "تم تقييد الطلبات مؤقتًا، انتظر قليلًا."
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
        return "تم استهلاك حصة الذكاء الاصطناعي. انتظر قليلًا."
    if "message to delete" in low or "message delete" in low:
        return "تعذر حذف بعض الرسائل."
    if "message not found" in low:
        return "إحدى الرسائل المطلوب حذفها غير موجودة."
    return "حدث خطأ: " + safe_str(err, "")

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
            uname = safe_str(s["username"], "").lstrip("@")
            buttons.append([Button.url("اشترك في " + safe_str(s["username"], ""), "https://t.me/" + uname)])
        await event.reply("يجب الاشتراك في القنوات التالية أولاً ثم إعادة المحاولة.", buttons=buttons)
        return False
    return True
