# web_bot.py — запуск бота через вебхук на Render (FastAPI + lifespan)
# ================================================================
import os
import logging
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram.types import Update

# --- Бот / диспетчер и регистрация хэндлеров — из основного файла ---
from ai_business_kit_bot import bot, dp, register_handlers, on_startup, on_shutdown

# опционально импортнём ADMIN_ID, если есть (не обязательно)
try:
    from ai_business_kit_bot import ADMIN_ID  # type: ignore
except Exception:
    ADMIN_ID = None

# --- ENV / CONFIG ---
BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ul_kit_123secret")
PORT = int(os.getenv("PORT", "10000"))

# Как часто проверяем вебхук и шлём heartbeat-лог (сек)
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "30"))  # минимум 30
# Как часто писать "OK" в лог (сек) — чтобы не спамить (по умолчанию 10 минут)
OK_LOG_PERIOD_SEC = int(os.getenv("OK_LOG_PERIOD_SEC", "600"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_bot")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else ""
START_TS = time.time()

app = FastAPI()  # объявим заранее; инициализация — через lifespan ниже

# хэндлы фоновых задач, чтобы корректно гасить на shutdown
_task_webhook_watchdog: asyncio.Task | None = None
_task_heartbeat: asyncio.Task | None = None
_task_self_ping: asyncio.Task | None = None

# ------------------------- фоновые задачи ------------------------- #

async def webhook_watchdog():
    """
    Проверяет каждые HEARTBEAT_INTERVAL_SEC секунд, что вебхук на месте.
    Никаких сообщений в Telegram не шлёт — только логирует.
    Сообщение 'OK' печатает не чаще, чем раз в OK_LOG_PERIOD_SEC.
    """
    if not WEBHOOK_URL:
        logger.warning("[WATCHDOG] BASE_URL не задан — пропускаю проверки вебхука")
        return

    interval = max(30, HEARTBEAT_INTERVAL_SEC)
    ok_log_period = max(60, OK_LOG_PERIOD_SEC)
    next_ok_log = 0.0

    while True:
        try:
            info = await bot.get_webhook_info()
            current = (info.url or "").rstrip("/")
            if current != WEBHOOK_URL:
                logger.warning("[WATCHDOG] ❌ webhook mismatch (%s != %s) — resetting", current, WEBHOOK_URL)
                await bot.set_webhook(
                    url=WEBHOOK_URL,
                    secret_token=WEBHOOK_SECRET,
                    drop_pending_updates=False,  # не теряем апдейты при авто-починке
                    allowed_updates=["message", "callback_query"],
                )
                logger.info("[WATCHDOG] ✅ webhook reset OK")
            else:
                now = time.time()
                if now >= next_ok_log:
                    logger.info("[WATCHDOG] ✅ webhook OK")
                    next_ok_log = now + ok_log_period
        except Exception as e:
            logger.warning("[WATCHDOG] ⚠️ get/set_webhook failed: %s", e)

        await asyncio.sleep(interval)


async def heartbeat_loop():
    """
    Пишет 'жив' каждые HEARTBEAT_INTERVAL_SEC в логи.
    В Telegram никаких сообщений не отправляет.
    """
    interval = max(30, HEARTBEAT_INTERVAL_SEC)
    ok_log_period = max(60, OK_LOG_PERIOD_SEC)
    next_ok_log = 0.0

    while True:
        try:
            uptime_min = int((time.time() - START_TS) / 60)
            now = time.time()
            if now >= next_ok_log:
                logger.info("[HEARTBEAT] alive; uptime=%s min; webhook=%s", uptime_min, WEBHOOK_URL or "-")
                next_ok_log = now + ok_log_period
        except Exception as e:
            logger.warning("[HEARTBEAT] failed: %s", e)

        await asyncio.sleep(interval)


async def self_ping_loop():
    """
    Пингуем локально /healthz каждые HEARTBEAT_INTERVAL_SEC.
    Без сообщений в TG. В логи предупреждаем только после 3 подряд сбоев.
    """
    import aiohttp

    interval = max(30, HEARTBEAT_INTERVAL_SEC)
    ping_url = f"http://127.0.0.1:{PORT}/healthz"  # ← всегда локально
    consecutive_failures = 0

    # даём uvicornу чуть разогреться перед первым пингом
    await asyncio.sleep(5)

    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(ping_url, headers={"User-Agent": "self-ping"}) as resp:
                    if resp.status == 200:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            txt = await resp.text()
                            logger.warning("[SELF-PING] HTTP %s for %s (x%s): %s",
                                           resp.status, ping_url, consecutive_failures, txt[:200])
        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.warning("[SELF-PING] failed (x%s) for %s: %s",
                               consecutive_failures, ping_url, e)

        await asyncio.sleep(interval)

# ------------------------- lifespan ------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan вместо устаревших @app.on_event."""
    # ---- STARTUP ----
    try:
        # 1) Регистрация хэндлеров
        try:
            register_handlers(dp, bot)  # если сигнатура (dp, bot)
        except TypeError:
            register_handlers(dp)       # если сигнатура (dp)
    except Exception as e:
        logger.warning("register_handlers skipped/failed: %s", e)

    # 2) Ставим вебхук (если BASE_URL задан)
    try:
        if BASE_URL:
            # Сносим старый, ставим новый с секретом
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            logger.info("[WEBHOOK] set to %s", WEBHOOK_URL)

            # Сторож вебхука
            asyncio.create_task(webhook_watchdog())
        else:
            logger.warning("[WEBHOOK] BASE_URL не задан — поставь вебхук через /set-webhook")
    except Exception as e:
        logger.exception("set_webhook failed: %s", e)

    # 3) Пользовательская инициализация приложения
    try:
        await on_startup()
    except Exception as e:
        logger.warning("on_startup() failed: %s", e)

    # 4) Heartbeat + Self-ping (оба — тихие, без сообщений в TG)
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(self_ping_loop())

    # Передаём управление FastAPI
    yield

    # ---- SHUTDOWN ----
    try:
        await on_shutdown()
    except Exception as e:
        logger.warning("on_shutdown() failed: %s", e)

    try:
        await bot.session.close()
    except Exception:
        pass


# Создаём приложение с lifespan
app.router.lifespan_context = lifespan  # эквивалент FastAPI(lifespan=lifespan)


# ------------------------- endpoints ------------------------- #

@app.get("/")
async def root():
    return {"ok": True, "service": "AI Business Kit Bot", "webhook": WEBHOOK_URL or None}


@app.get("/healthz")
async def healthz():
    try:
        info = await bot.get_webhook_info()
        uptime_sec = int(time.time() - START_TS)
        return {
            "ok": True,
            "webhook_url": info.url,
            "pending": info.pending_update_count,
            "ip_address": getattr(info, "ip_address", None),
            "last_error_message": getattr(info, "last_error_message", None),
            "last_error_date": getattr(info, "last_error_date", None),
            "uptime_sec": uptime_sec,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/get-webhook")
async def get_webhook():
    info = await bot.get_webhook_info()
    try:
        return info.model_dump()
    except Exception:
        return {
            "url": info.url,
            "has_custom_certificate": info.has_custom_certificate,
            "pending_update_count": info.pending_update_count,
            "ip_address": getattr(info, "ip_address", None),
            "last_error_message": getattr(info, "last_error_message", None),
            "last_error_date": getattr(info, "last_error_date", None),
        }


@app.get("/set-webhook")
async def set_webhook(request: Request, base: str | None = None):
    """
    Ручная установка вебхука:
    - /set-webhook?base=https://<your-app>.onrender.com
    - или /set-webhook (возьмём хост из текущего запроса)
    """
    base_url = (base or BASE_URL or f"{request.url.scheme}://{request.headers.get('host')}").rstrip("/")
    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    await bot.delete_webhook(drop_pending_updates=True)
    ok = await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("[WEBHOOK] set to %s -> %s", webhook_url, ok)
    return {"ok": bool(ok), "webhook": webhook_url}


@app.get("/delete-webhook")
async def delete_webhook():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("[WEBHOOK] deleted")
    return {"ok": True, "deleted": True}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # 1) Проверяем секрет в заголовке
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    # 2) Берём апдейт и валидируем (с контекстом бота)
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    has_msg = "message" in data or "edited_message" in data
    has_cb = "callback_query" in data
    logger.info("[WEBHOOK] recv: msg=%s cb=%s", has_msg, has_cb)

    try:
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception("feed_update failed: %s", e)
        # Возвращаем 200, чтобы TG не спамил ретраями; для ретраев верни 500
        return {"ok": False}

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting webhook app on 0.0.0.0:%s", PORT)
    # workers=1 и небольшой keep-alive — стабильнее на бесплатных/малых инстансах
    uvicorn.run("web_bot:app", host="0.0.0.0", port=PORT, workers=1, timeout_keep_alive=5)
