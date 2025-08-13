import os
import asyncio
from fastapi import FastAPI, HTTPException, Request
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
KEEPALIVE_TOKEN = os.getenv("KEEPALIVE_TOKEN", "pingme")  # для /health
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# --- ваши хендлеры (пример) ---
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer("Привет! Я теперь работаю без вебхука — через Long Polling 🐠")

# --- FastAPI (только чтобы Render видел открытый порт) ---
app = FastAPI()

@app.get("/health")
async def health(request: Request, token: str | None = None):
    if token != KEEPALIVE_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    # тут можно сделать подключение к БД/миграции и т.п.
    app.state.poller = asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    poller: asyncio.Task = app.state.poller
    poller.cancel()
    try:
        await poller
    except:
        pass
    await bot.session.close()
