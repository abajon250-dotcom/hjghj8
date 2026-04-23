import asyncio
import sqlite3
import time
import os
import re
from datetime import datetime
import aiohttp
import random

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ChatType

from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.errors import FloodWaitError, SessionPasswordNeededError, AuthKeyError, UnauthorizedError
import vk_api

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # если не нужен, оставьте пустым

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID, API_HASH must be set in environment variables")

DB_PATH = "bot.db"
SESSIONS_DIR = "/tmp/sessions" if os.name != 'nt' else "sessions"
TARIFFS = {"day": {"days": 1, "price": 2.5, "name": "1 день"},
           "week": {"days": 7, "price": 9, "name": "1 неделя"},
           "month": {"days": 30, "price": 15, "name": "1 месяц"}}

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        sub_until INTEGER DEFAULT 0,
        balance REAL DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tg_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_tg_id INTEGER,
        phone TEXT,
        session_file TEXT,
        is_active INTEGER DEFAULT 1,
        name TEXT DEFAULT '',
        last_used INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vk_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_tg_id INTEGER,
        token TEXT,
        vk_name TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        wallet TEXT,
        status TEXT DEFAULT 'pending'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS promocodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        days INTEGER,
        uses INTEGER DEFAULT 0,
        max_uses INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS used_promocodes (
        user_id INTEGER,
        code_id INTEGER,
        used_at INTEGER
    )''')
    try:
        c.execute("ALTER TABLE tg_accounts ADD COLUMN name TEXT DEFAULT ''")
    except: pass
    try:
        c.execute("ALTER TABLE tg_accounts ADD COLUMN last_used INTEGER DEFAULT 0")
    except: pass
    try:
        c.execute("ALTER TABLE vk_accounts ADD COLUMN is_active INTEGER DEFAULT 1")
    except: pass
    conn.commit()
    conn.close()

def get_user(tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, sub_until, balance FROM users WHERE tg_id=?", (tg_id,))
    row = c.fetchone()
    conn.close()
    return {"tg_id": row[0], "username": row[1], "sub_until": row[2] or 0, "balance": row[3]} if row else None

def create_user(tg_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (tg_id, username, sub_until, balance) VALUES (?, ?, 0, 0)", (tg_id, username))
    conn.commit()
    conn.close()

def is_subscribed(tg_id):
    if tg_id == ADMIN_ID:
        return True
    user = get_user(tg_id)
    return user and user["sub_until"] > int(time.time())

def set_subscription(tg_id, days):
    new_time = int(time.time()) + days * 86400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET sub_until=? WHERE tg_id=?", (new_time, tg_id))
    conn.commit()
    conn.close()

def get_balance(tg_id):
    user = get_user(tg_id)
    return user["balance"] if user else 0

def update_balance(tg_id, delta):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE tg_id=?", (delta, tg_id))
    conn.commit()
    conn.close()

def set_balance(tg_id, new_balance):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET balance = ? WHERE tg_id=?", (new_balance, tg_id))
    conn.commit()
    conn.close()

def add_tg_account(owner_tg_id, phone, session_file, name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tg_accounts (owner_tg_id, phone, session_file, name, last_used) VALUES (?,?,?,?,?)",
              (owner_tg_id, phone, session_file, name, int(time.time())))
    conn.commit()
    conn.close()

def get_user_tg_accounts(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, phone, name, is_active FROM tg_accounts WHERE owner_tg_id=? ORDER BY last_used DESC", (owner_tg_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "phone": r[1], "name": r[2], "is_active": r[3]} for r in rows]

def get_active_tg_account(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone, name FROM tg_accounts WHERE owner_tg_id=? AND is_active=1 ORDER BY last_used DESC LIMIT 1", (owner_tg_id,))
    row = c.fetchone()
    conn.close()
    return {"session_file": row[0], "phone": row[1], "name": row[2]} if row else None

def set_active_tg_account(owner_tg_id, account_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tg_accounts SET is_active=0 WHERE owner_tg_id=?", (owner_tg_id,))
    c.execute("UPDATE tg_accounts SET is_active=1, last_used=? WHERE id=? AND owner_tg_id=?", (int(time.time()), account_id, owner_tg_id))
    conn.commit()
    conn.close()

def delete_tg_account(owner_tg_id, account_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tg_accounts WHERE id=? AND owner_tg_id=?", (account_id, owner_tg_id))
    conn.commit()
    conn.close()

def deactivate_tg_account(owner_tg_id, account_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tg_accounts SET is_active=0 WHERE id=? AND owner_tg_id=?", (account_id, owner_tg_id))
    conn.commit()
    conn.close()

def add_vk_account(owner_tg_id, token, vk_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO vk_accounts (owner_tg_id, token, vk_name, is_active) VALUES (?,?,?,1)", (owner_tg_id, token, vk_name))
    conn.commit()
    conn.close()

def get_user_vk_accounts(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, vk_name, is_active FROM vk_accounts WHERE owner_tg_id=?", (owner_tg_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "is_active": r[2]} for r in rows]

def get_active_vk_account(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token, vk_name FROM vk_accounts WHERE owner_tg_id=? AND is_active=1 LIMIT 1", (owner_tg_id,))
    row = c.fetchone()
    conn.close()
    return {"token": row[0], "name": row[1]} if row else None

def set_active_vk_account(owner_tg_id, account_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE vk_accounts SET is_active=0 WHERE owner_tg_id=?", (owner_tg_id,))
    c.execute("UPDATE vk_accounts SET is_active=1 WHERE id=? AND owner_tg_id=?", (account_id, owner_tg_id))
    conn.commit()
    conn.close()

def delete_vk_account(owner_tg_id, account_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM vk_accounts WHERE id=? AND owner_tg_id=?", (account_id, owner_tg_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, sub_until, balance FROM users")
    rows = c.fetchall()
    conn.close()
    return [{"tg_id": r[0], "username": r[1], "sub_until": r[2] or 0, "balance": r[3]} for r in rows]

def add_withdraw_request(user_id, amount, wallet):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO withdraw_requests (user_id, amount, wallet, status) VALUES (?,?,?, 'pending')", (user_id, amount, wallet))
    conn.commit()
    conn.close()

def get_pending_withdraws():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, amount, wallet FROM withdraw_requests WHERE status='pending'")
    rows = c.fetchall()
    conn.close()
    return rows

def update_withdraw_status(req_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE withdraw_requests SET status=? WHERE id=?", (status, req_id))
    conn.commit()
    conn.close()

def create_promocode(code, days, max_uses=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO promocodes (code, days, max_uses) VALUES (?,?,?)", (code, days, max_uses))
    conn.commit()
    conn.close()

def get_promocode(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, days, uses, max_uses FROM promocodes WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return {"id": row[0], "days": row[1], "uses": row[2], "max_uses": row[3]} if row else None

def use_promocode(user_id, code_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE promocodes SET uses = uses + 1 WHERE id=?", (code_id,))
    c.execute("INSERT INTO used_promocodes (user_id, code_id, used_at) VALUES (?,?,?)", (user_id, code_id, int(time.time())))
    conn.commit()
    conn.close()

def get_all_promocodes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, code, days, uses, max_uses FROM promocodes")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "code": r[1], "days": r[2], "uses": r[3], "max_uses": r[4]} for r in rows]

def delete_promocode(code_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM promocodes WHERE id=?", (code_id,))
    conn.commit()
    conn.close()

# ========== CRYPTOBOT ==========
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

async def create_crypto_invoice(amount_usd: float, description: str):
    if not CRYPTOBOT_TOKEN:
        return None
    url = f"{CRYPTOBOT_API_URL}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    payload = {"asset": "USDT", "amount": str(amount_usd), "description": description, "paid_btn_name": "callback", "paid_btn_url": f"https://t.me/{BOT_TOKEN.split(':')[0]}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"pay_url": data["result"]["pay_url"], "invoice_id": data["result"]["invoice_id"]}
                return None
    except:
        return None

async def check_crypto_invoice(invoice_id: str):
    if not CRYPTOBOT_TOKEN:
        return None
    url = f"{CRYPTOBOT_API_URL}/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    params = {"invoice_ids": invoice_id}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["items"]:
                    return data["result"]["items"][0]["status"]
                return None
    except:
        return None

# ========== ОБРАБОТКА СЛЕТА СЕССИИ ==========
async def handle_session_error(user_id: int, account_id: int, phone: str):
    deactivate_tg_account(user_id, account_id)
    await bot.send_message(user_id, f"❌ Аккаунт {phone} был автоматически удалён из-за слетевшей сессии.")

def get_russian_error(e: Exception) -> str:
    error = str(e)
    if "Cannot find any entity" in error:
        return "Не удалось найти пользователя или чат. Проверьте ID или username."
    if "Too many requests" in error:
        return "Слишком много запросов. Подождите немного."
    if "FloodWaitError" in error:
        return "Превышен лимит запросов. Попробуйте позже."
    if "AuthKeyError" in error or "UnauthorizedError" in error:
        return "Сессия устарела. Аккаунт будет удалён."
    if "disconnected" in error:
        return "Соединение разорвано. Попробуйте ещё раз."
    if "The message cannot be empty" in error:
        return "Сообщение не может быть пустым. Добавьте текст или файл."
    return error

# ========== ПРОВЕРКА ПОДПИСКИ НА КАНАЛ (опционально) ==========
async def is_subscribed_to_channel(user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ["member", "creator", "administrator"]
    except:
        return False

@dp.callback_query(lambda c: c.data not in ["check_sub"])
async def subscription_middleware(callback: types.CallbackQuery):
    if not await is_subscribed_to_channel(callback.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton(text="✅ Проверить", callback_data="check_sub")]
        ])
        await callback.message.answer(f"❌ Подпишитесь на @{CHANNEL_USERNAME}", reply_markup=keyboard)
        await callback.answer()
        return

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: types.CallbackQuery):
    if await is_subscribed_to_channel(callback.from_user.id):
        await callback.message.delete()
        await start_cmd(callback.message)
    else:
        await callback.answer("❌ Вы не подписаны", show_alert=True)

# ========== КЛАВИАТУРЫ ==========
def main_menu(tg_id):
    buttons = [
        [InlineKeyboardButton(text="🎲 Играть", callback_data="game_menu")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔧 Мои аккаунты", callback_data="my_accounts")]
    ]
    if tg_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="👑 Админ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def game_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Куб", callback_data="game_cube")],
        [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="game_basketball")],
        [InlineKeyboardButton(text="🎯 Дартс", callback_data="game_darts")],
        [InlineKeyboardButton(text="⚽ Футбол", callback_data="game_football")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def cube_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Больше/Меньше (x2)", callback_data="cube_less_more")],
        [InlineKeyboardButton(text="Чёт/Нечет (x2)", callback_data="cube_even_odd")],
        [InlineKeyboardButton(text="Угадать число (x6)", callback_data="cube_exact")],
        [InlineKeyboardButton(text="Диапазон (x3)", callback_data="cube_range")],
        [InlineKeyboardButton(text="Больше/Меньше 3.5 (x2)", callback_data="cube_35")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
    ])

def basketball_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Точное попадание (x7)", callback_data="basket_exact")],
        [InlineKeyboardButton(text="Попадание в кольцо (x3)", callback_data="basket_ring")],
        [InlineKeyboardButton(text="Мимо (x1.5)", callback_data="basket_miss")],
        [InlineKeyboardButton(text="Щит (x2.5)", callback_data="basket_board")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
    ])

def darts_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Яблочко (x10)", callback_data="darts_bullseye")],
        [InlineKeyboardButton(text="20 (x5)", callback_data="darts_20")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
    ])

def football_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Гол в девятку (x8)", callback_data="foot_nine")],
        [InlineKeyboardButton(text="Гол в створ (x3)", callback_data="foot_target")],
        [InlineKeyboardButton(text="Мимо (x1.5)", callback_data="foot_miss")],
        [InlineKeyboardButton(text="Штанга/перекладина (x5)", callback_data="foot_post")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
    ])

def my_accounts_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Мои Telegram аккаунты", callback_data="list_tg_accounts")],
        [InlineKeyboardButton(text="📘 Мои VK аккаунты", callback_data="list_vk_accounts")],
        [InlineKeyboardButton(text="➕ Подключить новый", callback_data="connect_new_account")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def connect_new_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Telegram", callback_data="add_tg")],
        [InlineKeyboardButton(text="📘 VK", callback_data="add_vk")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="my_accounts")]
    ])

def tg_accounts_list(user_id):
    accounts = get_user_tg_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']} ({acc['phone']})", callback_data=f"tg_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add_tg")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def vk_accounts_list(user_id):
    accounts = get_user_vk_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']}", callback_data=f"vk_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add_vk")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="➕ Выдать баланс", callback_data="admin_add_balance"),
         InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin_remove_balance")],
        [InlineKeyboardButton(text="📢 Глобал рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎫 Промокоды", callback_data="admin_promocodes")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💰 Заявки на вывод", callback_data="admin_withdraws")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def after_game_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Ещё раз", callback_data="again")],
        [InlineKeyboardButton(text="⬆️ Повысить ставку (+1$)", callback_data="inc_bet")],
        [InlineKeyboardButton(text="⬇️ Понизить ставку (-1$)", callback_data="dec_bet")],
        [InlineKeyboardButton(text="💰 Ва-банк", callback_data="all_in")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])

def back_button(callback_data):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]])

# ========== FSM ==========
class AddTG(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()

class AddVK(StatesGroup):
    waiting_token = State()

class BroadcastTG(StatesGroup):
    waiting_text = State()
    waiting_delay = State()

class BroadcastVK(StatesGroup):
    waiting_text = State()
    waiting_delay = State()

class AdminAddBalance(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

class AdminRemoveBalance(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

class AdminBroadcast(StatesGroup):
    waiting_type = State()
    waiting_text = State()
    waiting_photo = State()
    waiting_confirm = State()

class AdminCreatePromocode(StatesGroup):
    waiting_code = State()
    waiting_days = State()
    waiting_max_uses = State()

class Withdraw(StatesGroup):
    waiting_amount = State()
    waiting_wallet = State()

class Deposit(StatesGroup):
    waiting_amount = State()

class GameBet(StatesGroup):
    waiting_bet = State()

class GameCube(StatesGroup):
    waiting_choice = State()
    waiting_exact = State()
    waiting_range = State()

class GameBasketball(StatesGroup):
    waiting_choice = State()

class GameDarts(StatesGroup):
    waiting_choice = State()

class GameFootball(StatesGroup):
    waiting_choice = State()

class ManageTG(StatesGroup):
    waiting_new_avatar = State()
    waiting_cloud_password = State()
    waiting_code_for_login = State()
    waiting_new_name = State()
    waiting_new_username = State()

class TGAction(StatesGroup):
    waiting_target = State()
    waiting_message = State()
    waiting_join_link = State()
    waiting_photo = State()
    waiting_file = State()
    waiting_schedule_delay = State()

class ActivatePromo(StatesGroup):
    waiting_code = State()

user_game_data = {}

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def show_tg_account_info(message: types.Message, client: TelegramClient, phone: str):
    try:
        if not client.is_connected():
            await client.connect()
        me = await client.get_me()
        if me is None:
            await message.answer("❌ Не удалось получить информацию об аккаунте.")
            return
        country_map = {"7": "🇷🇺 Россия", "380": "🇺🇦 Украина", "375": "🇧🇾 Беларусь", "1": "🇺🇸 США", "44": "🇬🇧 Великобритания", "49": "🇩🇪 Германия", "90": "🇹🇷 Турция", "86": "🇨🇳 Китай", "91": "🇮🇳 Индия"}
        country = "Неизвестно"
        if phone and phone.startswith('+'):
            for code in country_map:
                if phone.startswith('+' + code):
                    country = country_map[code]
                    break
        spam_status = "✅ Нет ограничений"
        try:
            spambot = await client.get_entity('@Spambot')
            await client.send_message(spambot, '/start')
            await asyncio.sleep(2)
            async for msg in client.iter_messages(spambot, limit=1):
                if msg and msg.text:
                    if 'no restrictions' in msg.text.lower():
                        spam_status = "✅ Нет ограничений"
                    elif 'limited' in msg.text.lower() or 'restricted' in msg.text.lower():
                        spam_status = "⚠️ Есть ограничения"
        except:
            spam_status = "❓ Не удалось проверить"
        dialogs = await client.get_dialogs()
        users = [d for d in dialogs if d.is_user]
        total_contacts = len(users)
        total_dialogs = len(dialogs)
        mutual = len([u for u in users if u.entity.username and u.entity.username])  # приблизительно
        info = (f"📱 *Telegram аккаунт*\n📞 Номер: `{phone[:4]}****{phone[-3:] if len(phone)>7 else ''}`\n🆔 ID: `{me.id}`\n👤 Имя: {me.first_name or ''} {me.last_name or ''}\n🌍 Страна: {country}\n🔒 Спам-блок: {spam_status}\n👥 Контактов (всего): {total_contacts}\n💬 Диалогов (всего): {total_dialogs}\n🤝 Взаимных контактов: {mutual}\n✅ Аккаунт подключён!")
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")

async def show_vk_account_info(message: types.Message, token: str):
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        user = vk.users.get(fields="city, country, followers_count, bdate")[0]
        user_id = user['id']
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        city = user.get('city', {}).get('title', 'Не указан')
        country = user.get('country', {}).get('title', 'Не указана')
        bdate = user.get('bdate', 'Не указана')
        followers = user.get('followers_count', 0)
        friends = vk.friends.get()['count']
        online = vk.friends.getOnline()['count'] if 'count' in vk.friends.getOnline() else 0
        info = (f"📘 *VK аккаунт*\n👤 Имя: {first_name} {last_name}\n🆔 ID: {user_id}\n🏙️ Город: {city}\n🌍 Страна: {country}\n🎂 Дата рождения: {bdate}\n👥 Друзей: {friends}\n👁️ Подписчиков: {followers}\n🟢 Онлайн друзей: {online}\n✅ Аккаунт подключён!")
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")

# ========== ОСНОВНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    create_user(message.from_user.id, message.from_user.username or str(message.from_user.id))
    await message.answer("🎲 Добро пожаловать!\nИспользуйте кнопки меню.", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Главное меню", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    balance = user["balance"]
    sub_until = datetime.fromtimestamp(user["sub_until"]).strftime('%d.%m.%Y %H:%M') if user["sub_until"] else "Нет"
    text = f"👤 Профиль\n💰 Баланс: {balance:.2f}$\n⏳ Подписка до: {sub_until}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пополнить", callback_data="deposit"),
         InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(text="💎 Подписка", callback_data="buy_sub"),
         InlineKeyboardButton(text="🎫 Активировать промокод", callback_data="activate_promo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Управление аккаунтами", reply_markup=my_accounts_menu())
    await callback.answer()

@dp.callback_query(F.data == "connect_new_account")
async def connect_new(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Выберите тип аккаунта:", reply_markup=connect_new_menu())
    await callback.answer()

@dp.callback_query(F.data == "list_tg_accounts")
async def list_tg_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    accounts = get_user_tg_accounts(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("У вас нет Telegram аккаунтов. Подключите новый.", reply_markup=back_button("my_accounts"))
        await callback.answer()
        return
    await callback.message.edit_text("Выберите аккаунт:", reply_markup=tg_accounts_list(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_acc_"))
async def tg_account_actions(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    accounts = get_user_tg_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активным" if not acc["is_active"] else "✅ Активен", callback_data=f"tg_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"tg_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="💬 Вступить в группу/канал", callback_data=f"tg_join_{acc_id}")],
        [InlineKeyboardButton(text="🚪 Выйти из чата", callback_data=f"tg_leave_{acc_id}")],
        [InlineKeyboardButton(text="✏️ Отправить сообщение", callback_data=f"tg_send_msg_{acc_id}")],
        [InlineKeyboardButton(text="🖼️ Отправить фото", callback_data=f"tg_send_photo_{acc_id}")],
        [InlineKeyboardButton(text="📄 Отправить документ", callback_data=f"tg_send_doc_{acc_id}")],
        [InlineKeyboardButton(text="⏰ Отложенная отправка", callback_data=f"tg_schedule_{acc_id}")],
        [InlineKeyboardButton(text="📋 Список диалогов", callback_data=f"tg_dialogs_{acc_id}")],
        [InlineKeyboardButton(text="🔐 Завершить все сессии", callback_data=f"tg_terminate_{acc_id}")],
        [InlineKeyboardButton(text="🔄 Обновить информацию", callback_data=f"tg_refresh_info_{acc_id}")],
        [InlineKeyboardButton(text="🖌️ Сменить аватарку", callback_data=f"tg_change_avatar_{acc_id}")],
        [InlineKeyboardButton(text="🔑 Установить облачный пароль", callback_data=f"tg_cloud_password_{acc_id}")],
        [InlineKeyboardButton(text="📲 Запросить код для входа", callback_data=f"tg_request_code_{acc_id}")],
        [InlineKeyboardButton(text="✏️ Сменить имя", callback_data=f"tg_change_name_{acc_id}")],
        [InlineKeyboardButton(text="📛 Сменить username", callback_data=f"tg_change_username_{acc_id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"tg_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_tg_accounts")]
    ])
    await callback.message.edit_text(f"Аккаунт: {acc['name']} ({acc['phone']})\nВыберите действие:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_set_active_"))
async def tg_set_active(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    set_active_tg_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт установлен как активный!", show_alert=True)
    await list_tg_accounts(callback)

@dp.callback_query(F.data.startswith("tg_del_"))
async def tg_delete(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    delete_tg_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_tg_accounts(callback)

# ========== УПРАВЛЕНИЕ АККАУНТОМ ==========
@dp.callback_query(F.data.startswith("tg_change_avatar_"))
async def tg_change_avatar_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Пришлите новое фото для аватарки:")
    await state.set_state(ManageTG.waiting_new_avatar)
    await callback.answer()

@dp.message(ManageTG.waiting_new_avatar, F.photo)
async def tg_change_avatar_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await message.bot.download_file(file.file_path, file_path)
        await client(UploadProfilePhotoRequest(file=await client.upload_file(file_path)))
        await message.answer("✅ Аватарка успешно изменена!")
    except (AuthKeyError, UnauthorizedError) as e:
        await handle_session_error(message.from_user.id, acc_id, phone)
        await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_cloud_password_"))
async def tg_cloud_password_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("🔐 Введите новый облачный пароль (минимум 1 символ):")
    await state.set_state(ManageTG.waiting_cloud_password)
    await callback.answer()

@dp.message(ManageTG.waiting_cloud_password)
async def tg_cloud_password_set(message: types.Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 1:
        await message.answer("❌ Пароль не может быть пустым.")
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client.edit_2fa(new_password=password)
        await message.answer("✅ Облачный пароль (2FA) успешно установлен!")
    except (AuthKeyError, UnauthorizedError) as e:
        await handle_session_error(message.from_user.id, acc_id, phone)
        await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_request_code_"))
async def tg_request_code(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        if me:
            await callback.message.answer("✅ Аккаунт уже активен. Код не требуется.")
            return
    except (AuthKeyError, UnauthorizedError):
        try:
            await client.send_code_request(phone)
            await callback.message.answer(f"📲 Код подтверждения отправлен на номер {phone}. Введите его командой /verifycode <код>")
            await state.update_data(acc_id=acc_id, phone=phone, session_file=session_file)
            await state.set_state(ManageTG.waiting_code_for_login)
        except Exception as e:
            await callback.message.answer(f"❌ Ошибка при запросе кода: {get_russian_error(str(e))}")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.message(ManageTG.waiting_code_for_login)
async def tg_verify_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    acc_id = data["acc_id"]
    phone = data["phone"]
    session_file = data["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone, code)
        await message.answer("✅ Код подтверждён. Аккаунт активен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_name_"))
async def tg_change_name_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новое имя (first name):")
    await state.set_state(ManageTG.waiting_new_name)
    await callback.answer()

@dp.message(ManageTG.waiting_new_name)
async def tg_change_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    new_name = message.text.strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateProfileRequest(first_name=new_name))
        await message.answer(f"✅ Имя успешно изменено на {new_name}")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tg_accounts SET name=? WHERE id=?", (new_name, acc_id))
        conn.commit()
        conn.close()
    except (AuthKeyError, UnauthorizedError) as e:
        await handle_session_error(message.from_user.id, acc_id, phone)
        await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_username_"))
async def tg_change_username_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новый username (без @):")
    await state.set_state(ManageTG.waiting_new_username)
    await callback.answer()

@dp.message(ManageTG.waiting_new_username)
async def tg_change_username(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    new_username = message.text.strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateUsernameRequest(username=new_username))
        await message.answer(f"✅ Username успешно изменён на @{new_username}")
    except (AuthKeyError, UnauthorizedError) as e:
        await handle_session_error(message.from_user.id, acc_id, phone)
        await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_refresh_info_"))
async def tg_refresh_info(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        if me is None:
            await callback.message.answer("❌ Не удалось получить информацию об аккаунте.")
            return
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or str(me.id)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tg_accounts SET name=? WHERE id=?", (name, acc_id))
        conn.commit()
        conn.close()
        await callback.message.answer(f"✅ Информация обновлена: {name}")
    except (AuthKeyError, UnauthorizedError) as e:
        await handle_session_error(callback.from_user.id, acc_id, phone)
        await callback.message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(str(e))}")
    finally:
        await client.disconnect()
    await callback.answer()# ========== ОСНОВНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    create_user(message.from_user.id, message.from_user.username or str(message.from_user.id))
    await message.answer("🎲 Добро пожаловать!\nИспользуйте кнопки меню.", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Главное меню", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    balance = user["balance"]
    sub_until = datetime.fromtimestamp(user["sub_until"]).strftime('%d.%m.%Y %H:%M') if user["sub_until"] else "Нет"
    text = f"👤 Профиль\n💰 Баланс: {balance:.2f}$\n⏳ Подписка до: {sub_until}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пополнить", callback_data="deposit"),
         InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(text="💎 Подписка", callback_data="buy_sub")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Управление аккаунтами", reply_markup=my_accounts_menu())
    await callback.answer()

@dp.callback_query(F.data == "connect_new_account")
async def connect_new(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("Выберите тип аккаунта:", reply_markup=connect_new_menu())
    await callback.answer()

@dp.callback_query(F.data == "list_tg_accounts")
async def list_tg_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    accounts = get_user_tg_accounts(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("У вас нет Telegram аккаунтов. Подключите новый.", reply_markup=back_button("my_accounts"))
        await callback.answer()
        return
    await callback.message.edit_text("Выберите аккаунт:", reply_markup=tg_accounts_list(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_acc_"))
async def tg_account_actions(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    accounts = get_user_tg_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активным" if not acc["is_active"] else "✅ Активен", callback_data=f"tg_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"tg_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="💬 Вступить в группу/канал", callback_data=f"tg_join_{acc_id}")],
        [InlineKeyboardButton(text="🚪 Выйти из чата", callback_data=f"tg_leave_{acc_id}")],
        [InlineKeyboardButton(text="✏️ Отправить сообщение", callback_data=f"tg_send_msg_{acc_id}")],
        [InlineKeyboardButton(text="🖼️ Отправить фото", callback_data=f"tg_send_photo_{acc_id}")],
        [InlineKeyboardButton(text="📄 Отправить документ", callback_data=f"tg_send_doc_{acc_id}")],
        [InlineKeyboardButton(text="⏰ Отложенная отправка", callback_data=f"tg_schedule_{acc_id}")],
        [InlineKeyboardButton(text="📋 Список диалогов", callback_data=f"tg_dialogs_{acc_id}")],
        [InlineKeyboardButton(text="🔐 Завершить все сессии", callback_data=f"tg_terminate_{acc_id}")],
        [InlineKeyboardButton(text="🔄 Обновить информацию", callback_data=f"tg_refresh_info_{acc_id}")],
        [InlineKeyboardButton(text="🖌️ Сменить аватарку", callback_data=f"tg_change_avatar_{acc_id}")],
        [InlineKeyboardButton(text="🔑 Установить облачный пароль", callback_data=f"tg_cloud_password_{acc_id}")],
        [InlineKeyboardButton(text="📲 Запросить код для входа", callback_data=f"tg_request_code_{acc_id}")],
        [InlineKeyboardButton(text="✏️ Сменить имя", callback_data=f"tg_change_name_{acc_id}")],
        [InlineKeyboardButton(text="📛 Сменить username", callback_data=f"tg_change_username_{acc_id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"tg_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_tg_accounts")]
    ])
    await callback.message.edit_text(f"Аккаунт: {acc['name']} ({acc['phone']})\nВыберите действие:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_set_active_"))
async def tg_set_active(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    set_active_tg_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт установлен как активный!", show_alert=True)
    await list_tg_accounts(callback)

@dp.callback_query(F.data.startswith("tg_del_"))
async def tg_delete(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    delete_tg_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_tg_accounts(callback)

# ========== УПРАВЛЕНИЕ АККАУНТОМ ==========
@dp.callback_query(F.data.startswith("tg_change_avatar_"))
async def tg_change_avatar_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Пришлите новое фото для аватарки:")
    await state.set_state(ManageTG.waiting_new_avatar)
    await callback.answer()

@dp.message(ManageTG.waiting_new_avatar, F.photo)
async def tg_change_avatar_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await message.bot.download_file(file.file_path, file_path)
        await client(UploadProfilePhotoRequest(file=await client.upload_file(file_path)))
        await message.answer("✅ Аватарка успешно изменена!")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# Установка облачного пароля (2FA)
@dp.callback_query(F.data.startswith("tg_cloud_password_"))
async def tg_cloud_password_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("🔐 Введите новый облачный пароль (минимум 1 символ):")
    await state.set_state(ManageTG.waiting_cloud_password)
    await callback.answer()

@dp.message(ManageTG.waiting_cloud_password)
async def tg_cloud_password_set(message: types.Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 1:
        await message.answer("❌ Пароль не может быть пустым.")
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client.edit_2fa(new_password=password)
        await message.answer("✅ Облачный пароль (2FA) успешно установлен!")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        error_text = str(e)
        if "PASSWORD_HASH_INVALID" in error_text:
            await message.answer("❌ Неверный текущий пароль. Сначала снимите 2FA или укажите верный пароль.")
        elif "FLOOD_WAIT" in error_text:
            await message.answer("❌ Слишком много попыток. Подождите несколько минут.")
        else:
            await message.answer(f"❌ Ошибка: {error_text}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_request_code_"))
async def tg_request_code(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        if me:
            await callback.message.answer("✅ Аккаунт уже активен. Код не требуется.")
            return
    except (AuthKeyError, UnauthorizedError):
        try:
            await client.send_code_request(phone)
            await callback.message.answer(f"📲 Код подтверждения отправлен на номер {phone}. Введите его через /verifycode <код>")
            await state.update_data(acc_id=acc_id, phone=phone, session_file=session_file)
            await state.set_state(ManageTG.waiting_code_for_login)
        except Exception as e:
            await callback.message.answer(f"❌ Ошибка при запросе кода: {get_russian_error(e)}")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.message(ManageTG.waiting_code_for_login)
async def tg_verify_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    acc_id = data["acc_id"]
    phone = data["phone"]
    session_file = data["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone, code)
        await message.answer("✅ Код подтверждён. Аккаунт активен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_name_"))
async def tg_change_name_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новое имя (first name):")
    await state.set_state(ManageTG.waiting_new_name)
    await callback.answer()

@dp.message(ManageTG.waiting_new_name)
async def tg_change_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    new_name = message.text.strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateProfileRequest(first_name=new_name))
        await message.answer(f"✅ Имя успешно изменено на {new_name}")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tg_accounts SET name=? WHERE id=?", (new_name, acc_id))
        conn.commit()
        conn.close()
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_username_"))
async def tg_change_username_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новый username (без @):")
    await state.set_state(ManageTG.waiting_new_username)
    await callback.answer()

@dp.message(ManageTG.waiting_new_username)
async def tg_change_username(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    new_username = message.text.strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateUsernameRequest(username=new_username))
        await message.answer(f"✅ Username успешно изменён на @{new_username}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# ========== ОТПРАВКА СООБЩЕНИЙ, ФОТО, ДОКУМЕНТОВ, ОТЛОЖЕННАЯ ОТПРАВКА ==========
@dp.callback_query(F.data.startswith("tg_send_msg_"))
async def tg_send_msg_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя (например, @username или 123456789):")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_send_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw.replace('-', '').isdigit():
        target = raw
    else:
        target = raw
    await state.update_data(target=target)
    await message.answer("Введите текст сообщения:")
    await state.set_state(TGAction.waiting_message)

@dp.message(TGAction.waiting_message)
async def tg_send_text(message: types.Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("❌ Текст сообщения не может быть пустым. Введите текст.")
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    text = message.text
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        try:
            entity = await client.get_entity(target)
        except ValueError:
            if target.isdigit():
                entity = await client.get_entity(int(target))
            else:
                raise Exception(f"Не удалось найти пользователя {target}")
        await client.send_message(entity, text)
        await message.answer(f"✅ Сообщение отправлено в {target}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
        await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
    except FloodWaitError as e:
        await message.answer(f"⚠️ Слишком много запросов. Подождите {e.seconds} секунд.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# Отправка фото (аналогично, с обработкой ошибок)
# Отправка фото (без обязательного текста)
@dp.callback_query(F.data.startswith("tg_send_photo_"))
async def tg_send_photo_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_photo_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw.replace('-', '').isdigit():
        target = raw
    else:
        target = raw
    await state.update_data(target=target)
    await message.answer("Теперь пришлите фото (можно с подписью):")
    await state.set_state(TGAction.waiting_photo)

@dp.message(TGAction.waiting_photo, F.photo)
async def tg_send_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        try:
            entity = await client.get_entity(target)
        except ValueError:
            if target.isdigit():
                entity = await client.get_entity(int(target))
            else:
                raise Exception("Не удалось найти пользователя")
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await message.bot.download_file(file.file_path, file_path)
        # Если есть подпись (caption), передаём её
        caption = message.caption if message.caption else None
        await client.send_file(entity, file_path, caption=caption)
        await message.answer(f"✅ Фото отправлено в {target}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# Отправка документа
@dp.callback_query(F.data.startswith("tg_send_doc_"))
async def tg_send_doc_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_doc_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw.replace('-', '').isdigit():
        target = raw
    else:
        target = raw
    await state.update_data(target=target)
    await message.answer("Теперь пришлите документ (файл):")
    await state.set_state(TGAction.waiting_file)

@dp.message(TGAction.waiting_file, F.document)
async def tg_send_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        try:
            entity = await client.get_entity(target)
        except ValueError:
            if target.isdigit():
                entity = await client.get_entity(int(target))
            else:
                raise Exception("Не удалось найти пользователя")
        doc = message.document
        file = await message.bot.get_file(doc.file_id)
        file_path = f"/tmp/{doc.file_id}"
        await message.bot.download_file(file.file_path, file_path)
        # Подпись (caption) для документа
        caption = message.caption if message.caption else None
        await client.send_file(entity, file_path, caption=caption)
        await message.answer(f"✅ Документ отправлен в {target}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()




# Отложенная отправка
@dp.callback_query(F.data.startswith("tg_schedule_"))
async def tg_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_schedule_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw.replace('-', '').isdigit():
        target = raw
    else:
        target = raw
    await state.update_data(target=target)
    await message.answer("Введите текст сообщения:")
    await state.set_state(TGAction.waiting_message)

@dp.message(TGAction.waiting_message)
async def tg_schedule_text(message: types.Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    await message.answer("Введите задержку в секундах (например, 60):")
    await state.set_state(TGAction.waiting_schedule_delay)

@dp.message(TGAction.waiting_schedule_delay)
async def tg_schedule_delay(message: types.Message, state: FSMContext):
    try:
        delay = int(message.text.strip())
        if delay <= 0:
            raise ValueError
        data = await state.get_data()
        acc_id = data["acc_id"]
        target = data["target"]
        text = data["message_text"]
        await message.answer(f"⏳ Сообщение будет отправлено через {delay} секунд.")
        await asyncio.sleep(delay)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
        row = c.fetchone()
        conn.close()
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file, phone = row
        client = TelegramClient(session_file, API_ID, API_HASH)
        await client.connect()
        try:
            await client.get_me()
            try:
                entity = await client.get_entity(target)
            except ValueError:
                if target.isdigit():
                    entity = await client.get_entity(int(target))
                else:
                    raise Exception(f"Не удалось найти пользователя {target}")
            await client.send_message(entity, text)
            await message.answer(f"✅ Отложенное сообщение отправлено в {target}")
        except (AuthKeyError, UnauthorizedError):
            await handle_session_error(message.from_user.id, acc_id, phone)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        finally:
            await client.disconnect()
    except:
        await message.answer("Введите корректное число секунд")
    await state.clear()

@dp.callback_query(F.data.startswith("tg_dialogs_"))
async def tg_dialogs_start(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        dialogs = await client.get_dialogs()
        text = "📋 Список диалогов (первые 20):\n"
        for i, d in enumerate(dialogs[:20]):
            name = d.name or str(d.entity.id)
            text += f"{i+1}. {name} (ID: {d.entity.id})\n"
        await callback.message.answer(text)
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(callback.from_user.id, acc_id, phone)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_terminate_"))
async def tg_terminate_sessions(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client.log_out()
        await callback.message.answer("✅ Все сессии завершены. Аккаунт будет удалён из списка.")
        deactivate_tg_account(callback.from_user.id, acc_id)
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(callback.from_user.id, acc_id, phone)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_refresh_info_"))
async def tg_refresh_info(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, callback.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        if me is None:
            await callback.message.answer("❌ Не удалось получить информацию об аккаунте.")
            return
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or str(me.id)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tg_accounts SET name=? WHERE id=?", (name, acc_id))
        conn.commit()
        conn.close()
        await callback.message.answer(f"✅ Информация обновлена: {name}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(callback.from_user.id, acc_id, phone)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_join_"))
async def tg_join_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ссылку или username группы/канала:")
    await state.set_state(TGAction.waiting_join_link)
    await callback.answer()

@dp.message(TGAction.waiting_join_link)
async def tg_join_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    link = message.text.strip()
    try:
        await client.get_me()
        if "joinchat" in link:
            hash_match = re.search(r'joinchat/([A-Za-z0-9_-]+)', link)
            if hash_match:
                await client(ImportChatInviteRequest(hash_match.group(1)))
                await message.answer("✅ Вступил(а) по ссылке-приглашению")
            else:
                raise Exception("Не удалось распознать ссылку")
        else:
            entity = await client.get_entity(link)
            await client(JoinChannelRequest(entity))
        await message.answer(f"✅ Вступил(а) в {link}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_leave_"))
async def tg_leave_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username чата/группы:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_leave_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file, phone = row
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    target = message.text.strip()
    try:
        await client.get_me()
        entity = await client.get_entity(target)
        await client.delete_dialog(entity)
        await message.answer(f"✅ Вышел(а) из чата {target}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_broadcast_"))
async def tg_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("📝 Введите текст рассылки:")
    await state.set_state(BroadcastTG.waiting_text)
    await callback.answer()

@dp.message(BroadcastTG.waiting_text)
async def broadcast_tg_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку между сообщениями (сек, рекомендуется 5):")
    await state.set_state(BroadcastTG.waiting_delay)

    @dp.message(BroadcastTG.waiting_delay)
    async def broadcast_tg_delay(message: types.Message, state: FSMContext):
        try:
            delay = float(message.text.strip())
            if delay < 1:
                await message.answer("⚠️ Минимальная задержка 1 секунда. Установлено 1 сек.")
                delay = 1
            data = await state.get_data()
            text = data["text"]
            acc_id = data["acc_id"]
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT session_file, phone FROM tg_accounts WHERE id=? AND owner_tg_id=?",
                      (acc_id, message.from_user.id))
            row = c.fetchone()
            conn.close()
            if not row:
                await message.answer("Аккаунт не найден")
                await state.clear()
                return
            session_file, phone = row
            client = TelegramClient(session_file, API_ID, API_HASH)
            await client.connect()
            try:
                await client.get_me()
                # Получаем все диалоги и фильтруем только пользователей
                dialogs = await client.get_dialogs()
                targets = [d for d in dialogs if d.is_user and not d.entity.bot]
                total = len(targets)
                if total == 0:
                    await message.answer("❌ Нет пользователей для рассылки.")
                    await state.clear()
                    return
                status_msg = await message.answer(f"🚀 Начинаю рассылку {total} контактам, задержка {delay} сек.")
                sent = 0
                for dialog in targets:
                    try:
                        await client.send_message(dialog.entity, text)
                        sent += 1
                        await asyncio.sleep(delay)
                        if sent % 10 == 0:
                            await status_msg.edit_text(f"📊 Прогресс: {sent}/{total}")
                    except Exception as e:
                        continue
                await status_msg.edit_text(f"✅ Отправлено {sent} из {total}")
            except (AuthKeyError, UnauthorizedError):
                await handle_session_error(message.from_user.id, acc_id, phone)
                await message.answer(f"❌ Аккаунт {phone} был удалён из-за слетевшей сессии.")
            finally:
                await client.disconnect()
                await state.clear()
        except Exception as e:
            await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
            await state.clear()
        # ========== VK АККАУНТЫ ==========
@dp.callback_query(F.data == "list_vk_accounts")
async def list_vk_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    accounts = get_user_vk_accounts(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("У вас нет VK аккаунтов. Подключите новый.", reply_markup=back_button("my_accounts"))
        await callback.answer()
        return
    await callback.message.edit_text("Выберите аккаунт:", reply_markup=vk_accounts_list(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_acc_"))
async def vk_account_actions(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    accounts = get_user_vk_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активным" if not acc["is_active"] else "✅ Активен", callback_data=f"vk_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"vk_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"vk_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_vk_accounts")]
    ])
    await callback.message.edit_text(f"Аккаунт: {acc['name']}\nВыберите действие:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_set_active_"))
async def vk_set_active(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    set_active_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт установлен как активный!", show_alert=True)
    await list_vk_accounts(callback)

@dp.callback_query(F.data.startswith("vk_del_"))
async def vk_delete(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    delete_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_vk_accounts(callback)

@dp.callback_query(F.data.startswith("vk_broadcast_"))
async def vk_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("📝 Введите текст рассылки:")
    await state.set_state(BroadcastVK.waiting_text)
    await callback.answer()

@dp.message(BroadcastVK.waiting_text)
async def broadcast_vk_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку между сообщениями (сек):")
    await state.set_state(BroadcastVK.waiting_delay)

@dp.message(BroadcastVK.waiting_delay)
async def broadcast_vk_delay(message: types.Message, state: FSMContext):
    try:
        delay = float(message.text.strip())
        data = await state.get_data()
        text = data["text"]
        acc_id = data["acc_id"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT token FROM vk_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
        row = c.fetchone()
        conn.close()
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row[0]
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        friends = vk.friends.get()["items"]
        convs = vk.messages.getConversations(count=200)["items"]
        targets = friends + [c["conversation"]["peer"]["id"] for c in convs]
        total = len(targets)
        await message.answer(f"Начинаю рассылку {total} получателям, задержка {delay} сек.")
        sent = 0
        for target in targets:
            try:
                if isinstance(target, int):
                    vk.messages.send(user_id=target, message=text, random_id=0)
                else:
                    vk.messages.send(peer_id=target, message=text, random_id=0)
                sent += 1
                await asyncio.sleep(delay)
            except:
                pass
        await message.answer(f"✅ Отправлено {sent} из {total}")
        await state.clear()
    except:
        await message.answer("Введите число")

# ========== ПОДПИСКА ==========
@dp.callback_query(F.data == "buy_sub")
async def buy_sub(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 2.5$", callback_data="tariff_day")],
        [InlineKeyboardButton(text="1 неделя - 9$", callback_data="tariff_week")],
        [InlineKeyboardButton(text="1 месяц - 15$", callback_data="tariff_month")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
    ])
    await callback.message.edit_text("Выберите тариф:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    tariff_key = callback.data.split("_")[1]
    tariff = TARIFFS[tariff_key]
    await state.update_data(tariff=tariff)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Оплатить с баланса", callback_data="pay_balance")],
        [InlineKeyboardButton(text="💳 Оплатить через CryptoBot", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Тариф: {tariff['name']} - {tariff['price']}$\nВыберите способ оплаты:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "pay_balance")
async def pay_balance(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка, выберите тариф заново", show_alert=True)
        return
    user_id = callback.from_user.id
    if get_balance(user_id) >= tariff["price"]:
        update_balance(user_id, -tariff["price"])
        set_subscription(user_id, tariff["days"])
        await callback.message.edit_text(f"✅ Подписка на {tariff['name']} активирована!", reply_markup=main_menu(user_id))
    else:
        await callback.answer(f"Не хватает. Нужно {tariff['price']}$", show_alert=True)
    await state.clear()
    await callback.answer()

crypto_pending = {}

@dp.callback_query(F.data == "pay_crypto")
async def pay_crypto(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка, выберите тариф заново", show_alert=True)
        return
    invoice = await create_crypto_invoice(tariff["price"], f"Подписка на {tariff['name']}")
    if not invoice:
        await callback.answer("Ошибка создания счёта. Попробуйте позже.", show_alert=True)
        return
    crypto_pending[callback.from_user.id] = {"invoice_id": invoice["invoice_id"], "days": tariff["days"]}
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_sub_{invoice['invoice_id']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Оплатите {tariff['price']} USDT", reply_markup=keyboard)
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("check_sub_"))
async def check_sub_payment(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in crypto_pending:
            days = crypto_pending[callback.from_user.id]["days"]
            set_subscription(callback.from_user.id, days)
            del crypto_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Подписка активирована на {days} дней!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Оплата подтверждена, но ошибка", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

# ========== ПОПОЛНЕНИЕ / ВЫВОД ==========
deposit_pending = {}

@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Сумма пополнения (мин 1$):")
    await state.set_state(Deposit.waiting_amount)
    await callback.answer()

@dp.message(Deposit.waiting_amount)
async def deposit_amount(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        amount = float(message.text.strip())
        if amount < 1:
            await message.answer("Мин 1$")
            return
        invoice = await create_crypto_invoice(amount, f"Пополнение баланса на {amount}$")
        if not invoice:
            await message.answer("Ошибка создания счёта", reply_markup=back_button("profile"))
            await state.clear()
            return
        deposit_pending[message.from_user.id] = {"invoice_id": invoice["invoice_id"], "amount": amount}
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_dep_{invoice['invoice_id']}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
        ])
        await message.answer(f"Счёт на {amount} USDT", reply_markup=keyboard)
    except:
        await message.answer("Введите число")
    await state.clear()

@dp.callback_query(F.data.startswith("check_dep_"))
async def check_dep_payment(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in deposit_pending:
            amount = deposit_pending[callback.from_user.id]["amount"]
            update_balance(callback.from_user.id, amount)
            del deposit_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Пополнение на {amount}$ успешно!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Ошибка зачисления", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Сумма вывода (мин 10$):")
    await state.set_state(Withdraw.waiting_amount)
    await callback.answer()

@dp.message(Withdraw.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        amount = float(message.text.strip())
        if amount < 10:
            await message.answer("Мин 10$")
            return
        if amount > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(amount=amount)
        await message.answer("💳 Адрес кошелька USDT TRC20:")
        await state.set_state(Withdraw.waiting_wallet)
    except:
        await message.answer("Введите число")

@dp.message(Withdraw.waiting_wallet)
async def withdraw_wallet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data["amount"]
    add_withdraw_request(message.from_user.id, amount, wallet)
    await message.answer(f"✅ Заявка на вывод {amount}$ создана", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(ADMIN_ID, f"📥 Заявка от {message.from_user.id}\nСумма: {amount}$\nКошелёк: {wallet}")
    await state.clear()

# ========== ПОДКЛЮЧЕНИЕ НОВЫХ АККАУНТОВ ==========
@dp.callback_query(F.data == "add_tg")
async def add_tg_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if not is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна платная подписка!", show_alert=True)
        return
    await callback.message.answer("📞 Введите номер телефона в формате +79991234567:")
    await state.set_state(AddTG.waiting_phone)
    await callback.answer()

@dp.message(AddTG.waiting_phone)
async def add_tg_phone(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    phone = message.text.strip()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_file = os.path.join(SESSIONS_DIR, f"{message.from_user.id}_{phone}.session")
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.send_code_request(phone)
        await state.update_data(phone=phone, session_file=session_file, client=client)
        await message.answer("🔑 Введите код из SMS (код действует 3 минуты):")
        await state.set_state(AddTG.waiting_code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

@dp.message(AddTG.waiting_code)
async def add_tg_code(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    code = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    phone = data["phone"]
    try:
        await client.sign_in(phone, code)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        add_tg_account(message.from_user.id, phone, data["session_file"], name)
        await show_tg_account_info(message, client, phone)
        await client.disconnect()
        await state.clear()
    except SessionPasswordNeededError:
        await message.answer("🔒 Введите двухфакторный пароль:")
        await state.set_state(AddTG.waiting_2fa)
    except Exception as e:
        error = str(e)
        if "expired" in error.lower():
            await message.answer("❌ Код истёк. Отправляю новый...")
            await client.send_code_request(phone)
        else:
            await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
            await client.disconnect()
            await state.clear()

@dp.message(AddTG.waiting_2fa)
async def add_tg_2fa(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    password = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        add_tg_account(message.from_user.id, data["phone"], data["session_file"], name)
        await show_tg_account_info(message, client, data["phone"])
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {get_russian_error(e)}")
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data == "add_vk")
async def add_vk_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if not is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна подписка!", show_alert=True)
        return
    await callback.message.answer("🔑 Введите токен VK (access_token) с правами на сообщения и друзей:")
    await state.set_state(AddVK.waiting_token)
    await callback.answer()

@dp.message(AddVK.waiting_token)
async def add_vk_token(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    token = message.text.strip()
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        user = vk.users.get(fields="city, country, followers_count, bdate")[0]
        name = f"{user['first_name']} {user['last_name']}"
        add_vk_account(message.from_user.id, token, name)
        await show_vk_account_info(message, token)
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Неверный токен или ошибка: {get_russian_error(e)}")
        await state.clear()

# ========== ИГРЫ (рабочие, с анимацией) ==========
@dp.callback_query(F.data == "game_menu")
async def game_menu_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Выберите игру:", reply_markup=game_menu())
    await callback.answer()

@dp.callback_query(F.data == "game_cube")
async def game_cube_menu(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Выберите режим игры в куб:", reply_markup=cube_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("cube_"))
async def game_cube_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    cube_mode = callback.data
    await state.update_data(cube_mode=cube_mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

@dp.message(GameBet.waiting_bet)
async def game_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Минимальная ставка 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"❌ Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(bet=bet)
        data = await state.get_data()
        if "cube_mode" in data:
            mode = data["cube_mode"]
            if mode in ["cube_less_more", "cube_even_odd", "cube_35"]:
                if mode == "cube_less_more":
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Меньше (1-3)", callback_data="cube_less"),
                         InlineKeyboardButton(text="Больше (4-6)", callback_data="cube_more")]
                    ])
                elif mode == "cube_even_odd":
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Чёт", callback_data="cube_even"),
                         InlineKeyboardButton(text="Нечет", callback_data="cube_odd")]
                    ])
                else:  # cube_35
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Больше 3.5", callback_data="cube_gt35"),
                         InlineKeyboardButton(text="Меньше 3.5", callback_data="cube_lt35")]
                    ])
                await message.answer("Выберите вариант:", reply_markup=keyboard)
                await state.set_state(GameCube.waiting_choice)
            elif mode == "cube_exact":
                await message.answer("Введите число от 1 до 6:")
                await state.set_state(GameCube.waiting_exact)
            elif mode == "cube_range":
                await message.answer("Введите диапазон в формате 'нижняя-верхняя' (например, 2-4):")
                await state.set_state(GameCube.waiting_range)
        elif "basketball_mode" in data:
            await state.set_state(GameBasketball.waiting_choice)
            await game_basketball_choice_after_bet(message, state)
        elif "darts_mode" in data:
            await state.set_state(GameDarts.waiting_choice)
            await game_darts_choice_after_bet(message, state)
        elif "football_mode" in data:
            await state.set_state(GameFootball.waiting_choice)
            await game_football_choice_after_bet(message, state)
    except:
        await message.answer("❌ Введите число")

# --- Кубик ---
@dp.callback_query(GameCube.waiting_choice)
async def game_cube_choice(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["cube_mode"]
    choice = callback.data
    msg = await callback.message.answer_dice(emoji="🎲")
    roll = msg.dice.value
    await asyncio.sleep(1)
    win = False
    if mode == "cube_less_more":
        if choice == "cube_less" and roll <= 3: win = True
        elif choice == "cube_more" and roll >= 4: win = True
    elif mode == "cube_even_odd":
        if choice == "cube_even" and roll % 2 == 0: win = True
        elif choice == "cube_odd" and roll % 2 == 1: win = True
    elif mode == "cube_35":
        if choice == "cube_gt35" and roll > 3.5: win = True
        elif choice == "cube_lt35" and roll < 3.5: win = True
    if win:
        payout = bet * 2
        update_balance(callback.from_user.id, payout)
        result = f"🎲 Выпало {roll}\n✅ ВЫИГРЫШ: {bet}$ x2 = {payout}$\n💰 Баланс: {get_balance(callback.from_user.id):.2f}$"
    else:
        update_balance(callback.from_user.id, -bet)
        result = f"🎲 Выпало {roll}\n❌ ПРОИГРЫШ: {bet}$\n💰 Баланс: {get_balance(callback.from_user.id):.2f}$"
    await callback.message.answer(result, reply_markup=after_game_menu())
    await state.clear()
    await callback.answer()

@dp.message(GameCube.waiting_exact)
async def game_cube_exact(message: types.Message, state: FSMContext):
    try:
        num = int(message.text.strip())
        if num < 1 or num > 6:
            raise ValueError
        data = await state.get_data()
        bet = data["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if roll == num:
            payout = bet * 6
            update_balance(message.from_user.id, payout)
            result = f"🎲 Выпало {roll}\n✅ УГАДАЛ! Выигрыш: {bet}$ x6 = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        else:
            update_balance(message.from_user.id, -bet)
            result = f"🎲 Выпало {roll}\n❌ НЕ УГАДАЛ. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        await message.answer(result, reply_markup=after_game_menu())
        await state.clear()
    except:
        await message.answer("❌ Введите число от 1 до 6")

@dp.message(GameCube.waiting_range)
async def game_cube_range(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split('-')
        low = int(parts[0])
        high = int(parts[1])
        if low < 1 or high > 6 or low > high:
            raise ValueError
        data = await state.get_data()
        bet = data["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if low <= roll <= high:
            payout = bet * 3
            update_balance(message.from_user.id, payout)
            result = f"🎲 Выпало {roll}\n✅ ПОПАЛ В ДИАПАЗОН! Выигрыш: {bet}$ x3 = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        else:
            update_balance(message.from_user.id, -bet)
            result = f"🎲 Выпало {roll}\n❌ МИМО ДИАПАЗОНА. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        await message.answer(result, reply_markup=after_game_menu())
        await state.clear()
    except:
        await message.answer("❌ Неверный формат. Пример: 2-4")

# --- Баскетбол ---
@dp.callback_query(F.data == "game_basketball")
async def game_basketball_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🏀 Выберите исход баскетбольного броска:", reply_markup=basketball_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("basket_"))
async def game_basketball_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(basketball_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_basketball_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["basketball_mode"]
    msg = await message.answer_dice(emoji="🏀")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "basket_exact": {"win": value == 5, "mult": 7, "name": "Точное попадание"},
        "basket_ring": {"win": value == 4, "mult": 3, "name": "Попадание в кольцо"},
        "basket_miss": {"win": value <= 2, "mult": 1.5, "name": "Мимо"},
        "basket_board": {"win": value == 3, "mult": 2.5, "name": "Щит"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"🏀 Бросок: {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"🏀 Бросок: {value}\n❌ {outcome['name']} не удалось. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# --- Дартс (без "Любое число") ---
@dp.callback_query(F.data == "game_darts")
async def game_darts_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎯 Выберите исход в дартсе:", reply_markup=darts_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("darts_"))
async def game_darts_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(darts_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_darts_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["darts_mode"]
    msg = await message.answer_dice(emoji="🎯")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "darts_bullseye": {"win": value == 6, "mult": 10, "name": "Яблочко"},
        "darts_20": {"win": value == 5, "mult": 5, "name": "20"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"🎯 Выпало {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"🎯 Выпало {value}\n❌ {outcome['name']} не выпало. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# --- Футбол ---
@dp.callback_query(F.data == "game_football")
async def game_football_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("⚽ Выберите исход футбольного удара:", reply_markup=football_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("foot_"))
async def game_football_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(football_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_football_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["football_mode"]
    msg = await message.answer_dice(emoji="⚽")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "foot_nine": {"win": value == 5, "mult": 8, "name": "Гол в девятку"},
        "foot_target": {"win": value == 4, "mult": 3, "name": "Гол в створ"},
        "foot_miss": {"win": value <= 2, "mult": 1.5, "name": "Мимо"},
        "foot_post": {"win": value == 3, "mult": 5, "name": "Штанга/перекладина"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"⚽ Удар: {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"⚽ Удар: {value}\n❌ {outcome['name']} не забит. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

@dp.callback_query(F.data == "again")
async def again_game(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await game_menu_callback(callback)

@dp.callback_query(F.data == "inc_bet")
async def inc_bet(callback: types.CallbackQuery):
    await callback.answer("Функция повышения ставки будет добавлена позже", show_alert=True)

@dp.callback_query(F.data == "dec_bet")
async def dec_bet(callback: types.CallbackQuery):
    await callback.answer("Функция понижения ставки будет добавлена позже", show_alert=True)

@dp.callback_query(F.data == "all_in")
async def all_in(callback: types.CallbackQuery):
    await callback.answer("Функция ва-банк будет добавлена позже", show_alert=True)# ========== VK АККАУНТЫ ==========
@dp.callback_query(F.data == "list_vk_accounts")
async def list_vk_accounts(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    accounts = get_user_vk_accounts(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("У вас нет VK аккаунтов. Подключите новый.", reply_markup=back_button("my_accounts"))
        await callback.answer()
        return
    await callback.message.edit_text("Выберите аккаунт:", reply_markup=vk_accounts_list(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_acc_"))
async def vk_account_actions(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    accounts = get_user_vk_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активным" if not acc["is_active"] else "✅ Активен", callback_data=f"vk_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"vk_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"vk_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_vk_accounts")]
    ])
    await callback.message.edit_text(f"Аккаунт: {acc['name']}\nВыберите действие:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_set_active_"))
async def vk_set_active(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[3])
    set_active_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт установлен как активный!", show_alert=True)
    await list_vk_accounts(callback)

@dp.callback_query(F.data.startswith("vk_del_"))
async def vk_delete(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    delete_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_vk_accounts(callback)

@dp.callback_query(F.data.startswith("vk_broadcast_"))
async def vk_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("📝 Введите текст рассылки:")
    await state.set_state(BroadcastVK.waiting_text)
    await callback.answer()

@dp.message(BroadcastVK.waiting_text)
async def broadcast_vk_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку между сообщениями (сек):")
    await state.set_state(BroadcastVK.waiting_delay)

@dp.message(BroadcastVK.waiting_delay)
async def broadcast_vk_delay(message: types.Message, state: FSMContext):
    try:
        delay = float(message.text.strip())
        data = await state.get_data()
        text = data["text"]
        acc_id = data["acc_id"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT token FROM vk_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
        row = c.fetchone()
        conn.close()
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row[0]
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        friends = vk.friends.get()["items"]
        convs = vk.messages.getConversations(count=200)["items"]
        targets = friends + [c["conversation"]["peer"]["id"] for c in convs]
        total = len(targets)
        await message.answer(f"Начинаю рассылку {total} получателям, задержка {delay} сек.")
        sent = 0
        for target in targets:
            try:
                if isinstance(target, int):
                    vk.messages.send(user_id=target, message=text, random_id=0)
                else:
                    vk.messages.send(peer_id=target, message=text, random_id=0)
                sent += 1
                await asyncio.sleep(delay)
            except:
                pass
        await message.answer(f"✅ Отправлено {sent} из {total}")
        await state.clear()
    except:
        await message.answer("Введите число")

# ========== ПОДПИСКА ==========
@dp.callback_query(F.data == "buy_sub")
async def buy_sub(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 2.5$", callback_data="tariff_day")],
        [InlineKeyboardButton(text="1 неделя - 9$", callback_data="tariff_week")],
        [InlineKeyboardButton(text="1 месяц - 15$", callback_data="tariff_month")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
    ])
    await callback.message.edit_text("Выберите тариф:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    tariff_key = callback.data.split("_")[1]
    tariff = TARIFFS[tariff_key]
    await state.update_data(tariff=tariff)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Оплатить с баланса", callback_data="pay_balance")],
        [InlineKeyboardButton(text="💳 Оплатить через CryptoBot", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Тариф: {tariff['name']} - {tariff['price']}$\nВыберите способ оплаты:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "pay_balance")
async def pay_balance(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка, выберите тариф заново", show_alert=True)
        return
    user_id = callback.from_user.id
    if get_balance(user_id) >= tariff["price"]:
        update_balance(user_id, -tariff["price"])
        set_subscription(user_id, tariff["days"])
        await callback.message.edit_text(f"✅ Подписка на {tariff['name']} активирована!", reply_markup=main_menu(user_id))
    else:
        await callback.answer(f"Не хватает. Нужно {tariff['price']}$", show_alert=True)
    await state.clear()
    await callback.answer()

crypto_pending = {}

@dp.callback_query(F.data == "pay_crypto")
async def pay_crypto(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка, выберите тариф заново", show_alert=True)
        return
    invoice = await create_crypto_invoice(tariff["price"], f"Подписка на {tariff['name']}")
    if not invoice:
        await callback.answer("Ошибка создания счёта. Попробуйте позже.", show_alert=True)
        return
    crypto_pending[callback.from_user.id] = {"invoice_id": invoice["invoice_id"], "days": tariff["days"]}
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_sub_{invoice['invoice_id']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Оплатите {tariff['price']} USDT", reply_markup=keyboard)
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("check_sub_"))
async def check_sub_payment(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in crypto_pending:
            days = crypto_pending[callback.from_user.id]["days"]
            set_subscription(callback.from_user.id, days)
            del crypto_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Подписка активирована на {days} дней!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Оплата подтверждена, но ошибка", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

# ========== ПОПОЛНЕНИЕ / ВЫВОД ==========
deposit_pending = {}

@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Сумма пополнения (мин 1$):")
    await state.set_state(Deposit.waiting_amount)
    await callback.answer()

@dp.message(Deposit.waiting_amount)
async def deposit_amount(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        amount = float(message.text.strip())
        if amount < 1:
            await message.answer("Мин 1$")
            return
        invoice = await create_crypto_invoice(amount, f"Пополнение баланса на {amount}$")
        if not invoice:
            await message.answer("Ошибка создания счёта", reply_markup=back_button("profile"))
            await state.clear()
            return
        deposit_pending[message.from_user.id] = {"invoice_id": invoice["invoice_id"], "amount": amount}
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_dep_{invoice['invoice_id']}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
        ])
        await message.answer(f"Счёт на {amount} USDT", reply_markup=keyboard)
    except:
        await message.answer("Введите число")
    await state.clear()

@dp.callback_query(F.data.startswith("check_dep_"))
async def check_dep_payment(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in deposit_pending:
            amount = deposit_pending[callback.from_user.id]["amount"]
            update_balance(callback.from_user.id, amount)
            del deposit_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Пополнение на {amount}$ успешно!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Ошибка зачисления", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Сумма вывода (мин 10$):")
    await state.set_state(Withdraw.waiting_amount)
    await callback.answer()

@dp.message(Withdraw.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        amount = float(message.text.strip())
        if amount < 10:
            await message.answer("Мин 10$")
            return
        if amount > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(amount=amount)
        await message.answer("💳 Адрес кошелька USDT TRC20:")
        await state.set_state(Withdraw.waiting_wallet)
    except:
        await message.answer("Введите число")

@dp.message(Withdraw.waiting_wallet)
async def withdraw_wallet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data["amount"]
    add_withdraw_request(message.from_user.id, amount, wallet)
    await message.answer(f"✅ Заявка на вывод {amount}$ создана", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(ADMIN_ID, f"📥 Заявка от {message.from_user.id}\nСумма: {amount}$\nКошелёк: {wallet}")
    await state.clear()

# ========== ПОДКЛЮЧЕНИЕ НОВЫХ АККАУНТОВ ==========
@dp.callback_query(F.data == "add_tg")
async def add_tg_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if not is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна платная подписка!", show_alert=True)
        return
    await callback.message.answer("📞 Введите номер телефона в формате +79991234567:")
    await state.set_state(AddTG.waiting_phone)
    await callback.answer()

@dp.message(AddTG.waiting_phone)
async def add_tg_phone(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    phone = message.text.strip()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_file = os.path.join(SESSIONS_DIR, f"{message.from_user.id}_{phone}.session")
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.send_code_request(phone)
        await state.update_data(phone=phone, session_file=session_file, client=client)
        await message.answer("🔑 Введите код из SMS (код действует 3 минуты):")
        await state.set_state(AddTG.waiting_code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

@dp.message(AddTG.waiting_code)
async def add_tg_code(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    code = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    phone = data["phone"]
    try:
        await client.sign_in(phone, code)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        add_tg_account(message.from_user.id, phone, data["session_file"], name)
        await show_tg_account_info(message, client, phone)
        await client.disconnect()
        await state.clear()
    except SessionPasswordNeededError:
        await message.answer("🔒 Введите двухфакторный пароль:")
        await state.set_state(AddTG.waiting_2fa)
    except Exception as e:
        error = str(e)
        if "expired" in error.lower():
            await message.answer("❌ Код истёк. Отправляю новый...")
            await client.send_code_request(phone)
        else:
            await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
            await client.disconnect()
            await state.clear()

@dp.message(AddTG.waiting_2fa)
async def add_tg_2fa(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    password = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        add_tg_account(message.from_user.id, data["phone"], data["session_file"], name)
        await show_tg_account_info(message, client, data["phone"])
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {get_russian_error(e)}")
        await client.disconnect()
        await state.clear()


async def show_tg_account_info(message: types.Message, client: TelegramClient, phone: str):
    try:
        if not client.is_connected():
            await client.connect()
        me = await client.get_me()
        if me is None:
            await message.answer("❌ Не удалось получить информацию об аккаунте.")
            return

        # Страна по коду телефона
        country_map = {"7": "🇷🇺 Россия", "380": "🇺🇦 Украина", "375": "🇧🇾 Беларусь", "1": "🇺🇸 США",
                       "44": "🇬🇧 Великобритания", "49": "🇩🇪 Германия", "90": "🇹🇷 Турция", "86": "🇨🇳 Китай",
                       "91": "🇮🇳 Индия"}
        country = "Неизвестно"
        if phone and phone.startswith('+'):
            for code in country_map:
                if phone.startswith('+' + code):
                    country = country_map[code]
                    break

        # Проверка спам-блока через @Spambot
        spam_status = "✅ Нет ограничений"
        try:
            spambot = await client.get_entity('@Spambot')
            await client.send_message(spambot, '/start')
            await asyncio.sleep(2)
            async for msg in client.iter_messages(spambot, limit=1):
                if msg and msg.text:
                    if 'no restrictions' in msg.text.lower():
                        spam_status = "✅ Нет ограничений"
                    elif 'limited' in msg.text.lower() or 'restricted' in msg.text.lower():
                        spam_status = "⚠️ Есть ограничения (спам-блок активен)"
        except:
            spam_status = "❓ Не удалось проверить"

        # Получаем диалоги и контакты
        dialogs = await client.get_dialogs()
        users = [d for d in dialogs if d.is_user]
        total_contacts = len(users)
        total_dialogs = len(dialogs)

        # Взаимные контакты (приблизительно: те, у кого есть диалог с нами) – используем количество диалогов с пользователями
        mutual_contacts = total_contacts  # или можно вычислить сложнее, но для простоты оставим так

        info = (
            f"📱 *Telegram аккаунт*\n"
            f"📞 Номер: `{phone[:4]}****{phone[-3:] if len(phone) > 7 else ''}`\n"
            f"🆔 ID: `{me.id}`\n"
            f"👤 Имя: {me.first_name or ''} {me.last_name or ''}\n"
            f"🌍 Страна: {country}\n"
            f"🔒 Спам-блок: {spam_status}\n"
            f"👥 Контактов (всего): {total_contacts}\n"
            f"💬 Диалогов (всего): {total_dialogs}\n"
            f"🤝 Взаимных контактов (примерно): {mutual_contacts}\n"
            f"✅ Аккаунт подключён!"
        )
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка получения информации: {get_russian_error(e)}")

@dp.callback_query(F.data == "add_vk")
async def add_vk_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if not is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна подписка!", show_alert=True)
        return
    await callback.message.answer("🔑 Введите токен VK (access_token) с правами на сообщения и друзей:")
    await state.set_state(AddVK.waiting_token)
    await callback.answer()

@dp.message(AddVK.waiting_token)
async def add_vk_token(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    token = message.text.strip()
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        user = vk.users.get(fields="city, country, followers_count, bdate")[0]
        name = f"{user['first_name']} {user['last_name']}"
        add_vk_account(message.from_user.id, token, name)
        await show_vk_account_info(message, token)
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Неверный токен или ошибка: {get_russian_error(e)}")
        await state.clear()

async def show_vk_account_info(message: types.Message, token: str):
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        user = vk.users.get(fields="city, country, followers_count, bdate")[0]
        user_id = user['id']
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        city = user.get('city', {}).get('title', 'Не указан')
        country = user.get('country', {}).get('title', 'Не указана')
        bdate = user.get('bdate', 'Не указана')
        followers = user.get('followers_count', 0)
        friends = vk.friends.get()['count']
        online = vk.friends.getOnline()['count'] if 'count' in vk.friends.getOnline() else 0
        info = (
            f"📘 *VK аккаунт*\n"
            f"👤 Имя: {first_name} {last_name}\n"
            f"🆔 ID: {user_id}\n"
            f"🏙️ Город: {city}\n"
            f"🌍 Страна: {country}\n"
            f"🎂 Дата рождения: {bdate}\n"
            f"👥 Друзей: {friends}\n"
            f"👁️ Подписчиков: {followers}\n"
            f"🟢 Онлайн друзей: {online}\n"
            f"✅ Аккаунт подключён!"
        )
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")

# ========== ИГРЫ (рабочие, с анимацией) ==========
@dp.callback_query(F.data == "game_menu")
async def game_menu_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Выберите игру:", reply_markup=game_menu())
    await callback.answer()

@dp.callback_query(F.data == "game_cube")
async def game_cube_menu(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Выберите режим игры в куб:", reply_markup=cube_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("cube_"))
async def game_cube_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    cube_mode = callback.data
    await state.update_data(cube_mode=cube_mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

@dp.message(GameBet.waiting_bet)
async def game_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Минимальная ставка 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"❌ Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(bet=bet)
        data = await state.get_data()
        if "cube_mode" in data:
            mode = data["cube_mode"]
            if mode in ["cube_less_more", "cube_even_odd", "cube_35"]:
                if mode == "cube_less_more":
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Меньше (1-3)", callback_data="cube_less"),
                         InlineKeyboardButton(text="Больше (4-6)", callback_data="cube_more")]
                    ])
                elif mode == "cube_even_odd":
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Чёт", callback_data="cube_even"),
                         InlineKeyboardButton(text="Нечет", callback_data="cube_odd")]
                    ])
                else:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Больше 3.5", callback_data="cube_gt35"),
                         InlineKeyboardButton(text="Меньше 3.5", callback_data="cube_lt35")]
                    ])
                await message.answer("Выберите вариант:", reply_markup=keyboard)
                await state.set_state(GameCube.waiting_choice)
            elif mode == "cube_exact":
                await message.answer("Введите число от 1 до 6:")
                await state.set_state(GameCube.waiting_exact)
            elif mode == "cube_range":
                await message.answer("Введите диапазон в формате 'нижняя-верхняя' (например, 2-4):")
                await state.set_state(GameCube.waiting_range)
        elif "basketball_mode" in data:
            await state.set_state(GameBasketball.waiting_choice)
            await game_basketball_choice_after_bet(message, state)
        elif "darts_mode" in data:
            await state.set_state(GameDarts.waiting_choice)
            await game_darts_choice_after_bet(message, state)
        elif "football_mode" in data:
            await state.set_state(GameFootball.waiting_choice)
            await game_football_choice_after_bet(message, state)
    except:
        await message.answer("❌ Введите число")

# --- Кубик ---
@dp.callback_query(GameCube.waiting_choice)
async def game_cube_choice(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["cube_mode"]
    choice = callback.data
    msg = await callback.message.answer_dice(emoji="🎲")
    roll = msg.dice.value
    await asyncio.sleep(1)
    win = False
    if mode == "cube_less_more":
        if choice == "cube_less" and roll <= 3: win = True
        elif choice == "cube_more" and roll >= 4: win = True
    elif mode == "cube_even_odd":
        if choice == "cube_even" and roll % 2 == 0: win = True
        elif choice == "cube_odd" and roll % 2 == 1: win = True
    elif mode == "cube_35":
        if choice == "cube_gt35" and roll > 3.5: win = True
        elif choice == "cube_lt35" and roll < 3.5: win = True
    if win:
        payout = bet * 2
        update_balance(callback.from_user.id, payout)
        result = f"🎲 Выпало {roll}\n✅ ВЫИГРЫШ: {bet}$ x2 = {payout}$\n💰 Баланс: {get_balance(callback.from_user.id):.2f}$"
    else:
        update_balance(callback.from_user.id, -bet)
        result = f"🎲 Выпало {roll}\n❌ ПРОИГРЫШ: {bet}$\n💰 Баланс: {get_balance(callback.from_user.id):.2f}$"
    await callback.message.answer(result, reply_markup=after_game_menu())
    await state.clear()
    await callback.answer()

@dp.message(GameCube.waiting_exact)
async def game_cube_exact(message: types.Message, state: FSMContext):
    try:
        num = int(message.text.strip())
        if num < 1 or num > 6:
            raise ValueError
        data = await state.get_data()
        bet = data["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if roll == num:
            payout = bet * 6
            update_balance(message.from_user.id, payout)
            result = f"🎲 Выпало {roll}\n✅ УГАДАЛ! Выигрыш: {bet}$ x6 = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        else:
            update_balance(message.from_user.id, -bet)
            result = f"🎲 Выпало {roll}\n❌ НЕ УГАДАЛ. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        await message.answer(result, reply_markup=after_game_menu())
        await state.clear()
    except:
        await message.answer("❌ Введите число от 1 до 6")

@dp.message(GameCube.waiting_range)
async def game_cube_range(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split('-')
        low = int(parts[0])
        high = int(parts[1])
        if low < 1 or high > 6 or low > high:
            raise ValueError
        data = await state.get_data()
        bet = data["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if low <= roll <= high:
            payout = bet * 3
            update_balance(message.from_user.id, payout)
            result = f"🎲 Выпало {roll}\n✅ ПОПАЛ В ДИАПАЗОН! Выигрыш: {bet}$ x3 = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        else:
            update_balance(message.from_user.id, -bet)
            result = f"🎲 Выпало {roll}\n❌ МИМО ДИАПАЗОНА. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
        await message.answer(result, reply_markup=after_game_menu())
        await state.clear()
    except:
        await message.answer("❌ Неверный формат. Пример: 2-4")

# --- Баскетбол ---
@dp.callback_query(F.data == "game_basketball")
async def game_basketball_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🏀 Выберите исход баскетбольного броска:", reply_markup=basketball_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("basket_"))
async def game_basketball_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(basketball_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_basketball_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["basketball_mode"]
    msg = await message.answer_dice(emoji="🏀")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "basket_exact": {"win": value == 5, "mult": 7, "name": "Точное попадание"},
        "basket_ring": {"win": value == 4, "mult": 3, "name": "Попадание в кольцо"},
        "basket_miss": {"win": value <= 2, "mult": 1.5, "name": "Мимо"},
        "basket_board": {"win": value == 3, "mult": 2.5, "name": "Щит"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"🏀 Бросок: {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"🏀 Бросок: {value}\n❌ {outcome['name']} не удалось. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# --- Дартс ---
@dp.callback_query(F.data == "game_darts")
async def game_darts_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎯 Выберите исход в дартсе:", reply_markup=darts_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("darts_"))
async def game_darts_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(darts_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_darts_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["darts_mode"]
    msg = await message.answer_dice(emoji="🎯")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "darts_bullseye": {"win": value == 6, "mult": 10, "name": "Яблочко"},
        "darts_20": {"win": value == 5, "mult": 5, "name": "20"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"🎯 Выпало {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"🎯 Выпало {value}\n❌ {outcome['name']} не выпало. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# --- Футбол ---
@dp.callback_query(F.data == "game_football")
async def game_football_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("⚽ Выберите исход футбольного удара:", reply_markup=football_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("foot_"))
async def game_football_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    mode = callback.data
    await state.update_data(football_mode=mode)
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_football_choice_after_bet(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bet = data["bet"]
    mode = data["football_mode"]
    msg = await message.answer_dice(emoji="⚽")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "foot_nine": {"win": value == 5, "mult": 8, "name": "Гол в девятку"},
        "foot_target": {"win": value == 4, "mult": 3, "name": "Гол в створ"},
        "foot_miss": {"win": value <= 2, "mult": 1.5, "name": "Мимо"},
        "foot_post": {"win": value == 3, "mult": 5, "name": "Штанга/перекладина"}
    }
    outcome = outcomes.get(mode)
    if not outcome:
        await message.answer("❌ Ошибка выбора")
        await state.clear()
        return
    if outcome["win"]:
        payout = bet * outcome["mult"]
        update_balance(message.from_user.id, payout)
        result = f"⚽ Удар: {value}\n✅ {outcome['name']}! Выигрыш: {bet}$ x{outcome['mult']} = {payout}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    else:
        update_balance(message.from_user.id, -bet)
        result = f"⚽ Удар: {value}\n❌ {outcome['name']} не забит. Проигрыш: {bet}$\n💰 Баланс: {get_balance(message.from_user.id):.2f}$"
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

@dp.callback_query(F.data == "again")
async def again_game(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await game_menu_callback(callback)

@dp.callback_query(F.data == "inc_bet")
async def inc_bet(callback: types.CallbackQuery):
    await callback.answer("Функция повышения ставки будет добавлена позже", show_alert=True)

@dp.callback_query(F.data == "dec_bet")
async def dec_bet(callback: types.CallbackQuery):
    await callback.answer("Функция понижения ставки будет добавлена позже", show_alert=True)

@dp.callback_query(F.data == "all_in")
async def all_in(callback: types.CallbackQuery):
    await callback.answer("Функция ва-банк будет добавлена позже", show_alert=True)

# ========== АДМИН-КОМАНДЫ ==========
@dp.message(Command("addbalance"))
async def add_balance_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет прав")
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /addbalance <user_id> <сумма>")
        return
    try:
        user_id = int(parts[1])
        amount = float(parts[2])
        user = get_user(user_id)
        if not user:
            await message.answer(f"Пользователь {user_id} не найден")
            return
        new_balance = user["balance"] + amount
        set_balance(user_id, new_balance)
        await message.answer(f"✅ Начислено {amount}$. Новый баланс пользователя {user_id}: {new_balance:.2f}$")
    except:
        await message.answer("Ошибка ввода. Пример: /addbalance 123456 100")

@dp.message(Command("removebalance"))
async def remove_balance_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет прав")
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /removebalance <user_id> <сумма>")
        return
    try:
        user_id = int(parts[1])
        amount = float(parts[2])
        user = get_user(user_id)
        if not user:
            await message.answer(f"Пользователь {user_id} не найден")
            return
        if amount > user["balance"]:
            await message.answer(f"Нельзя списать больше, чем есть (баланс: {user['balance']:.2f}$)")
            return
        new_balance = user["balance"] - amount
        set_balance(user_id, new_balance)
        await message.answer(f"✅ Списано {amount}$. Новый баланс пользователя {user_id}: {new_balance:.2f}$")
    except:
        await message.answer("Ошибка ввода. Пример: /removebalance 123456 50")

@dp.message(Command("users"))
async def list_users_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет прав")
        return
    users = get_all_users()
    if not users:
        await message.answer("Нет пользователей")
        return
    text = "👥 Список пользователей:\n"
    for u in users:
        sub = datetime.fromtimestamp(u['sub_until']).strftime('%d.%m.%Y') if u['sub_until'] else "Нет"
        text += f"ID {u['tg_id']} | {u['username']} | Подписка: {sub} | Баланс: {u['balance']:.2f}$\n"
    await message.answer(text)

# ========== ГРУППОВЫЕ КОМАНДЫ ==========
@dp.message(Command("dice"))
async def group_dice(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return
    msg = await message.answer_dice(emoji="🎲")
    await message.reply(f"🎲 Результат: {msg.dice.value}")

@dp.message(Command("dice2"))
async def group_dice2(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return
    msg1 = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(0.6)
    msg2 = await message.answer_dice(emoji="🎲")
    total = msg1.dice.value + msg2.dice.value
    await message.reply(f"🎲 {msg1.dice.value} + {msg2.dice.value} = {total}")

@dp.message(Command("balance"))
async def group_balance(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username or str(user_id))
    balance = get_balance(user_id)
    await message.reply(f"👤 {message.from_user.first_name}, ваш баланс: {balance:.2f}$")

@dp.message(Command("game"))
async def group_game(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Кинуть кубик", callback_data="group_dice")],
        [InlineKeyboardButton(text="🎲🎲 Кинуть два кубика", callback_data="group_dice2")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="group_balance")]
    ])
    await message.answer("Выберите действие:", reply_markup=keyboard)

@dp.callback_query(F.data == "group_dice")
async def group_dice_callback(callback: types.CallbackQuery):
    if callback.message.chat.type == ChatType.PRIVATE:
        await callback.answer("Только в группах", show_alert=True)
        return
    msg = await callback.message.answer_dice(emoji="🎲")
    await callback.message.reply(f"🎲 Результат: {msg.dice.value}")
    await callback.answer()

@dp.callback_query(F.data == "group_dice2")
async def group_dice2_callback(callback: types.CallbackQuery):
    if callback.message.chat.type == ChatType.PRIVATE:
        await callback.answer("Только в группах", show_alert=True)
        return
    msg1 = await callback.message.answer_dice(emoji="🎲")
    await asyncio.sleep(0.6)
    msg2 = await callback.message.answer_dice(emoji="🎲")
    total = msg1.dice.value + msg2.dice.value
    await callback.message.reply(f"🎲 {msg1.dice.value} + {msg2.dice.value} = {total}")
    await callback.answer()

@dp.callback_query(F.data == "group_balance")
async def group_balance_callback(callback: types.CallbackQuery):
    if callback.message.chat.type == ChatType.PRIVATE:
        await callback.answer("Только в группах", show_alert=True)
        return
    user_id = callback.from_user.id
    create_user(user_id, callback.from_user.username or str(user_id))
    balance = get_balance(user_id)
    await callback.message.reply(f"👤 {callback.from_user.first_name}, ваш баланс: {balance:.2f}$")
    await callback.answer()

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет прав", show_alert=True)
        return
    await callback.message.edit_text("👑 *Админ-панель*\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_menu())
    await callback.answer()

# ========== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (с пагинацией) ==========
@dp.callback_query(F.data == "admin_users")
async def admin_users_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.update_data(page=0)
    await show_admin_users_page(callback.message, state, 0)


async def show_admin_users_page(message: types.Message, state: FSMContext, page: int):
    users = get_all_users()
    per_page = 10
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]
    total_pages = (len(users) + per_page - 1) // per_page

    text = f"👥 *Пользователи (стр. {page + 1}/{total_pages})*\n\n"
    for u in page_users:
        sub = datetime.fromtimestamp(u['sub_until']).strftime('%d.%m.%Y') if u['sub_until'] else "Нет"
        text += f"🆔 ID: `{u['tg_id']}` | {u['username']} | Подписка: {sub} | Баланс: ${u['balance']:.2f}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    if page > 0:
        keyboard.inline_keyboard.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users_page_{page - 1}")])
    if end < len(users):
        if page > 0:
            keyboard.inline_keyboard[-1].append(
                InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"admin_users_page_{page + 1}"))
        else:
            keyboard.inline_keyboard.append(
                [InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"admin_users_page_{page + 1}")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("admin_users_page_"))
async def admin_users_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    await show_admin_users_page(callback.message, state, page)
    await callback.answer()

# ========== ГЛОБАЛЬНАЯ РАССЫЛКА (с текстом и фото) ==========
    @dp.callback_query(F.data == "admin_broadcast")
    async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id != ADMIN_ID: return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Текстовая рассылка", callback_data="broadcast_text")],
            [InlineKeyboardButton(text="🖼 Рассылка с фото", callback_data="broadcast_photo")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
        ])
        await callback.message.edit_text("📢 *Выберите тип рассылки*", parse_mode="Markdown", reply_markup=keyboard)

    @dp.callback_query(F.data == "broadcast_text")
    async def admin_broadcast_text_type(callback: types.CallbackQuery, state: FSMContext):
        await state.update_data(broadcast_type="text")
        await callback.message.answer("Введите текст рассылки:")
        await state.set_state(AdminBroadcast.waiting_text)
        await callback.answer()

    @dp.callback_query(F.data == "broadcast_photo")
    async def admin_broadcast_photo_type(callback: types.CallbackQuery, state: FSMContext):
        await state.update_data(broadcast_type="photo")
        await callback.message.answer("Введите текст (caption) для фото (можно оставить пустым, отправьте 'нет'):")
        await state.set_state(AdminBroadcast.waiting_text)
        await callback.answer()

    @dp.message(AdminBroadcast.waiting_text)
    async def admin_broadcast_text(message: types.Message, state: FSMContext):
        text = message.text.strip()
        if text.lower() == "нет":
            text = ""
        await state.update_data(text=text)
        data = await state.get_data()
        if data["broadcast_type"] == "photo":
            await message.answer("Теперь пришлите фото (одно изображение):")
            await state.set_state(AdminBroadcast.waiting_photo)
        else:
            await confirm_broadcast(message, state, text, None)

    @dp.message(AdminBroadcast.waiting_photo, F.photo)
    async def admin_broadcast_photo(message: types.Message, state: FSMContext):
        photo = message.photo[-1].file_id
        data = await state.get_data()
        text = data["text"]
        await confirm_broadcast(message, state, text, photo)

    async def confirm_broadcast(message: types.Message, state: FSMContext, text: str, photo: str = None):
        users = get_all_users()
        total = len(users)
        await state.update_data(total=total, text=text, photo=photo)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Начать рассылку", callback_data="broadcast_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")]
        ])
        await message.answer(
            f"📢 *Подтверждение рассылки*\nПолучателей: {total}\n\nТекст: {text[:200]}{'...' if len(text) > 200 else ''}\n{'Фото: есть' if photo else ''}",
            parse_mode="Markdown", reply_markup=keyboard)

    @dp.callback_query(F.data == "broadcast_confirm")
    async def admin_broadcast_execute(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        text = data["text"]
        photo = data.get("photo")
        users = get_all_users()
        await callback.message.edit_text("🚀 Начинаю рассылку...")
        sent = 0
        for u in users:
            try:
                if photo:
                    await bot.send_photo(chat_id=u['tg_id'], photo=photo, caption=text, parse_mode="Markdown")
                else:
                    await bot.send_message(chat_id=u['tg_id'], text=text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except:
                pass
        await callback.message.edit_text(f"✅ Рассылка завершена. Отправлено {sent} из {len(users)} пользователей.")
        await state.clear()
    await admin_panel(callback)

# ========== ПРОМОКОДЫ ==========
@dp.callback_query(F.data == "admin_promocodes")
async def admin_promocodes(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promocode")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promocodes")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text("🎫 *Управление промокодами*", parse_mode="Markdown", reply_markup=keyboard)

@dp.callback_query(F.data == "admin_create_promocode")
async def admin_create_promocode_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Введите код промокода (латиница, цифры):")
    await state.set_state(AdminCreatePromocode.waiting_code)
    await callback.answer()

@dp.message(AdminCreatePromocode.waiting_code)
async def admin_create_promocode_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    if get_promocode(code):
        await message.answer("❌ Такой промокод уже существует. Введите другой:")
        return
    await state.update_data(code=code)
    await message.answer("Введите количество дней подписки, которые даёт промокод (число):")
    await state.set_state(AdminCreatePromocode.waiting_days)

@dp.message(AdminCreatePromocode.waiting_days)
async def admin_create_promocode_days(message: types.Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 1:
            raise ValueError
        await state.update_data(days=days)
        await message.answer("Введите максимальное количество использований (по умолчанию 1):")
        await state.set_state(AdminCreatePromocode.waiting_max_uses)
    except:
        await message.answer("❌ Введите целое число больше 0")

@dp.message(AdminCreatePromocode.waiting_max_uses)
async def admin_create_promocode_max_uses(message: types.Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 1:
            raise ValueError
    except:
        max_uses = 1
    data = await state.get_data()
    code = data["code"]
    days = data["days"]
    create_promocode(code, days, max_uses)
    await message.answer(f"✅ Промокод `{code}` создан! Даёт {days} дней подписки, максимум использований: {max_uses}.", parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data == "admin_list_promocodes")
async def admin_list_promocodes(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    promos = get_all_promocodes()
    if not promos:
        await callback.message.edit_text("📭 Нет созданных промокодов.", reply_markup=back_button("admin_promocodes"))
        return
    text = "🎫 *Список промокодов:*\n\n"
    for p in promos:
        text += f"Код: `{p['code']}`\nДней: {p['days']}\nИспользовано: {p['uses']}/{p['max_uses']}\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data="admin_delete_promocode")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_promocodes")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

@dp.callback_query(F.data == "admin_delete_promocode")
async def admin_delete_promocode_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    promos = get_all_promocodes()
    if not promos:
        await callback.answer("Нет промокодов для удаления", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"❌ {p['code']}", callback_data=f"admin_del_promo_{p['id']}")] for p in promos] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_list_promocodes")]])
    await callback.message.edit_text("Выберите промокод для удаления:", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_del_promo_"))
async def admin_delete_promocode_exec(callback: types.CallbackQuery):
    promo_id = int(callback.data.split("_")[3])
    delete_promocode(promo_id)
    await callback.answer("Промокод удалён", show_alert=True)
    await admin_list_promocodes(callback)

# ========== АКТИВАЦИЯ ПРОМОКОДА ПОЛЬЗОВАТЕЛЕМ ==========
@dp.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите промокод:")
    await state.set_state(ActivatePromo.waiting_code)
    await callback.answer()

class WaitingPromoCode(StatesGroup):
    waiting_code = State()

@dp.message(WaitingPromoCode.waiting_code)
async def activate_promo(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = get_promocode(code)
    if not promo:
        await message.answer("❌ Такой промокод не существует.")
        await state.clear()
        return
    if promo["uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод уже использован максимальное количество раз.")
        await state.clear()
        return
    # Проверяем, не использовал ли пользователь этот промокод уже
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM used_promocodes WHERE user_id=? AND code_id=?", (message.from_user.id, promo["id"]))
    if c.fetchone():
        await message.answer("❌ Вы уже активировали этот промокод.")
        conn.close()
        await state.clear()
        return
    conn.close()
    # Активируем подписку
    use_promocode(message.from_user.id, promo["id"])
    current_sub = get_user(message.from_user.id)["sub_until"]
    new_time = max(current_sub, int(time.time())) + promo["days"] * 86400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET sub_until=? WHERE tg_id=?", (new_time, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Промокод `{code}` активирован! Подписка продлена на {promo['days']} дней.", parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_withdraw(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    req_id = int(callback.data.split("_")[1])
    update_withdraw_status(req_id, "rejected")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM withdraw_requests WHERE id=?", (req_id,))
    row = c.fetchone()
    if row:
        await bot.send_message(row[0], "❌ Заявка на вывод отклонена")
    conn.close()
    await callback.message.edit_text(f"❌ Заявка #{req_id} отклонена")
    await callback.answer()

# ========== ФУНКЦИЯ ДЛЯ РУССКИХ ОШИБОК ==========
def get_russian_error(e: Exception) -> str:
    error = str(e)
    if "Cannot find any entity" in error:
        return "Не удалось найти пользователя или чат. Проверьте ID или username."
    if "Too many requests" in error:
        return "Слишком много запросов. Подождите немного."
    if "FloodWaitError" in error:
        return "Превышен лимит запросов. Попробуйте позже."
    if "AuthKeyError" in error or "UnauthorizedError" in error:
        return "Сессия устарела. Аккаунт будет удалён."
    if "disconnected" in error:
        return "Соединение разорвано. Попробуйте ещё раз."
    if "first_name" in error:
        return "Ошибка получения имени пользователя."
    return error

# ========== ФОНОВАЯ ПРОВЕРКА АКТИВНЫХ АККАУНТОВ ==========
async def check_all_tg_accounts():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT id, owner_tg_id, phone, session_file FROM tg_accounts WHERE is_active=1")
            accounts = c.fetchall()
            conn.close()
            for acc_id, owner_tg_id, phone, session_file in accounts:
                client = TelegramClient(session_file, API_ID, API_HASH)
                try:
                    await client.connect()
                    await client.get_me()
                except (AuthKeyError, UnauthorizedError):
                    deactivate_tg_account(owner_tg_id, acc_id)
                    await bot.send_message(owner_tg_id, f"❌ Аккаунт {phone} был автоматически удалён из-за слетевшей сессии (фоновая проверка).")
                except Exception as e:
                    print(f"Ошибка при проверке аккаунта {phone}: {e}")
                finally:
                    await client.disconnect()
            await asyncio.sleep(1800)
        except Exception as e:
            print(f"Ошибка в фоновой проверке: {e}")
            await asyncio.sleep(60)

@dp.message(ActivatePromo.waiting_code)
async def activate_promo_exec(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = get_promocode(code)
    user_id = message.from_user.id
    if not promo:
        await message.answer("❌ Неверный промокод")
    elif promo["uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод больше не действителен")
    else:
        use_promocode(user_id, promo["id"])
        set_subscription(user_id, promo["days"])
        remaining = promo["max_uses"] - promo["uses"] - 1
        await message.answer(f"✅ Промокод активирован! Получено {promo['days']} дней подписки.\nОсталось использований: {remaining}")
        # Уведомление админу
        await bot.send_message(ADMIN_ID, f"🎫 Пользователь {user_id} активировал промокод {code}. Осталось использований: {remaining}")
    await state.clear()


# ========== ЗАПУСК ==========
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_all_tg_accounts())
    print("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())