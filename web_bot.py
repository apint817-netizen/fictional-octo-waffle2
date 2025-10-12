# web_bot.py
import os
from fastapi import FastAPI, Request
from aiogram.types import Update
from dotenv import load_dotenv

# 1) ENV
load_dotenv(".env.kit") or load_dotenv(".env")
BOT_TOKEN = os.getenv("BOT_TOKEN_KIT") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_KIT обязателен (Render → Environment)")

BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
PORT = int(os.getenv("PORT", "10000"))

# 2) ИМПОРТИРУЕМ ГОТОВЫЕ bot и dp ИЗ ОСНОВНОГО КОДА
from ai_business_kit_bot import bot, dp, register_handlers  # <-- никаких новых Bot/Dispatcher здесь
register_handlers(dp, bot)  # регистрируем хэндлеры на ЭТОТ dp/bot

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)  # тот самый dp/bot из ai_business_kit_bot.py
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    if BASE_URL:
        wh = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
        await bot.set_webhook(wh, drop_pending_updates=True)
        print(f"[WEBHOOK] set to {wh}")
    else:
        print("[WEBHOOK] BASE_URL не задан, сервер запущен, зайди /set-webhook")

@app.get("/set-webhook")
async def set_webhook(request: Request, base: str | None = None):
    base_url = (base or BASE_URL or f"{request.url.scheme}://{request.headers.get('host')}").rstrip("/")
    wh = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    await bot.set_webhook(wh, drop_pending_updates=True)
    return {"ok": True, "webhook": wh}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
