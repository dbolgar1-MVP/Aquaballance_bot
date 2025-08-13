import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from dotenv import load_dotenv

# === Загрузка переменных окружения ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Убираем лишние пробелы/переносы
DATABASE_URL = DATABASE_URL.strip()

# Добавляем sslmode=require, если не указано
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

print(f"[INFO] Connecting to DB: {DATABASE_URL}")

# === Инициализация бота ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# === Подключение к базе данных ===
db_pool: SimpleConnectionPool | None = None

def init_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)
            print("[INFO] Database connection pool created successfully.")
        except Exception as e:
            print(f"[ERROR] Database connection failed: {e}")
            raise

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer("Привет! 🐠 Я работаю без вебхука, через long polling!")

@dp.message_handler(commands=["add_aquarium"])
async def add_aquarium_cmd(message: types.Message):
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute("INSERT INTO aquariums (name) VALUES (%s)", ("Мой аквариум",))
        conn.commit()
        cur.close()
        db_pool.putconn(conn)
        await message.answer("Аквариум добавлен! ✅")
    except Exception as e:
        await message.answer(f"Ошибка при добавлении аквариума: {e}")

@dp.message_handler(commands=["list_aquariums"])
async def list_aquariums_cmd(message: types.Message):
    try:
        conn = db_pool.getconn()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM aquariums")
        rows = cur.fetchall()
        cur.close()
        db_pool.putconn(conn)

        if not rows:
            await message.answer("Аквариумов пока нет.")
        else:
            text = "\n".join([f"{row[0]}. {row[1]}" for row in rows])
            await message.answer(f"Список аквариумов:\n{text}")
    except Exception as e:
        await message.answer(f"Ошибка при получении списка: {e}")

if __name__ == "__main__":
    init_db_pool()
    executor.start_polling(dp, skip_updates=True)
