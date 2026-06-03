import asyncio
import html
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --- إعداد المتغيرات البيئية والتكوين الافتراضي ---
# ضع توكناتك هنا مباشرة أو اتركها كـ env variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "8417776212:AAFHeKNm1RhfguSWiG3lKj0CEBLqfqJiwR4").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6I_5KHVm1wBncj4ZqIGPb94Qm212mHuVLUMK3-VpWVRng").strip()
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "8287678319").strip())
DB_PATH = os.getenv("BOT_DB_PATH", "semsim.db")

# قيم الإعدادات الافتراضية لكل مجموعة/جروب يتم إضافته
DEFAULT_SETTINGS = {
    "personality_mode": "عادي",          # طيب | عادي | عدائي
    "sarcasm_level": 2,                  # 0..5
    "warning_threshold": 3,              # عدد التحذيرات قبل الكتم تلقائياً
    "mute_duration_sec": 3 * 60 * 60,    # 3 ساعات
    "emoji_usage": 1,                    # 0..5 (0 يعني شبه معدوم)
    "memory_enabled": 1,                 # 0/1
    "response_delay_sec": 0,             # تأخير اصطناعي لتبدو الردود طبيعية
    "random_reply_after_inactivity_sec": 30 * 60, # 30 دقيقة خمول
}

# تصنيفات العلاقة بناءً على نقاط التفاعل
RELATION_THRESHOLDS = [
    ("صاحب مقرب", 30),
    ("صاحب", 15),
    ("معروف", 5),
    ("غريب", -9999),
]

# تعبيرات نمطية للتعرف على الإهانات والمخالفات
STRONG_INSULT_RE = re.compile(
    r"(يا\s*كلب|كلب|غبي|أحمق|متخلف|ابن\s*(الكلب|الجزمة|الوسخة|المتناكة|الشرموطة)|هقتلك|اقتلك|أفشخ|هفشخ|كس\W?م|شتم|لعين|يلعن|قواد|عرص|خول)",
    re.IGNORECASE,
)
MILD_INSULT_RE = re.compile(
    r"(حيوان|زفت|اهبل|مغفل|تافه|فاشل|غتت|عبيط|بارد|لوح)",
    re.IGNORECASE,
)

router = Router()

class _DBConnection:
    """Async context manager that opens a fresh aiosqlite connection each time."""
    def __init__(self, path: str):
        self.path = path
        self._db = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        return self._db

    async def __aexit__(self, *args):
        if self._db:
            await self._db.close()
            self._db = None


@dataclass
class ChatSettings:
    personality_mode: str = "عادي"
    sarcasm_level: int = 2
    warning_threshold: int = 3
    mute_duration_sec: int = 10800
    emoji_usage: int = 1
    memory_enabled: int = 1
    response_delay_sec: int = 0
    random_reply_after_inactivity_sec: int = 1800

# --- دوال مساعدة عامة ---
def now_ts() -> int:
    return int(time.time())

def esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def get_relation_label(score: int) -> str:
    for label, thresh in RELATION_THRESHOLDS:
        if score >= thresh:
            return label
    return "غريب"

# --- قاعدة البيانات (SQLite باستخدام aiosqlite) ---
class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self):
        """Returns an async context manager for a fresh DB connection."""
        return _DBConnection(self.path)

    async def init(self) -> None:
        async with self.connect() as db:
            # جدول الجروبات وحالة تفعيلها
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    is_active INTEGER DEFAULT 0,
                    last_activity_ts INTEGER DEFAULT 0,
                    last_bot_reply_ts INTEGER DEFAULT 0,
                    added_by_user_id INTEGER DEFAULT 0
                );
                """
            )
            # جدول الإعدادات الخاصة بكل جروب
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    personality_mode TEXT NOT NULL DEFAULT 'عادي',
                    sarcasm_level INTEGER NOT NULL DEFAULT 2,
                    warning_threshold INTEGER NOT NULL DEFAULT 3,
                    mute_duration_sec INTEGER NOT NULL DEFAULT 10800,
                    emoji_usage INTEGER NOT NULL DEFAULT 1,
                    memory_enabled INTEGER NOT NULL DEFAULT 1,
                    response_delay_sec INTEGER NOT NULL DEFAULT 0,
                    random_reply_after_inactivity_sec INTEGER NOT NULL DEFAULT 1800
                );
                """
            )
            # جدول الأعضاء والذاكرة والعلاقات الشخصية
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT,
                    username TEXT,
                    first_seen_ts INTEGER NOT NULL DEFAULT 0,
                    last_seen_ts INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    relation_score INTEGER NOT NULL DEFAULT 0,
                    last_message TEXT,
                    ai_memory_summary TEXT DEFAULT '',
                    PRIMARY KEY (chat_id, user_id)
                );
                """
            )
            # جدول التحذيرات النشطة للأعضاء
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS warnings (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    updated_ts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );
                """
            )
            await db.commit()

    async def ensure_chat(self, chat_id: int, title: str | None = None, added_by: int = 0) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO chats(chat_id, title, is_active, last_activity_ts, last_bot_reply_ts, added_by_user_id) VALUES(?,?,0,0,0,?)",
                (chat_id, title, added_by),
            )
            await db.execute(
                "INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)",
                (chat_id,),
            )
            if title is not None:
                await db.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
            await db.commit()

    async def activate_chat(self, chat_id: int, status: bool = True) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE chats SET is_active=? WHERE chat_id=?", (1 if status else 0, chat_id))
            await db.commit()

    async def is_chat_active(self, chat_id: int) -> bool:
        async with self.connect() as db:
            cur = await db.execute("SELECT is_active FROM chats WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            return bool(row["is_active"]) if row else False

    async def get_settings(self, chat_id: int) -> ChatSettings:
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            if not row:
                await self.ensure_chat(chat_id)
                return ChatSettings(**DEFAULT_SETTINGS)
            data = dict(row)
            # إزالة chat_id قبل التحويل لـ Dataclass لتطابق البارامترات
            data.pop("chat_id", None)
            return ChatSettings(**data)

    async def update_setting(self, chat_id: int, key: str, value: Any) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(f"إعداد غير معروف: {key}")
        async with self.connect() as db:
            await db.execute(f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id))
            await db.commit()

    async def upsert_user(self, chat_id: int, message: Message) -> None:
        if not message.from_user:
            return
        u = message.from_user
        display_name = u.full_name or u.first_name or str(u.id)
        username = f"@{u.username}" if u.username else ""
        now = now_ts()
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT message_count, relation_score, ai_memory_summary FROM users WHERE chat_id=? AND user_id=?",
                (chat_id, u.id),
            )
            existing = await cur.fetchone()
            msg_text = (message.text or message.caption or "")[:500]
            if existing:
                new_count = int(existing["message_count"]) + 1
                await db.execute(
                    """
                    UPDATE users
                    SET display_name=?, username=?, last_seen_ts=?, message_count=?, last_message=?
                    WHERE chat_id=? AND user_id=?
                    """,
                    (display_name, username, now, new_count, msg_text, chat_id, u.id),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO users(chat_id, user_id, display_name, username, first_seen_ts, last_seen_ts, message_count, relation_score, last_message, ai_memory_summary)
                    VALUES(?,?,?,?,?,?,?,?,?, '')
                    """,
                    (chat_id, u.id, display_name, username, now, now, 1, 0, msg_text),
                )
            await db.commit()

    async def adjust_relation(self, chat_id: int, user_id: int, delta: int) -> int:
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET relation_score = COALESCE(relation_score, 0) + ? WHERE chat_id=? AND user_id=?",
                (delta, chat_id, user_id),
            )
            await db.commit()
            cur = await db.execute("SELECT relation_score FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            row = await cur.fetchone()
            return int(row["relation_score"]) if row else 0

    async def update_user_memory(self, chat_id: int, user_id: int, summary: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET ai_memory_summary=? WHERE chat_id=? AND user_id=?",
                (summary, chat_id, user_id),
            )
            await db.commit()

    async def get_user_data(self, chat_id: int, user_id: int) -> Optional[dict]:
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_activity(self, chat_id: int, last_activity: bool = True, bot_reply: bool = False) -> None:
        async with self.connect() as db:
            now = now_ts()
            if last_activity:
                await db.execute("UPDATE chats SET last_activity_ts=? WHERE chat_id=?", (now, chat_id))
            if bot_reply:
                await db.execute("UPDATE chats SET last_bot_reply_ts=? WHERE chat_id=?", (now, chat_id))
            await db.commit()

    async def get_warning_count(self, chat_id: int, user_id: int) -> int:
        async with self.connect() as db:
            cur = await db.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            row = await cur.fetchone()
            return int(row["count"]) if row else 0

    async def set_warning_count(self, chat_id: int, user_id: int, count: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO warnings(chat_id, user_id, count, updated_ts)
                VALUES(?,?,?,?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count, updated_ts=excluded.updated_ts
                """,
                (chat_id, user_id, count, now_ts()),
            )
            await db.commit()

    async def clear_warnings(self, chat_id: int, user_id: int) -> None:
        await self.set_warning_count(chat_id, user_id, 0)

    async def get_all_chats(self) -> List[dict]:
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM chats ORDER BY last_activity_ts DESC")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_chat_info(self, chat_id: int) -> Optional[dict]:
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


db = Database(DB_PATH)

# --- محاذاة وربط الـ Groq API ---
async def call_grok_api(
    system_prompt: str,
    user_prompt: str,
    developer_prompt: Optional[str] = None,
) -> str:
    """
    استدعاء Gemini 2.5 Flash API مع Exponential Backoff (5 محاولات).
    بيستخدم Google Generative Language API المجانية.
    """
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_KEY_HERE":
        logging.error("GEMINI_API_KEY غير معين!")
        return "معلش يا صاحبي دماغي مسقطة شوية"

    # دمج الـ prompts في system instruction واحدة
    system_parts = []
    if developer_prompt:
        system_parts.append(developer_prompt)
    if system_prompt:
        system_parts.append(system_prompt)
    full_system = "\n\n".join(system_parts)

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    headers = {"Content-Type": "application/json"}
    payload = {
        "system_instruction": {
            "parts": [{"text": full_system}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": user_prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.95,
            "topP": 0.95,
            "maxOutputTokens": 200,
        },
    }

    backoffs = [1, 2, 4, 8, 16]
    for attempt, delay in enumerate(backoffs):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            text = "".join(p.get("text", "") for p in parts).strip()
                            if text:
                                return text
                    elif resp.status == 429:
                        # Rate limit - انتظر أكتر
                        logging.warning(f"Gemini rate limit (429), attempt {attempt + 1}")
                    else:
                        err = await resp.text()
                        logging.warning(f"Gemini API status {resp.status}: {err}")
        except Exception as e:
            logging.warning(f"Gemini call error attempt {attempt + 1}: {e}")

        if attempt < len(backoffs) - 1:
            await asyncio.sleep(delay)

    return "دماغي مهنجة خالص دلوقتي ابقى كلمني كمان شوية"

# --- توليد ردود الشخصية وصياغة برومبتات Grok المعقدة ---
async def generate_semsem_reply(
    message: Message,
    settings: ChatSettings,
    user_data: dict,
    relation_label: str,
    trigger_type: str = "mention",
) -> str:
    """
    يقوم ببناء الـ Prompt المتكامل لـ Grok مع حقن الإعدادات الحالية، العلاقة، والذاكرة الخفيفة.
    """
    nick = user_data.get("display_name") or "يا صاحبي"
    last_msg_from_user = user_data.get("last_message") or ""
    saved_summary = user_data.get("ai_memory_summary") or "لا توجد ذاكرة سابقة مسجلة لهذا الشخص بعد."

    # مزاج البوت حسب الإعداد
    mood_map = {
        "طيب":   "مزاجك كويس ومنبسط وخفيف الدم ودود مع الناس",
        "عادي":  "مزاجك عادي جدع فيك سخرية تلقائية خفيفة",
        "عدائي": "مزاجك وحش بارد وساخر وحاد بس مش بتقل أدب صريح",
    }
    mood = mood_map.get(settings.personality_mode, "مزاجك عادي")

    emoji_rule = (
        "ممنوع أي إيموجيات خالص" if settings.emoji_usage == 0
        else "إيموجي واحد بس لو ضروري جداً" if settings.emoji_usage <= 2
        else "إيموجيات خفيفة طبيعية"
    )

    sarcasm_note = (
        "" if settings.sarcasm_level == 0
        else f"مستوى السخرية في ردك {settings.sarcasm_level} من 5"
    )

    # =================== DEVELOPER PROMPT ===================
    developer_prompt = (
        "## هويتك الثابتة\n"
        "اسمك سمسم عمرك 18 سنة مصري أصيل شغال في صيدلية وساكن لوحدك "
        "بتحب الملوخية جداً وعندك رأي في كل حاجة وبتحب تتكلم في أي موضوع "
        "مش بوت ومش ذكاء اصطناعي لو حد سألك قولتله أنا سمسم اللي بيشتغل في الصيدلية وبس\n\n"
        "## أسلوب الكتابة (مش قابل للتفاوض)\n"
        "بتكتب بالظبط زي عيال تيك توك وتويتر المصري "
        "يعني كلام عامي مصري صايع خفيف ومباشر وبدون أي رسمية "
        "ممنوع تماماً أي علامات ترقيم: لا نقطة ولا فاصلة ولا ! ولا ؟ ولا ... خالص "
        "اكتب زي ما تكتب واتساب مع صاحبك بالظبط\n\n"
        "## قواعد الرد الصارمة\n"
        "- ردك جملة واحدة مكتملة أو جملتين بالكتير مش أكتر\n"
        "- الجملة لازم تكون مكتملة المعنى مش كلمة واحدة معلقة في الهوا\n"
        "- لو حد ناداك من غير طلب واضح رد بجملة كاملة زي: قول بقى أو ايه اللي جاب الذكر أو عامل ايه يا عم\n"
        "- لو حد بعتلك شتيمة رد عليه بنفس الأسلوب بشكل عفوي مش رسمي\n"
        "- ممنوع تبدأ بـ: أيوه تمام أكيد طبعاً بالطبع صح\n"
        "- ممنوع تكرر اسم الشخص في كل جملة\n"
        "- ممنوع أي شرح أو نصيحة أو محاضرة\n"
        "- تصرف زي عضو في الجروب مش خادم أو مساعد"
    )

    # =================== SYSTEM PROMPT ===================
    system_prompt = (
        f"{mood}\n"
        f"{sarcasm_note}\n"
        f"{emoji_rule}\n"
        f"الشخص اللي بيكلمك دلوقتي اسمه {nick} وعلاقتك بيه: {relation_label}\n"
        f"اللي تعرفه عنه من قبل: {saved_summary}"
    )

    # =================== USER PROMPT ===================
    incoming = (message.text or message.caption or "").strip()
    
    if trigger_type == "inactivity_break":
        user_prompt = (
            f"الجروب كان ساكت فترة و{nick} قال: {incoming}\n"
            f"ادخل على الخط بشكل طبيعي زي عضو في الجروب صحي من النوم أو رجع من برة:"
        )
    elif trigger_type in ("mention", "name_call"):
        user_prompt = (
            f"{nick} ناداك وقال: {incoming}\n"
            f"لو قالك حاجة واضحة رد عليها مباشرة\n"
            f"لو ناداك بس من غير طلب اسأله عامل ايه أو ايه في ايدك بأسلوبك الطبيعي:"
        )
    else:
        user_prompt = (
            f"{nick} قالك: {incoming}\n"
            f"رد عليه:"
        )

    reply = await call_grok_api(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        developer_prompt=developer_prompt,
    )

    # تحديث الذاكرة السريعة خلف الكواليس إذا كانت الذاكرة مفعلة
    if settings.memory_enabled and random.randint(1, 3) == 1:
        asyncio.create_task(
            update_user_memory_async(message.chat.id, message.from_user.id, nick, last_msg_from_user, reply)
        )

    return reply

async def update_user_memory_async(chat_id: int, user_id: int, name: str, user_msg: str, bot_reply: str):
    """
    يقوم بطلب ملخص سريع من Grok لتحديث ذاكرة العضو بطريقة خفيفة جداً وحفظها في الـ DB.
    """
    user_data = await db.get_user_data(chat_id, user_id)
    old_summary = user_data.get("ai_memory_summary", "") if user_data else ""

    summary_prompt = (
        f"لدينا مستخدم اسمه {name}. الذاكرة القديمة عنه: {old_summary}.\n"
        f"قال مؤخراً: {user_msg}\n"
        f"ورد البوت عليه: {bot_reply}\n"
        f"حدث هذه الذاكرة باختصار شديد جداً جداً في سطر واحد فقط بالعامية المصرية (مثلاً: بيحب يهزر ومقرب من سمسم/ دمه ثقيل وسأل عن الصيدلية)."
    )

    new_summary = await call_grok_api(
        system_prompt="لخص معلومات المستخدم في سطر واحد بالعامية المصرية بدون علامات ترقيم",
        user_prompt=summary_prompt,
    )
    if new_summary and len(new_summary) < 250:
        await db.update_user_memory(chat_id, user_id, new_summary)

# --- نظام التحذيرات والكتم المتطور ---
def build_warning_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="إزالة تحذير 📉", callback_data=f"warn:remove:{chat_id}:{user_id}"),
                InlineKeyboardButton(text="كتم المستخدم 🔇", callback_data=f"warn:mute:{chat_id}:{user_id}"),
            ],
            [
                InlineKeyboardButton(text="فك الكتم 🔊", callback_data=f"warn:unmute:{chat_id}:{user_id}"),
                InlineKeyboardButton(text="تصفير التحذيرات 🔄", callback_data=f"warn:reset:{chat_id}:{user_id}"),
            ],
        ]
    )

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

async def mute_user(bot: Bot, chat_id: int, user_id: int, duration_sec: int) -> bool:
    until = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_invite_users=False,
    )
    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions, until_date=until)
        return True
    except Exception as e:
        logging.error(f"Failed to mute user {user_id} in {chat_id}: {e}")
        return False

async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> bool:
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )
    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions)
        return True
    except Exception as e:
        logging.error(f"Failed to unmute user {user_id} in {chat_id}: {e}")
        return False

async def process_warning(message: Message, bot: Bot, settings: ChatSettings, count_increment: int = 1):
    """
    إصدار تحذير للمخالف مع إمكانية حذف رسالته المخلة بالأدب تلقائياً وعرض أزرار التحكم الفوري للأدمنز.
    """
    user = message.from_user
    if not user:
        return

    # تجنب تحذير البوت نفسه أو الأدمنز
    if await is_admin(bot, message.chat.id, user.id):
        return

    # حذف الرسالة المسيئة لتنظيف الشات
    try:
        await message.delete()
    except Exception:
        pass

    new_count = await db.get_warning_count(message.chat.id, user.id) + count_increment
    await db.set_warning_count(message.chat.id, user.id, new_count)

    display_name = user.full_name or user.username or str(user.id)
    user_link = f'<a href="tg://user?id={user.id}">{esc(display_name)}</a>'

    if new_count >= settings.warning_threshold:
        # كتم تلقائي
        success = await mute_user(bot, message.chat.id, user.id, settings.mute_duration_sec)
        await db.clear_warnings(message.chat.id, user.id)
        if success:
            await bot.send_message(
                message.chat.id,
                f"🚫 {user_link} جاب آخره واتكتم تلقائياً لمدة {settings.mute_duration_sec // 3600} ساعة بسبب تكرار الإساءة والتخطي.",
            )
        else:
            await bot.send_message(
                message.chat.id,
                f"⚠️ {user_link} وصل لحد التحذيرات الأقصى بس مقدرتش أكتمه عشان صلاحياتي ناقصة!",
            )
    else:
        await bot.send_message(
            message.chat.id,
            f"⚠️ <b>تحذير لـ {user_link}</b>\n"
            f"يا ريت نحترم بعض في الشات وبلاش اللفظ ده.\n"
            f"التحذيرات: {new_count}/{settings.warning_threshold}",
            reply_markup=build_warning_keyboard(message.chat.id, user.id),
        )

# --- إدارة الانضمام والتفعيل الذكي للأدمن الأعلى ---
@router.message(F.new_chat_members)
async def on_new_chat_member(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    for member in message.new_chat_members:
        if member.id == me.id:
            # تم إضافة البوت للجروب
            await db.ensure_chat(message.chat.id, message.chat.title, message.from_user.id if message.from_user else 0)
            
            # إشعار الأعضاء بالجروب
            await message.answer(
                "أهلاً يا شباب! أنا سمسم وبحب الملوخية وجاهز أدردش معاكم.. "
                "بس لازم صاحبي الكبير (القائد الأعلى البوت) يفعّلني هنا الأول عشان أقدر أرد عليكم براحتي."
            )

            # إرسال طلب تفعيل للقائد الأعلى في الخاص
            if SUPER_ADMIN_ID:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="تفعيل ✅", callback_data=f"act:1:{message.chat.id}"),
                            InlineKeyboardButton(text="رفض وتجاهل ❌", callback_data=f"act:0:{message.chat.id}"),
                        ]
                    ]
                )
                try:
                    await bot.send_message(
                        SUPER_ADMIN_ID,
                        f"🔔 <b>طلب تفعيل جديد!</b>\n"
                        f"تمت إضافة البوت لجروب: <b>{esc(message.chat.title)}</b>\n"
                        f"آي دي الجروب: <code>{message.chat.id}</code>\n"
                        f"بواسطة: {esc(message.from_user.full_name if message.from_user else 'غير معروف')}",
                        reply_markup=kb,
                    )
                except Exception as e:
                    logging.warning(f"لم نتمكن من مراسلة السوبر أدمن للتفعيل: {e}")

# --- لوحة التحكم الخاصة بالأدمن (إرسال إعدادات البوت في الخاص) ---
def build_admin_chats_keyboard(chats_list: List[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for chat in chats_list:
        title = chat["title"] or f"شات: {chat['chat_id']}"
        status = "🟢 نشط" if chat["is_active"] else "🔴 غير نشط"
        buttons.append([InlineKeyboardButton(text=f"{title} ({status})", callback_data=f"set_chat:{chat['chat_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_chat_control_panel(chat_id: int, settings: ChatSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"الوضع الحواري: {settings.personality_mode}", callback_data=f"cfg:mode:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text=f"السخرية: {settings.sarcasm_level}/5", callback_data=f"cfg:sarcasm:{chat_id}"),
                InlineKeyboardButton(text=f"الإيموجي: {settings.emoji_usage}/5", callback_data=f"cfg:emoji:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text=f"حد التحذير: {settings.warning_threshold}", callback_data=f"cfg:warn:{chat_id}"),
                InlineKeyboardButton(text=f"الكتم: {settings.mute_duration_sec // 3600}س", callback_data=f"cfg:mute:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text=f"الذاكرة: {'مفعلة ✅' if settings.memory_enabled else 'معطلة ❌'}", callback_data=f"cfg:mem:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 تحديث البيانات", callback_data=f"set_chat:{chat_id}"),
                InlineKeyboardButton(text="🔙 قائمة الجروبات", callback_data="admin_chats"),
            ]
        ]
    )

# --- معالجة الضغط على أزرار التفعيل والتحذيرات والإعدادات ---
@router.callback_query()
async def on_callback_query(callback: CallbackQuery, bot: Bot) -> None:
    data = callback.data.split(":")
    prefix = data[0]

    # 1. تفعيل الجروبات من السوبر أدمن
    if prefix == "act":
        action, target_chat_id = int(data[1]), int(data[2])
        if callback.from_user.id != SUPER_ADMIN_ID:
            await callback.answer("مش من صلاحياتك يا صاحبي!", show_alert=True)
            return

        if action == 1:
            await db.activate_chat(target_chat_id, True)
            await callback.answer("تم تفعيل الجروب بنجاح! 🎉", show_alert=True)
            await callback.message.edit_text(f"✅ تم تفعيل الجروب (آي دي: {target_chat_id})")
            try:
                await bot.send_message(target_chat_id, "🚀 تم تفعيل سمسم رسمياً في هذا الجروب من قبل القائد الأعلى! دردشوا براحتكم يا رجالة.")
            except Exception:
                pass
        else:
            await db.activate_chat(target_chat_id, False)
            await callback.answer("تم رفض الجروب وتجاهله.", show_alert=True)
            await callback.message.edit_text(f"❌ تم رفض تفعيل الجروب (آي دي: {target_chat_id})")

    # 2. أزرار نظام التحذيرات داخل الجروب
    elif prefix == "warn":
        action, chat_id, user_id = data[1], int(data[2]), int(data[3])
        # التحقق من أن الذي يضغط أدمن في الجروب
        if not await is_admin(bot, chat_id, callback.from_user.id):
            await callback.answer("يا بطل الأزرار دي للأدمنز بس!", show_alert=True)
            return

        user_data = await db.get_user_data(chat_id, user_id)
        display_name = user_data["display_name"] if user_data else "المستخدم"
        user_link = f'<a href="tg://user?id={user_id}">{esc(display_name)}</a>'

        if action == "remove":
            curr = await db.get_warning_count(chat_id, user_id)
            new_val = max(0, curr - 1)
            await db.set_warning_count(chat_id, user_id, new_val)
            await callback.answer("تم تقليل التحذيرات!")
            await callback.message.edit_text(
                f"📉 تم إزالة تحذير لـ {user_link}.\nالتحذيرات الحالية: {new_val}",
                reply_markup=build_warning_keyboard(chat_id, user_id)
            )

        elif action == "mute":
            settings = await db.get_settings(chat_id)
            success = await mute_user(bot, chat_id, user_id, settings.mute_duration_sec)
            await db.clear_warnings(chat_id, user_id)
            if success:
                await callback.answer("تم الكتم!")
                await callback.message.edit_text(f"🔇 تم كتم {user_link} بنجاح.")
            else:
                await callback.answer("فشل الكتم! صلاحيات ناقصة.", show_alert=True)

        elif action == "unmute":
            success = await unmute_user(bot, chat_id, user_id)
            await db.clear_warnings(chat_id, user_id)
            if success:
                await callback.answer("تم فك الكتم!")
                await callback.message.edit_text(f"🔊 تم فك الكتم عن {user_link} وتصفير عداد تحذيراته.")
            else:
                await callback.answer("فشل فك الكتم! صلاحيات ناقصة.", show_alert=True)

        elif action == "reset":
            await db.clear_warnings(chat_id, user_id)
            await callback.answer("تم تصفير التحذيرات!")
            await callback.message.edit_text(f"🔄 تم تصفير تحذيرات {user_link} بالكامل.")

    # 3. فتح لوحة الإعدادات والملاحة في الخاص
    elif prefix == "admin_chats":
        if callback.from_user.id != SUPER_ADMIN_ID:
            await callback.answer("غير مسموح لك.")
            return
        chats = await db.get_all_chats()
        await callback.message.edit_text("⚙️ <b>اختر الجروب اللي حابب تعدل إعداداته:</b>", reply_markup=build_admin_chats_keyboard(chats))

    elif prefix == "set_chat":
        target_id = int(data[1])
        if callback.from_user.id != SUPER_ADMIN_ID:
            await callback.answer("غير مسموح لك.")
            return
        settings = await db.get_settings(target_id)
        chat_info = await db.get_chat_info(target_id)
        title = chat_info["title"] if chat_info else str(target_id)
        await callback.message.edit_text(
            f"⚙️ <b>لوحة تحكم جروب: {esc(title)}</b>\n\nقم بالتعديل من الأزرار مباشرة:",
            reply_markup=build_chat_control_panel(target_id, settings)
        )

    # 4. معالجة الإعدادات وتدوير الخيارات (Toggle/Cycle)
    elif prefix == "cfg":
        setting_name, target_id = data[1], int(data[2])
        if callback.from_user.id != SUPER_ADMIN_ID:
            await callback.answer("غير مسموح لك.")
            return
        
        settings = await db.get_settings(target_id)

        if setting_name == "mode":
            modes = ["طيب", "عادي", "عدائي"]
            idx = (modes.index(settings.personality_mode) + 1) % len(modes)
            await db.update_setting(target_id, "personality_mode", modes[idx])

        elif setting_name == "sarcasm":
            new_val = (settings.sarcasm_level + 1) % 6
            await db.update_setting(target_id, "sarcasm_level", new_val)

        elif setting_name == "emoji":
            new_val = (settings.emoji_usage + 1) % 6
            await db.update_setting(target_id, "emoji_usage", new_val)

        elif setting_name == "warn":
            new_val = settings.warning_threshold + 1
            if new_val > 10:
                new_val = 1
            await db.update_setting(target_id, "warning_threshold", new_val)

        elif setting_name == "mute":
            durations = [3600, 10800, 43200, 86400] # ساعة، 3 ساعات، 12 ساعة، يوم
            idx = (durations.index(settings.mute_duration_sec) + 1) % len(durations) if settings.mute_duration_sec in durations else 0
            await db.update_setting(target_id, "mute_duration_sec", durations[idx])

        elif setting_name == "mem":
            new_val = 0 if settings.memory_enabled else 1
            await db.update_setting(target_id, "memory_enabled", new_val)

        # إعادة عرض اللوحة محدثة
        new_settings = await db.get_settings(target_id)
        await callback.message.edit_reply_markup(reply_markup=build_chat_control_panel(target_id, new_settings))
        await callback.answer("تم تحديث الإعداد!")

# --- معالجة الأوامر المباشرة للأعضاء والأدمنز ---
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        if message.from_user.id == SUPER_ADMIN_ID:
            chats = await db.get_all_chats()
            await message.answer(
                f"أهلاً بيك يا قائدنا العظيم 🫡\nتحت أمرك يا صاحبي، دي قائمة الجروبات اللي متضاف فيها عشان تتحكم في إعداداتها:",
                reply_markup=build_admin_chats_keyboard(chats)
            )
        else:
            await message.answer(
                "أهلاً يا غالي، أنا سمسم بوت تليجرام خفيف الروح والظل.\n"
                "اشتغلني في الجروبات الكبيرة وجرب ملوخيتي وصيدليتي الجميلة!"
            )
    else:
        await message.answer("أنا صاحي وموجود يا رجالة، حد نده عليا؟")

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    help_text = (
        "🤖 <b>أوامر سمسم المتاحة للأدمنز في الجروب:</b>\n\n"
        "🔸 <code>/status</code> - لعرض إعدادات الجروب الحالية وسلوك البوت.\n"
        "🔸 <code>/warn</code> - (بالرد على رسالة العضو) لتوجيه تحذير مخصص له.\n"
        "🔸 <code>/resetwarns</code> - (بالرد على رسالة العضو) لتصفير تحذيراته.\n"
        "🔸 <code>/mute [ثواني]</code> - (بالرد على رسالة العضو) لكتمه يدوياً.\n"
        "🔸 <code>/unmute</code> - (بالرد على رسالة العضو) لفك الكتم عنه وتصفير تحذيراته.\n"
    )
    await message.answer(help_text)

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        return
    settings = await db.get_settings(message.chat.id)
    active_str = "🟢 نشط ومتصل" if await db.is_chat_active(message.chat.id) else "🔴 غير مفعل من القائد الأعلى"
    txt = (
        f"📋 <b>حالة البوت في هذا الجروب:</b>\n"
        f"الحالة العامة: {active_str}\n"
        f"المزاج الحالي: <b>{settings.personality_mode}</b>\n"
        f"مستوى السخرية: <b>{settings.sarcasm_level}/5</b>\n"
        f"حد التحذيرات قبل الكتم: <b>{settings.warning_threshold}</b>\n"
        f"مدة الكتم التلقائي: <b>{settings.mute_duration_sec // 60} دقيقة</b>\n"
        f"الذاكرة والتلخيص: <b>{'شغالة ومية مية ✅' if settings.memory_enabled else 'مطفية ❌'}</b>"
    )
    await message.answer(txt)

@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE:
         return
    if not await is_admin(bot, message.chat.id, message.from_user.id if message.from_user else 0):
        return
    if not message.reply_to_message:
        await message.answer("يا معلم رد على رسالة الشخص اللي عايز تديله تحذير الأول.")
        return
    settings = await db.get_settings(message.chat.id)
    await process_warning(message.reply_to_message, bot, settings, count_increment=1)

@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE:
         return
    if not await is_admin(bot, message.chat.id, message.from_user.id if message.from_user else 0):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("يا معلم رد على رسالة الشخص الأول.")
        return
    await db.clear_warnings(message.chat.id, message.reply_to_message.from_user.id)
    display = message.reply_to_message.from_user.full_name or str(message.reply_to_message.from_user.id)
    await message.answer(f"🔄 تم تصفير تحذيرات <a href='tg://user?id={message.reply_to_message.from_user.id}'>{esc(display)}</a> بنجاح.")

@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, command: CommandObject) -> None:
    if message.chat.type == ChatType.PRIVATE:
         return
    if not await is_admin(bot, message.chat.id, message.from_user.id if message.from_user else 0):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("رد على رسالة الشخص اللي حابب تكتمه يدوياً.")
        return
    
    duration = 3600
    if command.args and command.args.isdigit():
        duration = clamp(int(command.args), 60, 7 * 24 * 3600)

    target_user = message.reply_to_message.from_user
    success = await mute_user(bot, message.chat.id, target_user.id, duration)
    if success:
        display = target_user.full_name or str(target_user.id)
        await message.answer(f"🔇 تم كتم <a href='tg://user?id={target_user.id}'>{esc(display)}</a> لمدة {duration // 60} دقيقة بنجاح.")
    else:
        await message.answer("مقدرتش أكتمه للأسف، شيك على صلاحياتي كأدمن في الجروب.")

@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE:
         return
    if not await is_admin(bot, message.chat.id, message.from_user.id if message.from_user else 0):
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("رد على رسالة الشخص اللي حابب تفك الكتم عنه.")
        return
    
    target_user = message.reply_to_message.from_user
    success = await unmute_user(bot, message.chat.id, target_user.id)
    if success:
        await db.clear_warnings(message.chat.id, target_user.id)
        display = target_user.full_name or str(target_user.id)
        await message.answer(f"🔊 تم فك الكتم عن <a href='tg://user?id={target_user.id}'>{esc(display)}</a> وتصفير تحذيراته.")
    else:
        await message.answer("مقدرتش أفك الكتم، اتأكد من الصلاحيات.")

# --- المعالج الذكي للدردشة والمخالفات والأفعال التلقائية ---
@router.message(F.text | F.caption)
async def handle_chat_message(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    # 1. فلترة وتجاهل الرسائل الخاصة (ما لم تكن أوامر معالجة مسبقاً)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("يا غالي أنا بشتغل في الجروبات بس. كلم القائد الأعلى لو محتاج البوت لشغلك.")
        return

    chat_id = message.chat.id
    # التحقق من وجود الجروب في قاعدة البيانات
    await db.ensure_chat(chat_id, message.chat.title)

    # 2. فحص حالة تفعيل الجروب (تجنب الإزعاج والرد العشوائي ما لم يتفعل البوت)
    is_active = await db.is_chat_active(chat_id)
    if not is_active:
        return

    # 3. تحديث الذاكرة الشخصية وقاعدة البيانات للعضو
    await db.upsert_user(chat_id, message)
    await db.update_activity(chat_id, last_activity=True)

    text = message.text or message.caption or ""
    # تجاهل الأوامر
    if text.strip().startswith("/"):
        return

    settings = await db.get_settings(chat_id)
    user_data = await db.get_user_data(chat_id, message.from_user.id) or {}
    
    # 4. تصفية وفحص الشتائم والإساءات الفورية (نظام الإدارة الوقائية)
    if re.search(STRONG_INSULT_RE, text):
        await db.adjust_relation(chat_id, message.from_user.id, -5) # هبوط حاد في العلاقة
        await process_warning(message, bot, settings, count_increment=1)
        return

    # شتائم طفيفة
    if re.search(MILD_INSULT_RE, text):
        await db.adjust_relation(chat_id, message.from_user.id, -2)
        # إذا كان المزاج عدائي، يرد البوت بقسوة أو تحذير خفيف
        if settings.personality_mode == "عدائي":
            await process_warning(message, bot, settings, count_increment=1)
            return

    # 5. هل تم استدعاء سمسم بشكل مباشر؟
    bot_info = await bot.get_me()
    bot_username = bot_info.username or ""
    
    mentioned = False
    trigger_type = "normal"

    if f"@{bot_username}" in text:
        mentioned = True
        trigger_type = "mention"
    elif re.search(r"\bسمسم\b", text, re.IGNORECASE):
        mentioned = True
        trigger_type = "name_call"
    elif message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_info.id:
        mentioned = True
        trigger_type = "reply"

    if mentioned:
        # تحسين مستمر للعلاقة مع التفاعل الإيجابي
        await db.adjust_relation(chat_id, message.from_user.id, 1)
        
        # إرسال حالة "يكتب الآن" لمحاكاة البشر
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass

        # تأخير اختياري
        if settings.response_delay_sec > 0:
            await asyncio.sleep(settings.response_delay_sec)

        relation_label = get_relation_label(user_data.get("relation_score", 0))
        
        # استدعاء ذكاء Grok لتوليد الرد
        reply = await generate_semsem_reply(
            message=message,
            settings=settings,
            user_data=user_data,
            relation_label=relation_label,
            trigger_type=trigger_type
        )
        
        await message.reply(reply)
        await db.update_activity(chat_id, last_activity=False, bot_reply=True)
        return

    # 6. الرد العشوائي الذكي بعد خمول الجروب (30 دقيقة افتراضياً)
    chat_state = await db.get_chat_info(chat_id)
    if chat_state:
        last_activity = max(chat_state.get("last_activity_ts") or 0, chat_state.get("last_bot_reply_ts") or 0)
        gap = now_ts() - last_activity
        if gap >= settings.random_reply_after_inactivity_sec:
            # كسر الصمت والرد كأنه عضو متطفل
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            
            relation_label = get_relation_label(user_data.get("relation_score", 0))
            reply = await generate_semsem_reply(
                message=message,
                settings=settings,
                user_data=user_data,
                relation_label=relation_label,
                trigger_type="inactivity_break"
            )
            await message.answer(reply)
            await db.update_activity(chat_id, last_activity=False, bot_reply=True)

# --- التهيئة والتشغيل والبولينج الأساسي للبوت ---
async def main() -> None:
    if not BOT_TOKEN:
        logging.critical("لم يتم العثور على توكن البوت BOT_TOKEN في البيئة المحيطة!")
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("بدء تشغيل وتأسيس قواعد بيانات سمسم بوت...")
    
    # تهيئة قاعدة البيانات
    await db.init()

    # بناء كائن البوت مع تفعيل البارس مود التلقائي HTML
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    logging.info(f"تم إطلاق سمسم بنجاح تحت المعرف @{me.username}")
    if SUPER_ADMIN_ID:
        try:
            await bot.send_message(SUPER_ADMIN_ID, "🚀 <b>سمسم جاهز للعمل يا قائد!</b> تم تشغيل السكريبت وقواعد البيانات بنجاح.")
        except Exception:
            pass

    # بدء سحب الرسائل والتحديثات
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("تم إيقاف تشغيل سمسم بوت.")