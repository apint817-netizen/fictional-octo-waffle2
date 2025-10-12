# web_bot.py — запуск бота через вебхук на Render (FastAPI + lifespan)
# ================================================================

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from aiogram.types import Update

# --- ENV ---
BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ul_kit_123secret")
PORT = int(os.getenv("PORT", "10000"))

# --- Бот/диспетчер и регистрация хэндлеров — ИЗ ОСНОВНОГО ФАЙЛА ---
from ai_business_kit_bot import bot, dp, register_handlers, on_startup, on_shutdown

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan вместо устаревших @app.on_event."""
    # ---- STARTUP ----
    # 1) Регистрируем хэндлеры и хуки
    register_handlers(dp, bot)

    # 2) Ставим вебхук (если BASE_URL задан)
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
        # сносим старый вебхук (и висящие апдейты), ставим новый
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(webhook_url)
        logging.info(f"[WEBHOOK] set to {webhook_url}")
    else:
        logging.warning("[WEBHOOK] BASE_URL не задан — сервис жив, поставь вебхук через /set-webhook")

    # 3) Запускаем твою инициализацию
    await on_startup()

    # Передаём управление FastAPI
    yield

    # ---- SHUTDOWN ----
    await on_shutdown()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"ok": True, "service": "AI Business Kit Bot"}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/get-webhook")
async def get_webhook():
    info = await bot.get_webhook_info()
    # aiogram возвращает pydantic-модель — превратим в dict для удобства
    return {
        "url": info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count": info.pending_update_count,
        "ip_address": info.ip_address,
        "last_error_message": info.last_error_message,
        "last_error_date": info.last_error_date,
    }


@app.get("/set-webhook")
async def set_webhook(request: Request, base: str | None = None):
    """
    Ручная установка вебхука:
    - /set-webhook?base=https://fictional-octo-waffle2.onrender.com
    - или /set-webhook (возьмём хост из текущего запроса)
    """
    base_url = (base or BASE_URL or f"{request.url.scheme}://{request.headers.get('host')}").rstrip("/")
    webhook_url = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(webhook_url)
    logging.info(f"[WEBHOOK] set to {webhook_url}")
    return {"ok": True, "webhook": webhook_url}


@app.get("/delete-webhook")
async def delete_webhook():
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("[WEBHOOK] deleted")
    return {"ok": True, "deleted": True}


@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    # полезные короткие логи (не спамят)
    has_msg = "message" in data or "edited_message" in data
    has_cb = "callback_query" in data
    logging.info(f"[WEBHOOK] recv: msg={has_msg} cb={has_cb}")

    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    logging.info(f"Starting webhook app on 0.0.0.0:{PORT}")
    uvicorn.run("web_bot:app", host="0.0.0.0", port=PORT)
