import os
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import asyncpg
from utils.plot_utils import plot_time_series
from utils.compatibility import check_compatibility
from utils.db import init_db, create_aquarium, list_aquariums, insert_water_test, get_latest_test, get_water_tests_for_aquarium

load_dotenv(".env")  # load .env (or Aquaballance_bot.env if you prefer)

BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

if not BOT_TOKEN or not DATABASE_URL:
    raise RuntimeError('BOT_TOKEN and DATABASE_URL must be set in .env')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aquaballance")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Very small in-memory state machine for sequential prompts (user_id -> state dict)
USER_STATE = {}

def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить аквариум"), KeyboardButton(text="📋 Список аквариумов")],
            [KeyboardButton(text="💧 Добавить тест воды"), KeyboardButton(text="📈 Графики")],
            [KeyboardButton(text="🐠 Добавить обитателя"), KeyboardButton(text="❌ Удалить аквариум")]
        ],
        resize_keyboard=True
    )

@dp.message(Command(commands=['start']))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я Aquaballance_bot — помощник по качеству воды. Выберите действие:", reply_markup=kb_main())

@dp.message(lambda m: m.text == "➕ Добавить аквариум")
async def cmd_add_aquarium(message: types.Message):
    USER_STATE[message.from_user.id] = {'action':'add_aquarium', 'step':1, 'data':{}}
    await message.answer('Введите имя аквариума:')

@dp.message(lambda m: m.text == "📋 Список аквариумов")
async def cmd_list_aquariums(message: types.Message):
    rows = await list_aquariums(dp.storage.pool, message.from_user.id)
    if not rows:
        await message.answer('У вас нет аквариумов. Добавьте первым пунктом меню.')
        return
    text = '\n'.join(f"ID:{r['id']} — {r['name']} ({r['volume_l']} L)" for r in rows)
    await message.answer('Ваши аквариумы:\n' + text)

@dp.message(lambda m: m.text == "❌ Удалить аквариум")
async def cmd_delete_aquarium(message: types.Message):
    rows = await list_aquariums(dp.storage.pool, message.from_user.id)
    if not rows:
        await message.answer('Аквариумов нет.')
        return
    text = '\n'.join(f"ID:{r['id']} — {r['name']}" for r in rows)
    await message.answer('Выберите ID аквариума для удаления (введите ID):\n' + text)
    USER_STATE[message.from_user.id] = {'action':'delete_aquarium', 'step':1}

@dp.message(lambda m: m.text == "💧 Добавить тест воды")
async def cmd_add_test(message: types.Message):
    rows = await list_aquariums(dp.storage.pool, message.from_user.id)
    if not rows:
        await message.answer('Аквариумов нет. Сначала добавьте аквариум.')
        return
    text = '\n'.join(f"ID:{r['id']} — {r['name']}" for r in rows)
    await message.answer('Выберите ID аквариума для теста (введите ID):\n' + text)
    USER_STATE[message.from_user.id] = {'action':'add_test', 'step':1, 'data':{}}

@dp.message(lambda m: m.text == "📈 Графики")
async def cmd_graphs(message: types.Message):
    rows = await list_aquariums(dp.storage.pool, message.from_user.id)
    if not rows:
        await message.answer('Аквариумов нет.')
        return
    text = '\n'.join(f"ID:{r['id']} — {r['name']}" for r in rows)
    await message.answer('Выберите ID аквариума для графика (введите ID):\n' + text)
    USER_STATE[message.from_user.id] = {'action':'plots', 'step':1}

@dp.message(lambda m: m.text == "🐠 Добавить обитателя")
async def cmd_add_inhabitant(message: types.Message):
    rows = await list_aquariums(dp.storage.pool, message.from_user.id)
    if not rows:
        await message.answer('Аквариумов нет.')
        return
    text = '\n'.join(f"ID:{r['id']} — {r['name']}" for r in rows)
    await message.answer('Выберите ID аквариума для добавления обитателя (введите ID):\n' + text)
    USER_STATE[message.from_user.id] = {'action':'add_inhabitant', 'step':1}

@dp.message()
async def generic_handler(message: types.Message):
    uid = message.from_user.id
    state = USER_STATE.get(uid)
    text = message.text.strip()

    # If no pending state - ignore or show main menu
    if not state:
        await message.answer('Выберите действие из меню', reply_markup=kb_main())
        return

    action = state['action']

    # ADD AQUARIUM flow
    if action == 'add_aquarium':
        if state['step'] == 1:
            state['data']['name'] = text
            state['step'] = 2
            await message.answer('Введите объём (в литрах):')
            return
        if state['step'] == 2:
            try:
                vol = float(text)
            except:
                await message.answer('Неверный формат объёма. Введите число (литры).')
                return
            state['data']['volume_l'] = vol
            state['step'] = 3
            await message.answer('Краткое описание (или отправьте пустую строку):')
            return
        if state['step'] == 3:
            desc = text
            data = state['data']
            await create_aquarium(dp.storage.pool, message.from_user.id, data['name'], data['volume_l'], desc)
            USER_STATE.pop(uid, None)
            await message.answer(f"Аквариум '{data['name']}' ({data['volume_l']} L) создан.", reply_markup=kb_main())
            return

    # DELETE AQUARIUM flow
    if action == 'delete_aquarium':
        try:
            aid = int(text)
        except:
            await message.answer('Введите корректный ID (число).')
            return
        # perform delete
        await init_db.delete_aquarium(dp.storage.pool, aid)
        USER_STATE.pop(uid, None)
        await message.answer(f'Аквариум ID {aid} удалён.', reply_markup=kb_main())
        return

    # ADD TEST flow (sequential prompts)
    if action == 'add_test':
        if state['step'] == 1:
            try:
                aid = int(text)
            except:
                await message.answer('Введите корректный ID аквариума (число).')
                return
            state['data']['aquarium_id'] = aid
            state['step'] = 2
            await message.answer('Введите pH (например 7.2):')
            return
        # collect fields in order: ph, kh, gh, no2, no3, tan, po4, temp
        fields = ['ph','kh','gh','no2','no3','tan','po4','temp']
        cur_step = state['step'] - 2
        if cur_step < len(fields):
            try:
                val = float(text)
            except:
                await message.answer('Введите числовое значение, например 7.2')
                return
            state['data'][fields[cur_step]] = val
            state['step'] += 1
            if cur_step+1 < len(fields):
                await message.answer(f'Введите {fields[cur_step+1]}:')
                return
            else:
                # done, insert into DB
                d = state['data']
                measured_at = datetime.utcnow()
                await insert_water_test(dp.storage.pool, d['aquarium_id'], measured_at,
                                        d.get('ph'), d.get('kh'), d.get('gh'),
                                        d.get('no2'), d.get('no3'), d.get('tan'),
                                        d.get('po4'), d.get('temp'))
                USER_STATE.pop(uid, None)
                await message.answer('Тест воды сохранён.', reply_markup=kb_main())
                # after saving, compute compatibility alerts optionally
                return

    # PLOTS flow
    if action == 'plots':
        try:
            aid = int(text)
        except:
            await message.answer('Введите корректный ID аквариума.')
            return
        rows = await get_water_tests_for_aquarium(dp.storage.pool, aid, limit=50)
        if not rows:
            await message.answer('Нет данных по тестам.')
            USER_STATE.pop(uid, None)
            return
        dates = [r['measured_at'].isoformat() for r in rows]
        phs = [float(r['ph']) if r['ph'] is not None else None for r in rows]
        img = plot_time_series(dates, phs, f'ph_a{aid}')
        with open(img, 'rb') as fh:
            await message.answer_photo(photo=fh, caption='График pH (последние измерения)')
        USER_STATE.pop(uid, None)
        return

    # ADD INHABITANT flow
    if action == 'add_inhabitant':
        if state['step'] == 1:
            try:
                aid = int(text)
            except:
                await message.answer('Введите корректный ID аквариума (число).')
                return
            state['data']['aquarium_id'] = aid
            state['step'] = 2
            await message.answer('Введите название обитателя (на русском или латинице):')
            return
        if state['step'] == 2:
            name = text
            aid = state['data']['aquarium_id']
            latest = await get_latest_test(dp.storage.pool, aid)
            if not latest:
                await message.answer('Нет данных по параметрам воды — сначала внесите тест.')
                USER_STATE.pop(uid, None)
                return
            ph = float(latest['ph']) if latest['ph'] is not None else 7.0
            gh = float(latest['gh']) if latest['gh'] is not None else 8.0
            temp = float(latest['temp']) if latest['temp'] is not None else 25.0
            result = check_compatibility(name, ph, gh, temp)
            await message.answer(result, reply_markup=kb_main())
            USER_STATE.pop(uid, None)
            return

async def on_startup():
    # create DB pool and save to dispatcher.storage.pool
    dp.storage.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    # initialize tables (if not yet)
    await init_db(dp.storage.pool)
    log.info('DB pool created and init_db done.')

async def on_shutdown():
    await bot.session.close()
    await dp.storage.pool.close()

if __name__ == '__main__':
    try:
        asyncio.get_event_loop().run_until_complete(on_startup())
        log.info('Starting bot (Long Polling)...')
        dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        log.info('Shutting down...')
        asyncio.get_event_loop().run_until_complete(on_shutdown())
