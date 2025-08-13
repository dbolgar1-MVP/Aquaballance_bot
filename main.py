import os
import asyncio
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

import psycopg2
from psycopg2.pool import SimpleConnectionPool

# === ENV ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Добавляем sslmode=require, если не указано
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

# === DB POOL ===
db_pool: SimpleConnectionPool | None = None

def init_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)

@contextmanager
def get_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur, conn
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def apply_schema():
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        sql = schema_path.read_text(encoding="utf-8")
        with get_cursor() as (cur, _):
            cur.execute(sql)

async def db_exec(query: str, params: tuple = ()):
    def _run():
        with get_cursor() as (cur, _):
            cur.execute(query, params)
    return await asyncio.to_thread(_run)

async def db_fetchone(query: str, params: tuple = ()):
    def _run():
        with get_cursor() as (cur, _):
            cur.execute(query, params)
            return cur.fetchone()
    return await asyncio.to_thread(_run)

async def db_fetchall(query: str, params: tuple = ()):
    def _run():
        with get_cursor() as (cur, _):
            cur.execute(query, params)
            return cur.fetchall()
    return await asyncio.to_thread(_run)

# === TELEGRAM BOT ===
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await db_exec(
        """
        INSERT INTO users (telegram_id, username)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        (m.from_user.id, m.from_user.username),
    )
    await m.answer("Привет! Я бот Aquaballance 🐠\n"
                   "Доступные команды:\n"
                   "/add_aquarium <имя> <литры> [описание]\n"
                   "/list_aquariums")

@dp.message(Command("add_aquarium"))
async def cmd_add_aquarium(m: Message):
    parts = (m.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await m.answer("Использование: /add_aquarium <имя> <литры> [описание]")
        return
    name = parts[1]
    try:
        volume = float(parts[2])
    except ValueError:
        await m.answer("Литраж должен быть числом.")
        return
    description = parts[3] if len(parts) == 4 else None

    row = await db_fetchone("SELECT id FROM users WHERE telegram_id = %s", (m.from_user.id,))
    if not row:
        await m.answer("Сначала используйте /start")
        return
    user_id = row[0]

    await db_exec(
        "INSERT INTO aquariums (user_id, name, description, volume_liters) VALUES (%s, %s, %s, %s)",
        (user_id, name, description, volume),
    )
    await m.answer(f"✅ Аквариум '{name}' добавлен ({volume} л)")

@dp.message(Command("list_aquariums"))
async def cmd_list_aquariums(m: Message):
    rows = await db_fetchall(
        """
        SELECT id, name, COALESCE(volume_liters,0), COALESCE(description,'')
        FROM aquariums
        WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
        """,
        (m.from_user.id,),
    )
    if not rows:
        await m.answer("Аквариумов пока нет. Добавьте с помощью /add_aquarium")
        return
    text = "\n".join([f"[{aid}] {n} — {v} л — {d}" for aid, n, v, d in rows])
    await m.answer("Ваши аквариумы:\n" + text)

# === FASTAPI APP (для Render) ===
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    init_db_pool()
    apply_schema()
    app.state.poller = asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    app.state.poller.cancel()
    try:
        await app.state.poller
    except:
        pass
    await bot.session.close()

if __name__ == "__main__":
    init_db_pool()
    apply_schema()
    asyncio.run(dp.start_polling(bot))
