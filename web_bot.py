# web_bot.py (фрагмент ключевых мест)

import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv(".env.kit") or load_dotenv(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN_KIT") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN_KIT обязателен")

BASE_URL = (os.getenv("BASE_URL") or "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
PORT = int(os.getenv("PORT", "10000"))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

from ai_business_kit_bot import bot as _real_bot, dp as _real_dp, register_handlers
# используем твои хэндлеры
register_handlers(dp, bot)

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    # Если BASE_URL задан — ставим вебхук.
    if BASE_URL:
        wh = f"{BASE_URL}/webhook/{WEBHOOK_SECRET}"
        await bot.set_webhook(wh, drop_pending_updates=True)
        print(f"[WEBHOOK] set to {wh}")
    else:
        # Не падаем. Ждём ручной установки /set-webhook
        print("[WEBHOOK] BASE_URL не задан, сервер запущен, ждём /set-webhook")

@app.get("/set-webhook")
async def set_webhook(request: Request, base: str | None = None):
    """
    Ручная установка вебхука:
    - /set-webhook?base=https://fictional-octo-waffle2.onrender.com
    или
    - просто /set-webhook (тогда возьмём из Host-заголовка текущего запроса)
    """
    base_url = (base or BASE_URL or f"{request.url.scheme}://{request.headers.get('host')}").rstrip("/")
    wh = f"{base_url}/webhook/{WEBHOOK_SECRET}"
    await bot.set_webhook(wh, drop_pending_updates=True)
    return {"ok": True, "webhook": wh}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
