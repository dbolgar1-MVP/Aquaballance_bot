import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
import asyncio

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# SSL fix
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

db_pool: SimpleConnectionPool | None = None

def init_db_pool():
    global db_pool
    db_pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)
    print("[INFO] DB connected")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Привет! 🐠 Бот работает через long polling и подключен к БД!")

async def main():
    init_db_pool()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
