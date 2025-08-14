import os
import logging
from dotenv import load_dotenv
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from aiogram import Bot, Dispatcher, executor, types

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)

# === ЗАГРУЗКА .env ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env or environment variables")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env or environment variables")

# Убираем переносы строк и добавляем sslmode=require, если не указано
DATABASE_URL = DATABASE_URL.strip()
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

logging.info(f"Connecting to DB: {DATABASE_URL}")

# === ИНИЦИАЛИЗАЦИЯ БД ===
db_pool: SimpleConnectionPool | None = None

def init_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)
            logging.info("✅ Database connection pool created successfully")
        except Exception as e:
            logging.error(f"❌ Database connection failed: {e}")
            raise

def get_db_connection():
    if not db_pool:
        raise RuntimeError("Database pool is not initialized")
    return db_pool.getconn()

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

# === TELEGRAM BOT ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я бот для аквариумов 🐟\n"
                         "Используй /add_aquarium или /list_aquariums")

@dp.message_handler(commands=["add_aquarium"])
async def cmd_add_aquarium(message: types.Message):
    args = message.get_args()
    if not args:
        await message.answer("Использование: /add_aquarium <название>")
        return
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO aquariums (name) VALUES (%s)", (args,))
            conn.commit()
        await message.answer(f"✅ Аквариум '{args}' добавлен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        release_db_connection(conn)

@dp.message_handler(commands=["list_aquariums"])
async def cmd_list_aquariums(message: types.Message):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM aquariums ORDER BY id")
            rows = cur.fetchall()
        if not rows:
            await message.answer("Список аквариумов пуст.")
        else:
            text = "\n".join([f"{r[0]}. {r[1]}" for r in rows])
            await message.answer(f"📋 Аквариумы:\n{text}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        release_db_connection(conn)

# === ЗАПУСК ===
if __name__ == "__main__":
    init_db_pool()
    executor.start_polling(dp, skip_updates=True)
