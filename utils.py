import re
import json
import random
from telethon import errors, functions, Button
from config import GEMINI_API_KEY
from questions import BAD_WORDS, SAFE_FALLBACK

try:
    from google import genai
    from google.genai import types as genai_types
    _genai_available = True
except Exception:
    _genai_available = False

_gemini_client = None

def _init_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return True
    if not _genai_available:
        print("google-genai library not installed")
        return False
    if not GEMINI_API_KEY or len(GEMINI_API_KEY) < 20:
        print("Gemini key missing or too short")
        return False
    try:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini client ready")
        return True
    except Exception as e:
        print("Gemini client failed:", str(e))
        _gemini_client = None
        return False

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

def _parse_gemini_list(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
        if isinstance(data, list):
            return data
        return None
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
        "قواعد صارمة:\n"
        "1. احسب فقط الإجابات التي تنتمي فعليًا لتصنيف السؤال.\n"
        "2. اعتبر المكرر بمعنى واحد إجابة واحدة.\n"
        "3. إذا كان عدد الإجابات الصحيحة والمختلفة أقل من " + str(expected_count) + " فالنتيجة فشل مباشرة.\n"
        "4. لا تتهاون ولا تخترع إجابات صحيحة غير موجودة في القائمة.\n"
        "5. إذا الشك موجود في إجابة، اعتبرها خاطئة.\n\n"
        "أعد النتيجة بصيغة JSON فقط بدون أي نص خارجها:\n"
        "{\"correct\": true أو false, \"count\": رقم, \"reason\": \"سبب مختصر بالعربية\"}"
    )

    try:
        response = await _gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
    except Exception as e:
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

async def ai_generate_answers(question, target_count, difficulty="medium"):
    if not _init_gemini():
        return _local_generate_answers(question, target_count, difficulty)
    accuracy_map = {"easy": 0.55, "medium": 0.75, "hard": 0.9}
    accuracy = accuracy_map.get(difficulty, 0.75)
    actual_count = max(1, int(round(target_count * (accuracy + random.uniform(-0.15, 0.15)))))
    prompt = (
        "أنت لاعب عربي في تحدي ألعاب.\n"
        "التصنيف: " + question + "\n"
        "اطلب منك ذكر " + str(actual_count) + " إجابة.\n"
        "أعد قائمة عربية بالإجابات فقط، كل إجابة في سطر، بدون ترقيم ولا شرح.\n"
        "أعد فقط الإجابات، بلا أي كلام إضافي."
    )
    try:
        response = await _gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        cleaned = []
        for l in lines:
            l = re.sub(r"^[\-\*\d\.\)\s]+", "", l).strip()
            if l and len(l) < 60:
                cleaned.append(l)
        if not cleaned:
            return _local_generate_answers(question, target_count, difficulty)
        return cleaned[:actual_count]
    except Exception:
        return _local_generate_answers(question, target_count, difficulty)

def _local_generate_answers(question, target_count, difficulty="medium"):
    pool = {
        "ألعاب شوتر": ["pubg", "call of duty", "fortnite", "apex legends", "valorant", "cs go", "battlefield", "overwatch", "rainbow six", "halo"],
        "عواصم دول عربية": ["القاهرة", "بغداد", "الرياض", "دمشق", "بيروت", "عمان", "الدوحة", "الكويت", "مسقط", "الخرطوم"],
        "شخصيات أنمي ون بيس": ["لوفي", "زورو", "نامي", "سانجي", "تشوبر", "روبن", "فرانكي", "بروك", "جينبي", "ايس"],
        "لغات برمجة": ["python", "java", "javascript", "c", "c++", "c#", "php", "ruby", "go", "rust"],
        "ماركات سيارات عالمية": ["تويوتا", "هوندا", "نيسان", "فورد", "شيفروليه", "مرسيدس", "بي ام دبليو", "اودي", "بورش", "فولكس واجن"],
        "أندية كرة قدم أوروبية": ["ريال مدريد", "برشلونة", "مانشستر يونايتد", "ليفربول", "بايرن ميونخ", "يوفنتوس", "ميلان", "انتر", "تشيلسي", "ارسنال"],
        "أفلام خيال علمي": ["ماتريكس", "انسبشن", "انترستيلار", "افاتار", "ستار وورز", "ستار تريك", "بليد رانر", "الين", "ديون", "ذي مارشن"],
        "دول تبدأ بحرف الميم": ["مصر", "المغرب", "ماليزيا", "مالي", "مالطا", "موريتانيا", "مكسيك", "منغوليا", "مقدونيا", "مدغشقر"],
        "أعضاء جسم الإنسان": ["قلب", "كبد", "رئة", "كلية", "معدة", "دماغ", "عظم", "جلد", "عين", "اذن"],
        "فواكه لونها أحمر": ["تفاح", "فراولة", "كرز", "رمان", "بطيخ", "طماطم", "توت", "كريب فروت", "راسبيري", "كرانبيري"],
        "أسماء سور القرآن": ["الفاتحة", "البقرة", "آل عمران", "النساء", "المائدة", "الأنعام", "الأعراف", "التوبة", "يونس", "هود"],
        "حيوانات مفترسة": ["اسد", "نمر", "فهد", "ذئب", "دب", "تمساح", "قرش", "نسر", "ضبع", "ثعبان"],
        "شركات تقنية عالمية": ["ابل", "مايكروسوفت", "جوجل", "امازون", "ميتا", "سامسونج", "سوني", "انفيديا", "انتل", "اي ام دي"],
        "كواكب المجموعة الشمسية": ["عطارد", "الزهرة", "الأرض", "المريخ", "المشتري", "زحل", "أورانوس", "نبتون", "بلوتو"],
        "عناصر كيميائية": ["هيدروجين", "أكسجين", "كربون", "نيتروجين", "كالسيوم", "حديد", "صوديوم", "بوتاسيوم", "كلور", "زنك"],
        "ألعاب عالم مفتوح": ["gta", "minecraft", "witcher", "skyrim", "rdr2", "assassin creed", "far cry", "cyberpunk", "zelda", "elden ring"],
        "شخصيات مارفل": ["ايرون مان", "كابتن امريكا", "ثور", "هالك", "سبايدر مان", "بلاك ويدو", "هوك اي", "دكتور سترينج", "بلاك بانثر", "انت مان"],
        "أدوات المطبخ": ["سكين", "ملعقة", "شوكة", "طبق", "كوب", "مقلاة", "قدر", "خلاط", "فرن", "ثلاجة"],
        "أنواع خطوط عربية": ["نسخ", "ثلث", "رقعة", "ديواني", "كوفي", "فارسي", "اندلسي", "مغربي", "حر", "طغراء"],
        "أكلات شعبية عراقية": ["دولمة", "كباب", "تشريب", "مسكوف", "قوزي", "برياني", "قبة", "مرقة باميا", "كبة", "تمن"],
        "دول أفريقية": ["مصر", "نيجيريا", "جنوب افريقيا", "كينيا", "اثيوبيا", "المغرب", "الجزائر", "تونس", "ليبيا", "السودان"],
        "ألوان أساسية": ["احمر", "ازرق", "اصفر", "اخضر", "ابيض", "اسود", "بنفسجي", "برتقالي", "وردي", "بني"],
        "ألعاب باتل رويال": ["pubg", "fortnite", "apex", "warzone", "free fire", "fall guys", "rogue company", "spellbreak", "naraka", "vampire"],
        "رياضات أولمبية": ["جري", "سباحة", "ملاكمة", "مصارعة", "كمال اجسام", "جمباز", "تنس", "كرة سلة", "كرة طائرة", "مبارزة"],
        "أنهار العالم": ["النيل", "الفرات", "دجلة", "الامازون", "المسيسيبي", "اليانغتسي", "الفولغا", "الدانوب", "الراين", "الغانج"],
        "محيطات وبحار": ["الهادي", "الاطلسي", "الهندي", "المتجمد الشمالي", "المتجمد الجنوبي", "الاحمر", "الابيض المتوسط", "الاسود", "العرب", "قزوين"],
        "ماركات هواتف": ["سامسونج", "ابل", "هواوي", "شاومي", "نوكيا", "سوني", "ال جي", "موتورولا", "اوبو", "فيفو"],
        "أبطال League of Legends": ["ياسو", "زود", "غارين", "تيمو", "لي سين", "ريكتون", "كاتلين", "اهري", "جينكس", "فيغار"],
        "شخصيات ناروتو": ["ناروتو", "ساسكي", "ساكورا", "كاكاشي", "ايتاتشي", "غارا", "جيرايا", "اوروتشيمارو", "هيناتا", "نيجي"],
        "أسلحة PUBG": ["m416", "akm", "kar98", "awm", "ump45", "vector", "scar", "s686", "mini14", "dp28"],
        "زهور وورود": ["وردة", "ياسمين", "زنبقة", "توليب", "اوركيد", "اقحوان", "لافندر", "بنفسج", "شقائق", "عباد الشمس"],
        "طرق دفع إلكترونية": ["باي بال", "فيزا", "ماستر كارد", "امريكان اكسبريس", "ابل باي", "جوجل باي", "زين كاش", "كي كارد", "بيتكوين", "اتم"],
        "قنوات يوتيوب شهيرة": ["mrbeast", "pewdiepie", "tseries", "cocomelon", "dude perfect", "kids diana", "like nastya", "markiplier", "jacksepticeye", "dude"],
        "أجزاء الحاسوب": ["معالج", "رام", "قرص صلب", "شاشة", "لوحة مفاتيح", "فأرة", "كرت شاشة", "مزود طاقة", "مروحة", "صندوق"],
        "لغات العالم": ["عربية", "انجليزية", "فرنسية", "اسبانية", "المانية", "ايطالية", "روسية", "صينية", "يابانية", "تركية"],
        "قارات العالم": ["اسيا", "افريقيا", "اوروبا", "امريكا الشمالية", "امريكا الجنوبية", "استراليا", "انتاركتيكا"],
        "طيور لا تطير": ["نعامة", "بطريق", "ايمو", "كيوي", "دودو", "تاكاهي", "كاسواري", "بوكي", "غواق", "انقرض"],
        "أبطال تاريخيين": ["صلاح الدين", "خالد بن الوليد", "طارق بن زياد", "نابليون", "الاسكندر", "يوليوس قيصر", "هانيبال", "جنكيز خان", "عمر المختار", "نيلسون مانديلا"],
        "روايات عالمية": ["البؤساء", "جين اير", "موبي ديك", "الحرب والسلام", "الجريمة والعقاب", "الاخوة كارامازوف", "1984", "مزرعة الحيوان", "الامير الصغير", "دون كيشوت"],
        "وسائل نقل": ["سيارة", "دراجة", "طائرة", "قطار", "حافلة", "سفينة", "مترو", "شاحنة", "هليكوبتر", "صاروخ"],
    }
    base = pool.get(question, [])
    if not base:
        base = ["جواب" + str(i) for i in range(1, 15)]
    accuracy_map = {"easy": 0.55, "medium": 0.75, "hard": 0.9}
    accuracy = accuracy_map.get(difficulty, 0.75)
    target = max(1, int(round(target_count * (accuracy + random.uniform(-0.15, 0.15)))))
    random.shuffle(base)
    picked = base[:min(target, len(base))]
    return picked

async def ai_generate_bid(question, difficulty="medium"):
    if not _init_gemini():
        return _local_generate_bid(question, difficulty)
    ranges = {"easy": (3, 6), "medium": (5, 9), "hard": (8, 14)}
    low, high = ranges.get(difficulty, (5, 9))
    prompt = (
        "أنت لاعب عربي في تحدي ألعاب.\n"
        "السؤال: اذكر أكبر عدد من " + question + ".\n"
        "أعد رقمًا فقط بين " + str(low) + " و " + str(high) + " يمثل ما تستطيع ذكره.\n"
        "لا تكتب أي نص آخر، فقط الرقم."
    )
    try:
        response = await _gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
        digits = re.findall(r"\d+", text)
        if digits:
            n = int(digits[0])
            return max(1, min(n, 50))
        return _local_generate_bid(question, difficulty)
    except Exception:
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
