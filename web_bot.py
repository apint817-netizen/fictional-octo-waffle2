# web_bot.py — запуск ai_business_kit_bot через вебхук на Render
# =============================================================

import os
import logging
import asyncio
from fastapi import FastAPI, Request
from aiogram.types import Update
from aiogram import Bot

# --- Импорт твоего бота и диспетчера ---
from ai_business_kit_bot import bot, dp, register_handlers, on_startup, on_shutdown

# --- Настройка FastAPI ---
app = FastAPI()

# --- Конфигурация логов ---
logging.basicConfig(level=logging.INFO)

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN_KIT")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ul_kit_123secret")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

# =============================================================
# При старте приложения
# =============================================================
@app.on_event("startup")
async def on_startup_event():
    assert BASE_URL, "BASE_URL обязателен (Render URL вида https://<app>.onrender.com)"
    webhook_url = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"

    # Регистрируем хэндлеры и хуки
    register_handlers(dp, bot)

    # Настраиваем вебхук
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(webhook_url)
    logging.info(f"[WEBHOOK] set to {webhook_url}")

    # Инициализация логики бота
    await on_startup()

# =============================================================
# Завершение работы
# =============================================================
@app.on_event("shutdown")
async def on_shutdown_event():
    await on_shutdown()
    await bot.session.close()

# =============================================================
# Главная страница
# =============================================================
@app.get("/")
async def root():
    return {"ok": True, "service": "AI Business Kit Bot"}

# =============================================================
# Вебхук Telegram
# =============================================================
@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    logging.info(f"[WEBHOOK] update type={update.event_type} has_message={bool(update.message)}")
    await dp.feed_update(bot, update)
    return {"ok": True}

# =============================================================
# Точка входа при локальном запуске
# =============================================================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 10000))
    logging.info(f"Starting webhook app on 0.0.0.0:{port}")
    uvicorn.run("web_bot:app", host="0.0.0.0", port=port)
