# web_bot.py — запуск бота через вебхук на Render (FastAPI + lifespan)
# ================================================================
import os
import logging
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram.types import Update

# --- ENV / CONFIG ---
BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ul_kit_123secret")
PORT = int(os.getenv("PORT", "10000"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "300"))  # каждые 5 минут
HEARTBEAT_CHAT_ID = os.getenv("HEARTBEAT_CHAT_ID")  # опционально: куда слать пинг в TG

# --- Бот / диспетчер и регистрация хэндлеров — из основного файла ---
from ai_business_kit_bot import bot, dp, register_handlers, on_startup, on_shutdown

# опционально импортнём ADMIN_ID, если есть
try:
    from ai_business_kit_bot import ADMIN_ID  # type: ignore
except Exception:
    ADMIN_ID = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_bot")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else ""
START_TS = time.time()


# ------------------------- фоновые задачи ------------------------- #

async def webhook_watchdog():
    """
    Проверяет каждые 2 минуты, что вебхук на месте.
    Никаких сообщений в Telegram не шлёт — только логирует.
    """
    if not WEBHOOK_URL:
        logger.warning("[WATCHDOG] BASE_URL не задан — пропускаю проверки вебхука")
        return

    while True:
        try:
            info = await bot.get_webhook_info()
            current = (info.url or "").rstrip("/")
            if current != WEBHOOK_URL:
                logger.warning("[WATCHDOG] ❌ webhook mismatch (%s != %s) — resetting", current, WEBHOOK_URL)
                await bot.set_webhook(
                    url=WEBHOOK_URL,
                    secret_token=WEBHOOK_SECRET,
                    drop_pending_updates=False,
                )
                logger.info("[WATCHDOG] ✅ webhook reset OK")
            else:
                logger.info("[WATCHDOG] ✅ webhook OK")
        except Exception as e:
            logger.warning("[WATCHDOG] ⚠️ get/set_webhook failed: %s", e)

        await asyncio.sleep(120)  # 2 минуты

async def heartbeat_loop():
    """
    Пишет "жив" каждые HEARTBEAT_INTERVAL_SEC.
    Если указан HEARTBEAT_CHAT_ID — отправляет заметку в Telegram (необязательно).
    """
    interval = max(60, HEARTBEAT_INTERVAL_SEC)  # защитимся от слишком частых пингов
    while True:
        try:
            uptime_min = int((time.time() - START_TS) / 60)
            logger.info("[HEARTBEAT] alive; uptime=%s min; webhook=%s", uptime_min, WEBHOOK_URL or "-")
            if HEARTBEAT_CHAT_ID:
                try:
                    await bot.send_message(
                        int(HEARTBEAT_CHAT_ID),
                        f"✅ Heartbeat: alive\nUptime: {uptime_min} мин\nWebhook: {WEBHOOK_URL or '—'}"
                    )
                except Exception as e:
                    logger.debug("[HEARTBEAT] send_message skipped: %s", e)
        except Exception as e:
            logger.warning("[HEARTBEAT] failed: %s", e)
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

    # 4) Heartbeat
    asyncio.create_task(heartbeat_loop())

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


app = FastAPI(lifespan=lifespan)


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
    uvicorn.run("web_bot:app", host="0.0.0.0", port=PORT, workers=1, timeout_keep_alive=5)
