import os
import asyncio
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from dotenv import load_dotenv
import datetime

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL   = os.getenv("DATABASE_URL")   # пример: postgresql://user:pass@host:5432/db
KEEPALIVE_TOKEN = os.getenv("KEEPALIVE_TOKEN", "pingme")

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise RuntimeError("Set TELEGRAM_TOKEN and DATABASE_URL")

# --- SQLAlchemy async engine (конвертируем схему в asyncpg при необходимости)
if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DB_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    ASYNC_DB_URL = DATABASE_URL

engine: AsyncEngine = create_async_engine(ASYNC_DB_URL, pool_pre_ping=True)

# --- Telegram
bot = Bot(token=TELEGRAM_TOKEN)
dp  = Dispatcher()

# --- FastAPI для health и «привязки порта» на Render
app = FastAPI()

DDL = """
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aquariums (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  volume_l INTEGER CHECK (volume_l > 0),
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_settings (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  default_aquarium_id INTEGER REFERENCES aquariums(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS fish_species (
  id SERIAL PRIMARY KEY,
  common_name TEXT NOT NULL,
  latin_name TEXT,
  ph_min NUMERIC, ph_max NUMERIC,
  gh_min NUMERIC, gh_max NUMERIC,
  kh_min NUMERIC, kh_max NUMERIC,
  temp_min_c NUMERIC, temp_max_c NUMERIC,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS plant_species (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  ph_min NUMERIC, ph_max NUMERIC,
  temp_min_c NUMERIC, temp_max_c NUMERIC,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS aquarium_fish (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  species_id INTEGER NOT NULL REFERENCES fish_species(id),
  qty INTEGER NOT NULL CHECK (qty >= 0)
);
CREATE TABLE IF NOT EXISTS aquarium_plants (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  species_id INTEGER NOT NULL REFERENCES plant_species(id),
  qty INTEGER NOT NULL CHECK (qty >= 0)
);
CREATE TABLE IF NOT EXISTS water_tests (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ph NUMERIC, kh NUMERIC, gh NUMERIC,
  no2 NUMERIC, no3 NUMERIC,
  tan NUMERIC, po4 NUMERIC,
  temp_c NUMERIC,
  frac_nh3 NUMERIC,
  nh3_mgL NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_water_tests_aq_time ON water_tests(aquarium_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_aquariums_user ON aquariums(user_id);
"""

PG_FUNC = """
CREATE OR REPLACE FUNCTION calc_nh3_components(p_ph NUMERIC, p_temp_c NUMERIC, p_tan NUMERIC)
RETURNS TABLE(frac_nh3 NUMERIC, nh3_mgL NUMERIC, nh4_mgL NUMERIC)
LANGUAGE plpgsql AS $$
DECLARE pKa NUMERIC;
BEGIN
  IF p_ph IS NULL OR p_temp_c IS NULL OR p_tan IS NULL THEN
     RETURN QUERY SELECT NULL::NUMERIC, NULL::NUMERIC, NULL::NUMERIC;
     RETURN;
  END IF;
  pKa := 0.09018 + 2729.92 / (273.2 + p_temp_c);
  frac_nh3 := 1.0 / (1.0 + POWER(10, (pKa - p_ph)));
  nh3_mgL := p_tan * frac_nh3;
  nh4_mgL := p_tan - nh3_mgL;
  RETURN;
END $$;
"""

def unionized_fraction(ph: float, temp_c: float) -> float:
    """Доля NH3 (unionized)."""
    pKa = 0.09018 + 2729.92 / (273.2 + temp_c)
    return 1.0 / (1.0 + 10 ** (pKa - ph))

async def ensure_schema():
    async with engine.begin() as conn:
        for stmt in DDL.split(";"):
            if stmt.strip():
                await conn.execute(text(stmt))
        await conn.execute(text(PG_FUNC))

async def get_or_create_user(chat_id: int) -> int:
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT id FROM users WHERE chat_id=:c"), {"c": chat_id})).fetchone()
        if row:
            return row[0]
        new_id = (await conn.execute(text(
            "INSERT INTO users(chat_id) VALUES(:c) RETURNING id"), {"c": chat_id}
        )).scalar_one()
        return new_id

# ---------- Команды бота ----------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    user_id = await get_or_create_user(m.chat.id)
    text = (
        "Привет! Я Aquaballance 🐠\n\n"
        "Команды:\n"
        "/aq_add <имя> <литры> [описание] — добавить аквариум\n"
        "/aq_list — список аквариумов\n"
        "/aq_set <id> — сделать аквариум дефолтным\n"
        "/test_add <aq_id|default> <ph> <kh> <gh> <no2> <no3> <tan> <po4> <tempC> — добавить замер\n"
        "/help — помощь\n"
    )
    await m.answer(text)

@dp.message(Command("aq_add"))
async def cmd_aq_add(m: Message):
    # формат: /aq_add <имя> <литры> [описание...]
    args = (m.text or "").split(maxsplit=3)
    if len(args) < 3:
        await m.answer("Использование: /aq_add <имя> <литры> [описание]")
        return
    name = args[1]
    try:
        volume = int(args[2])
    except:
        await m.answer("Литраж должен быть целым числом.")
        return
    description = args[3] if len(args) == 4 else None
    user_id = await get_or_create_user(m.chat.id)
    async with engine.begin() as conn:
        aq_id = (await conn.execute(text(
            "INSERT INTO aquariums(user_id,name,description,volume_l) VALUES(:u,:n,:d,:v) RETURNING id"
        ), {"u": user_id, "n": name, "d": description, "v": volume})).scalar_one()
        # если нет дефолтного — назначим
        await conn.execute(text("""
            INSERT INTO user_settings(user_id, default_aquarium_id)
            VALUES(:u, :a)
            ON CONFLICT (user_id) DO NOTHING
        """), {"u": user_id, "a": aq_id})
    await m.answer(f"Аквариум добавлен: [{aq_id}] {name} — {volume} л" + (f"\nОписание: {description}" if description else ""))

@dp.message(Command("aq_list"))
async def cmd_aq_list(m: Message):
    user_id = await get_or_create_user(m.chat.id)
    async with engine.begin() as conn:
        def_row = (await conn.execute(text("SELECT default_aquarium_id FROM user_settings WHERE user_id=:u"), {"u": user_id})).fetchone()
        default_id = def_row[0] if def_row else None
        rows = (await conn.execute(text("""
            SELECT id, name, volume_l, COALESCE(description,'')
            FROM aquariums WHERE user_id=:u ORDER BY id
        """), {"u": user_id})).fetchall()
    if not rows:
        await m.answer("Аквариумов пока нет. Добавьте: /aq_add Имя 30 [описание]")
        return
    lines = []
    for r in rows:
        star = "⭐ " if default_id == r[0] else ""
        lines.append(f"{star}[{r[0]}] {r[1]} — {r[2]} л — {r[3]}")
    await m.answer("Ваши аквариумы:\n" + "\n".join(lines))

@dp.message(Command("aq_set"))
async def cmd_aq_set(m: Message):
    args = (m.text or "").split(maxsplit=1)
    if len(args) < 2:
        await m.answer("Использование: /aq_set <id>")
        return
    try:
        aq_id = int(args[1])
    except:
        await m.answer("id должен быть числом.")
        return
    user_id = await get_or_create_user(m.chat.id)
    async with engine.begin() as conn:
        # проверим, что аквариум принадлежит пользователю
        ok = (await conn.execute(text("SELECT 1 FROM aquariums WHERE id=:a AND user_id=:u"), {"a": aq_id, "u": user_id})).fetchone()
        if not ok:
            await m.answer("Такого аквариума нет у вас.")
            return
        await conn.execute(text("""
            INSERT INTO user_settings(user_id, default_aquarium_id)
            VALUES(:u,:a)
            ON CONFLICT (user_id)
            DO UPDATE SET default_aquarium_id = EXCLUDED.default_aquarium_id
        """), {"u": user_id, "a": aq_id})
    await m.answer(f"Дефолтный аквариум установлен: {aq_id}")

@dp.message(Command("test_add"))
async def cmd_test_add(m: Message):
    # /test_add <aq_id|default> ph kh gh no2 no3 tan po4 tempC
    parts = (m.text or "").split()
    if len(parts) < 10:
        await m.answer("Использование: /test_add <aq_id|default> ph kh gh no2 no3 tan po4 tempC")
        return
    user_id = await get_or_create_user(m.chat.id)

    async with engine.begin() as conn:
        if parts[1].lower() == "default":
            row = (await conn.execute(text("SELECT default_aquarium_id FROM user_settings WHERE user_id=:u"), {"u": user_id})).fetchone()
            if not row or not row[0]:
                await m.answer("Сначала установите дефолтный аквариум: /aq_set <id>")
                return
            aq_id = int(row[0])
        else:
            aq_id = int(parts[1])

        vals = list(map(float, parts[2:10]))
        ph, kh, gh, no2, no3, tan, po4, temp_c = vals
        frac = unionized_fraction(ph, temp_c)
        nh3 = tan * frac

        await conn.execute(text("""
            INSERT INTO water_tests(aquarium_id, measured_at, ph, kh, gh, no2, no3, tan, po4, temp_c, frac_nh3, nh3_mgL)
            VALUES(:aq, :t, :ph, :kh, :gh, :no2, :no3, :tan, :po4, :temp, :frac, :nh3)
        """), {
            "aq": aq_id, "t": datetime.datetime.utcnow(),
            "ph": ph, "kh": kh, "gh": gh, "no2": no2, "no3": no3,
            "tan": tan, "po4": po4, "temp": temp_c, "frac": frac, "nh3": nh3
        })

    msg = f"Тест сохранён. Доля NH₃ (unionized) = {frac:.4f}. NH₃ = {nh3:.4f} mg/L."
    if nh3 > 0.05:
        msg += "\n⚠️ Внимание: NH₃ > 0.05 mg/L — возможна токсичность, рекомендована срочная подмена и аэрация."
    await m.answer(msg)

# ---- FastAPI endpoints ----
@app.get("/health")
async def health(request: Request, token: Optional[str] = None):
    if token != KEEPALIVE_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    await ensure_schema()
    # стартуем polling в фоне
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
