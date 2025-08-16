import os
import math
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from dotenv import load_dotenv
import psycopg2
from psycopg2.pool import SimpleConnectionPool

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)

# =============== ЛОГИРОВАНИЕ ===============
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aquaballance")

# =============== ENV ===============
load_dotenv()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# добавим sslmode=require если не указан
if "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

# =============== БД ПУЛ ===============
db_pool: Optional[SimpleConnectionPool] = None

def init_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)
        log.info("✅ DB pool created")

def db_exec(sql: str, params: Tuple | None = None, fetch: str = "none"):
    """
    sync helper (используется в executor ниже)
    fetch: "one" | "all" | "none"
    """
    assert db_pool is not None, "DB pool is not initialized"
    conn = db_pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
    finally:
        db_pool.putconn(conn)

async def adb_exec(sql: str, params: Tuple | None = None, fetch: str = "none"):
    """async wrapper чтобы не блокировать event loop"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, db_exec, sql, params, fetch)

# =============== СХЕМА (ensure) ===============
SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_seen TIMESTAMPTZ DEFAULT now(),
    active_aquarium_id INTEGER
);

CREATE TABLE IF NOT EXISTS aquariums (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    volume_l NUMERIC(10,2),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aquariums_user ON aquariums(user_id);

CREATE TABLE IF NOT EXISTS aquarium_fish (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    species TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    added_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aquarium_plants (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    species TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    added_at TIMESTAMPTZ DEFAULT now()
);

-- Хранение настроек подмен
CREATE TABLE IF NOT EXISTS water_settings (
    aquarium_id INTEGER PRIMARY KEY REFERENCES aquariums(id) ON DELETE CASCADE,
    change_volume_pct NUMERIC(5,2),
    period_days INTEGER
);

-- Измерения воды
CREATE TABLE IF NOT EXISTS measurements (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ph NUMERIC(4,2),
    kh NUMERIC(5,2),
    gh NUMERIC(5,2),
    no2 NUMERIC(6,3),
    no3 NUMERIC(6,2),
    tan NUMERIC(6,3),          -- total ammonia (NH3+NH4), mg/L
    nh3 NUMERIC(6,3),          -- free ammonia, mg/L (расчитываем)
    nh4 NUMERIC(6,3),          -- ammonium, mg/L (расчитываем)
    po4 NUMERIC(6,2),
    temperature_c NUMERIC(5,2),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_meas_aq_time ON measurements(aquarium_id, measured_at DESC);
"""

async def ensure_schema():
    await adb_exec(SCHEMA_SQL)

# =============== NH3 / NH4 КАЛЬКУЛЯТОР ===============
def nh3_fraction(pH: float, temp_c: float) -> float:
    """
    Доля NH3 из TAN по формуле Emerson et al. (1975) для пресной воды.
    pKa = 0.09018 + 2729.92 / (273.15 + T)
    fraction_NH3 = 1 / (1 + 10^(pKa - pH))
    """
    t_k = 273.15 + float(temp_c)
    pKa = 0.09018 + (2729.92 / t_k)
    frac = 1.0 / (1.0 + pow(10.0, (pKa - float(pH))))
    return max(0.0, min(1.0, frac))

def split_tan_to_nh3_nh4(tan: float, pH: float, temp_c: float) -> Tuple[float, float]:
    tan = max(0.0, float(tan))
    frac = nh3_fraction(pH, temp_c)
    nh3 = tan * frac
    nh4 = tan - nh3
    return nh3, nh4

# =============== СОВМЕСТИМОСТЬ РЫБ/РАСТЕНИЙ (минимальный справочник) ===============
FISH_GUIDE = {
    # название: (pH_min, pH_max, GH_min, GH_max, T_min, T_max, NO2_max, NH3_max)
    "гуппи": (6.8, 8.2, 6, 18, 22, 28, 0.1, 0.02),
    "неон": (5.0, 7.2, 1, 10, 22, 26, 0.05, 0.01),
    "скалярия": (6.0, 7.5, 3, 12, 24, 30, 0.1, 0.02),
    "золотая рыбка": (6.5, 8.0, 5, 20, 18, 24, 0.1, 0.02),
}

PLANT_GUIDE = {
    # название: (pH_min, pH_max, GH_min, GH_max, T_min, T_max, NO3_min, NO3_max, PO4_min, PO4_max)
    "анубиас": (6.0, 8.0, 3, 15, 22, 28, 5, 30, 0.2, 2.0),
    "роголистник": (6.0, 8.0, 3, 18, 18, 28, 1, 20, 0.1, 1.5),
    "элодея": (6.5, 8.0, 4, 18, 18, 28, 1, 20, 0.1, 1.5),
}

def check_fish_compat(meas: Dict[str, float], species: str) -> Tuple[bool, List[str]]:
    sp = FISH_GUIDE.get(species.lower())
    if not sp:
        return True, [f"Нет справочника по '{species}'. Добавлено без проверки."]
    ph, gh, t, no2, nh3 = meas.get("ph"), meas.get("gh"), meas.get("temperature_c"), meas.get("no2"), meas.get("nh3")
    (pH_min, pH_max, GH_min, GH_max, T_min, T_max, NO2_max, NH3_max) = sp
    probs = []
    if ph is not None and (ph < pH_min or ph > pH_max):
        probs.append(f"pH вне диапазона {pH_min}-{pH_max}")
    if gh is not None and (gh < GH_min or gh > GH_max):
        probs.append(f"GH вне диапазона {GH_min}-{GH_max}")
    if t is not None and (t < T_min or t > T_max):
        probs.append(f"Температура вне диапазона {T_min}-{T_max}°C")
    if no2 is not None and no2 > NO2_max:
        probs.append(f"NO₂ высокое (> {NO2_max} мг/л)")
    if nh3 is not None and nh3 > NH3_max:
        probs.append(f"NH₃ высокое (> {NH3_max} мг/л)")
    return (len(probs) == 0), probs

def check_plant_compat(meas: Dict[str, float], species: str) -> Tuple[bool, List[str]]:
    sp = PLANT_GUIDE.get(species.lower())
    if not sp:
        return True, [f"Нет справочника по '{species}'. Добавлено без проверки."]
    ph, gh, t, no3, po4 = meas.get("ph"), meas.get("gh"), meas.get("temperature_c"), meas.get("no3"), meas.get("po4")
    (pH_min, pH_max, GH_min, GH_max, T_min, T_max, NO3_min, NO3_max, PO4_min, PO4_max) = sp
    probs = []
    if ph is not None and (ph < pH_min or ph > pH_max):
        probs.append(f"pH вне диапазона {pH_min}-{pH_max}")
    if gh is not None and (gh < GH_min or gh > GH_max):
        probs.append(f"GH вне диапазона {GH_min}-{GH_max}")
    if t is not None and (t < T_min or t > T_max):
        probs.append(f"Температура вне диапазона {T_min}-{T_max}°C")
    if no3 is not None and (no3 < NO3_min or no3 > NO3_max):
        probs.append(f"NO₃ вне диапазона {NO3_min}-{NO3_max} мг/л")
    if po4 is not None and (po4 < PO4_min or po4 > PO4_max):
        probs.append(f"PO₄ вне диапазона {PO4_min}-{PO4_max} мг/л")
    return (len(probs) == 0), probs

# =============== UI КНОПКИ ===============
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Аквариум"), KeyboardButton(text="📃 Аквариумы")],
            [KeyboardButton(text="🧪 Измерение"), KeyboardButton(text="📈 График")],
            [KeyboardButton(text="🐟 Добавить рыбу"), KeyboardButton(text="🌿 Добавить растение")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="💡 Советы")]
        ],
        resize_keyboard=True
    )

def aquariums_inline(rows: List[Tuple[int, str]], action_prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for aq_id, name in rows:
        buttons.append([InlineKeyboardButton(text=f"{aq_id} • {name}", callback_data=f"{action_prefix}:{aq_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# =============== БОТ ===============
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
r = Router()
dp.include_router(r)

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
async def ensure_user(user_id: int, username: Optional[str]):
    row = await adb_exec("SELECT 1 FROM users WHERE user_id=%s", (user_id,), "one")
    if not row:
        await adb_exec("INSERT INTO users(user_id, username) VALUES(%s,%s)", (user_id, username), "none")

async def get_active_aq(user_id: int) -> Optional[int]:
    row = await adb_exec("SELECT active_aquarium_id FROM users WHERE user_id=%s", (user_id,), "one")
    if row and row[0]:
        return int(row[0])
    return None

async def get_last_meas(aq_id: int) -> Optional[Dict[str, float]]:
    row = await adb_exec("""
        SELECT ph, gh, temperature_c, no2, no3, tan, nh3, nh4, po4
        FROM measurements
        WHERE aquarium_id=%s
        ORDER BY measured_at DESC
        LIMIT 1
    """, (aq_id,), "one")
    if not row:
        return None
    cols = ["ph", "gh", "temperature_c", "no2", "no3", "tan", "nh3", "nh4", "po4"]
    return {cols[i]: (None if row[i] is None else float(row[i])) for i in range(len(cols))}

def parse_kv_args(s: str) -> Dict[str, float]:
    """
    Пример: "ph=7.2 gh=8 kh=4 no2=0.02 no3=10 tan=0.2 po4=0.5 t=25"
    Ключи: ph, gh, kh, no2, no3, tan, po4, t|temp
    """
    out = {}
    for chunk in s.split():
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip().lower()
        v = v.strip().replace(",", ".")
        try:
            out[k] = float(v)
        except:
            pass
    return out

# ---------- КОМАНДЫ ----------
@r.message(Command("start"))
async def cmd_start(m: Message):
    await ensure_user(m.from_user.id, m.from_user.username)
    await m.answer(
        "Привет! Я 🐠 <b>Aquaballance</b>.\n"
        "Я помогу вести параметры воды, проверю совместимость рыб/растений и дам рекомендации.\n\n"
        "Команды:\n"
        "• /add_aquarium <название> [объём_л]\n"
        "• /list_aquariums — список и выбор активного\n"
        "• /add_measure ph=.. gh=.. kh=.. no2=.. no3=.. tan=.. po4=.. t=..\n"
        "• /history [N] — последние N измерений (по умолчанию 5)\n"
        "• /add_fish <вид> <кол-во>\n"
        "• /add_plant <вид> <кол-во>\n"
        "• /suggest — советы по последним измерениям\n"
        "• /chart <метрика> [N] — график (ph/no3/nh3 и т.д.)\n",
        reply_markup=main_menu()
    )

@r.message(F.text == "📃 Аквариумы")
@r.message(Command("list_aquariums"))
async def list_aquariums(m: Message):
    await ensure_user(m.from_user.id, m.from_user.username)
    rows = await adb_exec("SELECT id, name FROM aquariums WHERE user_id=%s ORDER BY id", (m.from_user.id,), "all")
    if not rows:
        await m.answer("У тебя пока нет аквариумов. Добавь: /add_aquarium <название> [объём_л]")
        return
    kb = aquariums_inline([(r[0], r[1]) for r in rows], "setactive")
    await m.answer("Выбери активный аквариум:", reply_markup=kb)

@r.callback_query(F.data.startswith("setactive:"))
async def set_active_cb(cq: CallbackQuery):
    aq_id = int(cq.data.split(":")[1])
    # проверим, что аквариум принадлежит пользователю
    row = await adb_exec("SELECT 1 FROM aquariums WHERE id=%s AND user_id=%s", (aq_id, cq.from_user.id), "one")
    if not row:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await adb_exec("UPDATE users SET active_aquarium_id=%s WHERE user_id=%s", (aq_id, cq.from_user.id))
    await cq.message.edit_text(f"Активный аквариум: <b>{aq_id}</b>")
    await cq.answer("Готово!")

@r.message(F.text == "➕ Аквариум")
async def shortcut_add_aq(m: Message):
    await m.answer("Использование: /add_aquarium <название> [объём_л]")

@r.message(Command("add_aquarium"))
async def add_aquarium(m: Message):
    await ensure_user(m.from_user.id, m.from_user.username)
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await m.answer("Использование: /add_aquarium <название> [объём_л]")
        return
    name = parts[1].strip()
    volume = None
    if len(parts) == 3:
        try:
            volume = float(parts[2].replace(",", "."))
        except:
            volume = None
    await adb_exec(
        "INSERT INTO aquariums(user_id, name, volume_l) VALUES(%s,%s,%s)",
        (m.from_user.id, name, volume)
    )
    # если активного нет — назначим
    active = await get_active_aq(m.from_user.id)
    if not active:
        row = await adb_exec("SELECT id FROM aquariums WHERE user_id=%s ORDER BY id DESC LIMIT 1",
                             (m.from_user.id,), "one")
        if row:
            await adb_exec("UPDATE users SET active_aquarium_id=%s WHERE user_id=%s", (row[0], m.from_user.id))
    await m.answer(f"✅ Аквариум «{name}» добавлен.", reply_markup=main_menu())

@r.message(F.text == "🧪 Измерение")
async def shortcut_measure(m: Message):
    await m.answer("Пример:\n"
                   "<code>/add_measure ph=7.2 gh=8 kh=4 no2=0.02 no3=10 tan=0.2 po4=0.5 t=25</code>")

@r.message(Command("add_measure"))
async def add_measure(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    kv = parse_kv_args(m.text or "")
    # маппинг допустимых ключей
    ph = kv.get("ph")
    kh = kv.get("kh")
    gh = kv.get("gh")
    no2 = kv.get("no2")
    no3 = kv.get("no3")
    tan = kv.get("tan")
    po4 = kv.get("po4")
    t = kv.get("t") or kv.get("temp") or kv.get("temperature") or kv.get("temperature_c")
    nh3 = nh4 = None
    if tan is not None and ph is not None and t is not None:
        nh3, nh4 = split_tan_to_nh3_nh4(tan, ph, t)

    await adb_exec("""
        INSERT INTO measurements (aquarium_id, ph, kh, gh, no2, no3, tan, nh3, nh4, po4, temperature_c)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (aq_id, ph, kh, gh, no2, no3, tan, nh3, nh4, po4, t))
    txt = "✅ Измерение сохранено."
    if nh3 is not None and nh4 is not None:
        txt += f"\nРасчёт: NH₃={nh3:.3f} мг/л, NH₄={nh4:.3f} мг/л"
    await m.answer(txt)

@r.message(Command("history"))
async def history(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    parts = (m.text or "").split()
    limit = 5
    if len(parts) == 2:
        try:
            limit = max(1, min(30, int(parts[1])))
        except:
            pass
    rows = await adb_exec("""
        SELECT measured_at, ph, kh, gh, no2, no3, tan, nh3, nh4, po4, temperature_c
        FROM measurements WHERE aquarium_id=%s
        ORDER BY measured_at DESC LIMIT %s
    """, (aq_id, limit), "all")
    if not rows:
        await m.answer("История пуста. Добавь измерение: /add_measure ...")
        return
    lines = []
    for r in rows:
        t = r[0].strftime("%Y-%m-%d %H:%M")
        ph, kh, gh, no2, no3, tan, nh3, nh4, po4, tc = r[1:11]
        lines.append(
            f"• {t}  pH={ph} KH={kh} GH={gh} NO₂={no2} NO₃={no3} TAN={tan} NH₃={nh3} NH₄={nh4} PO₄={po4} T={tc}°C"
        )
    await m.answer("Последние измерения:\n" + "\n".join(lines))

@r.message(F.text == "🐟 Добавить рыбу")
async def shortcut_fish(m: Message):
    await m.answer("Пример: <code>/add_fish гуппи 5</code>")

@r.message(Command("add_fish"))
async def add_fish(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await m.answer("Использование: /add_fish <вид> <кол-во>")
        return
    species = parts[1].strip()
    try:
        qty = int(parts[2])
    except:
        await m.answer("Количество должно быть целым числом.")
        return

    # проверка совместимости на основе последних измерений
    meas = await get_last_meas(aq_id) or {}
    ok, probs = check_fish_compat(meas, species)
    await adb_exec("INSERT INTO aquarium_fish (aquarium_id, species, qty) VALUES (%s,%s,%s)", (aq_id, species, qty))
    if ok:
        await m.answer(f"✅ «{species}» x{qty} добавлены. Совместимость: OK.")
    else:
        await m.answer("⚠️ Добавлены, но есть замечания:\n- " + "\n- ".join(probs))

@r.message(F.text == "🌿 Добавить растение")
async def shortcut_plant(m: Message):
    await m.answer("Пример: <code>/add_plant анубиас 3</code>")

@r.message(Command("add_plant"))
async def add_plant(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await m.answer("Использование: /add_plant <вид> <кол-во>")
        return
    species = parts[1].strip()
    try:
        qty = int(parts[2])
    except:
        await m.answer("Количество должно быть целым числом.")
        return

    meas = await get_last_meas(aq_id) or {}
    ok, probs = check_plant_compat(meas, species)
    await adb_exec("INSERT INTO aquarium_plants (aquarium_id, species, qty) VALUES (%s,%s,%s)", (aq_id, species, qty))
    if ok:
        await m.answer(f"✅ «{species}» x{qty} добавлены. Совместимость: OK.")
    else:
        await m.answer("⚠️ Добавлены, но есть замечания:\n- " + "\n- ".join(probs))

@r.message(F.text == "⚙️ Настройки")
async def shortcut_settings(m: Message):
    await m.answer("Пример:\n"
                   "<code>/set_water_change 30 7</code>\n"
                   "означает 30% каждые 7 дней.")

@r.message(Command("set_water_change"))
async def set_water_change(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    parts = (m.text or "").split()
    if len(parts) != 3:
        await m.answer("Использование: /set_water_change <процент> <период_дней>")
        return
    try:
        pct = float(parts[1].replace(",", "."))
        days = int(parts[2])
    except:
        await m.answer("Проверь формат чисел.")
        return
    await adb_exec("""
        INSERT INTO water_settings(aquarium_id, change_volume_pct, period_days)
        VALUES (%s,%s,%s)
        ON CONFLICT (aquarium_id) DO UPDATE
        SET change_volume_pct=EXCLUDED.change_volume_pct,
            period_days=EXCLUDED.period_days
    """, (aq_id, pct, days))
    await m.answer(f"✅ Настройки подмен сохранены: {pct}% каждые {days} дн.")

@r.message(F.text == "💡 Советы")
@r.message(Command("suggest"))
async def suggest(m: Message):
    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return
    meas = await get_last_meas(aq_id)
    if not meas:
        await m.answer("Нет измерений. Добавь: /add_measure ...")
        return
    tips = []
    if meas.get("nh3") is not None and meas["nh3"] > 0.02:
        tips.append("NH₃ высокий — срочно подмена 30–50%, усилить аэрацию.")
    if meas.get("no2") is not None and meas["no2"] > 0.1:
        tips.append("NO₂ высокий — проверь фильтр, биологическую загрузку, подмена воды.")
    if meas.get("no3") is not None and meas["no3"] > 30:
        tips.append("NO₃ > 30 мг/л — увеличь частоту подмен, добавь живые растения.")
    if meas.get("ph") is not None and (meas["ph"] < 6.0 or meas["ph"] > 8.5):
        tips.append("pH вне 6.0–8.5 — проверь KH и корректируй плавно.")
    if not tips:
        tips.append("Показатели в норме для большинства неприхотливых рыб. Продолжай наблюдение.")
    await m.answer("Рекомендации:\n- " + "\n- ".join(tips))

# (опционально) простой график одной метрики
@r.message(F.text == "📈 График")
async def shortcut_chart(m: Message):
    await m.answer("Пример: <code>/chart ph 20</code> (метрика и количество точек)")

@r.message(Command("chart"))
async def chart_cmd(m: Message):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        await m.answer(f"Модуль matplotlib не установлен: {e}")
        return

    user_id = m.from_user.id
    aq_id = await get_active_aq(user_id)
    if not aq_id:
        await m.answer("Сначала выбери активный аквариум: /list_aquariums")
        return

    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Использование: /chart <метрика> [N]. Метрики: ph, no3, nh3, po4, t")
        return
    metric = parts[1].lower()
    key_map = {"ph":"ph","no3":"no3","nh3":"nh3","po4":"po4","t":"temperature_c"}
    col = key_map.get(metric)
    if not col:
        await m.answer("Неизвестная метрика. Доступно: ph, no3, nh3, po4, t")
        return
    limit = 20
    if len(parts) >= 3:
        try:
            limit = max(3, min(100, int(parts[2])))
        except:
            pass

    rows = await adb_exec(
        f"SELECT measured_at, {col} FROM measurements WHERE aquarium_id=%s AND {col} IS NOT NULL ORDER BY measured_at DESC LIMIT %s",
        (aq_id, limit),
        "all"
    )
    if not rows:
        await m.answer("Нет данных для графика.")
        return

    xs = [r[0] for r in rows][::-1]
    ys = [float(r[1]) for r in rows][::-1]

    # рисуем
    import matplotlib.pyplot as plt
    plt.figure()
    plt.plot(xs, ys, marker="o")
    plt.title(f"{metric.upper()} динамика")
    plt.xlabel("Дата")
    plt.ylabel(metric.upper())
    plt.tight_layout()
    path = "/tmp/chart.png"
    plt.savefig(path)
    plt.close()
    await bot.send_photo(m.chat.id, FSInputFile(path))

# =============== FASTAPI APP ДЛЯ HEALTH + ЖИЗНЕННОГО ПОРТА ===============
app = FastAPI()

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "service": "aquaballance-bot"})

@app.on_event("startup")
async def on_startup():
    init_db_pool()
    await ensure_schema()
    # Стартуем лонг-поллинг как фоновую задачу
    asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))

@app.on_event("shutdown")
async def on_shutdown():
    if db_pool:
        db_pool.closeall()
    await bot.session.close()

# Локальный запуск: uvicorn main:app --reload
if __name__ == "__main__":
    # локально можно просто поднять uvicorn
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
