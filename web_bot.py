# web_bot.py
import os
import asyncio
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from dotenv import load_dotenv

# 1) Загружаем переменные (файл не обязателен — Render ENV ок)
load_dotenv(".env.kit") or load_dotenv(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN_KIT") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_KIT обязателен (Render → Environment)")

BASE_URL = os.getenv("BASE_URL")  # твой https://<app>.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
PORT = int(os.getenv("PORT", "10000"))

# 2) Создаём bot/dp и РЕГИСТРИРУЕМ твои хэндлеры
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ---- ВАЖНО ----
# Подключи регистрацию своих хэндлеров.
# Если в ai_business_kit_bot.py есть функция register_handlers(dp) – импортни:
try:
    from ai_business_kit_bot import register_handlers  # <- сделай такую функцию, см. ниже
    register_handlers(dp, bot)  # передай bot, если нужно
except ImportError:
    # fallback: если нет отдельной функции, тут можно кратко зарегистрировать минимум:
    pass

# 3) FastAPI-приложение + обработчик вебхука
app = FastAPI()

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    assert BASE_URL, "BASE_URL обязателен (Render URL вида https://<app>.onrender.com)"
    await bot.set_webhook(f"{BASE_URL}/webhook/{WEBHOOK_SECRET}", drop_pending_updates=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
