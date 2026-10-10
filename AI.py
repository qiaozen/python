#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import asyncio
import logging
import sqlite3
import tempfile
import datetime
from collections import defaultdict
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "8932963283:AAGetav1UJrwNsOJp17DTOmB1eGaBGmkhiw"
LLM_API_KEY = "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"
LLM_BASE_URL = "https://api.groq.com/openai/v1"

LLM_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "allam-2-7b",
]

DIFFICULTY_MODELS = {
    "simple":  ["openai/gpt-oss-20b", "allam-2-7b"],
    "medium":  ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
    "complex": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
}

CLASSIFIER_MODEL = "allam-2-7b"

ENABLE_DIFFICULTY_CACHE = True

WHISPER_API_KEY = ""
WHISPER_BASE_URL = "https://api.groq.com/openai/v1"
WHISPER_MODEL = "whisper-large-v3-turbo"

COOLDOWN_SECONDS = 300

SYSTEM_PROMPT = """你是一个聪明智慧的AI助手。
回答要细致化，详情化
可以用emoji，但不要滥用，用户要求的内容尽可能的完成"""

# ============ 身份问答固定回复 ============
# 想改机器人自我介绍，只改这里即可
IDENTITY_REPLY = """我是小小臻AI 
由小小臻搭建的智能助手，会自动判断问题难度并匹配合适的模型来回答。
有什么想问的直接发就行～"""

# 命中任意正则就用固定回复（不区分大小写）
IDENTITY_PATTERNS = [
    r"你(是|叫)(谁|什么|啥)",
    r"你的?名字",
    r"你(用|是)的?什么模型",
    r"什么模型",
    r"who\s*are\s*you",
    r"what('s| is) your name",
]


def is_identity_question(text: str) -> bool:
    """判断是否是在问机器人身份（名字/是谁/什么模型）。"""
    if not text:
        return False
    lower = text.lower().strip()
    return any(re.search(p, lower) for p in IDENTITY_PATTERNS)


# =========================================

RATE_LIMIT = 10
RATE_WINDOW = 60
MAX_HISTORY = 20

DB_PATH = "bot_data.db"

TG_CONNECT_TIMEOUT = 30
TG_READ_TIMEOUT = 30

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

INIT_CREDITS = 20
CHECKIN_CREDITS = 5
COST_SIMPLE = 1
COST_MEDIUM = 3
COST_COMPLEX = 5

ADMIN_ID = 8183948921

PAGE_SIZE = 20

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


LANG_EXT = {
    "python": "py", "py": "py",
    "shell": "sh", "bash": "sh", "sh": "sh",
    "javascript": "js", "js": "js", "node": "js",
    "typescript": "ts", "ts": "ts",
    "java": "java", "kotlin": "kt",
    "c": "c", "cpp": "cpp", "c++": "cpp", "c#": "cs",
    "go": "go", "golang": "go",
    "rust": "rs", "php": "php", "ruby": "rb", "rb": "rb",
    "html": "html", "css": "css", "scss": "scss",
    "sql": "sql", "json": "json",
    "yaml": "yaml", "yml": "yml",
    "markdown": "md", "md": "md",
    "txt": "txt", "文本": "txt", "text": "txt",
    "csv": "csv", "xml": "xml", "toml": "toml",
    "ini": "ini", "conf": "conf", "env": "env",
    "dockerfile": "dockerfile", "makefile": "makefile",
}


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                msg_count   INTEGER DEFAULT 0,
                credits     INTEGER DEFAULT 0,
                last_checkin TEXT,
                banned      INTEGER DEFAULT 0,
                ban_reason  TEXT
            );
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_history_user
                ON history(user_id, id DESC);
            CREATE TABLE IF NOT EXISTS announcements (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
        if "credits" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 0")
        if "last_checkin" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_checkin TEXT")
        if "banned" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        if "ban_reason" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
        if "msg_count" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN msg_count INTEGER DEFAULT 0")

    def get_user(self, user_id):
        return self.conn.execute(
            "SELECT user_id, username, first_name, msg_count, credits, last_checkin, banned, ban_reason "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    def upsert_user(self, user_id, username, first_name):
        existing = self.get_user(user_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO users (user_id, username, first_name, msg_count, credits) "
                "VALUES (?, ?, ?, 1, ?)",
                (user_id, username or "", first_name or "", INIT_CREDITS),
            )
        else:
            self.conn.execute(
                "UPDATE users SET username = ?, first_name = ?, msg_count = msg_count + 1 "
                "WHERE user_id = ?",
                (username or "", first_name or "", user_id),
            )
        self.conn.commit()

    def add_message(self, user_id, role, content):
        self.conn.execute(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        self.conn.commit()

    def get_history(self, user_id, limit=MAX_HISTORY):
        rows = self.conn.execute("""
            SELECT role, content FROM history
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit)).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def clear_history(self, user_id):
        self.conn.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def get_stats(self, user_id):
        row = self.conn.execute(
            "SELECT msg_count, credits, last_checkin FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        total = self.conn.execute(
            "SELECT COUNT(*) FROM history WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        return {
            "msg_count": row[0] if row else 0,
            "credits": row[1] if row else 0,
            "last_checkin": row[2] if row else None,
            "history": total,
        }

    def get_global_stats(self):
        total = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE banned = 1"
        ).fetchone()[0]
        total_credits = self.conn.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM users"
        ).fetchone()[0]
        total_msgs = self.conn.execute(
            "SELECT COALESCE(SUM(msg_count), 0) FROM users"
        ).fetchone()[0]
        return {
            "total_users": total,
            "banned_users": banned,
            "total_credits": total_credits,
            "total_msgs": total_msgs,
        }

    def get_credits(self, user_id):
        row = self.conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0

    def add_credits(self, user_id, amount):
        self.conn.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (amount, user_id),
        )
        self.conn.commit()

    def spend_credits(self, user_id, amount):
        current = self.get_credits(user_id)
        if current < amount:
            return False
        self.conn.execute(
            "UPDATE users SET credits = credits - ? WHERE user_id = ?",
            (amount, user_id),
        )
        self.conn.commit()
        return True

    def try_checkin(self, user_id):
        today = datetime.date.today().isoformat()
        row = self.conn.execute(
            "SELECT last_checkin FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        last = row[0] if row else None
        if last == today:
            return False, today
        self.conn.execute(
            "UPDATE users SET last_checkin = ?, credits = credits + ? WHERE user_id = ?",
            (today, CHECKIN_CREDITS, user_id),
        )
        self.conn.commit()
        return True, today

    def set_banned(self, user_id, banned, reason=None):
        self.conn.execute(
            "UPDATE users SET banned = ?, ban_reason = ? WHERE user_id = ?",
            (1 if banned else 0, reason if banned else None, user_id),
        )
        self.conn.commit()

    def is_banned(self, user_id):
        row = self.conn.execute(
            "SELECT banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return bool(row[0]) if row else False

    def get_ban_reason(self, user_id):
        row = self.conn.execute(
            "SELECT ban_reason FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def list_banned(self):
        return self.conn.execute(
            "SELECT user_id, username, first_name, ban_reason "
            "FROM users WHERE banned = 1"
        ).fetchall()

    def list_users(self, offset=0, limit=20):
        return self.conn.execute(
            "SELECT user_id, username, first_name, banned, credits "
            "FROM users ORDER BY user_id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def count_users(self):
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ---------- 公告 ----------
    def add_announcement(self, content):
        self.conn.execute(
            "INSERT INTO announcements (content) VALUES (?)", (content,)
        )
        self.conn.commit()

    def list_all_user_ids(self):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT user_id FROM users WHERE banned = 0"
            ).fetchall()
        ]


db = Database(DB_PATH)


class RateLimiter:
    def __init__(self, max_calls, window):
        self.max_calls = max_calls
        self.window = window
        self.buckets = defaultdict(list)

    def check(self, user_id):
        now = time.time()
        bucket = self.buckets[user_id]
        bucket[:] = [t for t in bucket if now - t < self.window]
        if len(bucket) >= self.max_calls:
            wait = int(self.window - (now - bucket[0])) + 1
            return False, wait
        bucket.append(now)
        return True, 0


limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW)


class LLMClient:
    def __init__(self, base_url, api_key, models):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.models = models
        self.current_idx = 0
        self.session = None
        self.cooldown = {}

    @property
    def current_model(self):
        return self.models[self.current_idx]

    def _find_available(self, candidates, tried):
        now = time.time()
        for model in candidates:
            if model in tried:
                continue
            if self.cooldown.get(model, 0) < now:
                return model
        return None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=90, connect=15)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def _try_model(self, model, messages, temperature, max_tokens):
        session = await self._get_session()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
        }

        try:
            async with session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return True, data["choices"][0]["message"]["content"].strip(), False

                if resp.status == 401:
                    return False, "❌ Groq API Key 无效。", False

                if resp.status == 403:
                    return False, "🚫 Cloudflare 拦截，换梯子节点。", False

                if resp.status == 404:
                    logger.warning(f"⚠️ 模型 {model} 不存在，跳过")
                    return False, None, True

                if resp.status == 429:
                    logger.warning(f"⏳ 模型 {model} 额度限制，切换")
                    self.cooldown[model] = time.time() + COOLDOWN_SECONDS
                    return False, None, True

                text = await resp.text()
                logger.error(f"LLM {model} API {resp.status}: {text[:200]}")
                return False, None, True

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.warning(f"模型 {model} 网络错误: {e}")
            return False, None, True
        except Exception as e:
            logger.exception(f"模型 {model} 调用异常")
            return False, None, True

    async def chat(self, messages, temperature=0.7, max_tokens=4096,
                   candidates=None, tag=""):
        pool = candidates or self.models
        tried = set()
        failed = []

        for _ in range(len(pool)):
            model = self._find_available(pool, tried)
            if model is None:
                break
            tried.add(model)

            ok, reply, should_continue = await self._try_model(
                model, messages, temperature, max_tokens
            )
            if ok:
                if model in self.models:
                    self.current_idx = self.models.index(model)
                logger.info(f"✅ 使用模型: {model} {tag}")
                return reply
            if reply:
                return reply
            failed.append(model)

        return f"⚠️ 所有候选模型都不可用（{', '.join(failed)}），请稍后再试。"

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


llm = LLMClient(LLM_BASE_URL, LLM_API_KEY, LLM_MODELS)


SIMPLE_HINTS = [
    "你好", "hi", "hello", "在吗", "谢谢", "thanks",
    "几点", "今天", "天气", "翻译",
]
COMPLEX_HINTS = [
    "架构", "设计", "重构", "优化", "算法", "证明", "推导",
    "详细", "深入", "分析", "对比", "方案", "实现一个",
    "写一个完整", "debug", "报错", "异常", "性能",
    "architecture", "design", "algorithm", "optimize", "refactor",
]

_difficulty_cache: dict = {}


def quick_difficulty(text: str) -> Optional[str]:
    if not text:
        return "simple"

    lower = text.lower()
    length = len(text)

    if length <= 20:
        if any(h in lower for h in COMPLEX_HINTS):
            return "complex"
        return "simple"

    if any(h in lower for h in COMPLEX_HINTS):
        return "complex"

    if length <= 15 and any(h in lower for h in SIMPLE_HINTS):
        return "simple"

    if length > 300:
        return "complex"

    if length > 80:
        return "medium"

    return None


async def classify_difficulty(text: str) -> str:
    quick = quick_difficulty(text)
    if quick:
        logger.info(f"🧠 难度判断（规则）: {quick}")
        return quick

    cache_key = text[:60]
    if ENABLE_DIFFICULTY_CACHE and cache_key in _difficulty_cache:
        logger.info(f"🧠 难度判断（缓存）: {_difficulty_cache[cache_key]}")
        return _difficulty_cache[cache_key]

    logger.info(f"🧠 调用 {CLASSIFIER_MODEL} 判断难度...")
    prompt = (
        "判断下面用户问题的难度，只返回一个单词：\n"
        "simple（简单闲聊、常识、短问题）\n"
        "medium（需要一定解释、中等长度问题）\n"
        "complex（需要深入分析、代码、架构、多步推理）\n\n"
        "只返回 simple / medium / complex 中的一个，不要其他文字。\n\n"
        f"问题：{text}"
    )

    try:
        session = await llm._get_session()
        payload = {
            "model": CLASSIFIER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 8,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
        }
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions", json=payload, headers=headers
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"].strip().lower()
                for level in ("simple", "medium", "complex"):
                    if level in raw:
                        if ENABLE_DIFFICULTY_CACHE:
                            _difficulty_cache[cache_key] = level
                        logger.info(f"🧠 难度判断（模型）: {level}")
                        return level
    except Exception as e:
        logger.warning(f"难度判断失败，默认 medium: {e}")

    return "medium"


DIFFICULTY_COST = {
    "simple": COST_SIMPLE,
    "medium": COST_MEDIUM,
    "complex": COST_COMPLEX,
}


FILE_WORDS = ["文件", "脚本", "file", "script"]
ACTION_WORDS = ["生成", "写", "创建", "做", "给我", "来一个", "来个", "帮我", "写个", "整个"]
SUSPECT_WORDS = [
    "代码", "程序", "函数", "类", "模块", "工具", "网页", "页面",
    "爬虫", "接口", "api", "bot", "机器人", "游戏", "小程序",
    "code", "program", "function", "class", "module", "tool",
    "page", "web", "crawler", "game", "app",
]


def detect_file_intent(text: str):
    if not text:
        return False, None, None, False

    lower = text.lower()
    has_file = any(w in lower for w in FILE_WORDS) or any(w in text for w in FILE_WORDS)
    has_action = any(w in text for w in ACTION_WORDS)
    has_suspect = any(w in lower for w in SUSPECT_WORDS)

    if has_file:
        for lang, ext in LANG_EXT.items():
            if lang in lower:
                return True, ext, lang, False
        return True, "txt", "txt", False

    if has_action and has_suspect:
        return False, None, None, True

    return False, None, None, False


async def smart_detect(text: str):
    prompt = (
        "判断下面用户消息是否要求生成一个可下载的文件（如代码文件、文本文件）。\n"
        "如果是，只返回 JSON：{\"file\": true, \"ext\": \"py\", \"lang\": \"python\"}\n"
        "如果不是，只返回：{\"file\": false}\n"
        "常见 ext：py sh js ts java c cpp go rs php rb html css sql json yaml md txt csv xml\n"
        "只返回 JSON，不要任何其他文字。\n\n"
        f"用户消息：{text}"
    )
    try:
        r = await llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100,
            candidates=[CLASSIFIER_MODEL, "openai/gpt-oss-20b"],
            tag="[检测]",
        )
        r = r.strip().strip("`").replace("json\n", "").strip()
        m = re.search(r"\{.*\}", r, re.DOTALL)
        if not m:
            return False, None, None
        data = json.loads(m.group(0))
        if data.get("file"):
            return True, data.get("ext", "txt"), data.get("lang", "text")
    except Exception as e:
        logger.warning(f"smart_detect 失败: {e}")
    return False, None, None


def extract_code_block(text: str, ext: str) -> str:
    pattern = r"```[a-zA-Z0-9_+\-#\.]*\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        return matches[0].strip()
    return text.strip()


async def generate_file(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        user_text: str, ext: str, lang: str):
    user = update.effective_user

    if not db.spend_credits(user.id, COST_COMPLEX):
        await safe_reply(
            update,
            f"💸 积分不足（生成文件需 {COST_COMPLEX} 分，当前 {db.get_credits(user.id)} 分）\n"
            f"发送 /checkin 每日签到领 {CHECKIN_CREDITS} 积分。",
        )
        return

    system_hint = (
        f"用户要求生成一个 .{ext} 文件（语言：{lang}）。\n"
        f"请只输出一个代码块（用 ``` 包裹），不要任何解释文字。\n"
        f"代码块内容会被原样保存为文件，所以要保证完整、可直接运行、无省略。"
    )

    history = db.get_history(user.id, MAX_HISTORY)
    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + system_hint}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    reply = await llm.chat(
        messages,
        max_tokens=4096,
        candidates=DIFFICULTY_MODELS["complex"],
        tag="[文件]",
    )

    db.add_message(user.id, "user", user_text)
    db.add_message(user.id, "assistant", reply)

    code = extract_code_block(reply, ext)
    if not code:
        db.add_credits(user.id, COST_COMPLEX)
        await safe_reply(update, f"⚠️ AI 没按格式输出，已退回积分：\n\n{reply}")
        return

    ts = int(time.time())
    filename = f"ai_{ts}.{ext}"
    filepath = os.path.join(tempfile.gettempdir(), filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)

    try:
        with open(filepath, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=(
                    f"📄 `{filename}` · {len(code)} 字符 · {lang} · "
                    f"模型: {llm.current_model} · 剩余 {db.get_credits(user.id)} 分"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.exception("发文件失败")
        await safe_reply(update, f"❌ 发文件失败: {e}")
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass


async def transcribe_voice(file_path: str) -> str:
    if not WHISPER_API_KEY:
        return ""
    session = aiohttp.ClientSession()
    try:
        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename="audio.ogg")
            form.add_field("model", WHISPER_MODEL)
            async with session.post(
                f"{WHISPER_BASE_URL}/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {WHISPER_API_KEY}",
                    "User-Agent": BROWSER_UA,
                },
                data=form,
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                return data.get("text", "").strip()
    except Exception as e:
        logger.error(f"语音转写失败: {e}")
        return ""
    finally:
        await session.close()


async def ask_llm(user_id, user_text):
    difficulty = await classify_difficulty(user_text)
    cost = DIFFICULTY_COST.get(difficulty, COST_MEDIUM)

    if not db.spend_credits(user_id, cost):
        return (
            f"💸 积分不足（本次需 {cost} 分，当前 {db.get_credits(user_id)} 分）\n"
            f"发送 /checkin 每日签到领 {CHECKIN_CREDITS} 积分。"
        )

    candidates = DIFFICULTY_MODELS.get(difficulty, LLM_MODELS)
    logger.info(f"🧠 难度={difficulty} → 扣 {cost} 分 → 候选模型={candidates}")

    history = db.get_history(user_id, MAX_HISTORY)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    reply = await llm.chat(messages, candidates=candidates, tag="[回答]")

    if reply.startswith("⚠️ 所有候选模型") and candidates != LLM_MODELS:
        logger.warning("低档模型全部失败，升级到完整模型池重试")
        reply = await llm.chat(messages, candidates=LLM_MODELS, tag="[回答-升级]")

    if reply.startswith("⚠️ 所有候选模型"):
        db.add_credits(user_id, cost)
        return reply + f"\n（已退回 {cost} 积分）"

    db.add_message(user_id, "user", user_text)
    db.add_message(user_id, "assistant", reply)

    remaining = db.get_credits(user_id)
    return f"{reply}\n\n— 本次消耗 {cost} 分 · 剩余 {remaining} 分"


async def safe_reply(update: Update, text: str, **kwargs):
    for attempt in range(3):
        try:
            await update.message.reply_text(text, **kwargs)
            return
        except Exception as e:
            logger.warning(f"回复失败 (第{attempt+1}次): {e}")
            if attempt < 2:
                await asyncio.sleep(1.5 ** attempt)


async def try_typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
    except Exception:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = db.get_user(user.id) is None
    db.upsert_user(user.id, user.username, user.first_name)
    credits = db.get_credits(user.id)
    welcome = f"🎁 新用户注册礼包 +{INIT_CREDITS} 积分已到账！\n\n" if is_new else ""
    text = (
        f"👋 你好，{user.first_name}\n\n"
        f"{welcome}"
        "我是你的 AI 小助手 *小小臻AI*\n"
        "自动判断难度，匹配合适模型\n\n"
        f"💰 当前积分：*{credits}*\n\n"
        "💬 *普通聊天*: 直接发消息\n"
        "📄 *生成文件*: \n"
        "   • 给我生成py文件\n"
        "   • 写一个shell脚本\n"
        "   • 创建一个html界面\n\n"
        "🎁 /checkin 每日签到领积分\n"
        "👤 /me 查看账户信息\n"
        "🧹 /clear 清空对话记忆"
    )
    await safe_reply(update, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_history(update.effective_user.id)
    await safe_reply(update, "🧹 对话记忆已清空。")


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    stats = db.get_stats(user.id)
    last = stats["last_checkin"] or "从未签到"
    text = (
        f"👤 *账户信息*\n\n"
        f"昵称: {user.first_name}\n"
        f"用户名: @{user.username if user.username else '（无）'}\n"
        f"ID: `{user.id}`\n\n"
        f"💰 积分: *{stats['credits']}*\n"
        f"💬 消息数: {stats['msg_count']}\n"
        f"📝 对话记录: {stats['history']}\n"
        f"📅 上次签到: {last}\n\n"
        f"*消耗规则*\n"
        f"🟢 简单 {COST_SIMPLE} 分\n"
        f"🟡 中等 {COST_MEDIUM} 分\n"
        f"🔴 复杂 {COST_COMPLEX} 分\n"
        f"📄 生成文件 {COST_COMPLEX} 分\n\n"
        f"🎁 每日签到 +{CHECKIN_CREDITS} 分"
    )
    await safe_reply(update, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    ok, today = db.try_checkin(user.id)
    credits = db.get_credits(user.id)
    if ok:
        text = (
            f"✅ *签到成功！*\n\n"
            f"📅 日期: {today}\n"
            f"🎁 获得: +{CHECKIN_CREDITS} 积分\n"
            f"💰 当前积分: *{credits}*"
        )
    else:
        text = (
            f"📅 今天（{today}）已经签过啦，明天再来！\n\n"
            f"💰 当前积分: *{credits}*"
        )
    await safe_reply(update, text, parse_mode=ParseMode.MARKDOWN)


ADMIN_PENDING = {}


def admin_panel_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ 加分", callback_data="adm:add"),
            InlineKeyboardButton("➖ 减分", callback_data="adm:sub"),
        ],
        [
            InlineKeyboardButton("🚫 封禁", callback_data="adm:ban"),
            InlineKeyboardButton("✅ 解封", callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton("📋 封禁列表", callback_data="adm:banlist"),
            InlineKeyboardButton("📊 统计", callback_data="adm:stats"),
        ],
        [
            InlineKeyboardButton("👥 用户统计", callback_data="adm:count"),
            InlineKeyboardButton("📢 发布公告", callback_data="adm:announce"),
        ],
    ])


def admin_back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主界面", callback_data="adm:home")],
    ])


def admin_panel_text():
    g = db.get_global_stats()
    return (
        "🛠 *管理员面板*\n\n"
        f"👥 用户总数: *{g['total_users']}*\n"
        f"🚫 已封禁: *{g['banned_users']}*\n"
        f"💰 积分总量: *{g['total_credits']}*\n"
        f"💬 消息总量: *{g['total_msgs']}*\n\n"
        "请选择操作："
    )


def render_user_page(page=0):
    total = db.count_users()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    offset = page * PAGE_SIZE
    rows = db.list_users(offset=offset, limit=PAGE_SIZE)

    lines = [f"👥 *用户统计*（共 {total} 人）\n"]
    if not rows:
        lines.append("📭 暂无用户。")
    else:
        for uid, uname, fname, banned, credits in rows:
            name = fname or uname or "未知"
            if uname:
                name += f"（@{uname}）"
            status = "🚫 已封禁" if banned else "✅ 正常"
            lines.append(f"• *{name}*\n  ID: `{uid}`\n  状态: {status} | 💰 {credits} 分")

    lines.append(f"\n第 {page + 1} / {total_pages} 页")
    return "\n".join(lines), page, total_pages


def user_page_kb(page, total_pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"adm:userlist:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="adm:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"adm:userlist:{page+1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 返回主界面", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


async def show_user_list(query, page=0):
    text, page, total_pages = render_user_page(page)
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=user_page_kb(page, total_pages),
    )


async def cmd_gl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await safe_reply(update, "🚫 你没有权限使用此指令。")
        return
    await safe_reply(
        update,
        admin_panel_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_panel_kb(),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await safe_reply(update, "🚫 你没有权限使用此指令。")
        return

    stats = db.get_stats(user.id)

    now = time.time()
    cooldown_info = []
    for m in LLM_MODELS:
        cd = llm.cooldown.get(m, 0)
        if cd > now:
            cooldown_info.append(f"`{m}`: 冷却 {int(cd - now)}s")
        else:
            cooldown_info.append(f"`{m}`: ✅ 可用")

    diff_info = []
    for level, models in DIFFICULTY_MODELS.items():
        emoji = {"simple": "🟢", "medium": "🟡", "complex": "🔴"}.get(level, "⚪")
        cost = DIFFICULTY_COST.get(level, COST_MEDIUM)
        diff_info.append(f"{emoji} *{level}*（{cost}分）: `{' → '.join(models)}`")

    text = (
        f"📊 *统计*\n"
        f"消息数: {stats['msg_count']}\n"
        f"对话记录: {stats['history']}\n"
        f"💰 积分: *{stats['credits']}*\n"
        f"当前模型: `{llm.current_model}`\n\n"
        f"*难度 → 模型映射*\n" + "\n".join(diff_info) + "\n\n"
        f"*模型池状态*\n" + "\n".join(cooldown_info)
    )
    await safe_reply(update, text, parse_mode=ParseMode.MARKDOWN)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    if user.id != ADMIN_ID:
        await query.edit_message_text("🚫 你没有权限使用此面板。")
        return

    data = query.data

    if data == "adm:noop":
        return

    if data == "adm:home":
        ADMIN_PENDING.pop(user.id, None)
        await query.edit_message_text(
            admin_panel_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_panel_kb(),
        )
        return

    if data.startswith("adm:userlist:"):
        try:
            page = int(data.split(":")[-1])
        except ValueError:
            page = 0
        await show_user_list(query, page)
        return

    if data == "adm:add":
        ADMIN_PENDING[user.id] = "add"
        await query.edit_message_text(
            "➕ *加分*\n请发送：`用户ID 积分数`\n例如：`123456789 10`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
    elif data == "adm:sub":
        ADMIN_PENDING[user.id] = "sub"
        await query.edit_message_text(
            "➖ *减分*\n请发送：`用户ID 积分数`\n例如：`123456789 5`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
    elif data == "adm:ban":
        ADMIN_PENDING[user.id] = "ban"
        await query.edit_message_text(
            "🚫 *封禁*\n请发送：`用户ID 原因`\n例如：`123456789 恶意刷分`\n（原因可省略，默认「未说明」）",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
    elif data == "adm:unban":
        ADMIN_PENDING[user.id] = "unban"
        await query.edit_message_text(
            "✅ *解封*\n请发送：`用户ID`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
    elif data == "adm:banlist":
        rows = db.list_banned()
        if not rows:
            text = "📭 当前没有封禁用户。"
        else:
            lines = ["🚫 *封禁列表*\n"]
            for uid, uname, fname, reason in rows:
                name = fname or uname or "未知"
                reason = reason or "未说明"
                lines.append(f"• `{uid}` — {name}\n  原因：{reason}")
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_back_kb()
        )
    elif data == "adm:stats":
        stats = db.get_stats(user.id)
        now = time.time()
        cooldown_info = []
        for m in LLM_MODELS:
            cd = llm.cooldown.get(m, 0)
            if cd > now:
                cooldown_info.append(f"`{m}`: 冷却 {int(cd - now)}s")
            else:
                cooldown_info.append(f"`{m}`: ✅ 可用")
        diff_info = []
        for level, models in DIFFICULTY_MODELS.items():
            emoji = {"simple": "🟢", "medium": "🟡", "complex": "🔴"}.get(level, "⚪")
            cost = DIFFICULTY_COST.get(level, COST_MEDIUM)
            diff_info.append(f"{emoji} *{level}*（{cost}分）: `{' → '.join(models)}`")
        text = (
            f"📊 *系统统计*\n"
            f"我的消息数: {stats['msg_count']}\n"
            f"我的对话记录: {stats['history']}\n"
            f"我的积分: *{stats['credits']}*\n"
            f"当前模型: `{llm.current_model}`\n\n"
            f"*难度 → 模型映射*\n" + "\n".join(diff_info) + "\n\n"
            f"*模型池状态*\n" + "\n".join(cooldown_info)
        )
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_back_kb()
        )
    elif data == "adm:count":
        await show_user_list(query, page=0)
    elif data == "adm:announce":
        ADMIN_PENDING[user.id] = "announce"
        await query.edit_message_text(
            "📢 *发布公告*\n\n"
            "直接发送公告内容，会立刻推送给所有用户。\n"
            "支持 Markdown（*粗体* _斜体_ `代码`）。",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )


async def admin_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id != ADMIN_ID:
        return False
    action = ADMIN_PENDING.get(user.id)
    if not action:
        return False

    text = (update.message.text or "").strip()
    parts = text.split()

    if action == "announce":
        ADMIN_PENDING.pop(user.id, None)
        if not text:
            await safe_reply(update, "❌ 公告内容不能为空。")
            return True

        db.add_announcement(text)

        user_ids = db.list_all_user_ids()
        broadcast = f"📢 *公告*\n\n{text}"

        success, fail = 0, 0
        for uid in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=broadcast,
                    parse_mode=ParseMode.MARKDOWN,
                )
                success += 1
            except Exception as e:
                logger.warning(f"推送 {uid} 失败: {e}")
                fail += 1
            await asyncio.sleep(0.05)  # 约 20 条/秒，防限流

        await safe_reply(
            update,
            f"✅ 公告已推送\n📤 成功 {success} | ❌ 失败 {fail}",
            reply_markup=admin_back_kb(),
        )
        return True

    ADMIN_PENDING.pop(user.id, None)

    if action in ("add", "sub"):
        if len(parts) != 2:
            await safe_reply(update, "❌ 格式不对，应为：`用户ID 积分数`", parse_mode=ParseMode.MARKDOWN)
            return True
        try:
            target = int(parts[0])
            amount = int(parts[1])
        except ValueError:
            await safe_reply(update, "❌ 用户ID和积分数必须是数字。")
            return True
        if amount <= 0:
            await safe_reply(update, "❌ 积分数必须大于 0。")
            return True
        if db.get_user(target) is None:
            await safe_reply(update, f"❌ 用户 `{target}` 不存在。", parse_mode=ParseMode.MARKDOWN)
            return True
        if action == "add":
            db.add_credits(target, amount)
            await safe_reply(
                update,
                f"✅ 已给 `{target}` 加 *{amount}* 分\n💰 当前积分：*{db.get_credits(target)}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_back_kb(),
            )
        else:
            db.add_credits(target, -amount)
            await safe_reply(
                update,
                f"✅ 已给 `{target}` 扣 *{amount}* 分\n💰 当前积分：*{db.get_credits(target)}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_back_kb(),
            )
        return True

    if action == "ban":
        if len(parts) < 1:
            await safe_reply(update, "❌ 格式不对，应为：`用户ID 原因`", parse_mode=ParseMode.MARKDOWN)
            return True
        try:
            target = int(parts[0])
        except ValueError:
            await safe_reply(update, "❌ 用户ID必须是数字。")
            return True
        reason = " ".join(parts[1:]).strip() or "未说明"
        if db.get_user(target) is None:
            await safe_reply(update, f"❌ 用户 `{target}` 不存在。", parse_mode=ParseMode.MARKDOWN)
            return True
        if target == ADMIN_ID:
            await safe_reply(update, "😅 不能封禁管理员自己。")
            return True
        db.set_banned(target, True, reason)
        await safe_reply(
            update,
            f"🚫 已封禁 `{target}`\n原因：{reason}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
        return True

    if action == "unban":
        if len(parts) != 1:
            await safe_reply(update, "❌ 格式不对，应为：`用户ID`", parse_mode=ParseMode.MARKDOWN)
            return True
        try:
            target = int(parts[0])
        except ValueError:
            await safe_reply(update, "❌ 用户ID必须是数字。")
            return True
        if db.get_user(target) is None:
            await safe_reply(update, f"❌ 用户 `{target}` 不存在。", parse_mode=ParseMode.MARKDOWN)
            return True
        db.set_banned(target, False)
        await safe_reply(
            update,
            f"✅ 已解封 `{target}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_back_kb(),
        )
        return True

    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_text = update.message.text.strip()
    if not user_text:
        return

    if await admin_pending_text(update, context):
        return

    if db.is_banned(user.id) and user.id != ADMIN_ID:
        reason = db.get_ban_reason(user.id) or "未说明"
        await safe_reply(
            update,
            f"🚫 你已被封禁，无法使用本机器人。\n原因：{reason}",
        )
        return

    allowed, wait = limiter.check(user.id)
    if not allowed:
        await safe_reply(update, f"⏳ 太快啦，请 {wait} 秒后再试。")
        return

    db.upsert_user(user.id, user.username, user.first_name)
    await try_typing(update, context)

    # ===== 身份问答：固定回复，不走 LLM、不扣积分 =====
    if is_identity_question(user_text):
        await safe_reply(update, IDENTITY_REPLY)
        return
    # =================================================

    is_file, ext, lang, is_suspect = detect_file_intent(user_text)

    if is_suspect:
        is_file, ext, lang = await smart_detect(user_text)

    if is_file:
        await generate_file(update, context, user_text, ext, lang)
        return

    reply = await ask_llm(user.id, user_text)
    await safe_reply(update, reply)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if db.is_banned(user.id) and user.id != ADMIN_ID:
        reason = db.get_ban_reason(user.id) or "未说明"
        await safe_reply(
            update,
            f"🚫 你已被封禁，无法使用本机器人。\n原因：{reason}",
        )
        return

    allowed, wait = limiter.check(user.id)
    if not allowed:
        await safe_reply(update, f"⏳ 太快啦，请 {wait} 秒后再试。")
        return

    if not WHISPER_API_KEY:
        await safe_reply(update, "🎤 语音功能未启用（WHISPER_API_KEY 为空）。")
        return

    await try_typing(update, context)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    path = os.path.join(tempfile.gettempdir(), f"{voice.file_unique_id}.ogg")
    await file.download_to_drive(path)

    text = await transcribe_voice(path)
    try:
        os.remove(path)
    except OSError:
        pass

    if not text:
        await safe_reply(update, "😅 没听清，再试一次？")
        return

    await safe_reply(update, f"🎤 识别到: _{text}_", parse_mode=ParseMode.MARKDOWN)

    db.upsert_user(user.id, user.username, user.first_name)

    # ===== 身份问答（语音同样拦截）=====
    if is_identity_question(text):
        await safe_reply(update, IDENTITY_REPLY)
        return
    # ===================================

    is_file, ext, lang, is_suspect = detect_file_intent(text)
    if is_suspect:
        is_file, ext, lang = await smart_detect(text)

    if is_file:
        await generate_file(update, context, text, ext, lang)
        return

    reply = await ask_llm(user.id, text)
    await safe_reply(update, reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "🖼️ 收到图片啦！当前模型不支持图片识别。")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, asyncio.TimeoutError) or "TimedOut" in type(err).__name__:
        logger.warning(f"网络超时（已忽略）: {err}")
        return
    logger.error("处理更新出错", exc_info=err)


async def on_startup(app: Application):
    logger.info(f"Bot 启动 | 模型池: {' → '.join(LLM_MODELS)}")
    for level, models in DIFFICULTY_MODELS.items():
        logger.info(f"  {level}: {' → '.join(models)}")
    await llm._get_session()


async def on_shutdown(app: Application):
    logger.info("Bot 关闭")
    await llm.close()


def main():
    if not BOT_TOKEN or BOT_TOKEN == "填入你的telegram_token":
        print("❌ 请先替换 BOT_TOKEN")
        return
    if not LLM_API_KEY or LLM_API_KEY == "填入你的gsk_key":
        print("❌ 请先替换 LLM_API_KEY")
        return

    print(f"🚀 启动中... 模型池: {' → '.join(LLM_MODELS)}")
    print("🧠 智能难度匹配已启用（规则优先 + 智能文件检测）:")
    for level, models in DIFFICULTY_MODELS.items():
        print(f"   {level}: {' → '.join(models)}")
    print(f"💰 积分系统: 注册送 {INIT_CREDITS} | 签到 +{CHECKIN_CREDITS} | "
          f"扣分 简单{COST_SIMPLE}/中等{COST_MEDIUM}/复杂{COST_COMPLEX} | 生成文件{COST_COMPLEX}")
    print(f"🔒 管理员面板 /gl 仅限 ID: {ADMIN_ID}")
    print("🤖 身份问答固定回复已启用")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(TG_CONNECT_TIMEOUT)
        .read_timeout(TG_READ_TIMEOUT)
        .write_timeout(TG_READ_TIMEOUT)
        .pool_timeout(TG_READ_TIMEOUT)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("gl", cmd_gl))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(admin_callback))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_error_handler(on_error)

    logger.info("🚀 Bot 已启动，Ctrl+C 停止")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=1.0,
    )


if __name__ == "__main__":
    main()
