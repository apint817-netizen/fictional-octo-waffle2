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
import tempfile
import asyncio
import logging
import shutil
import json
import re
import csv
import io
import zipfile
import functools
import aiohttp
import random
from datetime import datetime
from aiogram.exceptions import TelegramBadRequest
from typing import Optional, Tuple, Dict, Any, List
from asyncio import get_running_loop
from html import escape

if sys.platform == "win32":
    # ДОЛЖНО стоять ДО создания Bot/Dispatcher и любых aiohttp-сессий
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import FSInputFile  # добавь импорт
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
import os
from pathlib import Path
import logging

# === PATHS ===
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)

TMP_DIR = os.path.join(DATA_DIR, "tmp_restore")
os.makedirs(TMP_DIR, exist_ok=True)

DATA_FILE = os.getenv("DATA_FILE") or str(DATA_DIR / "paid_users.json")
ASSETS_FILE = os.getenv("ASSETS_FILE") or str(DATA_DIR / "kit_assets.json")

print(f"[PATHS] BASE_DIR={BASE_DIR} | DATA_DIR={DATA_DIR}")
print(f"[FILES] DATA_FILE={DATA_FILE} | ASSETS_FILE={ASSETS_FILE}")

BACKUP_FILES = {
    "paid_users.json": DATA_FILE,
    "kit_assets.json": ASSETS_FILE,
}

# === LOGGING ===
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")


# === JSON HELPERS ===
def _read_json_safe(path: str):
    """Безопасное чтение JSON (возвращает dict или None при ошибке)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.warning("JSON read failed for %s: %s", path, e)
        return None  # сигнал о проблеме


def _write_json_atomic(path: str, data):
    """Атомарная запись JSON с резервной копией."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # резервная копия предыдущего
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak")
    os.replace(tmp, path)

def make_backup_zip_file() -> str:
    """Создать ZIP-бэкап как временный файл на диске и вернуть путь."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"ai_business_bot_backup_{ts}.zip"
    tmp_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(tmp_dir, exist_ok=True)
    zip_path = os.path.join(tmp_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        meta = {"created_at": ts, "files": [], "app": "AI Business Kit", "version": "2.0"}
        for arcname, realpath in {
            "paid_users.json": DATA_FILE,
            "kit_assets.json": ASSETS_FILE,
        }.items():
            try:
                if os.path.exists(realpath):
                    zf.write(realpath, arcname)
                    meta["files"].append(arcname)
                else:
                    zf.writestr(arcname + ".missing", "FILE_NOT_FOUND")
            except Exception as e:
                zf.writestr(arcname + ".error", str(e))
        zf.writestr("_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    logging.info("[BACKUP] File created: %s", zip_path)
    return zip_path
    
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

HEARTBEAT_ENABLED = (os.getenv("HEARTBEAT_ENABLED", "true").lower() == "true")
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "1800"))  # 30 мин по умолчанию
HEARTBEAT_IMMEDIATE = (os.getenv("HEARTBEAT_IMMEDIATE", "false").lower() == "true")

# Куда слать “пульс”: по умолчанию — админу
try:
    HEARTBEAT_CHAT_ID = int(os.getenv("HEARTBEAT_CHAT_ID") or ADMIN_ID)
except Exception:
    HEARTBEAT_CHAT_ID = ADMIN_ID

_heartbeat_task: asyncio.Task | None = None

async def _heartbeat_loop():
    """Периодически шлём админу «бот активен» + время."""
    # небольшой джиттер, чтобы при рестарте не бомбить ровно в одно и то же время
    async def _sleep_with_jitter(base_sec: int):
        jitter = int(base_sec * 0.1)  # ±10%
        await asyncio.sleep(max(5, base_sec + random.randint(-jitter, jitter)))

    if HEARTBEAT_IMMEDIATE:
        with suppress(Exception):
            ts = datetime.now().strftime("%H:%M:%S %d.%m.%Y")
            await bot.send_message(
                HEARTBEAT_CHAT_ID,
                f"✅ Бот запущен и активен (старт: {ts})"
            )

    while True:
        try:
            ts = datetime.now().strftime("%H:%M:%S %d.%m.%Y")
            await bot.send_message(
                HEARTBEAT_CHAT_ID,
                f"✅ Бот активен | {ts}"
            )
        except Exception as e:
            logging.warning("[HEARTBEAT] send failed: %s", e)
        await _sleep_with_jitter(HEARTBEAT_INTERVAL_SEC)    

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

# обязательно:
# from aiogram.exceptions import TelegramBadRequest
# import os

async def send_sbp_qr(chat_id: int, caption_html: str, reply_markup=None):
    """
    Универсальный отправитель QR:
    1) пробуем как фото (file_id photo или URL-картинка),
    2) если file_id оказался Document — шлём как document,
    3) если ничего нет — отправляем текст.
    """
    qr_file_id = get_asset_file_id("sbp_qr") or os.getenv("SBP_QR_FILE_ID")
    qr_url = os.getenv("SBP_QR_URL")

    # 1) как фото
    try:
        if qr_file_id:
            await bot.send_photo(
                chat_id, qr_file_id, caption=caption_html,
                reply_markup=reply_markup, parse_mode="HTML"
            )
            return
        if qr_url and qr_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            await bot.send_photo(
                chat_id, qr_url, caption=caption_html,
                reply_markup=reply_markup, parse_mode="HTML"
            )
            return
    except TelegramBadRequest as e:
        if "can't use file of type Document as Photo" not in str(e):
            raise  # не скрываем другие ошибки

    # 2) как документ (и file_id-документ, и любой URL)
    if qr_file_id or qr_url:
        await bot.send_document(
            chat_id, qr_file_id or qr_url, caption=caption_html,
            reply_markup=reply_markup, parse_mode="HTML"
        )
        return

    # 3) только текст
    await bot.send_message(
        chat_id, caption_html, reply_markup=reply_markup, parse_mode="HTML"
    )
    
async def send_sbp_qr_for_order(chat_id: int, order_id: str, reply_markup=None):
    """
    Собирает текст оплаты (с комментарием и ссылкой) и вызывает универсальный отправитель.
    Никаких прямых send_photo тут больше нет.
    """
    parts = [
        "💳 <b>Оплата по СБП</b>",
        f"Сумма: <b>{SBP_PRICE_RUB} ₽</b>",
    ]
    if SBP_RECIPIENT_NAME:
        parts.append(f"Получатель: <b>{SBP_RECIPIENT_NAME}</b>")

    parts += [
        f"Номер заказа: <code>{order_id}</code>",
        "",
        "1️⃣ Отсканируйте QR",
        f"2️⃣ В комментарии укажите: <code>{SBP_COMMENT_PREFIX} {order_id}</code>",
        "3️⃣ Оплатите",
        "4️⃣ Пришлите сюда <b>скрин чека</b>",
        "",
        "<b>Важно!</b> В комментарии к переводу укажите:",
        f"<code>{SBP_COMMENT_PREFIX} {order_id}</code>",
        "Например: <code>AIKIT @username</code>",
    ]

    sbp_url = os.getenv("SBP_QR_URL")
    if sbp_url:
        parts += ["", "🔗 <b>Ссылка для оплаты:</b>", sbp_url]

    caption = "\n".join(parts)
    await send_sbp_qr(chat_id, caption, reply_markup=reply_markup)

import os  # если не импортирован

async def _send_guide_pack(user_id: int):
    """
    Пытаемся отправить PPTX-версию гайда. Если нет — падаем на PDF.
    Приоритет: override (assets) -> ENV file_id -> ENV url.
    """
    # 1) Пробуем PPTX
    sent = await _send_document_safely(
        chat_id=user_id,
        file_id_env=os.getenv("GUIDE_PPTX_FILE_ID"),
        url=os.getenv("GUIDE_PPTX_URL"),
        filename="How_to_Launch_Telegram_Bot_UpgradeLab_2025.pptx",
        caption="🧭 <b>Гайд по запуску бота (PPTX)</b>\nОткройте, чтобы пройти все шаги с нуля.",
        cache_key="guide_pptx_file_id",
        file_id_override=get_asset_file_id("guide_pptx")
    )

    # _send_document_safely должен возвращать True/False — если у тебя он без возврата,
    # просто оберни в try/except и считай отправило.
    if sent:
        return

    # 2) Fallback на PDF
    await _send_document_safely(
        chat_id=user_id,
        file_id_env=os.getenv("PDF_GUIDE_FILE_ID"),
        url=os.getenv("PDF_GUIDE_URL"),
        filename="AI_Business_Bot_Template_QuickStart_RU.pdf",
        caption="🧭 <b>Гайд по запуску бота (PDF)</b>\nПошаговая инструкция для новичков.",
        cache_key="guide_file_id",
        file_id_override=get_asset_file_id("guide")
    )

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

AI_SYSTEM_PROMPT_UNIVERSAL_RAW = os.getenv("AI_SYSTEM_PROMPT_UNIVERSAL") or (
    "Ты профессиональный AI-ассистент-исполнитель. Помогаешь пользователю создавать тексты, описания, идеи, "
    "стратегии и любые материалы. Пиши по-русски, структурно, по делу. Предлагай чёткие шаги и готовые шаблоны."
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

# ---------------------------
# БАЗЫ ДАННЫХ (JSON файлы)
# ---------------------------
DATA_FILE   = os.path.join(DATA_DIR, "paid_users.json")
ASSETS_FILE = os.path.join(DATA_DIR, "kit_assets.json")

# === ASSETS CACHE ===
ASSETS_CACHE: dict = {}

# ---------------------------
# БОТ/ДИСПЕТЧЕР
# ---------------------------
bot = Bot(token=TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

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
class AdminRestore(StatesGroup):
    waiting_file = State()
    
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
        [InlineKeyboardButton(text="🤖 ИИ-помощник", callback_data="ai_choice")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="support_request")],
        [InlineKeyboardButton(text="🔄 Получить файлы снова", callback_data="get_files_again")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="open_faq")]
    ]
    if is_admin:
        rows.insert(0, [InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ai_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 GPT-чат (универсальный)", callback_data="ai_open_demo")],
        [InlineKeyboardButton(text="💼 Консультант по набору", callback_data="ai_open")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
    ])

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
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="list_users")
        ],
        [
            InlineKeyboardButton(text="📥 Покупатели", callback_data="admin_buyers"),
            InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="admin_export_buyers")
        ],
        [
            InlineKeyboardButton(text="👤 Связаться", callback_data="admin_contact_open")
        ],
        [
            InlineKeyboardButton(text="✉️ Ответ пользователю", callback_data="admin_reply_prompt"),
            InlineKeyboardButton(text="📣 Рассылка", callback_data="open_broadcast")
        ],
        [
            InlineKeyboardButton(text="🤖 ИИ (админ)", callback_data="ai_admin_open"),
            InlineKeyboardButton(text="💾 Backup", callback_data="create_backup"),
            InlineKeyboardButton(text="♻️ Restore", callback_data="admin_restore")
        ],
        [
            InlineKeyboardButton(text="🗑 Очистить базу", callback_data="clear_all")
        ],
        [
            InlineKeyboardButton(text="↩️ Закрыть", callback_data="back_to_main")
        ]
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
        "🎉 <b>Доступ к AI Business Kit активирован!</b>\n\n"
        "Поздравляем — теперь у вас есть полный комплект для создания собственного цифрового продукта с ИИ 💼\n\n"
        "🚀 <b>В вашем наборе:</b>\n"
        "• 100 готовых ChatGPT-промптов для бизнеса и контента\n"
        "• Шаблон Telegram-бота с CRM и автоответами\n"
        "• PDF-гайд по запуску за 10 минут\n"
        "• README-файл с инструкциями и рекомендациями\n\n"
        "💡 Всё, что нужно для старта уже у вас — даже отдельный ChatGPT-аккаунт не нужен.\n"
        "Используйте встроенного ИИ прямо в этом боте, чтобы тестировать и применять промпты.\n\n"
        "👇 Что можно сделать прямо сейчас:\n"
        "• «🔄 Получить файлы снова» — переотправим материалы\n"
        "• «🤖 ИИ-помощник» — два режима: консультант по набору и универсальный реализатор промптов\n"
        "• «💬 Поддержка» — помощь и консультации\n"
        "• «❓ FAQ» — ответы на популярные вопросы\n\n"
        "🚀 Начните с открытия PDF-гайда — там пошагово показано, как запустить шаблонного бота."
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
    # Если не хочешь слать презентацию верифицированным — оставляй return.
    # Если хочешь слать всем — закомментируй две строки ниже.
    if is_user_verified(message.from_user.id):
        await show_verified_home(message.chat.id)
        return

    caption = (
        "👋 <b>Добро пожаловать в AI Business Kit</b>\n\n"
        "📘 <b>Краткая презентация + инструкция</b>\n"
        "Узнай, как создать свой цифровой продукт с ИИ за один вечер 🚀\n\n"
        "💡 Набор поможет вам:\n"
        "• Автоматизировать рутину и сэкономить время\n"
        "• Создавать контент и идеи для бизнеса\n"
        "• Запустить собственного Telegram-бота без кода\n"
        "• Начать зарабатывать на продаже AI-решений\n\n"
        "🚀 <b>Что вы получите:</b>\n"
        "• 100 ChatGPT-промптов для бизнеса\n"
        "• Шаблон Telegram-бота с CRM\n"
        "• Пошаговый PDF-гайд по запуску (10 минут)\n\n"
        f"💵 <b>Стоимость:</b> {SBP_PRICE_RUB} ₽\n\n"
        "Как получить:\n"
        "1️⃣ Нажмите «Оплата по СБП (QR)» и оплатите\n"
        "2️⃣ Нажмите «✅ Я оплатил(а)»\n"
        "3️⃣ Отправьте скриншот чека для подтверждения\n\n"
        "⏱ Проверка занимает обычно 5–15 минут"
    )

    # Приоритет: kit_assets.json -> ENV file_id -> ENV url
    pres_cache_id = get_asset_file_id("presentation")  # из kit_assets.json
    pres_env_id   = os.getenv("PDF_PRESENTATION_FILE_ID")
    pres_url      = os.getenv("PDF_PRESENTATION_URL")

    # Пытаемся отправить документ с caption (единое сообщение)
    sent = False
    for doc in (pres_cache_id, pres_env_id, pres_url):
        if not doc:
            continue
        with suppress(Exception):
            await message.answer_document(
                document=doc,
                caption=caption,
                parse_mode="HTML",
                reply_markup=_menu_kb_for(message.from_user.id)
            )
            sent = True
            break

    if not sent:
        # Фолбэк: просто текст, если документа нет/не отправился
        await message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=_menu_kb_for(message.from_user.id)
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
    # важное: фиксируем режим «универсал»
    await state.update_data(ai_is_admin=False, ai_mode="universal")
    await callback.message.answer(
        "🤖 <b>ИИ активен и готов к работе!</b>\n\n"
        "Это универсальный режим: используйте его, чтобы создавать тексты, идеи, описания и решения для задач прямо здесь.",
        reply_markup=kb_ai_chat(is_admin=False),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "ai_choice")
async def ai_choice_cb(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)
    await callback.message.edit_text(
        "Выбери режим работы ИИ 👇",
        reply_markup=kb_ai_choice(),
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
    ai_mode = (data.get("ai_mode") or "").strip()  # 'universal' | ''(consultant by default)
    uid = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    verified = is_user_verified(uid)

    # ДЕМО-режим включаем только для консультанта (универсал — без демо-приписок)
    is_demo_allowed = (not verified) and DEMO_AI_ENABLED and (not is_admin) and (ai_mode != "universal")

    # демо-лимиты
    if is_demo_allowed:
        ok, reason = _demo_quota_ok(uid)
        if not ok:
            logging.info("[AI-HANDLER] demo quota blocked uid=%s reason=%s", uid, reason)
            await _safe_send_answer(message, "⚠️ " + reason, _menu_kb_for(message.from_user.id))
            return

    # История: короче в демо (только для консультанта)
    desired_hist = max(2, min(6, AI_MAX_HISTORY)) if is_demo_allowed else None
    _push_history(uid, is_admin, "user", user_text, desired=desired_hist)

    # Строим сообщения
    msgs = _build_messages(uid, is_admin, user_text, is_demo=is_demo_allowed)

    # Если режим «универсал» — подменим system-промпт первой записи
    if ai_mode == "universal" and msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = _fmt_prompt(AI_SYSTEM_PROMPT_UNIVERSAL_RAW, user_id=uid, is_admin=is_admin)

    # «печатает…»
    with suppress(Exception):
        await bot.send_chat_action(message.chat.id, "typing")

    # Унифицируем вызов: используем _ai_complete_demo и для прод-режима, передавая готовые msgs
    try:
        logging.info("[AI-HANDLER] call model=%s demo_allowed=%s admin=%s mode=%s",
                     OPENAI_MODEL, is_demo_allowed, is_admin, ai_mode or "consultant")
        reply = await _ai_complete_demo(uid, is_admin, msgs)
    except Exception as e:
        logging.warning("AI call failed, retry once: %s", e)
        reply = await _ai_complete_demo(uid, is_admin, msgs)

    _push_history(uid, is_admin, "assistant", reply, desired=desired_hist)

    # Демо-приписка только если это консультант и пользователь не верифицирован
    suffix = ""
    if is_demo_allowed and not verified:
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

    if is_demo_allowed:
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
async def back_to_main(callback: types.CallbackQuery):
    await _safe_cb_answer(callback)

    text = (
        "👋 <b>Добро пожаловать в AI Business Kit</b>\n\n"
        "📘 <b>Краткая презентация + инструкция</b>\n"
        "Узнай, как создать свой цифровой продукт с ИИ за один вечер 🚀\n\n"
        "💡 <b>Набор поможет вам:</b>\n"
        "• Автоматизировать рутину и сэкономить время\n"
        "• Создавать контент и идеи для бизнеса\n"
        "• Запустить собственного Telegram-бота без кода\n"
        "• Начать зарабатывать на продаже AI-решений\n\n"
        "🚀 <b>Что вы получите:</b>\n"
        "• 100 ChatGPT-промптов для бизнеса\n"
        "• Шаблон Telegram-бота с CRM\n"
        "• Пошаговый PDF-гайд по запуску (10 минут)\n\n"
        f"💵 <b>Стоимость:</b> {SBP_PRICE_RUB} ₽\n\n"
        "Как получить:\n"
        "1️⃣ Нажмите «Оплата по СБП (QR)» и оплатите\n"
        "2️⃣ Нажмите «✅ Я оплатил(а)»\n"
        "3️⃣ Отправьте скриншот чека для подтверждения\n\n"
        "⏱️ Проверка занимает обычно 5–15 минут"
    )

    PRESENTATION_FILE_ID = os.getenv("PDF_PRESENTATION_FILE_ID")
    PRESENTATION_URL = os.getenv("PDF_PRESENTATION_URL")

    try:
        # если есть file_id — отправляем как документ
        if PRESENTATION_FILE_ID:
            await callback.message.answer_document(
                document=PRESENTATION_FILE_ID,
                caption=text,
                parse_mode="HTML",
                reply_markup=_menu_kb_for(callback.from_user.id)
            )
        # если есть URL — отправляем по ссылке
        elif PRESENTATION_URL:
            await callback.message.answer_document(
                document=PRESENTATION_URL,
                caption=text,
                parse_mode="HTML",
                reply_markup=_menu_kb_for(callback.from_user.id)
            )
        # fallback: просто текст
        else:
            await callback.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=_menu_kb_for(callback.from_user.id)
            )

    except Exception as e:
        logging.warning(f"[BACK_TO_MAIN] failed to send presentation: {e}")
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_menu_kb_for(callback.from_user.id)
        )

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
async def backup_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return

    try:
        # 1) Создаём ZIP с paid_users.json и kit_assets.json
        zip_path = make_backup_zip_file()
        zip_name = os.path.basename(zip_path)

        # 2) Отправляем ZIP как документ
        await bot.send_document(
            chat_id=message.chat.id,
            document=FSInputFile(zip_path, filename=zip_name),
            caption=(
                f"💾 <b>Backup создан:</b> <code>{zip_name}</code>\n\n"
                "♻️ Для восстановления пришлите этот ZIP <i>ответом</i> на это сообщение\n"
                "или используйте команду /restore_backup и загрузите ZIP.\n\n"
                "Отмена восстановления: /cancel"
            ),
            parse_mode="HTML",
            reply_markup=kb_admin_back()
        )

        # Переводим FSM в ожидание файла (как в create_backup_cb),
        # чтобы админ мог сразу залить файл ответом на это же сообщение
        await state.set_state(AdminRestore.waiting_file)

    except Exception as e:
        logging.exception("Backup (ZIP) create/send failed: %s", e)
        await message.answer("❌ Ошибка при создании или отправке бэкапа.", reply_markup=kb_admin_back())

@dp.message(Command("assets_debug"))
async def assets_debug(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    d = _load_assets()
    keys = ", ".join(sorted(d.keys())) or "—"
    await message.answer(
        "🧩 <b>kit_assets.json</b>\n"
        f"Ключи: <code>{keys}</code>\n\n"
        f"prompts: {bool((d.get('prompts') or {}).get('file_id'))}\n"
        f"guide: {bool((d.get('guide') or {}).get('file_id'))}\n"
        f"presentation: {bool((d.get('presentation') or {}).get('file_id'))}\n"
        f"bot_template: {bool((d.get('bot_template') or {}).get('file_id'))}\n"
        f"sbp_qr: {bool((d.get('sbp_qr') or {}).get('file_id'))}",
        parse_mode="HTML"
    )

@dp.message(Command("restore_backup"))
async def backup_restore_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")
    await state.set_state(AdminRestore.waiting_file)
    await message.answer(
        "♻️ Режим восстановления включён.\n"
        "Пришлите ZIP (предпочтительно) или один из JSON:\n"
        "• <code>paid_users.json</code>\n"
        "• <code>kit_assets.json</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_restore")
async def admin_restore_cb(callback: types.CallbackQuery, state: FSMContext):
    """Запуск восстановления из админ-панели (аналог команды /restore_backup)."""
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return

    await _safe_cb_answer(callback)

    # ставим то же состояние, что и у команды /restore_backup
    await state.set_state(AdminRestore.waiting_file)
    await bot.send_message(
        callback.from_user.id,
        "♻️ Режим восстановления включён.\n"
        "Пришлите ZIP (предпочтительно) или один из JSON:\n"
        "• <code>paid_users.json</code>\n"
        "• <code>kit_assets.json</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )    

@dp.message(Command("cancel"))
async def cancel_restore(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        await state.clear()
        return await message.answer("✅ Режим восстановления отменён.")

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
async def create_backup_cb(callback: types.CallbackQuery, state: FSMContext):
    """Создать ZIP-бэкап и предложить восстановление."""
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return

    await _safe_cb_answer(callback)

    try:
        # 1) Создаём ZIP на диске
        zip_path = make_backup_zip_file()
        zip_name = os.path.basename(zip_path)

        # 2) Отправляем файл с диска (без загрузки в память)
        await bot.send_document(
            chat_id=callback.from_user.id,
            document=FSInputFile(zip_path, filename=zip_name),
            caption=(
                f"💾 <b>Backup создан:</b> <code>{zip_name}</code>\n\n"
                "♻️ Чтобы <b>восстановить</b>, пришлите ZIP/JSON <i>ответом</i> на это сообщение "
                "или используйте команду /restore_backup.\n\n"
                "Отмена: /cancel"
            ),
            parse_mode="HTML",
            reply_markup=kb_admin_back()
        )

        # 3) Переводим FSM в ожидание файла для восстановления
        await state.set_state(AdminRestore.waiting_file)
        logging.info("[BACKUP] Sent %s to admin %s", zip_name, callback.from_user.id)

    except Exception as e:
        logging.exception("Backup create/send failed: %s", e)
        await bot.send_message(
            callback.from_user.id,
            "❌ Ошибка при создании или отправке бэкапа.",
            reply_markup=kb_admin_back()
        )
        
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
    """
    Админ подтвердил оплату — отмечаем пользователя как оплаченного,
    выдаём материалы и переводим его в домашний экран купившего.
    Дополнительно уведомляем админа о результате.
    """
    if callback.from_user.id != ADMIN_ID:
        await _safe_cb_answer(callback, "❌ Нет доступа", show_alert=True)
        return

    await _safe_cb_answer(callback)

    # Кого одобрили
    try:
        user_id = int(callback.data.split("_")[1])
    except Exception:
        await bot.send_message(
            ADMIN_ID,
            "⚠️ Некорректный формат callback-данных approve_*",
            parse_mode="HTML"
        )
        return

    users = load_paid_users()
    username = (users.get(str(user_id)) or {}).get("username", "unknown")

    # Отмечаем пользователя как оплаченного
    save_paid_user(user_id, username)

    # Выдаём файлы + показываем экран купившего
    try:
        await send_files_to_user(user_id, include_presentation=False)

        # После выдачи — показать домашний экран купившего (единое сообщение с поздравлением)
        with suppress(Exception):
            await show_verified_home(user_id)

        # Уведомляем администратора
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Выдано</b>\nПользователь @{username} (ID: <code>{user_id}</code>) получил все материалы.",
            parse_mode="HTML",
            reply_markup=kb_admin_back()
        )

        # Обновляем исходное сообщение с кнопками
        with suppress(Exception):
            await callback.message.edit_text(
                f"✅ Подтверждено. Пользователь @{username} получил файлы.",
                reply_markup=kb_admin_back(),
                parse_mode="HTML"
            )

    except Exception as e:
        logging.exception("send_files_to_user failed: %s", e)

        # Сообщаем админу о проблеме
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

        # Сообщение под кнопками запроса
        with suppress(Exception):
            await callback.message.edit_text(
                "⚠️ Оплата подтверждена, но при отправке файлов произошла ошибка — проверьте логи.",
                reply_markup=kb_admin_back(),
                parse_mode="HTML"
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

@dp.message(AdminRestore.waiting_file, F.document)
async def backup_restore_file(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Нет доступа")

    doc = message.document
    file_name = (doc.file_name or "").lower()

    # Скачиваем в память
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)

    restored, errors = [], []

    try:
        if file_name.endswith(".zip"):
            with zipfile.ZipFile(buf) as zf:
                for arcname, realpath in BACKUP_FILES.items():
                    if arcname in zf.namelist():
                        try:
                            data = json.loads(zf.read(arcname).decode("utf-8"))
                            if data is None:
                                raise ValueError("invalid json")
                            _write_json_atomic(realpath, data)
                            restored.append(arcname)
                        except Exception as e:
                            errors.append(f"{arcname}: {e}")
        elif file_name.endswith(".json"):
            # одиночный JSON: определим по имени
            target = None
            for arcname, realpath in BACKUP_FILES.items():
                if file_name == arcname:
                    target = realpath
                    break
            if not target:
                return await message.answer(
                    "⚠️ Имя файла не распознано. Ожидаю <code>paid_users.json</code> или <code>kit_assets.json</code>.",
                    parse_mode="HTML"
                )
            data = json.loads(buf.read().decode("utf-8"))
            _write_json_atomic(target, data)
            restored.append(file_name)
        else:
            return await message.answer("⚠️ Пришлите .zip или .json")
    except zipfile.BadZipFile:
        return await message.answer("⚠️ Невалидный ZIP-архив")
    except json.JSONDecodeError:
        return await message.answer("⚠️ Невалидный JSON")
    except Exception as e:
        logging.exception("Restore failed: %s", e)
        return await message.answer(f"❌ Ошибка восстановления: {e}")

    await state.clear()
    ok_list = "• " + "\n• ".join(restored) if restored else "—"
    err_list = "• " + "\n• ".join(errors) if errors else "—"
    await message.answer(
        "✅ Восстановление завершено.\n\n"
        f"<b>Обновлены:</b>\n{ok_list}\n\n"
        f"<b>Ошибки:</b>\n{err_list}\n\n"
        "ℹ️ Для надёжности перезапусти сервис на Render, если файлы критичны.",
        parse_mode="HTML"
    )

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
    Стратегия отправки (экономим трафик):
    0) file_id_override (kit_assets.json, /bind_*) — самый приоритетный
    1) file_id из .env
    2) персональный кэш file_id (paid_users.json)
    3) передать URL напрямую (Telegram сам скачает) → закэшировать file_id
    4) fallback: отправить ссылку текстом
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

    # 2) кэшированный file_id (персонально на пользователя)
    users = load_paid_users()
    rec = users.get(str(chat_id), {}) if isinstance(users, dict) else {}
    cache = rec.get("cache", {})
    file_id_cached = cache.get(cache_key)
    if file_id_cached:
        try:
            msg = await bot.send_document(chat_id, file_id_cached, caption=caption, parse_mode="HTML")
            return msg
        except Exception as e:
            logging.warning("Cached file_id failed (%s): %s", cache_key, e)

    # 3) отдаём URL напрямую — Telegram сам скачает (трафик Render ≈ 0) и кэшируем новый file_id
    if url:
        try:
            msg = await bot.send_document(
                chat_id,
                url,
                caption=caption,
                parse_mode="HTML"
            )
            # кэшируем новый file_id
            try:
                file_id_new = msg.document.file_id if (msg and getattr(msg, "document", None)) else None
                if file_id_new:
                    users = load_paid_users()
                    rec = users.get(str(chat_id), {}) if isinstance(users, dict) else {}
                    rec.setdefault("cache", {})
                    rec["cache"][cache_key] = file_id_new
                    users[str(chat_id)] = rec
                    save_users(users)
            except Exception as e:
                logging.warning("Cache update after URL send failed (%s): %s", cache_key, e)
            return msg
        except Exception as e:
            logging.warning("Send by URL failed (%s): %s", cache_key, e)

    # 4) чистая ссылка (последний шанс)
    if url:
        await bot.send_message(chat_id, f"{caption}\n{url}", parse_mode="HTML")
    else:
        await bot.send_message(chat_id, f"{caption}\n(файл временно недоступен)", parse_mode="HTML")
    return None

# убедись, что сверху файла есть: import os
async def send_files_to_user(user_id: int, include_presentation: bool = False):
    """
    Комплект выдачи после подтверждения:
    - Всегда: промпты + ГАЙД (PDF) + шаблон бота + README
    - Опционально: презентация (обычно не шлём после оплаты и при повторной выдаче)
    """
    # 1) Промпты
    await _send_document_safely(
        chat_id=user_id,
        file_id_env=PDF_PROMPTS_FILE_ID,
        url=PDF_PROMPTS_URL,
        filename="100_prompts_for_business.pdf",
        caption="📘 <b>100 ChatGPT-промптов для бизнеса</b>",
        cache_key="prompts_file_id",
        file_id_override=get_asset_file_id("prompts")
    )

    # 2) Гайд по запуску (Только PDF)
    await _send_document_safely(
        chat_id=user_id,
        file_id_env=os.getenv("PDF_GUIDE_FILE_ID"),
        url=os.getenv("PDF_GUIDE_URL"),
        filename="AI_Business_Bot_Launch_Guide.pdf",
        caption="🧭 <b>Гайд по запуску бота (шаг за шагом)</b>\n"
                "Полная инструкция по установке, настройке и запуску шаблонного бота.",
        cache_key="guide_file_id",
        file_id_override=get_asset_file_id("guide")
    )

    # 3) Презентация — только если явно разрешили (обычно False)
    if include_presentation:
        await _send_document_safely(
            chat_id=user_id,
            file_id_env=PDF_PRESENTATION_FILE_ID,
            url=PDF_PRESENTATION_URL,
            filename="AI_Business_Kit_Product_Presentation.pdf",
            caption="🖼️ <b>Презентация продукта</b>",
            cache_key="presentation_file_id",
            file_id_override=get_asset_file_id("presentation")
        )

    # 4) Шаблон бота (file_id → локальный файл)
    bot_tpl_override = get_asset_file_id("bot_template")
    bot_tpl_sent = False
    try:
        if bot_tpl_override:
            await bot.send_document(
                user_id,
                bot_tpl_override,
                caption="🤖 <b>AI Business Bot Template</b> — готовый код для запуска",
                parse_mode="HTML"
            )
            bot_tpl_sent = True
        else:
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
        await bot.send_message(
            user_id,
            "⚠️ Не удалось отправить файл шаблона бота. Напишите в поддержку: " + BRAND_SUPPORT_TG
        )

    # 5) README
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

    # 6) Финальное сообщение
    try:
        await bot.send_message(
            user_id,
            "✅ Готово! Если нужна помощь — нажмите «Поддержка».",
            reply_markup=_menu_kb_for(user_id),
            parse_mode="HTML"
        )
    except Exception:
        pass

    # 7) Уведомление админу
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
    """README для шаблонного бота (Template) — краткая техсправка."""
    return (
        "AI Business Bot Template — README\n"
        "=================================\n\n"
        "Готовый Telegram-бот на aiogram v3 для цифрового бизнеса 💼\n"
        "— магазин цифровых товаров\n"
        "— учёт покупателей и баланса\n"
        "— авто-выдача файлов и поддержка чатов\n"
        "— встроенная интеграция с AI (через OpenAI или OpenRouter)\n\n"
        "Этот README — краткая справка для настройки. Подробный пошаговый гайд находится в файле:\n"
        "📘 <AI_Business_Bot_Template_QuickStart_RU.pdf>\n\n"
        "───────────────────────────────\n"
        "1) Что входит\n"
        "───────────────────────────────\n"
        "• bot_template.py — основной код бота\n"
        "• .env.template — пример конфигурации\n"
        "• kit_assets.json — кэш file_id материалов\n"
        "• paid_users.json — база клиентов (создаётся автоматически)\n\n"
        "───────────────────────────────\n"
        "2) Минимальные требования\n"
        "───────────────────────────────\n"
        "• Python 3.10+\n"
        "• Аккаунт Telegram и токен от @BotFather\n"
        "• Ваш Telegram ID (узнать в @userinfobot)\n\n"
        "───────────────────────────────\n"
        "3) Быстрый старт\n"
        "───────────────────────────────\n"
        "1️⃣ Создайте виртуальное окружение и установите зависимости:\n"
        "    python -m venv .venv\n"
        "    .venv\\Scripts\\activate      # Windows\n"
        "    source .venv/bin/activate    # macOS/Linux\n"
        "    pip install aiogram python-dotenv requests\n\n"
        "2️⃣ Скопируйте .env.template → .env и укажите свои значения:\n"
        "    BOT_TOKEN_TEMPLATE=<токен_бота>\n"
        "    ADMIN_ID=<ваш_ID>\n"
        "    BRAND_NAME=UpgradeLab\n"
        "    DEMO_MODE=true\n\n"
        "3️⃣ Запустите бота:\n"
        "    python bot_template.py\n\n"
        "После запуска в консоли появится сообщение:\n"
        "🚀 Template Bot запущен\n\n"
        "───────────────────────────────\n"
        "4) Основные команды\n"
        "───────────────────────────────\n"
        "Пользователь:\n"
        "  • /start — главное меню\n"
        "  • /help — справка\n"
        "  • /balance — баланс и пополнение\n"
        "  • /support — чат с админом\n\n"
        "Админ:\n"
        "  • /admin — панель управления\n"
        "  • /broadcast — рассылка\n"
        "  • /backup — резерв базы\n"
        "  • /endchat — завершить диалог\n\n"
        "───────────────────────────────\n"
        "5) Где искать помощь\n"
        "───────────────────────────────\n"
        "📘 Подробный гайд: AI_Business_Bot_Template_QuickStart_RU.pdf\n"
        "🌐 Бренд: UpgradeLab — https://boosty.to/upgradelab\n"
        "💬 Поддержка: @sarkis_20032\n\n"
        "───────────────────────────────\n"
        "End of README — v2.0 (2025)\n"
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
    await send_files_to_user(callback.from_user.id, include_presentation=False)
    
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

    # 1) Регистрируем заказ и переводим в ожидание скрина
    order_id = _gen_order_id()
    uname = callback.from_user.username or "без_username"
    uid = callback.from_user.id
    save_pending_user(uid, uname)

    await state.set_state(PaymentStates.waiting_screenshot)
    await state.update_data(order_id=order_id, user_id=uid, username=uname, is_support=False)

    # 2) Текст для оплаты
    parts = [
        "💳 <b>Оплата по СБП</b>",
        f"Сумма: <b>{SBP_PRICE_RUB} ₽</b>",
    ]
    if SBP_RECIPIENT_NAME:
        parts.append(f"Получатель: <b>{SBP_RECIPIENT_NAME}</b>")

    parts += [
        f"Номер заказа: <code>{order_id}</code>",
        "",
        "1️⃣ Отсканируйте QR",
        f"2️⃣ В комментарии укажите: <code>{SBP_COMMENT_PREFIX} {order_id}</code>",
        "3️⃣ Оплатите",
        "4️⃣ Пришлите сюда <b>скрин чека</b>",
    ]

    # Ссылка на оплату (если есть)
    sbp_url = os.getenv("SBP_QR_URL")
    if sbp_url:
        parts += ["", "🔗 <b>Ссылка для оплаты:</b>", sbp_url]

    # Ещё раз подсказка про комментарий (один раз, без дублирования)
    parts += [
        "",
        "<b>Важно!</b> В комментарии к переводу укажите:",
        f"<code>{SBP_COMMENT_PREFIX} {order_id}</code>",
        "Например: <code>AIKIT @username</code>",
    ]

    text = "\n".join(parts)

    # 3) Пытаемся отправить QR с умным фолбэком: photo -> document -> только текст
    kb = kb_verification_back()
    qr_file_id = get_asset_file_id("sbp_qr") or os.getenv("SBP_QR_FILE_ID")
    qr_url = sbp_url  # ту же ссылку можно использовать как документ, если это не изображение

    # 3.1 Как фото (если file_id - photo, или URL на картинку)
    try:
        if qr_file_id:
            await callback.message.answer_photo(
                qr_file_id, caption=text, reply_markup=kb, parse_mode="HTML"
            )
            return
        if qr_url and qr_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            await callback.message.answer_photo(
                qr_url, caption=text, reply_markup=kb, parse_mode="HTML"
            )
            return
    except TelegramBadRequest as e:
        # типично: "can't use file of type Document as Photo" -> шлём документом
        if "can't use file of type Document as Photo" not in str(e):
            # если иная ошибка — пробросим дальше, чтобы не скрыть баг
            raise

    # 3.2 Как документ (подходит для file_id документа или любого URL — даже PDF)
    if qr_file_id or qr_url:
        await callback.message.answer_document(
            document=qr_file_id or qr_url,
            caption=text,
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    # 3.3 Финальный фолбэк — только текст
    await callback.message.answer(
        text + "\n\n⚠️ QR временно недоступен. Свяжитесь с поддержкой: " + BRAND_SUPPORT_TG,
        reply_markup=kb,
        parse_mode="HTML"
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

    # Запускаем heartbeat (если включён)
    global _heartbeat_task
    if HEARTBEAT_ENABLED and _heartbeat_task is None:
        try:
            _heartbeat_task = asyncio.create_task(_heartbeat_loop())
            logging.info(
                "[HEARTBEAT] started → interval=%ss, chat_id=%s, immediate=%s",
                HEARTBEAT_INTERVAL_SEC, HEARTBEAT_CHAT_ID, HEARTBEAT_IMMEDIATE
            )
        except Exception as e:
            logging.warning("[HEARTBEAT] start failed: %s", e)


async def on_shutdown():
    logging.info("🛑 Остановка бота...")

    # Останавливаем heartbeat
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await _heartbeat_task
        _heartbeat_task = None
        logging.info("[HEARTBEAT] stopped")

# ================= MAIN (замена) =================
def register_handlers(dp: Dispatcher, bot: Bot):
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Admin: BACKUP / RESTORE
    dp.callback_query.register(create_backup_cb, F.data == "create_backup")
    dp.callback_query.register(admin_restore_cb, F.data == "admin_restore")

    dp.message.register(backup_handler, Command("backup"))
    dp.message.register(backup_restore_start, Command("restore_backup"))
    dp.message.register(cancel_restore, Command("cancel"))
    dp.message.register(backup_restore_file, AdminRestore.waiting_file & F.document)

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
