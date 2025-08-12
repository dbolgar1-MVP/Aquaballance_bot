# main.py — минимальный FastAPI webhook + простая sqlite/postgres интеграция
import os, math, io, datetime
from fastapi import FastAPI, Request, HTTPException
import requests
from sqlalchemy import create_engine, text
import matplotlib.pyplot as plt

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN env var")
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
# For sqlite use check_same_thread; for postgres it's ignored
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})

app = FastAPI()

# Create minimal tables if not exist
DDL = """
CREATE TABLE IF NOT EXISTS aquariums (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT,
  name TEXT,
  volume_l INTEGER,
  created_at TIMESTAMP DEFAULT now()
);
CREATE TABLE IF NOT EXISTS water_tests (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER,
  measured_at TIMESTAMP,
  ph NUMERIC, kh NUMERIC, gh NUMERIC,
  no2 NUMERIC, no3 NUMERIC, nh3_total NUMERIC,
  po4 NUMERIC, temp_c NUMERIC,
  fraction_nh3 NUMERIC, unionized_nh3_mgL NUMERIC,
  created_at TIMESTAMP DEFAULT now()
);
"""
with engine.begin() as conn:
    # Render/Postgres accepts SERIAL; sqlite will just create columns (works for MVP)
    for stmt in DDL.split(";"):
        if stmt.strip():
            conn.execute(text(stmt))

def send_message(chat_id:int, text:str):
    requests.post(f"{BOT_API}/sendMessage", json={"chat_id": chat_id, "text": text})

def send_photo(chat_id:int, png_bytes:bytes, caption:str=""):
    files = {"photo": ("chart.png", png_bytes, "image/png")}
    data = {"chat_id": chat_id, "caption": caption}
    requests.post(f"{BOT_API}/sendPhoto", data=data, files=files)

def fraction_unioned_ammonia(pH:float, temp_c:float) -> float:
    # pKa approximation (temperature dependent)
    pKa = 0.09018 + 2729.92 / (273.2 + temp_c)
    frac = 1.0 / (1.0 + 10**(pKa - pH))
    return frac

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    if token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")
    update = await req.json()
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return {"ok": True}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()
    # /start
    if text.startswith("/start"):
        send_message(chat_id, "Привет! Я АкваХранитель (MVP).\nКоманды: /add_aq name volume_l, /add_test aq_id ph kh gh no2 no3 nh3_total po4 temp_c, /chart aq_id param")
        return {"ok": True}
    # /add_aq <name> <volume>
    if text.startswith("/add_aq"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "Использование: /add_aq <name> <volume_l>")
            return {"ok": True}
        name = parts[1]
        try:
            volume = int(parts[2])
        except:
            send_message(chat_id, "Volume должен быть числом (литры).")
            return {"ok": True}
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO aquariums(chat_id,name,volume_l) VALUES(:c,:n,:v)"),
                         {"c": chat_id, "n": name, "v": volume})
        send_message(chat_id, f"Аквариум '{name}' {volume}л добавлен.")
        return {"ok": True}
    # /add_test <aq_id> ph kh gh no2 no3 nh3_total po4 temp_c
    if text.startswith("/add_test"):
        parts = text.split()
        if len(parts) < 9:
            send_message(chat_id, "Использование: /add_test <aq_id> ph kh gh no2 no3 nh3_total po4 temp_c")
            return {"ok": True}
        try:
            aq_id = int(parts[1]); ph=float(parts[2]); kh=float(parts[3]); gh=float(parts[4])
            no2=float(parts[5]); no3=float(parts[6]); nh3_total=float(parts[7]); po4=float(parts[8]); temp_c=float(parts[9])
        except Exception as e:
            send_message(chat_id, "Ошибка парсинга: " + str(e))
            return {"ok": True}
        frac = fraction_unioned_ammonia(ph, temp_c)
        unionized = nh3_total * frac
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO water_tests(aquarium_id,measured_at,ph,kh,gh,no2,no3,nh3_total,po4,temp_c,fraction_nh3,unionized_nh3_mgL)
                VALUES(:aq,:m,:ph,:kh,:gh,:no2,:no3,:nh3,:po4,:temp,:frac,:un)
            """), {"aq":aq_id,"m":datetime.datetime.utcnow(),"ph":ph,"kh":kh,"gh":gh,"no2":no2,"no3":no3,"nh3":nh3_total,"po4":po4,"temp":temp_c,"frac":frac,"un":unionized})
        reply = f"Тест сохранён. unionized NH3 fraction={frac:.4f}, unionized NH3={unionized:.4f} mg/L"
        if unionized > 0.05:
            reply += "\n⚠️ Внимание: unionized NH3 > 0.05 mg/L — возможна токсичность."
        send_message(chat_id, reply)
        return {"ok": True}
    # /chart <aq_id> <param>
    if text.startswith("/chart"):
        parts = text.split()
        if len(parts)<3:
            send_message(chat_id, "Использование: /chart <aq_id> <param>")
            return {"ok": True}
        aq_id = int(parts[1]); param = parts[2]
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT measured_at,ph,kh,gh,no2,no3,nh3_total,po4,temp_c,unionized_nh3_mgL FROM water_tests WHERE aquarium_id=:aq ORDER BY measured_at"),
                                {"aq":aq_id}).fetchall()
        if not rows:
            send_message(chat_id, "Нет тестов для этого аквариума.")
            return {"ok": True}
        # map param to column index
        cols = {"ph":1,"kh":2,"gh":3,"no2":4,"no3":5,"nh3_total":6,"po4":7,"temp_c":8,"unionized_nh3":9}
        if param not in cols:
            send_message(chat_id, "Параметр неизвестен.")
            return {"ok": True}
        xs = [r[0] for r in rows]
        ys = [float(r[cols[param]]) for r in rows]
        plt.figure(figsize=(8,4)); plt.plot(xs,ys,marker='o'); plt.title(f"AQ {aq_id} — {param}"); plt.grid(True)
        buf = io.BytesIO(); plt.savefig(buf, format='png', bbox_inches='tight'); buf.seek(0); plt.close()
        send_photo(chat_id, buf.getvalue(), caption=f"Динамика {param}")
        return {"ok": True}
    # unknown
    send_message(chat_id, "Команда не распознана. /start для помощи.")
    return {"ok": True}

