# ai_business_kit_bot.py — v2.2 (full, commented)
# ============================================================
# Бот выдачи «AI Business Kit» после подтверждения оплаты
# Ключевые фичи:
# - Надёжная отправка файлов (приоритет: file_id override → ENV file_id → персональный кэш → загрузка по URL → голая ссылка)
# - Кэширование file_id в paid_users.json (персонально на пользователя)
# - Админ-панель: список покупателей, экспорт CSV, быстрый ответ, рассылка (по флагу только verified), backup/очистка
# - Чаты «админ ↔ пользователь» 1-на-1: разовое сообщение и полноценный диалог
# - Встроенный ИИ-помощник для пользователя и для админа с разными системными промптами
# - Улучшены callback-ответы: безопасная обёртка _safe_cb_answer, чтобы не ловить таймаут
# ============================================================

# ---------------------------
# БАЗОВЫЕ ИМПОРТЫ
# ---------------------------
import sys
import asyncio
import logging
import json
import re
import csv
import io
import functools
import aiohttp
import random
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List
from asyncio import get_running_loop
from html import escape

if sys.platform == "win32":
    # ДОЛЖНО стоять ДО создания Bot/Dispatcher и любых aiohttp-сессий
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.chat_action import ChatActionSender
from collections import deque
from contextlib import suppress
# === ENV LOADING (Render-friendly) ===
import os
from dotenv import load_dotenv

def load_env():
    """
    Приоритет:
    1) APP_ENV_FILE (если задана)
    2) .env.kit
    3) .env
    Если ни один не найден — не падаем (Render передаст переменные через UI).
    """
    env_file = os.getenv("APP_ENV_FILE")
    loaded = False
    if env_file:
        loaded = load_dotenv(env_file)
        print(f"[ENV] APP_ENV_FILE={env_file} loaded={loaded}")
    if not loaded:
        loaded = load_dotenv(".env.kit") or load_dotenv(".env")
        print(f"[ENV] fallback .env.kit/.env loaded={loaded}")

load_env()

# === PATHS ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
print(f"[PATHS] BASE_DIR={BASE_DIR} | DATA_DIR={DATA_DIR}")

# === LOGGING (по желанию) ===
import logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# ---------------------------
# НАСТРОЙКИ ИЗ ENV
# ---------------------------
TOKEN_KIT = (os.getenv("BOT_TOKEN_KIT") or "").strip()
TOKEN = TOKEN_KIT
if not TOKEN:
    raise RuntimeError("BOT_TOKEN_KIT обязателен. Заполни его в Render → Environment.")

PDF_PRESENTATION_FILE_ID = (os.getenv("PDF_PRESENTATION_FILE_ID") or "").strip()
PDF_PRESENTATION_URL     = (os.getenv("PDF_PRESENTATION_URL") or "").strip()
PDF_PROMPTS_FILE_ID      = (os.getenv("PDF_PROMPTS_FILE_ID") or "").strip()
PDF_GUIDE_FILE_ID        = (os.getenv("PDF_GUIDE_FILE_ID") or "").strip()
PDF_PROMPTS_URL          = (os.getenv("PDF_PROMPTS_URL") or "").strip()
PDF_GUIDE_URL            = (os.getenv("PDF_GUIDE_URL") or "").strip()

BOOSTY_LINK  = os.getenv("BOOSTY_LINK") or ""
ADMIN_ID     = int(os.getenv("ADMIN_ID") or 0)
BROADCAST_VERIFIED_ONLY = (os.getenv("BROADCAST_VERIFIED_ONLY", "true").lower() == "true")

SBP_QR_FILE_ID     = (os.getenv("SBP_QR_FILE_ID") or "").strip()
SBP_QR_URL         = (os.getenv("SBP_QR_URL") or "").strip()
SBP_PRICE_RUB      = int(os.getenv("SBP_PRICE_RUB") or 3500)
SBP_COMMENT_PREFIX = (os.getenv("SBP_COMMENT_PREFIX") or "Order#").strip()
SBP_RECIPIENT_NAME = (os.getenv("SBP_RECIPIENT_NAME") or "").strip()

OPENAI_API_KEY  = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://openrouter.ai/api/v1").strip()  # <- фикс
OPENAI_MODEL    = (os.getenv("OPENAI_MODEL") or "openai/gpt-4o-mini").strip()
AI_MAX_HISTORY  = int(os.getenv("AI_MAX_HISTORY") or 6)

DEMO_AI_ENABLED      = (os.getenv("DEMO_AI_ENABLED", "true").lower() == "true")
DEMO_AI_DAILY_LIMIT  = int(os.getenv("DEMO_AI_DAILY_LIMIT") or 5)
DEMO_AI_COOLDOWN_SEC = int(os.getenv("DEMO_AI_COOLDOWN_SEC") or 15)

BRAND_CREATED_AT = os.getenv("BRAND_CREATED_AT") or "N/A"
BRAND_NAME       = os.getenv("BRAND_NAME") or "AI Business Kit"
BRAND_OWNER      = os.getenv("BRAND_OWNER") or "Owner"
BRAND_URL        = os.getenv("BRAND_URL") or "https://example.com"
BRAND_SUPPORT_TG = os.getenv("BRAND_SUPPORT_TG") or "@support"

PRODUCT_CODE = "KIT"
print(f"[INIT] PRODUCT_CODE={PRODUCT_CODE} | BRAND={BRAND_NAME}")

class _SafeDict(dict):
    def __missing__(self, key):
        return "N/A"

def _sanitize_prompt_template(tpl: str) -> str:
    """
    Превращает {user_id или N/A} -> {user_id}
    И в целом урезает всё в фигурных скобках до первого слова-идентификатора.
    Например: {BRAND_NAME} остаётся без изменений.
    """
    return re.sub(r"\{(\w+)[^}]*\}", r"{\1}", tpl)

def _fmt_prompt(tpl: str, **kwargs) -> str:
    tpl = _sanitize_prompt_template(tpl)
    base = {
        "BRAND_CREATED_AT": BRAND_CREATED_AT,
        "BRAND_NAME":       BRAND_NAME,
        "BRAND_OWNER":      BRAND_OWNER,
        "BRAND_URL":        BRAND_URL,
        "BRAND_SUPPORT_TG": BRAND_SUPPORT_TG,
    }
    base.update(kwargs)
    return tpl.format_map(_SafeDict(base))

# ---------------------------
# ХЕЛПЕР ДЛЯ ОБЯЗАТЕЛЬНЫХ ПЕРЕМЕННЫХ
# ---------------------------
from html import escape

async def _answer_html_safe(msg: types.Message, text: str, **kwargs):
    """
    Пытаемся отправить как HTML, но если Telegram ругнётся на разметку — шлём plain text.
    """
    try:
        # Экраним любые <>& на всякий случай, чтобы не ломать HTML
        safe = escape(text)
        return await msg.answer(safe, parse_mode="HTML", **kwargs)
    except Exception as e:
        logging.warning("HTML send failed, fallback to plain: %s", e)
        try:
            # Фолбэк: без parse_mode
            return await msg.answer(text, **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        except Exception as e2:
            logging.error("Plain send failed: %s", e2)
            # Последний шанс: обрежем всё «опасное»
            plain = re.sub(r"<[^>]*>", "", text)
            return await msg.answer(plain, **{k: v for k, v in kwargs.items() if k != "parse_mode"})

def _must_get(key: str, fallback: str = "") -> str:
    val = (os.getenv(key) or "").strip()
    if not val and not fallback:
        print(f"❌ В .env отсутствует обязательная переменная: {key}")
        sys.exit(1)
    return val or fallback

def _gen_order_id() -> str:
    # короткий и удобный: дата + 4 цифры
    return datetime.now().strftime("%m%d%H%M") + "-" + f"{random.randint(0,9999):04d}"

async def _send_sbp_qr(chat_id: int, order_id: str):
    caption = (
        f"💳 <b>Оплата по СБП</b>\n"
        f"Сумма: <b>{SBP_PRICE_RUB} ₽</b>\n"
        + (f"Получатель: <b>{SBP_RECIPIENT_NAME}</b>\n" if SBP_RECIPIENT_NAME else "")
        + "\n<b>Важно!</b> В комментарии к переводу укажите:\n"
        f"<code>{SBP_COMMENT_PREFIX} {order_id}</code>\n\n"
        "После оплаты пришлите сюда <b>скриншот</b> операции (видны дата/время, сумма, статус, получатель)."
    )

    # приоритет: привязанный file_id → ENV file_id → скачиваем по URL → просто текст
    qr_file_id_override = get_sbp_qr_file_id()
    if qr_file_id_override:
        await bot.send_photo(chat_id, qr_file_id_override, caption=caption, parse_mode="HTML")
        return
    if SBP_QR_FILE_ID:
        try:
            await bot.send_photo(chat_id, SBP_QR_FILE_ID, caption=caption, parse_mode="HTML")
            return
        except Exception:
            pass
    if SBP_QR_URL:
        data = await _download_bytes_async(SBP_QR_URL)
        if data:
            await bot.send_photo(
                chat_id,
                photo=types.BufferedInputFile(data, filename="sbp_qr.png"),
                caption=caption, parse_mode="HTML"
            )
            return
    await bot.send_message(chat_id, "⚠️ QR временно недоступен. Свяжитесь с поддержкой: " + BRAND_SUPPORT_TG)

def _today_key() -> str:
    return datetime.now().strftime("%Y%m%d")

def _demo_quota_ok(uid: int) -> Tuple[bool, str]:
    if not DEMO_AI_ENABLED:
        return False, "Демо-режим временно отключён."
    day = _today_key()
    cnt = (_demo_hits.get(uid) or {}).get(day, 0)
    if cnt >= DEMO_AI_DAILY_LIMIT:
        return False, f"Лимит демо-чата исчерпан на сегодня ({DEMO_AI_DAILY_LIMIT}). Оформите доступ — и лимитов не будет."
    return True, ""

def _demo_register_hit(uid: int):
    day = _today_key()
    d = _demo_hits.get(uid) or {}
    d[day] = d.get(day, 0) + 1
    _demo_hits[uid] = d

# ---------------------------
# СИСТЕМНЫЕ ПРОМПТЫ ДЛЯ ИИ
# ---------------------------
AI_SYSTEM_PROMPT_USER_DEMO_RAW = os.getenv("AI_SYSTEM_PROMPT_USER_DEMO") or (
    "Ты — демо-ассистент набора «{BRAND_NAME}». Отвечай кратко, по делу и по-русски. "
    "Не раскрывай приватные материалы, не отправляй файлы/ключи, а в конце дай 1–2 шага, как купить набор."
)

AI_SYSTEM_PROMPT_USER_RAW = _must_get(
    "AI_SYSTEM_PROMPT_USER_KIT",
    # Безопасный дефолт, если забыли положить переменную в .env.kit
    "Ты — дружелюбный ИИ-консультант набора «AI Business Kit». Отвечай кратко, по делу и по-русски. "
    "Помогаешь с получением материалов, установкой бота, оплатой и базовым маркетингом. "
    "Если нужна поддержка человека — дай ссылку {BRAND_SUPPORT_TG}. "
    "В конце сложных ответов предлагай 3 шага «что сделать дальше»."
)

AI_SYSTEM_PROMPT_ADMIN_RAW = _must_get(
    "AI_SYSTEM_PROMPT_ADMIN_KIT",
    "Ты — техничный помощник владельца «AI Business Kit». Даёшь точные подсказки по aiogram v3, "
    "логике выдачи файлов, кэшу file_id, рассылке, JSON-базе paid_users.json и kit_assets.json. "
    "Если видишь проблему — предложи конкретный патч/фрагмент кода. "
    "При критике формируй служебный сигнал ##ADMIN_ALERT##."
)

print("[PROMPT_USER]", AI_SYSTEM_PROMPT_USER_RAW[:120].replace("\n", " "))
print("[PROMPT_ADMIN]", AI_SYSTEM_PROMPT_ADMIN_RAW[:120].replace("\n", " "))

def _fmt_prompt(tpl: str, user_id: int, is_admin: bool) -> str:
    """Подставляем брендовые плейсхолдеры в системный промпт ИИ."""
    return tpl.format(
        BRAND_CREATED_AT=BRAND_CREATED_AT,
        BRAND_NAME=BRAND_NAME,
        BRAND_OWNER=BRAND_OWNER,
        BRAND_URL=BRAND_URL,
        BRAND_SUPPORT_TG=BRAND_SUPPORT_TG,
        user_id=user_id
    )

# ---------------------------
# БАЗЫ ДАННЫХ (JSON файлы)
# ---------------------------
DATA_FILE   = os.path.join(DATA_DIR, "paid_users.json")
ASSETS_FILE = os.path.join(DATA_DIR, "kit_assets.json")

# ---------------------------
# БОТ/ДИСПЕТЧЕР
# ---------------------------
bot = Bot(token=TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def register_handlers(dp: Dispatcher, bot: Bot):
    """
    Здесь РЕГИСТРИРУЕМ все хэндлеры/роутеры/мидлвари.
    Ничего не запускаем.
    """
    # примеры:
    # dp.message.register(start, CommandStart())
    # dp.message.register(ping, Command("ping"))
    # dp.callback_query.register(on_buy_click, F.data == "buy")

    # если у тебя были вызовы dp.startup.register / dp.shutdown.register — их можно оставить здесь:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

# ---------------------------
# БЕЗОПАСНЫЙ ОТВЕТ НА CALLBACK
# ---------------------------
async def _safe_cb_answer(cb: types.CallbackQuery, text: str = "", show_alert: bool = False):
    """
    Безопасно отвечаем на callback, чтобы убрать «часики».
    Если query уже протух — просто молча игнорируем исключение.
    """
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logging.debug("callback.answer skipped: %s", e)

# ---------------------------
# СОСТОЯНИЯ FSM
# ---------------------------
class AdminContactStates(StatesGroup):
    selecting_user = State()  # выбор пользователя из списка
    composing_once = State()  # разовое сообщение
    chatting       = State()  # активный диалог

class PaymentStates(StatesGroup):
    waiting_screenshot = State()  # ожидание скрина чека

class _ReplyStates(StatesGroup):
    waiting = State()  # быстрый ответ из уведомления

class BroadcastStates(StatesGroup):
    waiting_content = State()  # сбор контента рассылки
    confirm_send    = State()  # подтверждение

class SupportStates(StatesGroup):
    waiting_text = State()  # пользователь пишет в поддержку

class AIChatStates(StatesGroup):
    chatting = State()  # чат с ИИ

# ---------------------------
# ДИАЛОГИ АДМИН ↔ ПОЛЬЗОВАТЕЛЬ (активные соединения)
# ---------------------------
_active_admin_chats: Dict[int, int] = {}  # admin_id -> user_id
_active_user_chats: Dict[int, int] = {}  # user_id -> admin_id

# ---------------------------
# ИСТОРИЯ ДЛЯ ИИ
# ---------------------------
_user_histories: Dict[str, deque] = {}

# Демо-квоты (на день)
_demo_hits: Dict[int, Dict[str, int]] = {}  # {uid: {YYYYMMDD: count}}
_demo_last: Dict[int, float] = {}           # {uid: last_ts}

def _desired_hist_maxlen() -> int:
    pairs = max(1, min(50, AI_MAX_HISTORY))
    return pairs * 2  # user+assistant

def _hist_key(uid: int, is_admin: bool) -> str:
    return f"{'admin' if is_admin else 'user'}:{uid}"

def _push_history(uid: int, is_admin: bool, role: str, content: str, desired: Optional[int] = None):
    """
    Кладём событие в историю. Если задан desired — после добавления
    обрезаем историю до desired последних СООБЩЕНИЙ (не пар).
    """
    key = _hist_key(uid, is_admin)
    dq = _user_histories.get(key)
    if dq is None:
        dq = deque(maxlen=AI_MAX_HISTORY * 2)  # запас по умолчанию
        _user_histories[key] = dq

    dq.append({"role": role, "content": content})

    if desired is not None:
        # мягкая обрезка без изменения dq.maxlen (его менять нельзя)
        while len(dq) > desired:
            dq.popleft()

def _demo_quota_ok(uid: int) -> tuple[bool, str]:
    today = datetime.now().strftime("%Y-%m-%d")
    rec = _demo_hits.get(uid)
    if not rec or rec.get("date") != today:
        return True, ""
    if rec.get("count", 0) < DEMO_AI_DAILY_LIMIT:
        return True, ""
    return False, f"Лимит демо-диалога на сегодня исчерпан ({DEMO_AI_DAILY_LIMIT}). Оформите доступ, чтобы общаться без ограничений."

def _demo_register_hit(uid: int):
    today = datetime.now().strftime("%Y-%m-%d")
    rec = _demo_hits.get(uid)
    if not rec or rec.get("date") != today:
        _demo_hits[uid] = {"date": today, "count": 1}
    else:
        rec["count"] = rec.get("count", 0) + 1

def _hist_key(uid: int, is_admin: bool) -> str:
    return f"{'admin' if is_admin else 'user'}:{uid}"

def _push_history(uid: int, is_admin: bool, role: str, content: str, desired: Optional[int] = None):
    """
    desired — желаемая общая длина deque (кол-во сообщений user+assistant),
    чтобы в демо урезать историю.
    """
    key = _hist_key(uid, is_admin)
    maxlen = desired or (AI_MAX_HISTORY * 2)  # пары user+assistant
    dq = _user_histories.get(key)
    if dq is None or dq.maxlen != maxlen:
        dq = deque(maxlen=maxlen)
        _user_histories[key] = dq
    dq.append({"role": role, "content": content})

    # вычислим текущий желаемый лимит сообщений
    default_max = AI_MAX_HISTORY * 2  # AI_MAX_HISTORY — кол-во ПАР; умножаем на 2 → сообщения
    msg_max = int(desired) if desired and desired > 0 else default_max
    msg_max = max(2, min(msg_max, default_max))  # не дать вырасти выше прод-лимита и ниже 2

    dq = _user_histories.get(key)

    if dq is None:
        dq = deque(maxlen=msg_max)
        _user_histories[key] = dq
    else:
        # если лимит поменялся — пересоберём очередь с новым maxlen
        if dq.maxlen != msg_max:
            dq = deque(dq, maxlen=msg_max)
            _user_histories[key] = dq

    dq.append({"role": role, "content": content})

def _build_messages(uid: int, is_admin: bool, user_text: str, is_demo: bool = False) -> List[Dict[str, str]]:
    sys_prompt = _fmt_prompt(
        AI_SYSTEM_PROMPT_ADMIN_RAW if is_admin else AI_SYSTEM_PROMPT_USER_RAW,
        user_id=uid, is_admin=is_admin
    )
    msgs = [{"role": "system", "content": sys_prompt}]
    dq = _user_histories.get(_hist_key(uid, is_admin)) or []
    msgs.extend(dq)
    msgs.append({"role": "user", "content": user_text})
    return msgs

def _headers_for_openai():
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    h["Referer"] = BRAND_URL  # было HTTP-Referer
    h["X-Title"] = BRAND_NAME
    return h

async def _post_async(url, headers, json, timeout=60):
    """Асинхронный POST-запрос (fallback-метод, если aiohttp не используется напрямую)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(requests.post, url=url, headers=headers, json=json, timeout=timeout)
    )


async def _ai_complete(uid: int, is_admin: bool, user_text: str) -> str:
    if not OPENAI_API_KEY:
        return "⚠️ OPENAI_API_KEY не задан в .env"

    payload = {
        "model": OPENAI_MODEL,
        "messages": _build_messages(uid, is_admin, user_text, is_demo=False),
        "temperature": 0.2,
    }
    url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=45, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers_for_openai()) as s:
            for attempt in range(3):  # ретраи
                async with s.post(url, json=payload) as resp:
                    txt = await resp.text()
                    logging.info("[AI] HTTP %s attempt=%s body=%s", resp.status, attempt, txt[:300])
                    if resp.status == 200:
                        try:
                            data = json.loads(txt)
                        except Exception:
                            data = await resp.json()
                        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "⚠️ Пустой ответ модели."
                    if resp.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.5 * attempt + 0.5)
                        continue
                    return f"⚠️ Ошибка ИИ: {resp.status} {txt[:200]}"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("AI error: %s", e)
        return f"⚠️ Исключение: {e}"

    return "⚠️ Таймаут ИИ. Попробуйте ещё раз."


async def _ai_complete_demo(uid: int, is_admin: bool, prepared_messages: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "⚠️ OPENAI_API_KEY не задан в .env"

    payload = {
        "model": OPENAI_MODEL,
        "messages": prepared_messages,
        "temperature": 0.2,
        # "max_tokens": 400,  # можно включить при желании
    }
    url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=30, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers_for_openai()) as s:
            for attempt in range(3):
                async with s.post(url, json=payload) as resp:
                    txt = await resp.text()
                    logging.info("[AI-DEMO] HTTP %s attempt=%s body=%s", resp.status, attempt, txt[:300])
                    if resp.status == 200:
                        try:
                            data = json.loads(txt)
                        except Exception:
                            data = await resp.json()
                        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "⚠️ Пустой ответ модели."
                    if resp.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.5 * attempt + 0.5)
                        continue
                    return f"⚠️ Ошибка ИИ (демо): {resp.status} {txt[:200]}"
    except Exception as e:
        logging.exception("AI demo error: %s", e)
        return f"⚠️ Исключение (демо): {e}"

    return "⚠️ Таймаут ИИ (демо). Попробуйте ещё раз."        

# ---------------------------
# ПРИМИТИВНАЯ «БАЗА ДАННЫХ» (JSON)
# ---------------------------
def _demo_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def _get_demo_stats(uid: int) -> Dict[str, Any]:
    users = load_paid_users()
    rec = users.get(str(uid)) or {}
    demo = rec.get("demo_ai") or {"date": _demo_today_str(), "count": 0, "last_ts": 0}
    return demo

def _save_demo_stats(uid: int, demo: Dict[str, Any]):
    users = load_paid_users()
    rec = users.get(str(uid)) or {}
    rec["demo_ai"] = demo
    users[str(uid)] = rec
    save_users(users)

def _demo_quota_ok(uid: int) -> Tuple[bool, str]:
    if not DEMO_AI_ENABLED:
        return False, "Демо-режим временно отключён."
    demo = _get_demo_stats(uid)
    # сброс по дате
    if demo.get("date") != _demo_today_str():
        demo["date"] = _demo_today_str()
        demo["count"] = 0
    # cooldown
    now_ts = int(datetime.now().timestamp())
    last_ts = int(demo.get("last_ts", 0))
    if now_ts - last_ts < DEMO_AI_COOLDOWN_SEC:
        return False, f"Подождите {DEMO_AI_COOLDOWN_SEC - (now_ts - last_ts)} сек. перед следующим вопросом."
    # лимит в день
    if int(demo.get("count", 0)) >= DEMO_AI_DAILY_LIMIT:
        return False, f"Лимит в демо {DEMO_AI_DAILY_LIMIT}/день исчерпан. Оформите доступ, чтобы общаться без ограничений."
    return True, ""

def _demo_register_hit(uid: int):
    demo = _get_demo_stats(uid)
    if demo.get("date") != _demo_today_str():
        demo["date"] = _demo_today_str()
        demo["count"] = 0
    demo["count"] = int(demo.get("count", 0)) + 1
    demo["last_ts"] = int(datetime.now().timestamp())
    _save_demo_stats(uid, demo)

def _atomic_write(path: str, data: dict):
    """Атомарная запись JSON (через tmp-файл)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_paid_users() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users: dict):
    _atomic_write(DATA_FILE, users)

def save_pending_user(user_id: int, username: str):
    """Сохраняем запись (ещё не подтверждён)."""
    users = load_paid_users()
    rec = users.get(str(user_id), {})
    rec.setdefault("verified", False)
    rec.setdefault("purchase_date", None)
    rec.setdefault("cache", {})  # кэш file_id для этого пользователя
    rec["username"] = username or rec.get("username", "unknown")
    users[str(user_id)] = rec
    save_users(users)

def save_paid_user(user_id: int, username: str):
    """Подтверждаем оплату пользователя."""
    users = load_paid_users()
    rec = users.get(str(user_id), {})
    rec["username"] = username or rec.get("username", "unknown")
    rec["purchase_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec["verified"] = True
    rec.setdefault("cache", {})
    users[str(user_id)] = rec
    save_users(users)

def is_user_verified(user_id: int) -> bool:
    return load_paid_users().get(str(user_id), {}).get("verified", False)

def clear_database():
    """Полная очистка БД."""
    save_users({})

def remove_user(user_id: int) -> bool:
    users = load_paid_users()
    if str(user_id) in users:
        del users[str(user_id)]
        save_users(users)
        return True
    return False

def backup_database() -> Optional[str]:
    """Создаём backup paid_users.json."""
    try:
        users = load_paid_users()
        backup_filename = os.path.join(
            BASE_DIR, f"paid_users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        _atomic_write(backup_filename, users)
        return backup_filename
    except Exception:
        return None

# ---------------------------
# ГЛОБАЛЬНЫЙ КЭШ file_id ДЛЯ МАТЕРИАЛОВ (kit_assets.json)
# ---------------------------
def _load_assets() -> Dict[str, Any]:
    try:
        with open(ASSETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_assets(d: dict):
    _atomic_write(ASSETS_FILE, d)

def get_asset_file_id(key: str) -> Optional[str]:
    """
    key: 'prompts' | 'guide' | 'presentation'
    """
    d = _load_assets()
    v = (d.get(key) or {}).get("file_id")
    return v or None

def get_sbp_qr_file_id() -> Optional[str]:
    d = _load_assets()
    return (d.get("sbp_qr") or {}).get("file_id") or None

def set_asset_file_id(key: str, file_id: str):
    d = _load_assets()
    entry = d.get(key) or {}
    entry["file_id"] = file_id
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d[key] = entry
    _save_assets(d)

# ---------------------------
# КЛАВИАТУРЫ
# ---------------------------
def kb_start(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"💳 Оплата по СБП (QR) — {SBP_PRICE_RUB} ₽", callback_data="pay_sbp")],
        [InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data="request_verification")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="open_faq")]
    ]
    if is_admin:
        rows.insert(0, [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_after_payment(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🤖 ИИ-помощник", callback_data="ai_open")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_request")],
        [InlineKeyboardButton(text="🔄 Получить файлы снова", callback_data="get_files_again")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="open_faq")]
    ]
    if is_admin:
        rows.insert(0, [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

    # ✅ Универсальный выбор меню для клиента/админа
def _menu_kb_for(user_id: int) -> InlineKeyboardMarkup:
    is_admin = (user_id == ADMIN_ID)
    if is_user_verified(user_id):
        return kb_after_payment(is_admin=is_admin)
    return kb_start(is_admin=is_admin)

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
    ])

def kb_support() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать сообщение", callback_data="support_message")],
        [InlineKeyboardButton(text="📞 Связаться с менеджером", callback_data="support_manager_info")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
    ])

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
         InlineKeyboardButton(text="👥 Пользователи", callback_data="list_users")],
        [InlineKeyboardButton(text="📥 Покупатели", callback_data="admin_buyers"),
         InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="admin_export_buyers")],
        [InlineKeyboardButton(text="👤 Связаться", callback_data="admin_contact_open")],
        [InlineKeyboardButton(text="✉️ Ответ пользователю", callback_data="admin_reply_prompt"),
         InlineKeyboardButton(text="📣 Рассылка", callback_data="open_broadcast")],
        [InlineKeyboardButton(text="🤖 ИИ (админ)", callback_data="ai_admin_open"),
         InlineKeyboardButton(text="💾 Backup", callback_data="create_backup")],
        [InlineKeyboardButton(text="🗑 Очистить базу", callback_data="clear_all")],
        [InlineKeyboardButton(text="↩️ Закрыть", callback_data="back_to_main")]
    ])

def kb_ai_chat(is_admin: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏹️ Завершить чат", callback_data=("ai_admin_close" if is_admin else "ai_close")),
        InlineKeyboardButton(text="↩️ В меню", callback_data="back_to_main"),
    ]])

def kb_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_home")],
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="back_to_main")]
    ])

def kb_admin_quick_reply(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Ответить пользователю", callback_data=f"admin_quick_reply_{uid}")],
        [InlineKeyboardButton(text="💬 Войти в диалог",       callback_data=f"admin_chat_enter_{uid}")]
    ])

def kb_broadcast_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Разослать", callback_data="broadcast_send")],
        [InlineKeyboardButton(text="❌ Отмена",     callback_data="broadcast_cancel")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_home")]
    ])

def kb_verification_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
    ])

def _verified_home_text() -> str:
    return (
        "🎉 <b>Доступ активирован</b>\n\n"
        "Вы уже купили AI Business Kit. Что дальше?\n\n"
        "• «🔄 Получить файлы снова» — переотправим материалы\n"
        "• «💬 Написать в поддержку» — вопрос/помощь\n"
        "• «❓ FAQ» — ответы на частые вопросы\n"
        "• «🤖 ИИ-помощник» — ваш ассистент"
    )

async def show_verified_home(chat_id: int):
    await bot.send_message(chat_id, _verified_home_text(),
                           reply_markup=kb_after_payment(is_admin=(chat_id == ADMIN_ID)),
                           parse_mode="HTML")

# ---------------------------
# /start, /help, /about
# ---------------------------
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    if is_user_verified(message.from_user.id):
        await show_verified_home(message.chat.id)
        return

    text = (
        "👋 <b>Добро пожаловать в AI Business Kit</b>\n\n"
        "🚀 Что вы получите:\n"
        "• 100 ChatGPT-промптов для бизнеса\n"
        "• Шаблон Telegram-бота с CRM\n"
        "• Пошаговое руководство по запуску (10 минут)\n\n"
        f"💵 <b>Стоимость:</b> {SBP_PRICE_RUB} ₽ (разовый платёж)\n\n"
        "Как получить:\n"
        "1) Нажмите «Оплата по СБП (QR)» и оплатите\n"
        "2) Нажмите «Я оплатил(а)»\n"
        "3) Отправьте скриншот чека для подтверждения\n\n"
        "⏱ Проверка занимает обычно 5–15 минут"
    )

    # 👇 правильный отступ — 4 пробела
    await message.answer(
        text,
        reply_markup=_menu_kb_for(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "• /start — начать заново\n"
        "• «Я оплатил(а)» — прислать чек на проверку\n"
        "• «Поддержка» — задать вопрос\n\n"
        "После подтверждения платежа получите:\n"
        "• 100 промптов (PDF)\n"
        "• Презентацию продукта (PDF)\n"
        "• Шаблон бота (Python файл)\n",
        parse_mode="HTML"
    )

@dp.message(Command("about"))
async def about_cmd(message: types.Message):
    await message.answer(
        "ℹ️ <b>О наборе AI Business Kit</b>\n\n"
        "Набор материалов и кода для быстрого старта:\n"
        "• 100 промптов для бизнеса\n"
        "• Пошаговое руководство\n"
        "• Шаблон Telegram-бота с CRM\n\n"
        "Поддержка по вопросам: нажмите «Написать в поддержку».",
        parse_mode="HTML"
    )

# ---------------------------
# FAQ
# ---------------------------
FAQ_TEXT = (
    "❓ <b>FAQ</b>\n\n"
    "1) Токен — @BotFather → /newbot\n"
    "2) Свой ID — @myidbot / @userinfobot\n"
    "3) Шаблон — pip install aiogram → python bot_template.py\n"
    "4) Демо товары — уже в базе шаблона\n"
    "5) Ответ пользователю — кнопка «✉️» в уведомлении или команда /reply\n"
)

@dp.callback_query(F.data == "open_faq")
async def open_faq_handler(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)
    await callback.message.edit_text(FAQ_TEXT, reply_markup=kb_back_main(), parse_mode="HTML")

# ---------------------------
# ПОДДЕРЖКА (кнопки)
# ---------------------------
@dp.callback_query(F.data == "support_request")
async def support_request_handler(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)
    text = (
        "💬 <b>Служба поддержки</b>\n\n"
        "Опишите вопрос — ответим в этом чате.\n"
        "Если срочно — нажмите «Связаться с менеджером»."
    )
    await callback.message.edit_text(text, reply_markup=kb_support(), parse_mode="HTML")

# --------------------------
# 📞 БЛОК ПОДДЕРЖКИ
# --------------------------

@dp.callback_query(F.data == "support_message")
async def support_message_handler(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)

    # ВАЖНО: включаем режим поддержки
    await state.set_state(SupportStates.waiting_text)
    await state.update_data(is_support=True)

    await callback.message.answer(
        "✉️ Напишите сообщение (можно фото/документ/голосовое) — оператор получит его и ответит здесь.",
        reply_markup=kb_back_main(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "support_manager_info")
async def support_manager_info(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)

    support_tag = BRAND_SUPPORT_TG.strip()
    if not support_tag:
        support_tag = "— не указан —"
    elif not support_tag.startswith("@"):
        support_tag = "@" + support_tag

    text = (
        "👨‍💼 <b>Контакт менеджера поддержки</b>\n\n"
        f"📩 <b>{support_tag}</b>\n\n"
        "Рекомендуем обращаться по вопросам:\n"
        "• 💳 Оплата и подтверждение доступа\n"
        "• 📂 Повторная выдача файлов\n"
        "• ⚙️ Технические неполадки бота\n\n"
        "💡 <i>Начни сообщение с фразы:</i>\n"
        "«AI Business Kit — ... (суть вопроса)»"
    )

    await callback.message.answer(text, reply_markup=kb_back_main(), parse_mode="HTML")

# ---------------------------
# ЧАТ С ИИ (пользователь/админ)
# ---------------------------
@dp.callback_query(F.data == "ai_open_demo")
async def ai_open_demo_cb(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)
    await state.set_state(AIChatStates.chatting)
    await state.update_data(ai_is_admin=False)
    await callback.message.answer(
        "🤖 ИИ (демо-режим): готов ответить на вопросы о наборе, оплате и запуске.\n"
        "❗️ Чтобы активировать полноценный доступ, оплатите комплект.",
        reply_markup=kb_ai_chat(is_admin=False),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "ai_open")
async def ai_open_cb(callback: types.CallbackQuery, state: FSMContext):
    if not is_user_verified(callback.from_user.id):
        await _safe_cb_answer(callback, "Сначала подтвердите оплату.", show_alert=True)
        return
    await _safe_cb_answer(callback)
    await state.set_state(AIChatStates.chatting)
    await state.update_data(ai_is_admin=False)
    await callback.message.answer(
        "🤖 Готов к диалогу. Напиши вопрос про набор, запуск, маркетинг и т. п.",
        reply_markup=kb_ai_chat(is_admin=False), parse_mode="HTML"
    )

@dp.callback_query(F.data == "ai_demo_open")
async def ai_demo_open_cb(callback: types.CallbackQuery, state: FSMContext):
    # без проверки оплаты
    await _safe_cb_answer(callback)
    await state.set_state(AIChatStates.chatting)
    # явный флаг демо, чтобы не зависеть от статуса верификации
    await state.update_data(ai_is_admin=False, ai_force_demo=True)
    await callback.message.answer(
        f"🤖 Демо-режим ИИ включён.\n"
        f"Доступно до {DEMO_AI_DAILY_LIMIT} сообщений в день.\n"
        "Спросите что-нибудь про набор, установку бота или маркетинг.",
        reply_markup=kb_ai_chat(is_admin=False),
        parse_mode="HTML"
    )        

@dp.callback_query(F.data == "ai_admin_open")
async def ai_admin_open_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)
    await state.set_state(AIChatStates.chatting)
    await state.update_data(ai_is_admin=True)
    await callback.message.answer(
        "🤖 ИИ (админ): готов. Спрашивай по коду/логике/базе.",
        reply_markup=kb_ai_chat(is_admin=True), parse_mode="HTML"
    )

@dp.callback_query(F.data == "ai_close")
async def ai_close_cb(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)
    await state.clear()
    uid = callback.from_user.id
    await callback.message.answer("Чат закрыт. Чем ещё помочь?", reply_markup=_menu_kb_for(uid), parse_mode="HTML")


@dp.callback_query(F.data == "ai_admin_close")
async def ai_admin_close_cb(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback, "Чат ИИ (админ) закрыт")
    await state.clear()
    await callback.message.answer("Чат ИИ (админ) закрыт.", reply_markup=_menu_kb_for(ADMIN_ID), parse_mode="HTML")

# ---------------------------
# ВСПОМОГАТЕЛЬНОЕ: ПАГИНАЦИЯ пользователей для «Связаться»
# ---------------------------
def _paginate_users(page: int = 1, per_page: int = 10, verified_only: bool = False):
    """
    Возвращает (items, page, pages, total), где:
      items: список кортежей (user_id:int, username:str, verified:bool, purchase_date:str|None)
    """
    users = load_paid_users()
    items = []
    for uid, rec in users.items():
        if not isinstance(rec, dict):
            continue
        if verified_only and not rec.get("verified"):
            continue
        try:
            uid_int = int(uid)
        except Exception:
            continue
        items.append((
            uid_int,
            rec.get("username", "unknown"),
            bool(rec.get("verified", False)),
            rec.get("purchase_date")
        ))

    # сортировка: сначала verified, потом по дате (свежие выше)
    items.sort(key=lambda x: (not x[2], x[3] or ""), reverse=True)

    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], page, pages, total

def kb_admin_contact_list(page: int, pages: int, verified_only: bool) -> InlineKeyboardMarkup:
    """Нижняя панель навигации в списке пользователей."""
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"admin_contact_page_{page-1}_{int(verified_only)}"
        ))
    if page < pages:
        nav.append(InlineKeyboardButton(
            text="Вперёд ➡️",
            callback_data=f"admin_contact_page_{page+1}_{int(verified_only)}"
        ))

    filt = InlineKeyboardButton(
        text=("✅ Только покупатели" if verified_only else "👥 Все пользователи"),
        callback_data=f"admin_contact_toggle_{int(not verified_only)}_p{page}"
    )
    back = InlineKeyboardButton(text="↩️ В админ-панель", callback_data="admin_home")

    rows = []
    if nav:
        rows.append(nav)
    rows.append([filt])
    rows.append([back])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin_contact_user(uid: int) -> InlineKeyboardMarkup:
    """Кнопки действий по выбранному пользователю."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Разовое сообщение", callback_data=f"admin_msg_once_{uid}")],
        [InlineKeyboardButton(text="💬 Войти в диалог", callback_data=f"admin_chat_enter_{uid}")],
        [InlineKeyboardButton(text="↩️ К списку", callback_data="admin_contact_open")]
    ])

# ---------------------------
# СВЯЗАТЬСЯ (админ) — список/пагинация/выбор/режимы
# ---------------------------
@dp.callback_query(F.data == "admin_contact_open")
async def admin_contact_open_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)  # сразу снимаем «часики»

    users_page, page, pages, total = _paginate_users(page=1, per_page=10, verified_only=True)

    lines = [
        "👤 <b>Выбор пользователя для общения</b>\n",
        f"Пользователи: {total}\n",
        "Выберите из списка:"
    ]
    for uid, uname, ver, _ in users_page:
        mark = "✅" if ver else "❔"
        lines.append(f"{mark} <code>{uid}</code> @{uname or 'unknown'}")
    text = "\n".join(lines)

    kb_users = [[
        InlineKeyboardButton(
            text=f"{'✅' if ver else '❔'} @{uname or 'unknown'} ({uid})",
            callback_data=f"admin_contact_pick_{uid}"
        )
    ] for uid, uname, ver, _ in users_page]

    nav = kb_admin_contact_list(page, pages, verified_only=True)
    inline_keyboard = kb_users + nav.inline_keyboard

    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="HTML"
    )
    await state.set_state(AdminContactStates.selecting_user)

@dp.callback_query(F.data.regexp(r"^admin_contact_page_(\d+)_(\d)$"))
async def admin_contact_page_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)

    m = re.match(r"^admin_contact_page_(\d+)_(\d)$", callback.data)
    page = int(m.group(1))
    verified_only = bool(int(m.group(2)))

    users_page, page, pages, total = _paginate_users(page=page, per_page=10, verified_only=verified_only)

    lines = [f"👤 <b>Выбор пользователя</b>  |  страница {page}/{pages}\n", f"Пользователи: {total}\n"]
    for uid, uname, ver, _ in users_page:
        mark = "✅" if ver else "❔"
        lines.append(f"{mark} <code>{uid}</code> @{uname or 'unknown'}")
    text = "\n".join(lines)

    kb_users = [[
        InlineKeyboardButton(
            text=f"{'✅' if ver else '❔'} @{uname or 'unknown'} ({uid})",
            callback_data=f"admin_contact_pick_{uid}"
        )
    ] for uid, uname, ver, _ in users_page]

    nav = kb_admin_contact_list(page, pages, verified_only)
    inline_keyboard = kb_users + nav.inline_keyboard

    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="HTML")
    except Exception:
        await bot.send_message(callback.message.chat.id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="HTML")

    await state.set_state(AdminContactStates.selecting_user)

@dp.callback_query(F.data.regexp(r"^admin_contact_toggle_(\d)_p(\d+)$"))
async def admin_contact_toggle_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)

    m = re.match(r"^admin_contact_toggle_(\d)_p(\d+)$", callback.data)
    verified_only = bool(int(m.group(1)))
    page = int(m.group(2))

    users_page, page, pages, total = _paginate_users(page=page, per_page=10, verified_only=verified_only)

    lines = [f"👤 <b>Выбор пользователя</b>  |  страница {page}/{pages}\n", f"Пользователи: {total}\n"]
    for uid, uname, ver, _ in users_page:
        mark = "✅" if ver else "❔"
        lines.append(f"{mark} <code>{uid}</code> @{uname or 'unknown'}")
    text = "\n".join(lines)

    kb_users = [[
        InlineKeyboardButton(
            text=f"{'✅' if ver else '❔'} @{uname or 'unknown'} ({uid})",
            callback_data=f"admin_contact_pick_{uid}"
        )
    ] for uid, uname, ver, _ in users_page]

    nav = kb_admin_contact_list(page, pages, verified_only)
    inline_keyboard = kb_users + nav.inline_keyboard

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="HTML")
    await state.set_state(AdminContactStates.selecting_user)

@dp.callback_query(F.data.regexp(r"^admin_contact_pick_(\d+)$"))
async def admin_contact_pick_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)

    uid = int(callback.data.split("_")[-1])
    users = load_paid_users()
    rec = users.get(str(uid)) or {}
    uname = rec.get("username", "unknown")
    ver = rec.get("verified", False)

    text = (
        "👤 <b>Пользователь выбран</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Username: @{uname}\n"
        f"Статус: {'✅ покупатель' if ver else '❔ не подтверждён'}\n\n"
        "Выберите действие:"
    )
    await callback.message.edit_text(text, reply_markup=kb_admin_contact_user(uid), parse_mode="HTML")
    await state.update_data(target_id=uid)

@dp.callback_query(F.data.regexp(r"^admin_msg_once_(\d+)$"))
async def admin_msg_once_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)

    uid = int(callback.data.split("_")[-1])
    await state.set_state(AdminContactStates.composing_once)
    await state.update_data(target_id=uid)
    await callback.message.edit_text(
        f"✍️ Напишите сообщение (можно с медиа), которое отправим пользователю <code>{uid}</code> один раз.",
        reply_markup=kb_admin_back(), parse_mode="HTML"
    )

@dp.message(AdminContactStates.composing_once)
async def admin_send_once(message: types.Message, state: FSMContext):
    """Админ отправляет разовое сообщение выбранному пользователю."""
    if message.from_user.id != ADMIN_ID:
        return
    target_id = (await state.get_data()).get("target_id")
    if not target_id:
        await state.clear()
        await message.answer("⚠️ Не выбран получатель. Откройте: Админ → 👤 Связаться")
        return

    caption = (message.caption or message.text or "").strip() or " "
    try:
        if message.photo:
            await bot.send_photo(target_id, message.photo[-1].file_id, caption=caption, parse_mode="HTML")
        elif message.document:
            await bot.send_document(target_id, message.document.file_id, caption=caption, parse_mode="HTML")
        elif message.video:
            await bot.send_video(target_id, message.video.file_id, caption=caption, parse_mode="HTML")
        elif message.animation:
            await bot.send_animation(target_id, message.animation.file_id, caption=caption, parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(target_id, message.audio.file_id, caption=caption, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(target_id, message.voice.file_id, caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(target_id, caption, parse_mode="HTML")
        await message.answer(f"✅ Отправлено пользователю <code>{target_id}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки пользователю {target_id}: {e}")
    await state.clear()

def kb_admin_chat_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Завершить диалог", callback_data="admin_chat_end")],
        [InlineKeyboardButton(text="↩️ В админ-панель", callback_data="admin_home")]
    ])

@dp.callback_query(F.data.regexp(r"^admin_chat_enter_(\d+)$"))
async def admin_chat_enter_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)

    uid = int(callback.data.split("_")[-1])
    # завершаем предыдущий чат, если был
    old_user = _active_admin_chats.get(ADMIN_ID)
    if old_user:
        _active_admin_chats.pop(ADMIN_ID, None)
        _active_user_chats.pop(old_user, None)

    # устанавливаем новое сопоставление
    _active_admin_chats[ADMIN_ID] = uid
    _active_user_chats[uid] = ADMIN_ID

    await state.set_state(AdminContactStates.chatting)
    await state.update_data(target_id=uid)

    await callback.message.edit_text(
        f"💬 Диалог с пользователем <code>{uid}</code> открыт.\nПиши сюда — сообщения будут пересылаться.",
        reply_markup=kb_admin_chat_controls(), parse_mode="HTML"
    )
    # уведомим пользователя
    try:
        await bot.send_message(uid, "👨‍💼 Поддержка подключилась к чату. Можете писать здесь.")
    except Exception:
        pass

@dp.callback_query(F.data == "admin_chat_end")
async def admin_chat_end_cb(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return

    await _safe_cb_answer(callback, "Диалог закрыт")

    uid = _active_admin_chats.pop(ADMIN_ID, None)
    if uid:
        _active_user_chats.pop(uid, None)
        # клиенту — меню клиента
        with suppress(Exception):
            await bot.send_message(
                uid,
                "✅ Диалог с администратором завершён.",
                reply_markup=_menu_kb_for(uid),
                parse_mode="HTML"
            )

    await state.clear()

    # админу — меню админа (без перехода в админ-панель автоматически)
    try:
        await callback.message.edit_text(
            "⛔ Диалог закрыт.",
            reply_markup=_menu_kb_for(ADMIN_ID),
            parse_mode="HTML"
        )
    except Exception:
        with suppress(Exception):
            await bot.send_message(
                callback.message.chat.id,
                "⛔ Диалог закрыт.",
                reply_markup=_menu_kb_for(ADMIN_ID),
                parse_mode="HTML"
            )

# Админ в активном диалоге → релеим ТЕКСТ (не команды)
@dp.message(AdminContactStates.chatting, F.text & ~F.text.startswith("/"))
async def admin_chat_relay_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    uid = _active_admin_chats.get(ADMIN_ID)
    if not uid:
        await state.clear()
        await message.answer("⚠️ Диалог не активен. Открой: Админ → 👤 Связаться → «Войти в диалог».")
        return
    await bot.send_message(uid, message.text, parse_mode="HTML")

# Админ в активном диалоге → релеим МЕДИА
@dp.message(AdminContactStates.chatting, F.photo | F.document | F.video | F.animation | F.audio | F.voice)
async def admin_chat_relay_media(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    uid = _active_admin_chats.get(ADMIN_ID)
    if not uid:
        await state.clear()
        await message.answer("⚠️ Диалог не активен.")
        return

    caption = (message.caption or "").strip() or None
    try:
        if message.photo:
            await bot.send_photo(uid, message.photo[-1].file_id, caption=caption, parse_mode="HTML")
        elif message.document:
            await bot.send_document(uid, message.document.file_id, caption=caption, parse_mode="HTML")
        elif message.video:
            await bot.send_video(uid, message.video.file_id, caption=caption, parse_mode="HTML")
        elif message.animation:
            await bot.send_animation(uid, message.animation.file_id, caption=caption, parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(uid, message.audio.file_id, caption=caption, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(uid, message.voice.file_id, caption=caption, parse_mode="HTML")
    except Exception as e:
        logging.exception("Admin relay media error: %s", e)
        await message.answer(f"❌ Ошибка отправки: {e}")

# Пользователь → админ: ТЕКСТ (если есть активный канал)
@dp.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def user_chat_relay_text(message: types.Message, state: FSMContext):
    """
    Обработка текстовых сообщений вне состояний:
    - если пользователь пишет администратору (активный диалог) → пересылаем админу
    - если запущена поддержка → передаём в process_support_message
    - если нет — предлагаем поддержку или демо-ИИ
    """
    # не обрабатываем сообщения самого админа
    if message.from_user.id == ADMIN_ID:
        return

    uid = message.from_user.id
    admin = _active_user_chats.get(uid)

    # если есть активный диалог — просто пересылаем админу
    if admin:
        try:
            await bot.send_message(
                admin,
                f"👤 <b>От пользователя {uid}</b>\n\n{message.text}",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning("Relay user text error: %s", e)
        return

    # если нет активного диалога — проверяем состояния
    current = await state.get_state()
    if current == SupportStates.waiting_text:
        await process_support_message(message, state)
        return

    # ---- обработка триггеров ----
    txt = (message.text or "").strip().lower()

    # ИИ-команда без кнопки (например, "ии", "ai", "чат")
    ai_triggers = ("ai", "ии", "помощник", "ассистент", "чат")
    if any(txt == t or txt.startswith(t + " ") for t in ai_triggers):
        await state.set_state(AIChatStates.chatting)
        await state.update_data(ai_is_admin=False)

        verified = is_user_verified(uid)
        if verified:
            await message.answer(
                "🤖 Готов к диалогу. Спроси про материалы, запуск или маркетинг.",
                reply_markup=kb_ai_chat(is_admin=False),
                parse_mode="HTML"
            )
        else:
            if DEMO_AI_ENABLED:
                await state.update_data(ai_force_demo=True)
                await message.answer(
                    f"🤖 Демо-режим ИИ включён. Доступно до {DEMO_AI_DAILY_LIMIT} сообщений в день.\n"
                    "Спросите что-нибудь про набор или запуск.",
                    reply_markup=kb_ai_chat(is_admin=False),
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    "⚠️ Демо-режим временно отключён. Для полного доступа оформите покупку.",
                    reply_markup=_menu_kb_for(message.from_user.id), parse_mode="HTML"
                )
        return

    # триггеры поддержки
    support_triggers = ("поддержка", "support", "help", "менеджер")
    if any(txt == t or txt.startswith(t + " ") for t in support_triggers):
        await message.answer(
            "💬 Напишите сообщение — передам в поддержку.",
            reply_markup=kb_back_main(), parse_mode="HTML"
        )
        await state.set_state(SupportStates.waiting_text)
        return

    # ---- если ничего не подошло ----
    if is_user_verified(uid):
        await message.answer(
            "💬 Нужна помощь? Напишите «поддержка» или нажмите кнопку ниже:",
            reply_markup=_menu_kb_for(message.from_user.id),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "👋 Доступ к файлам появится после подтверждения оплаты.\n"
            "А пока можно попробовать 🤖 демо-чат с ИИ (кнопка ниже).",
            reply_markup=_menu_kb_for(message.from_user.id),
            parse_mode="HTML"
        )
 
    # если есть активный диалог с админом — просто релеим ему
    try:
        await bot.send_message(
            admin,
            f"👤 <b>От пользователя {message.from_user.id}</b>\n\n{message.text}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning("Relay user text error: %s", e)

# Пользователь → админ: МЕДИА
@dp.message(StateFilter(None), F.photo | F.document | F.video | F.animation | F.audio | F.voice)
async def user_chat_relay_media(message: types.Message, state: FSMContext):
    # ⛔ Если ждём скрин оплаты — не перехватываем!
    current = await state.get_state()
    if current == PaymentStates.waiting_screenshot:
        return

    if message.from_user.id == ADMIN_ID:
        return

    admin = _active_user_chats.get(message.from_user.id)
    if not admin:
        return
    caption = (message.caption or "").strip() or ""
    try:
        if message.photo:
            await bot.send_photo(
                admin, message.photo[-1].file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
        elif message.document:
            await bot.send_document(
                admin, message.document.file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
        elif message.video:
            await bot.send_video(
                admin, message.video.file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
        elif message.animation:
            await bot.send_animation(
                admin, message.animation.file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
        elif message.audio:
            await bot.send_audio(
                admin, message.audio.file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
        elif message.voice:
            await bot.send_voice(
                admin, message.voice.file_id,
                caption=f"👤 От пользователя {message.from_user.id}\n\n{caption}",
                parse_mode="HTML"
            )
    except Exception as e:
        logging.warning("Relay to admin error: %s", e)

@dp.message(F.photo)
async def _guard_photo_to_payment(message: types.Message, state: FSMContext):
    if await state.get_state() == PaymentStates.waiting_screenshot:
        await process_screenshot(message, state)        

# ---------------------------
# ИИ-ЧАТ (состояние)
# ---------------------------
async def _safe_send_answer(msg: types.Message, text: str, markup=None):
    """Отправка с HTML и безопасным фоллбеком в plain-текст при ошибках парсинга."""
    try:
        await msg.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logging.warning("send with HTML failed: %s; fallback to plain", e)
        plain = re.sub(r"<[^>]+>", "", text or "")
        await msg.answer(plain, reply_markup=markup)

@dp.message(AIChatStates.chatting, F.text & ~F.text.startswith("/"))
async def ai_chat_handler(message: types.Message, state: FSMContext):
    logging.info("[AI-HANDLER] enter uid=%s text_len=%s", message.from_user.id, len(message.text or ""))
    data = await state.get_data()
    is_admin = bool(data.get("ai_is_admin"))
    uid = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    verified = is_user_verified(uid)
    is_demo = (not verified) and DEMO_AI_ENABLED and (not is_admin)

    # демо-лимиты
    if is_demo:
        ok, reason = _demo_quota_ok(uid)
        if not ok:
            logging.info("[AI-HANDLER] demo quota blocked uid=%s reason=%s", uid, reason)
            await _safe_send_answer(message, "⚠️ " + reason, _menu_kb_for(message.from_user.id))
            return

    # короче история в демо
    desired_hist = max(2, min(6, AI_MAX_HISTORY)) if is_demo else None
    _push_history(uid, is_admin, "user", user_text, desired=desired_hist)

    # строим сообщения (ВАЖНО: до try/except, чтобы msgs был определён)
    msgs = _build_messages(uid, is_admin, user_text, is_demo=is_demo)

    # «печатает…»; если ChatActionSender упадёт — не мешаем ответу
    with suppress(Exception):
        await bot.send_chat_action(message.chat.id, "typing")

    try:
        logging.info("[AI-HANDLER] call model=%s demo=%s admin=%s", OPENAI_MODEL, is_demo, is_admin)
        reply = await (_ai_complete_demo(uid, is_admin, msgs) if is_demo
                       else _ai_complete(uid, is_admin, user_text))
    except Exception as e:
        logging.warning("ChatActionSender/AI call failed, fallback: %s", e)
        # на всякий случай делаем прямой повтор запроса
        reply = await (_ai_complete_demo(uid, is_admin, msgs) if is_demo
                       else _ai_complete(uid, is_admin, user_text))

    _push_history(uid, is_admin, "assistant", reply, desired=desired_hist)

    suffix = ""
    if is_demo and not verified:
        suffix = (
            "\n\n—\n<i>Это демо-режим (есть лимит по сообщениям). "
            "Чтобы получить полный доступ и файлы, нажмите «Оплата по СБП (QR)».</i>"
        )

    logging.info("[AI-HANDLER] reply_len=%s", len(reply or ""))
    await _safe_send_answer(
        message,
        (reply or "⚠️ Пустой ответ.") + suffix,
        kb_ai_chat(is_admin=is_admin) if verified else _menu_kb_for(message.from_user.id)
    )

    if is_demo:
        _demo_register_hit(uid)

@dp.message(Command("ai_diag"))
async def ai_diag(message: types.Message):
    # только админу
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return

    info = [
        f"BASE_URL: {OPENAI_BASE_URL}",
        f"MODEL: {OPENAI_MODEL}",
        f"KEY: {'set' if bool(OPENAI_API_KEY) else 'MISSING'}",
    ]
    await message.answer("🔎 AI DIAG:\n" + "\n".join(info))

    if not OPENAI_API_KEY:
        await message.answer("⚠️ В .env не задан OPENAI_API_KEY"); return

    try:
        payload = {
            "model": OPENAI_MODEL,
            "messages": [{"role": "system", "content": "ping"}, {"role": "user", "content": "ping"}],
            "temperature": 0.0
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers_for_openai()) as s:
            async with s.post(f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions", json=payload) as resp:
                txt = await resp.text()
                await message.answer(f"HTTP {resp.status}\n{txt[:800]}")
    except Exception as e:
        await message.answer(f"❌ EXC: {e}")


@dp.message(Command("ai"))
async def ai_open_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    verified = is_user_verified(uid)

    await state.set_state(AIChatStates.chatting)
    await state.update_data(ai_is_admin=False)

    if verified:
        await message.answer(
            "🤖 Готов к диалогу. Задайте вопрос по набору, оплате или запуску.",
            reply_markup=kb_ai_chat(is_admin=False)
        )
    else:
        await message.answer(
            f"🧪 Демо-режим: до {DEMO_AI_DAILY_LIMIT} сообщений/день, пауза {DEMO_AI_COOLDOWN_SEC} сек.\n"
            "Задайте вопрос — отвечу кратко и по делу.",
            reply_markup=kb_ai_chat(is_admin=False)
        )

# ---------------------------
# «НАЗАД В ГЛАВНОЕ»
# ---------------------------
@dp.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)
    await state.clear()

    uid = callback.from_user.id
    verified = is_user_verified(uid)

    sales_text = (
        "👋 <b>Добро пожаловать в AI Business Kit</b>\n\n"
        "🚀 Что вы получите:\n"
        "• 100 ChatGPT-промптов для бизнеса\n"
        "• Шаблон Telegram-бота с CRM\n"
        "• Пошаговое руководство по запуску (10 минут)\n\n"
        "💵 <b>Стоимость:</b> 3 500 ₽ (разовый платёж)\n\n"
        "Как получить:\n"
        "1) Нажмите «Оплата по СБП (QR)» и оплатите\n"
        "2) Нажмите «Я оплатил(а)»\n"
        "3) Отправьте скриншот чека для подтверждения\n\n"
        "⏱ Проверка занимает обычно 5–15 минут"
    )

    # ✅ умное меню
    kb = _menu_kb_for(uid)
    text = _verified_home_text() if verified else sales_text

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(callback.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

# ---------------------------
# АДМИН: /admin + общий рендер панели
# ---------------------------
def _render_admin_home_text() -> str:
    users = load_paid_users()
    verified = [u for u in users.values() if isinstance(u, dict) and u.get("verified")]
    return (
        "👑 <b>Панель администратора</b>\n\n"
        f"💰 Подтвержденных: {len(verified)}\n"
        f"👥 Всего записей: {len(users)}\n"
        f"🎯 Конверсия: {len(verified)/max(len(users),1)*100:.1f}%\n"
    )

async def _go_admin_home(chat_id: int, as_edit: Optional[types.Message] = None):
    text = _render_admin_home_text()
    if as_edit:
        try:
            await as_edit.edit_text(text, reply_markup=kb_admin_panel(), parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb_admin_panel(), parse_mode="HTML")

@dp.message(Command("admin"))
async def admin_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    await message.answer(_render_admin_home_text(), reply_markup=kb_admin_panel(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_home")
async def admin_home_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return
    await _safe_cb_answer(callback)
    await _go_admin_home(callback.message.chat.id, as_edit=callback.message)

@dp.message(Command("back_admin"))
async def back_admin_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    await _go_admin_home(message.chat.id)

@dp.message(F.text.regexp(r"^(назад( в админку)?|в админку|админ|admin)$", flags=re.IGNORECASE))
async def back_admin_text(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await _go_admin_home(message.chat.id)

# ---------------------------
# БАЗОВЫЕ АДМИН-КОМАНДЫ (очистка/backup/удаление/списки/экспорт)
# ---------------------------
@dp.message(Command("clear_db"))
async def clear_db_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    backup_file = backup_database()
    clear_database()
    text = "🗑️ <b>База очищена.</b>"
    if backup_file:
        text += f"\n💾 Backup: <code>{os.path.basename(backup_file)}</code>"
    await message.answer(text, reply_markup=kb_admin_back(), parse_mode="HTML")

@dp.message(Command("backup"))
async def backup_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    backup_file = backup_database()
    if backup_file:
        await message.answer(
            f"💾 <b>Backup:</b> <code>{os.path.basename(backup_file)}</code>",
            reply_markup=kb_admin_back(), parse_mode="HTML"
        )
    else:
        await message.answer("❌ Ошибка при создании backup", reply_markup=kb_admin_back())

@dp.message(Command("remove_user"))
async def remove_user_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    try:
        user_id = int(message.text.split()[1])
    except Exception:
        await message.answer("Использование: <code>/remove_user USER_ID</code>", parse_mode="HTML")
        return
    ok = remove_user(user_id)
    await message.answer("✅ Удалён" if ok else "❌ Не найден", reply_markup=kb_admin_back())

@dp.message(Command("buyers"))
async def buyers_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    users = load_paid_users()
    verified = [(uid, u) for uid, u in users.items() if isinstance(u, dict) and u.get("verified")]
    if not verified:
        await message.answer("📭 Пока нет подтверждённых покупателей.")
        return
    lines = ["👥 <b>Покупатели</b> (первые 70):\n"]
    for uid, u in verified[:70]:
        line = f"✅ @{u.get('username','unknown')} | ID: {uid}"
        if u.get("purchase_date"):
            line += f" | {u['purchase_date'][:16]}"
        lines.append(line)
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("export_buyers"))
async def export_buyers_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    users = load_paid_users()
    verified = [(uid, u) for uid, u in users.items() if isinstance(u, dict) and u.get("verified")]
    if not verified:
        await message.answer("📭 Нет данных для экспорта."); return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["user_id", "username", "purchase_date"])
    for uid, u in verified:
        writer.writerow([uid, u.get("username",""), u.get("purchase_date","")])
    data = output.getvalue().encode("utf-8")
    output.close()

    await bot.send_document(
        message.chat.id,
        document=types.BufferedInputFile(data, filename=f"buyers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"),
        caption="📦 Экспорт покупателей (CSV)",
    )

# Те же функции через кнопки:
@dp.callback_query(F.data == "admin_buyers")
async def admin_buyers_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    users = load_paid_users()
    verified = [(uid, u) for uid, u in users.items() if isinstance(u, dict) and u.get("verified")]
    if not verified:
        await callback.message.edit_text("📭 Пока нет подтверждённых покупателей.", reply_markup=kb_admin_back())
        return

    lines = ["👥 <b>Покупатели</b> (первые 70):\n"]
    for uid, u in verified[:70]:
        line = f"✅ @{u.get('username','unknown')} | ID: {uid}"
        if u.get("purchase_date"):
            line += f" | {u['purchase_date'][:16]}"
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n... (обрезано)"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_back())

@dp.callback_query(F.data == "admin_export_buyers")
async def admin_export_buyers_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    users = load_paid_users()
    verified = [(uid, u) for uid, u in users.items() if isinstance(u, dict) and u.get("verified")]
    if not verified:
        await callback.message.edit_text("📭 Нет данных для экспорта.", reply_markup=kb_admin_back())
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["user_id", "username", "purchase_date"])
    for uid, u in verified:
        writer.writerow([uid, u.get("username",""), u.get("purchase_date","")])
    data = output.getvalue().encode("utf-8")
    output.close()

    await bot.send_document(
        callback.message.chat.id,
        document=types.BufferedInputFile(data, filename=f"buyers_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"),
        caption="📦 Экспорт покупателей (CSV)"
    )

@dp.callback_query(F.data == "admin_reply_prompt")
async def admin_reply_prompt_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    text = (
        "✉️ <b>Ответ пользователю</b>\n\n"
        "Способы отправки:\n"
        "• Команда: <code>/reply USER_ID Текст</code>\n"
        "• Ответом на уведомление от бота: <code>/reply Текст</code>\n"
        "• Можно прикреплять медиа и подпись — всё уйдёт пользователю.\n\n"
        "Примеры:\n"
        "<code>/reply 641521378 Привет! Ваш доступ открыт ✅</code>\n"
        "<code>(Reply на уведомление) /reply Спасибо за обращение!</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_back())

# ---------------------------
# ПРИВЯЗКА FILE_ID через Telegram
# ---------------------------
@dp.message(Command("bind_sbp_qr"))
async def bind_sbp_qr_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    if not message.reply_to_message:
        await message.answer("Ответьте этой командой на сообщение с изображением QR СБП (фото/документ)."); return
    file_id = None
    if message.reply_to_message.photo:
        file_id = message.reply_to_message.photo[-1].file_id
    elif message.reply_to_message.document:
        file_id = message.reply_to_message.document.file_id
    if not file_id:
        await message.answer("Нужна картинка или документ с QR."); return
    d = _load_assets()
    d["sbp_qr"] = {"file_id": file_id, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _save_assets(d)
    await message.answer("✅ QR СБП привязан по file_id. Теперь будет использоваться кэш.")

@dp.message(Command("bind_prompts"))
async def bind_prompts_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer("Ответьте этой командой на <b>документ</b> с PDF промптов.", parse_mode="HTML")
        return
    file_id = message.reply_to_message.document.file_id
    set_asset_file_id("prompts", file_id)
    await message.answer("✅ Промпты привязаны по file_id. Теперь будет использоваться кэшированный file_id.")

@dp.message(Command("bind_guide"))
async def bind_guide_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer("Ответьте этой командой на <b>документ</b> с PDF-руководством.", parse_mode="HTML")
        return
    file_id = message.reply_to_message.document.file_id
    set_asset_file_id("guide", file_id)
    await message.answer("✅ Гайд привязан по file_id. Теперь будет использоваться кэшированный file_id.")


@dp.message(Command("bind_presentation"))
async def bind_presentation_cmd(message: types.Message):
    """Привязка PDF-презентации по file_id (ответом на сообщение с документом)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer("Ответьте этой командой на <b>документ</b> с PDF-презентацией.", parse_mode="HTML")
        return
    file_id = message.reply_to_message.document.file_id
    set_asset_file_id("presentation", file_id)
    await message.answer("✅ Презентация привязана по file_id. Теперь будет использоваться кэшированный file_id.")

@dp.message(Command("bind_bot"))
async def bind_bot_cmd(message: types.Message):
    """Привязка шаблонного бота по file_id (ответом на документ bot_template.py)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return

    # Команду нужно отправлять ответом на сообщение с документом
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer(
            "Ответьте этой командой на <b>документ</b> с файлом <code>bot_template.py</code>.",
            parse_mode="HTML"
        )
        return

    doc = message.reply_to_message.document
    file_id = doc.file_id

    # (мягкая) проверка имени файла — не блокируем, просто предупреждаем
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".py"):
        await message.answer("⚠️ Похоже, это не .py файл. Всё равно привязываю по file_id — проверьте название.")

    # Сохраняем в глобальный кэш kit_assets.json
    set_asset_file_id("bot_template", file_id)

    await message.answer("✅ Шаблон бота привязан по file_id. Теперь будет использоваться кэшированный file_id.")   

# ---------------------------
# АДМИН-КНОПКИ: очистка/статы/backup/все пользователи
# ---------------------------
@dp.callback_query(F.data == "clear_all")
async def cb_clear_all(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    backup_file = backup_database()
    clear_database()
    txt = "🗑️ База данных очищена."
    if backup_file:
        txt += f"\n💾 Backup: <code>{os.path.basename(backup_file)}</code>"
    await callback.message.edit_text(txt, reply_markup=kb_admin_back(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    users = load_paid_users()
    verified = [u for u in users.values() if isinstance(u, dict) and u.get("verified")]
    txt = (
        "📊 <b>Статистика</b>\n\n"
        f"💰 Подтверждено: {len(verified)}\n"
        f"👥 Всего: {len(users)}\n"
        f"🎯 Конверсия: {len(verified)/max(len(users),1)*100:.1f}%"
    )
    await callback.message.edit_text(txt, reply_markup=kb_admin_back(), parse_mode="HTML")

@dp.callback_query(F.data == "create_backup")
async def cb_create_backup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    backup_file = backup_database()
    if backup_file:
        await callback.message.edit_text(
            f"💾 Backup: <code>{os.path.basename(backup_file)}</code>",
            reply_markup=kb_admin_back(), parse_mode="HTML"
        )
    else:
        await callback.message.edit_text("❌ Ошибка при создании backup", reply_markup=kb_admin_back())

@dp.callback_query(F.data == "list_users")
async def cb_list_users(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    users = load_paid_users()
    if not users:
        await callback.message.edit_text("📭 База пустая", reply_markup=kb_admin_back())
        return

    lines = ["👥 <b>Пользователи</b>\n"]
    for uid, u in list(users.items())[:80]:
        mark = "✅" if u.get("verified") else "❌"
        line = f"{mark} @{u.get('username','unknown')} | ID: {uid}"
        if u.get("purchase_date"):
            line += f" | {u['purchase_date'][:16]}"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (обрезано)"
    await callback.message.edit_text(text, reply_markup=kb_admin_back(), parse_mode="HTML")

# ---------------------------
# ПОДТВЕРЖДЕНИЕ ОПЛАТЫ (flow)
# ---------------------------
VERIFICATION_TEXT = (
    "📸 <b>Подтверждение оплаты</b>\n\n"
    "Отправьте <b>скриншот чека оплаты</b> по СБП. Должны быть видны:\n"
    "• Дата и время платежа\n"
    "• Сумма (3500₽) и статус (успешно/выполнено)\n"
    "• Получатель\n"
    "• Комментарий с вашим <b>Order#</b> (если поле доступно в банке)\n\n"
    "Если у вас ещё нет QR — нажмите «Оплата по СБП (QR)» в меню.\n"
)

@dp.callback_query(F.data == "request_verification")
async def request_verification_handler(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)
    uid = callback.from_user.id
    uname = callback.from_user.username or "без_username"
    save_pending_user(uid, uname)

    data_prev = await state.get_data()
    order_id = data_prev.get("order_id") or _gen_order_id()
    await state.set_state(PaymentStates.waiting_screenshot)
    await state.update_data(user_id=uid, username=uname, is_support=False, order_id=order_id)

    await callback.message.edit_text(
        VERIFICATION_TEXT,
        reply_markup=kb_verification_back(),
        parse_mode="HTML"
    )
    await state.set_state(PaymentStates.waiting_screenshot)
    await state.update_data(user_id=uid, username=uname, is_support=False)

@dp.message(PaymentStates.waiting_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    """Пользователь прислал скрин — отправляем администратору на подтверждение (с Order#)."""
    data = await state.get_data()

    # Если state переиспользуем для поддержки — не трогаем логику
    if data.get("is_support"):
        await process_support_message(message, state)
        return

    # Достаём данные
    user_id  = data.get("user_id")  or message.from_user.id
    username = data.get("username") or (message.from_user.username or "без_username")
    order_id = data.get("order_id") or _gen_order_id()

    # Подстрахуемся, что юзер записан
    save_pending_user(user_id, username)

    # Кнопки для админа
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить",  callback_data=f"reject_{user_id}")
    ]])

    # Отправляем админу
    try:
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=(
                "📸 <b>ЗАПРОС ПОДТВЕРЖДЕНИЯ</b>\n\n"
                f"@{username}\n"
                f"ID: {user_id}\n"
                f"Order: {order_id}\n"
                f"{datetime.now().strftime('%H:%M %d.%m.%Y')}"
            ),
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning("Forward screenshot to admin failed: %s", e)
        await message.answer("❌ Не удалось отправить скрин на проверку. Попробуйте ещё раз или свяжитесь с поддержкой.")
        return

    # ✅ ОДНО уведомление пользователю + ОДИН clear()
    await message.answer(
        "✅ Скрин загружен и отправлен на проверку.\n"
        "Мы уведомим вас, как только доступ будет открыт.",
        reply_markup=kb_back_main(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment_handler(callback: types.CallbackQuery):
    """Админ подтвердил оплату — отмечаем пользователя и выдаём материалы.
       Дополнительно: отправляем админу отдельное уведомление о результате выдачи."""
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return

    await _safe_cb_answer(callback)

    # кто одобрен
    user_id = int(callback.data.split("_")[1])
    users = load_paid_users()
    username = users.get(str(user_id), {}).get("username", "unknown")

    # отмечаем оплаченным
    save_paid_user(user_id, username)

    # уведомляем пользователя
    await bot.send_message(
        user_id,
        "🎉 <b>Оплата подтверждена!</b>\nОтправляю материалы…",
        parse_mode="HTML"
    )

    # выдаём файлы + уведомляем администратора о факте выдачи
    try:
        await send_files_to_user(user_id)

        # 1) отдельное новое уведомление админу (не редактирование)
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Выдано</b>\nПользователь @{username} (ID: <code>{user_id}</code>) получил все материалы.",
            parse_mode="HTML",
            reply_markup=kb_admin_back()
        )

        # 2) обновляем исходное сообщение с кнопками, если возможно
        with suppress(Exception):
            await callback.message.edit_text(
                f"✅ Подтверждено. Пользователь @{username} получил файлы.",
                reply_markup=kb_admin_back(), parse_mode="HTML"
            )

    except Exception as e:
        logging.exception("send_files_to_user failed: %s", e)

        # сообщаем админу о проблеме
        await bot.send_message(
            ADMIN_ID,
            (
                "⚠️ <b>Не удалось выслать файлы</b>\n"
                f"Пользователь @{username} (ID: <code>{user_id}</code>)\n"
                f"Ошибка: <code>{e}</code>"
            ),
            parse_mode="HTML",
            reply_markup=kb_admin_back()
        )

        with suppress(Exception):
            await callback.message.edit_text(
                "⚠️ Оплата подтверждена, но при отправке файлов произошла ошибка — проверьте логи.",
                reply_markup=kb_admin_back(), parse_mode="HTML"
            )

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment_handler(callback: types.CallbackQuery):
    """Админ отклонил — уведомляем пользователя."""
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    user_id = int(callback.data.split("_")[1])
    await bot.send_message(
        user_id,
        "❌ <b>Оплата не подтверждена</b>\n"
        "Проверьте, чтобы на скриншоте были видны дата, сумма и статус платежа. "
        "Попробуйте отправить более чёткое подтверждение.",
        parse_mode="HTML"
    )
    try:
        await callback.message.edit_text(
            "❌ Запрос отклонён. Пользователь уведомлён.",
            reply_markup=kb_admin_back(), parse_mode="HTML"
        )
    except Exception:
        pass

# ---------------------------
# БЫСТРЫЙ ОТВЕТ АДМИНА (/reply + кнопка «✉️»)
# ---------------------------
USER_ID_RE = re.compile(r"ID:\s*(\d+)")

def _extract_user_id_from_reply(msg: types.Message) -> Optional[int]:
    """Пытаемся вытащить ID пользователя из текста/подписи исходного уведомления."""
    if not msg.reply_to_message:
        return None
    src = (msg.reply_to_message.text or "") + "\n" + (msg.reply_to_message.caption or "")
    m = USER_ID_RE.search(src)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def _split_reply_command(message: types.Message) -> Tuple[Optional[int], Optional[str]]:
    """
    Разбираем команду /reply:
      /reply USER_ID Текст
      (reply-на уведомление) /reply Текст
    Возвращаем (user_id, текст).
    """
    raw = (message.text or message.caption or "").strip()
    raw = re.sub(r"^/reply(@\w+)?\s*", "", raw, flags=re.IGNORECASE)
    m = re.match(r"^(\d+)\s+(.*)$", raw, flags=re.S)
    if m:
        return int(m.group(1)), (m.group(2) or "").strip() or None
    return _extract_user_id_from_reply(message), (raw or "").strip() or None

@dp.callback_query(F.data.startswith("admin_quick_reply_"))
async def admin_quick_reply_start(callback: types.CallbackQuery, state: FSMContext):
    """Старт «быстрого ответа» по кнопке ✉️ из уведомления."""
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    target_id = int(callback.data.split("_")[-1])
    await state.set_state(_ReplyStates.waiting)
    await state.update_data(target_id=target_id)
    await callback.message.reply(
        f"✍️ Введите текст/медиа ответа пользователю <code>{target_id}</code>.",
        parse_mode="HTML"
    )

@dp.message(_ReplyStates.waiting)
async def admin_quick_reply_send(message: types.Message, state: FSMContext):
    """Отправляем ответ пользователю (поддерживаются медиа)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return

    target_id = (await state.get_data()).get("target_id")
    if not target_id:
        await state.clear()
        await message.answer("⚠️ Не найден получатель. Запустите из уведомления ещё раз.")
        return

    caption = (message.caption or message.text or "").strip() or " "
    try:
        if message.photo:
            await bot.send_photo(target_id, message.photo[-1].file_id, caption=caption, parse_mode="HTML")
        elif message.document:
            await bot.send_document(target_id, message.document.file_id, caption=caption, parse_mode="HTML")
        elif message.video:
            await bot.send_video(target_id, message.video.file_id, caption=caption, parse_mode="HTML")
        elif message.animation:
            await bot.send_animation(target_id, message.animation.file_id, caption=caption, parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(target_id, message.audio.file_id, caption=caption, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(target_id, message.voice.file_id, caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(target_id, "💬 <b>Ответ поддержки</b>\n\n" + caption, parse_mode="HTML")
        await message.answer(f"✅ Ответ отправлен (ID: <code>{target_id}</code>)", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки пользователю {target_id}: {e}")

    await state.clear()

@dp.message(Command("reply"))
async def admin_reply_handler(message: types.Message):
    """Команда /reply: /reply USER_ID Текст или (reply) /reply Текст (+медиа в подписи)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return

    target_id, reply_text = _split_reply_command(message)
    if not target_id:
        await message.answer(
            "Использование:\n"
            "<code>/reply USER_ID Текст</code>\n"
            "или ответом на уведомление: <code>/reply Текст</code>\n"
            "Поддерживаются медиа в подписи.", parse_mode="HTML")
        return
    try:
        if message.photo:
            await bot.send_photo(target_id, message.photo[-1].file_id, caption=reply_text or " ", parse_mode="HTML")
        elif message.document:
            await bot.send_document(target_id, message.document.file_id, caption=reply_text or " ", parse_mode="HTML")
        elif message.video:
            await bot.send_video(target_id, message.video.file_id, caption=reply_text or " ", parse_mode="HTML")
        elif message.animation:
            await bot.send_animation(target_id, message.animation.file_id, caption=reply_text or " ", parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(target_id, message.audio.file_id, caption=reply_text or " ", parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(target_id, message.voice.file_id, caption=reply_text or " ", parse_mode="HTML")
        else:
            if not reply_text:
                await message.answer("❗ Укажите текст после /reply"); return
            await bot.send_message(target_id, "💬 <b>Ответ поддержки</b>\n\n" + reply_text, parse_mode="HTML")
        await message.answer(f"✅ Ответ отправлен (ID: <code>{target_id}</code>)", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить пользователю {target_id}: {e}")

USER_ID_RE = re.compile(r"ID:\s*(\d+)")  # у тебя уже есть — оставь одну копию

@dp.message(F.reply_to_message, ~F.text.startswith("/"))
async def admin_reply_by_reply(message: types.Message):
    """
    Админ отвечает на уведомление (reply) — отправляем это пользователю без /reply.
    Работает для текста и любого медиа.
    """
    if message.from_user.id != ADMIN_ID:
        return
    src = (message.reply_to_message.text or "") + "\n" + (message.reply_to_message.caption or "")
    m = USER_ID_RE.search(src)
    if not m:
        return  # не похоже на наше служебное уведомление (нет "ID: <num>")

    try:
        target_id = int(m.group(1))
    except Exception:
        return

    caption = (message.caption or message.text or "").strip() or " "

    try:
        if message.photo:
            await bot.send_photo(target_id, message.photo[-1].file_id, caption=caption, parse_mode="HTML")
        elif message.document:
            await bot.send_document(target_id, message.document.file_id, caption=caption, parse_mode="HTML")
        elif message.video:
            await bot.send_video(target_id, message.video.file_id, caption=caption, parse_mode="HTML")
        elif message.animation:
            await bot.send_animation(target_id, message.animation.file_id, caption=caption, parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(target_id, message.audio.file_id, caption=caption, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(target_id, message.voice.file_id, caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(target_id, "💬 <b>Ответ поддержки</b>\n\n" + caption, parse_mode="HTML")

        await message.answer(f"✅ Ответ отправлен (ID: <code>{target_id}</code>)", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки пользователю {target_id}: {e}")

# ---------------------------
# РАССЫЛКА (FSM)
# ---------------------------
@dp.message(Command("broadcast"))
async def broadcast_start_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return
    await state.set_state(BroadcastStates.waiting_content)
    await state.update_data(payload=None)
    await message.answer(
        "📣 Отправьте сюда текст/фото/видео/док/аудио/voice/GIF для рассылки.\n"
        "Покажу предпросмотр и попрошу подтвердить.",
        reply_markup=kb_admin_back(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "open_broadcast")
async def broadcast_open_from_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    await state.set_state(BroadcastStates.waiting_content)
    await state.update_data(payload=None)
    await callback.message.edit_text(
        "📣 Отправьте сюда текст/фото/видео/док/аудио/voice/GIF.\n"
        "После — подтверждение перед отправкой.",
        reply_markup=kb_admin_back(), parse_mode="HTML"
    )

def _pack_message_payload(msg: types.Message) -> Dict[str, Any]:
    """Унифицированное представление контента для рассылки."""
    payload: Dict[str, Any] = {"type": "text", "text": (msg.text or msg.caption or "").strip()}
    if msg.photo:
        payload = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": (msg.caption or "").strip()}
    elif msg.document:
        payload = {"type": "document", "file_id": msg.document.file_id, "caption": (msg.caption or "").strip()}
    elif msg.video:
        payload = {"type": "video", "file_id": msg.video.file_id, "caption": (msg.caption or "").strip()}
    elif msg.animation:
        payload = {"type": "animation", "file_id": msg.animation.file_id, "caption": (msg.caption or "").strip()}
    elif msg.audio:
        payload = {"type": "audio", "file_id": msg.audio.file_id, "caption": (msg.caption or "").strip()}
    elif msg.voice:
        payload = {"type": "voice", "file_id": msg.voice.file_id, "caption": (msg.caption or "").strip()}
    return payload

async def _send_preview(chat_id: int, p: Dict[str, Any]):
    """Показываем предпросмотр будущей рассылки админу."""
    t, cap = p.get("type"), p.get("caption") or None
    if t == "photo":
        await bot.send_photo(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    elif t == "document":
        await bot.send_document(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    elif t == "video":
        await bot.send_video(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    elif t == "animation":
        await bot.send_animation(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    elif t == "audio":
        await bot.send_audio(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    elif t == "voice":
        await bot.send_voice(chat_id, p["file_id"], caption=cap, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, p.get("text") or " ", parse_mode="HTML")

@dp.message(BroadcastStates.waiting_content)
async def broadcast_collect_content(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа"); return

    payload = _pack_message_payload(message)
    await state.update_data(payload=payload)
    await _send_preview(message.chat.id, payload)

    await state.set_state(BroadcastStates.confirm_send)
    await message.answer("✅ Предпросмотр выше. Отправляем?", reply_markup=kb_broadcast_confirm())

async def _broadcast_send_to(user_id: int, p: Dict[str, Any]) -> bool:
    """Отправляем один экземпляр рассылки конкретному пользователю."""
    t, cap = p.get("type"), p.get("caption") or None
    try:
        if t == "photo":
            await bot.send_photo(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        elif t == "document":
            await bot.send_document(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        elif t == "video":
            await bot.send_video(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        elif t == "animation":
            await bot.send_animation(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        elif t == "audio":
            await bot.send_audio(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        elif t == "voice":
            await bot.send_voice(user_id, p["file_id"], caption=cap, parse_mode="HTML")
        else:
            await bot.send_message(user_id, p.get("text") or " ", parse_mode="HTML")
        return True
    except Exception as e:
        logging.warning("Broadcast fail to %s: %s", user_id, e)
        return False

@dp.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.", reply_markup=kb_admin_back())

@dp.callback_query(F.data == "broadcast_send")
async def broadcast_do_send(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True); return
    await _safe_cb_answer(callback)

    data    = await state.get_data()
    payload = data.get("payload")
    if not payload:
        await callback.message.edit_text("⚠️ Нет контента. Начните заново: /broadcast",
                                         reply_markup=kb_admin_back())
        await state.clear()
        return

    users = load_paid_users()
    targets: List[int] = []
    for uid, info in users.items():
        if not isinstance(info, dict):
            continue
        if BROADCAST_VERIFIED_ONLY and not info.get("verified"):
            continue
        try:
            targets.append(int(uid))
        except Exception:
            pass

    total, ok, fail = len(targets), 0, 0
    await callback.message.edit_text(f"🚀 Рассылка запущена ({total} получателей)…")
    for uid in targets:
        if await _broadcast_send_to(uid, payload):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.03)  # небольшая пауза, чтобы не ловить flood

    await state.clear()
    await callback.message.edit_text(
        f"📣 Готово.\n\n✅ Успешно: {ok}\n❌ Ошибок: {fail}\n👥 Всего: {total}",
        reply_markup=kb_admin_back()
    )

# ---------------------------
# ВЫДАЧА ФАЙЛОВ (надёжная)
# ---------------------------
# ---------------------------
# ВЫДАЧА ФАЙЛОВ (надёжная)
# ---------------------------
async def _download_bytes_async(url: str) -> Optional[bytes]:
    """Асинхронно скачиваем файл по URL (короткий таймаут)."""
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.read()
        return None
    except Exception as e:
        logging.warning("Download error for %s: %s", url, e)
        return None

async def _send_document_safely(
    chat_id: int,
    file_id_env: str,
    url: str,
    filename: str,
    caption: str,
    cache_key: str,
    file_id_override: Optional[str] = None
):
    """
    Стратегия отправки:
    0) file_id_override (kit_assets.json, /bind_*) — самый приоритетный
    1) file_id из .env
    2) персональный кэш file_id (paid_users.json)
    3) скачать по URL → отправить → закэшировать file_id
    4) fallback: просто отправить ссылку текстом
    """
    # 0) override
    if file_id_override:
        try:
            msg = await bot.send_document(chat_id, file_id_override, caption=caption, parse_mode="HTML")
            return msg
        except Exception as e:
            logging.warning("Override file_id failed (%s): %s", cache_key, e)

    # 1) ENV file_id
    if file_id_env:
        try:
            msg = await bot.send_document(chat_id, file_id_env, caption=caption, parse_mode="HTML")
            return msg
        except Exception as e:
            logging.warning("ENV file_id failed (%s): %s", cache_key, e)

    # 2) кэшированный file_id
    users = load_paid_users()
    rec = users.get(str(chat_id), {})
    cache = rec.get("cache", {})
    file_id_cached = cache.get(cache_key)
    if file_id_cached:
        try:
            msg = await bot.send_document(chat_id, file_id_cached, caption=caption, parse_mode="HTML")
            return msg
        except Exception as e:
            logging.warning("Cached file_id failed (%s): %s", cache_key, e)

    # 3) скачиваем по URL и отправляем как bytes
    if url:
        data = await _download_bytes_async(url)  # <-- тут был синхронный вызов, заменили на await
        if data:
            try:
                msg = await bot.send_document(
                    chat_id,
                    document=types.BufferedInputFile(data, filename=filename),
                    caption=caption,
                    parse_mode="HTML"
                )
                # кэшируем новый file_id
                file_id_new = msg.document.file_id if msg and msg.document else None
                if file_id_new:
                    users = load_paid_users()
                    rec = users.get(str(chat_id), {})
                    rec.setdefault("cache", {})
                    rec["cache"][cache_key] = file_id_new
                    users[str(chat_id)] = rec
                    save_users(users)
                return msg
            except Exception as e:
                logging.warning("Send as bytes failed (%s): %s", cache_key, e)

    # 4) чистая ссылка
    if url:
        await bot.send_message(chat_id, f"{caption}\n{url}", parse_mode="HTML")
    else:
        await bot.send_message(chat_id, f"{caption}\n(файл временно недоступен)", parse_mode="HTML")
    return None

async def send_files_to_user(user_id: int):
    """Комплект выдачи после подтверждения: промпты + презентация + шаблон бота + README (без гайда)."""
    # 1) Промпты (c приоритетом: override -> ENV -> user cache -> URL -> ссылка)
    await _send_document_safely(
        chat_id=user_id,
        file_id_env=PDF_PROMPTS_FILE_ID,
        url=PDF_PROMPTS_URL,
        filename="100_prompts_for_business.pdf",
        caption="📘 <b>100 ChatGPT-промптов для бизнеса</b>",
        cache_key="prompts_file_id",
        file_id_override=get_asset_file_id("prompts")
    )

    # 2) Презентация
    await _send_document_safely(
        chat_id=user_id,
        file_id_env=PDF_PRESENTATION_FILE_ID,
        url=PDF_PRESENTATION_URL,
        filename="AI_Business_Kit_Product_Presentation.pdf",
        caption="🖼️ <b>Презентация продукта</b>",
        cache_key="presentation_file_id",
        file_id_override=get_asset_file_id("presentation")
    )

    # 3) Шаблон бота (приоритет: привязанный file_id -> локальный файл)
    bot_tpl_override = get_asset_file_id("bot_template")
    bot_tpl_sent = False
    try:
        if bot_tpl_override:
            # Быстрая отправка по file_id
            await bot.send_document(
                user_id,
                bot_tpl_override,
                caption="🤖 <b>AI Business Bot Template</b> — готовый код для запуска",
                parse_mode="HTML"
            )
            bot_tpl_sent = True
        else:
            # Локальный fallback (прочитаем файл, отправим как bytes и закешируем file_id у пользователя)
            bot_template_code = create_bot_template()
            msg = await bot.send_document(
                user_id,
                document=types.BufferedInputFile(
                    bot_template_code.encode("utf-8"),
                    filename="ai_business_bot_template.py"
                ),
                caption="🤖 <b>AI Business Bot Template</b> — готовый код для запуска",
                parse_mode="HTML"
            )
            # Персональный кэш file_id
            try:
                if msg and msg.document and msg.document.file_id:
                    users = load_paid_users()
                    rec = users.get(str(user_id), {})
                    rec.setdefault("cache", {})
                    rec["cache"]["bot_template_py_file_id"] = msg.document.file_id
                    users[str(user_id)] = rec
                    save_users(users)
            except Exception:
                pass
            bot_tpl_sent = True
    except Exception as e:
        logging.warning("Send bot template failed for %s: %s", user_id, e)

    if not bot_tpl_sent:
        # В крайнем случае — подсказка
        await bot.send_message(
            user_id,
            "⚠️ Не удалось отправить файл шаблона бота. Напишите в поддержку: " + BRAND_SUPPORT_TG
        )

    # 4) README (текстовый файл)
    try:
        readme_text = create_readme()
        await bot.send_document(
            user_id,
            document=types.BufferedInputFile(
                readme_text.encode("utf-8"),
                filename="README_AI_Business_Bot_Template.txt"
            ),
            caption="🧾 README (бот из шаблона)",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning("Send README failed for %s: %s", user_id, e)

    # 5) Финальное сообщение пользователю + умное меню
    try:
        await bot.send_message(
            user_id,
            "✅ Готово! Если нужна помощь — нажмите «Поддержка».",
            reply_markup=_menu_kb_for(user_id),
            parse_mode="HTML"
        )
    except Exception:
        pass

    # 6) Уведомление администратору
    try:
        users = load_paid_users()
        rec = users.get(str(user_id), {}) if isinstance(users, dict) else {}
        uname = rec.get("username", "unknown")
        when = datetime.now().strftime("%H:%M %d.%m.%Y")
        await bot.send_message(
            ADMIN_ID,
            (
                "📦 <b>Файлы отправлены</b>\n\n"
                f"Пользователь: @{uname}\n"
                f"ID: {user_id}\n"
                f"Время: {when}"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning("Notify admin about files sent failed: %s", e)

# ---------------------------
# КОНТЕНТ: шаблон бота + README файла
# ---------------------------
def create_bot_template() -> str:
    """Возвращает содержимое bot_template.py (лежит рядом с этим файлом)."""
    path = os.path.join(BASE_DIR, "bot_template.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def create_readme() -> str:
    """README именно для ШАБЛОННОГО бота (Template), а не продающего KIT."""
    return (
        "AI Business Bot Template — README\n"
        "=================================\n\n"
        "Готовый Telegram-бот на aiogram v3: магазин цифровых продуктов, баланс, DEMO-пополнение,\n"
        "выдача файлов, админ-панель, поддержка, рассылки, экспорт покупателей и кэш file_id.\n\n"
        "📘 Это руководство описывает запуск и настройку именно шаблонного бота (Template),\n"
        "а не продающего AI Business Kit.\n\n"
        "───────────────────────────────────────────────\n"
        "1) Что входит в комплект\n"
        "───────────────────────────────────────────────\n"
        "• bot_template.py — готовый бизнес-бот (магазин + баланс + CRM)\n"
        "• admin-панель: статистика, рассылки, пользователи\n"
        "• поддержка чатов пользователь ↔ админ\n"
        "• DEMO-пополнение (без реальных платежей)\n"
        "• выдача файлов по file_id или URL (с кэшем)\n\n"
        "───────────────────────────────────────────────\n"
        "2) Требования\n"
        "───────────────────────────────────────────────\n"
        "• Python 3.10+\n"
        "• Telegram аккаунт и токен от @BotFather\n"
        "• Ваш Telegram ID (узнать в @userinfobot)\n\n"
        "───────────────────────────────────────────────\n"
        "3) Установка зависимостей\n"
        "───────────────────────────────────────────────\n"
        "python -m venv .venv\n"
        ".venv\\Scripts\\activate      # Windows\n"
        "source .venv/bin/activate    # macOS/Linux\n\n"
        "pip install aiogram python-dotenv requests\n\n"
        "───────────────────────────────────────────────\n"
        "4) Создаём .env.template\n"
        "───────────────────────────────────────────────\n"
        "В корне рядом с bot_template.py создайте файл .env.template:\n\n"
        "BOT_TOKEN_TEMPLATE=<токен_бота_из_BotFather>\n"
        "ADMIN_ID=<ваш_Telegram_ID>\n\n"
        "# === МАГАЗИН / БРЕНД ===\n"
        "BRAND_NAME=UpgradeLab Store\n"
        "BRAND_URL=https://example.com\n"
        "BRAND_CREATED_AT=2025-10-09\n"
        "BRAND_OWNER=UpgradeLab\n"
        "BRAND_SUPPORT_TG=@your_support\n\n"
        "# === РЕЖИМЫ ===\n"
        "DEMO_MODE=true\n"
        "BROADCAST_VERIFIED_ONLY=false\n\n"
        "# === МАТЕРИАЛЫ ===\n"
        "PDF_PROMPTS_FILE_ID=\n"
        "PDF_PRESENTATION_FILE_ID=\n"
        "PDF_GUIDE_FILE_ID=\n\n"
        "# === ИЛИ URL-ВАРИАНТ ===\n"
        "PDF_PROMPTS_URL=\n"
        "PDF_PRESENTATION_URL=\n"
        "PDF_GUIDE_URL=\n\n"
        "# === AI-ПОДДЕРЖКА (опционально) ===\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_MODEL=gpt-4o-mini\n\n"
        "# === ПРОМПТЫ ===\n"
        "AI_SYSTEM_PROMPT_USER_TEMPLATE=Ты — ассистент магазина {BRAND_NAME}. Отвечай по делу, дружелюбно и кратко.\n"
        "AI_SYSTEM_PROMPT_ADMIN_TEMPLATE=Ты — техничный помощник владельца {BRAND_NAME}. Даёшь точные подсказки по aiogram v3.\n\n"
        "───────────────────────────────────────────────\n"
        "5) Запуск бота\n"
        "───────────────────────────────────────────────\n"
        "set APP_ENV_FILE=.env.template        # Windows CMD\n"
        "$env:APP_ENV_FILE='.env.template'     # PowerShell\n"
        "export APP_ENV_FILE=.env.template     # macOS/Linux\n\n"
        "python bot_template.py\n\n"
        "После запуска в консоли появится:\n"
        "🚀 Template Bot запущен\n"
        "База: paid_users.json\n\n"
        "───────────────────────────────────────────────\n"
        "6) Основные функции бота\n"
        "───────────────────────────────────────────────\n"
        "Пользователь:\n"
        "  • Магазин — просмотр товаров (демо)\n"
        "  • Баланс — пополнение DEMO / через СБП\n"
        "  • Поддержка — чат с админом\n"
        "  • AI-помощник — при наличии ключа OpenAI\n\n"
        "Админ:\n"
        "  /admin — панель управления\n"
        "  /broadcast — рассылка\n"
        "  /reply — ответ пользователю\n"
        "  /backup — резерв базы\n"
        "  /endchat — завершить диалог\n\n"
        "───────────────────────────────────────────────\n"
        "7) Привязка файлов через Telegram\n"
        "───────────────────────────────────────────────\n"
        "Отправьте PDF в Telegram (себе или админу) и ответьте:\n"
        "  /bind_prompts        — для промптов\n"
        "  /bind_presentation   — для презентации\n"
        "  /bind_guide          — резервный вариант\n\n"
        "✅ Преимущество file_id — мгновенная отправка без скачивания.\n\n"
        "───────────────────────────────────────────────\n"
        "8) DEMO-пополнение\n"
        "───────────────────────────────────────────────\n"
        "В демо-режиме (`DEMO_MODE=true`) бот зачисляет тестовую сумму.\n"
        "Для реальных платежей поставьте DEMO_MODE=false и настройте СБП.\n\n"
        "───────────────────────────────────────────────\n"
        "9) Диалог с пользователем\n"
        "───────────────────────────────────────────────\n"
        "• Пользователь открывает «Поддержку» и пишет сообщение.\n"
        "• Админ получает уведомление и может войти в диалог.\n"
        "• Завершение — кнопка «⛔ Завершить диалог» или /endchat.\n\n"
        "───────────────────────────────────────────────\n"
        "10) Файлы проекта\n"
        "───────────────────────────────────────────────\n"
        "bot_template.py         — основной код шаблона\n"
        "paid_users.json         — база пользователей (создаётся автоматически)\n"
        "kit_assets.json         — кэш file_id материалов\n"
        ".env.template           — конфиг для шаблонного бота\n\n"
        "───────────────────────────────────────────────\n"
        "11) Советы и FAQ\n"
        "───────────────────────────────────────────────\n"
        "• /admin не работает → проверьте ADMIN_ID и фильтры `~F.text.startswith('/')`\n"
        "• Ошибка file_id → заново выполните /bind_prompts или /bind_presentation\n"
        "• Для рассылок добавьте задержку 30–50 мс между сообщениями\n"
        "• Делайте backup paid_users.json перед очисткой\n"
        "• Для файлов >50 МБ используйте Telegram file_id вместо URL\n\n"
        "───────────────────────────────────────────────\n"
        "12) Контакты\n"
        "───────────────────────────────────────────────\n"
        "Бренд: UpgradeLab\n"
        "Сайт: https://boosty.to/upgradelab\n"
        "Поддержка: @sarkis_20032\n\n"
        "───────────────────────────────────────────────\n"
        "End of README\n"
    )

# ---------------------------
# Переотправка комплекта по кнопке «Получить файлы снова»
# ---------------------------
async def send_welcome_files(message: types.Message):
    """(опциональная обёртка) Сообщение + выдача файлов"""
    await message.answer("🎉 С возвращением! Вот ваши файлы:", parse_mode="HTML")
    await send_files_to_user(message.from_user.id)
# ---------------------------
# Обработка поддержки (текст) + вспомогательная функция
# ---------------------------
@dp.message(SupportStates.waiting_text)
async def process_support_message(message: types.Message, state: FSMContext):
    """
    Любое входящее сообщение пользователя в режиме поддержки уходит админу
    с кнопкой «✉️ Ответить пользователю».
    """
    user = message.from_user
    uid = user.id
    uname = user.username or "без_username"

    # Сохраним пользователя в базе (если вдруг его нет)
    save_pending_user(uid, uname)

    kb = kb_admin_quick_reply(uid)

    # Собираем и отправляем админу текст/медиа
    try:
        header = (
            "📨 <b>НОВОЕ СООБЩЕНИЕ В ПОДДЕРЖКУ</b>\n\n"
            f"@{uname}\nID: {uid}\n"
            f"{datetime.now().strftime('%H:%M %d.%m.%Y')}"
        )
        if message.photo:
            await bot.send_photo(
                ADMIN_ID,
                message.photo[-1].file_id,
                caption=f"{header}\n\n{message.caption or ''}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        elif message.document:
            await bot.send_document(
                ADMIN_ID,
                message.document.file_id,
                caption=f"{header}\n\n{message.caption or ''}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        elif message.video:
            await bot.send_video(
                ADMIN_ID,
                message.video.file_id,
                caption=f"{header}\n\n{message.caption or ''}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        elif message.animation:
            await bot.send_animation(
                ADMIN_ID,
                message.animation.file_id,
                caption=f"{header}\n\n{message.caption or ''}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        elif message.audio:
            await bot.send_audio(
                ADMIN_ID,
                message.audio.file_id,
                caption=f"{header}\n\n{message.caption or ''}",
                reply_markup=kb,
                parse_mode="HTML"
            )
        elif message.voice:
            await bot.send_voice(
                ADMIN_ID,
                message.voice.file_id,
                caption=f"{header}\n\n(голосовое)",
                reply_markup=kb,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                f"{header}\n\n{message.text or '(без текста)'}",
                reply_markup=kb,
                parse_mode="HTML"
            )
    except Exception as e:
        logging.warning("Send support message to admin failed: %s", e)

    # Подтверждение пользователю
    await message.answer(
        "✅ Сообщение отправлено в поддержку. Обычно отвечаем в течение 5–15 минут.",
        reply_markup=_menu_kb_for(uid),
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(SupportStates.waiting_text)
async def support_waiting_text(message: types.Message, state: FSMContext):
    await process_support_message(message, state)

# ---------------------------
# «Получить файлы снова»
# ---------------------------
@dp.callback_query(F.data == "get_files_again")
async def get_files_again_cb(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)
    uid = callback.from_user.id

    # Проверка статуса пользователя
    if not is_user_verified(uid):
        await callback.message.answer(
            "❌ Доступ ещё не активирован. "
            "Нажмите «Я оплатил(а)» и отправьте скрин подтверждения.",
            reply_markup=_menu_kb_for(uid),
            parse_mode="HTML"
        )
        return

    # Сообщение пользователю о начале отправки
    try:
        await callback.message.answer("🔄 Переотправляю комплект файлов…", parse_mode="HTML")
    except Exception:
        pass

    # Отправляем файлы
    await send_files_to_user(uid)

    # Уведомляем админа о том, что пользователь запросил повторную выдачу
    try:
        await bot.send_message(
            ADMIN_ID,
            f"♻️ <b>Пользователь запросил повторную выдачу файлов</b>\n"
            f"ID: {uid}\n"
            f"Время: {datetime.now().strftime('%H:%M %d.%m.%Y')}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning("Не удалось уведомить админа о повторной выдаче: %s", e)

# ---------------------------
# Оплата: если пришёл не скрин (док/текст) в ожидании скрина
# ---------------------------
@dp.callback_query(F.data == "pay_sbp")
async def pay_sbp_handler(callback: types.CallbackQuery, state: FSMContext):
    await _safe_cb_answer(callback)

    order_id = _gen_order_id()
    uname = callback.from_user.username or "без_username"
    uid = callback.from_user.id
    save_pending_user(uid, uname)

    # ✅ ВАЖНО: перевести в режим ожидания скрина
    await state.set_state(PaymentStates.waiting_screenshot)
    await state.update_data(order_id=order_id, user_id=uid, username=uname, is_support=False)

    text = (
        f"💳 <b>Оплата по СБП</b>\n\n"
        f"Сумма: <b>{SBP_PRICE_RUB} ₽</b>\n"
        f"Номер заказа: <code>{order_id}</code>\n\n"
        "1️⃣ Отсканируйте QR\n"
        "2️⃣ Оплатите\n"
        "3️⃣ Пришлите сюда <b>скрин чека</b>\n"
    )

    qr_file_id = get_sbp_qr_file_id()
    if qr_file_id:
        await callback.message.answer_photo(qr_file_id, caption=text, reply_markup=kb_verification_back(), parse_mode="HTML")
    else:
        await callback.message.answer(text + "\n⚠️ QR не привязан. Выполните /bind_sbp_qr.", reply_markup=kb_verification_back(), parse_mode="HTML")

@dp.message(PaymentStates.waiting_screenshot)
async def waiting_screenshot_fallback(message: types.Message, state: FSMContext):
    # Разрешим также document (когда скрин как файл) и короткий текст — в ответ попросим фото
    if message.document:
        # Пробуем принять документ как чек
        data = await state.get_data()
        user_id = data.get("user_id") or message.from_user.id
        username = data.get("username") or (message.from_user.username or "без_username")
        save_pending_user(user_id, username)
        try:
            await bot.send_document(
                ADMIN_ID, message.document.file_id,
                caption=("📸 <b>ЗАПРОС ПОДТВЕРЖДЕНИЯ</b>\n\n"
                         f"@{username}\nID: {user_id}\n"
                         f"{datetime.now().strftime('%H:%M %d.%m.%Y')}"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить",  callback_data=f"reject_{user_id}")
                ]]),
                parse_mode="HTML"
            )
            await message.answer(
                "✅ Документ принят и отправлен на проверку. Сообщим о результате.",
                reply_markup=kb_back_main(), parse_mode="HTML"
            )
            await state.clear()
            return
        except Exception as e:
            logging.warning("Forward doc for verification failed: %s", e)

    await message.answer(
        "ℹ️ Пожалуйста, отправьте <b>фото/скриншот</b> подтверждения оплаты. "
        "Должны быть видны дата, сумма, статус и получатель.",
        reply_markup=kb_verification_back(), parse_mode="HTML"
    )

# ---------------------------
# Вспомогательные команды
# ---------------------------
@dp.message(Command("endchat"))
async def endchat_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return

    uid = _active_admin_chats.pop(ADMIN_ID, None)
    if uid:
        _active_user_chats.pop(uid, None)
        # клиенту — меню клиента
        with suppress(Exception):
            await bot.send_message(
                uid,
                "✅ Диалог с администратором завершён.",
                reply_markup=_menu_kb_for(uid),
                parse_mode="HTML"
            )

    await state.clear()

    # админу — меню админа
    await message.answer(
        "⛔ Диалог закрыт.",
        reply_markup=_menu_kb_for(ADMIN_ID),
        parse_mode="HTML"
    )

# ---------------------------
# Fallback для прочих команд/сообщений
# ---------------------------
@dp.message(F.text & F.text.startswith("/"))
async def unknown_command(message: types.Message):
    """Обработка неизвестных команд от пользователя или админа."""
    uid = message.from_user.id

    # Если это админ — выводим список доступных команд
    if uid == ADMIN_ID:
        await message.answer(
            "🤖 Неизвестная команда.\n\n"
            "<b>Доступные команды администратора:</b>\n"
            "• /admin — панель администратора\n"
            "• /reply — ответ пользователю\n"
            "• /broadcast — рассылка\n"
            "• /backup — резервная копия\n"
            "• /clear_db — очистка БД\n"
            "• /buyers — список покупателей\n"
            "• /export_buyers — экспорт CSV\n"
            "• /endchat — завершить диалог\n"
            "• /whoami — информация о пользователе",
            parse_mode="HTML"
        )
    else:
        # Для обычного пользователя — стандартный ответ и меню
        await message.answer(
            "🤖 Неизвестная команда. Нажмите /start или воспользуйтесь меню ниже.",
            reply_markup=_menu_kb_for(uid),
            parse_mode="HTML"
        )

# ---------------------------
# on_startup / on_shutdown
# ---------------------------
async def on_startup():
    logging.info("🚀 KIT Bot стартует...")
    # Убедимся, что файловые БД существуют
    if not os.path.exists(DATA_FILE):
        save_users({})
    if not os.path.exists(ASSETS_FILE):
        _save_assets({})
    logging.info("📦 База: %s | Кэш: %s", os.path.basename(DATA_FILE), os.path.basename(ASSETS_FILE))

async def on_shutdown():
    logging.info("🛑 Остановка бота...")

# ================= MAIN (замена) =================
async def main():
    # регистрируем хендлеры и хуки
    register_handlers(dp, bot)

    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling cancelled (shutdown).")
        raise
    finally:
        with suppress(Exception):
            await dp.fsm.storage.close()
        with suppress(Exception):
            await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Stopped by sarkis_20032")

        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Stopped by sarkis_20032")
