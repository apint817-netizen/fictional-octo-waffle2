# web_bot.py — запуск бота через вебхук на Render (FastAPI + lifespan)
# ================================================================
import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from aiogram.types import Update

# --- ENV ---
BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ul_kit_123secret")
PORT = int(os.getenv("PORT", "10000"))

# --- Бот/диспетчер и регистрация хэндлеров — ИЗ ОСНОВНОГО ФАЙЛА ---
from ai_business_kit_bot import bot, dp, register_handlers, on_startup, on_shutdown

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_bot")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan вместо устаревших @app.on_event."""
    # ---- STARTUP ----
    try:
        # 1) Регистрируем хэндлеры (если функция ожидает bot — передаем)
        try:
            register_handlers(dp, bot)  # твоя сигнатура как раз (dp, bot)
        except TypeError:
            register_handlers(dp)       # если где-то без bot
    except Exception as e:
        logger.warning("register_handlers skipped/failed: %s", e)

    # 2) Ставим вебхук (если BASE_URL задан)
    try:
        if BASE_URL:
            # Сносим старый вебхук (и висящие апдейты), ставим новый с секретом
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET,          # ВАЖНО: секрет в set_webhook
                drop_pending_updates=True,
            )
            logger.info("[WEBHOOK] set to %s", WEBHOOK_URL)
        else:
            logger.warning("[WEBHOOK] BASE_URL не задан — сервис жив, поставь вебхук через /set-webhook")
    except Exception as e:
        logger.exception("set_webhook failed: %s", e)

    # 3) Запускаем твою инициализацию
    try:
        await on_startup()
    except Exception as e:
        logger.warning("on_startup() failed: %s", e)

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


@app.get("/")
async def root():
    return {"ok": True, "service": "AI Business Kit Bot", "webhook": WEBHOOK_URL or None}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/get-webhook")
async def get_webhook():
    info = await bot.get_webhook_info()
    # aiogram v3: это pydantic-модель → вернем dict
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
        secret_token=WEBHOOK_SECRET,   # ВАЖНО: секрет должен совпадать
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
        # можно вернуть 200, чтобы TG не спамил ретраями; если хотите ретраи — верните 500
        return {"ok": False}

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting webhook app on 0.0.0.0:%s", PORT)
    uvicorn.run("web_bot:app", host="0.0.0.0", port=PORT)
