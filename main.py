import asyncio
import sqlite3
import time
import os
import re
import random
from datetime import datetime
import aiohttp

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
from telethon.errors import FloodWaitError, SessionPasswordNeededError
import vk_api

# ========== ЧТЕНИЕ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")

# Проверка обязательных переменных
if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID, API_HASH must be set in environment variables")

# ========== КОНСТАНТЫ ==========
DB_PATH = "bot.db"
SESSIONS_DIR = "/tmp/sessions" if os.name != 'nt' else "sessions"
TARIFFS = {
    "day": {"days": 1, "price": 5, "name": "1 день"},
    "week": {"days": 7, "price": 20, "name": "1 неделя"},
    "month": {"days": 30, "price": 40, "name": "1 месяц"}
}

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
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
    # Миграции
    for col in ['name', 'last_used']:
        try:
            c.execute(f"ALTER TABLE tg_accounts ADD COLUMN {col} TEXT DEFAULT ''" if col == 'name' else f"ALTER TABLE tg_accounts ADD COLUMN {col} INTEGER DEFAULT 0")
        except:
            pass
    try:
        c.execute("ALTER TABLE vk_accounts ADD COLUMN is_active INTEGER DEFAULT 1")
    except:
        pass
    conn.commit()
    conn.close()

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ ==========
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

# ========== CRYPTOBOT ==========
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

async def create_crypto_invoice(amount_usd: float, description: str):
    if not CRYPTOBOT_TOKEN:
        return None
    url = f"{CRYPTOBOT_API_URL}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(amount_usd),
        "description": description,
        "paid_btn_name": "callback",
        "paid_btn_url": f"https://t.me/LzCcOuWBot"
    }
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

# ========== ПРОВЕРКА ПОДПИСКИ НА КАНАЛ ==========
async def is_subscribed_to_channel(user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ["member", "creator", "administrator"]
    except:
        return False

# ========== СПАМ-БОТ ==========
async def check_spambot(client: TelegramClient):
    try:
        spambot = await client.get_entity('@Spambot')
        await client.send_message(spambot, '/start')
        await asyncio.sleep(3)
        async for msg in client.iter_messages(spambot, limit=1):
            text = msg.text or ''
            if 'no restrictions' in text.lower():
                return "✅ Нет ограничений"
            elif 'limited' in text.lower() or 'restricted' in text.lower():
                return "⚠️ Есть ограничения"
            else:
                return "🤷 Не удалось определить"
    except Exception as e:
        return f"❌ Ошибка: {e}"

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
        [InlineKeyboardButton(text="🎲 1 куб (больше/меньше) x2", callback_data="game_1cube")],
        [InlineKeyboardButton(text="🎲🎲 2 куба (сумма 7) x2.4", callback_data="game_2cube")],
        [InlineKeyboardButton(text="🎲 Мини-игры", callback_data="mini_games")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def mini_games_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💣 Мины", callback_data="game_mines")],
        [InlineKeyboardButton(text="☂️ Зонтик (x2)", callback_data="game_umbrella")],
        [InlineKeyboardButton(text="🎰 Кено (x10)", callback_data="game_keno")],
        [InlineKeyboardButton(text="🐋 Кит (x50)", callback_data="game_whale")],
        [InlineKeyboardButton(text="🎰 Автомат (x60)", callback_data="game_slots")],
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
        status = "✅" if acc["is_active"] else "⭕"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']} ({acc['phone']})", callback_data=f"tg_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add_tg")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def vk_accounts_list(user_id):
    accounts = get_user_vk_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "⭕"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']}", callback_data=f"vk_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add_vk")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="➕ Выдать баланс", callback_data="admin_add_balance")],
        [InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin_remove_balance")],
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]
    ])

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

class Withdraw(StatesGroup):
    waiting_amount = State()
    waiting_wallet = State()

class Deposit(StatesGroup):
    waiting_amount = State()

class Game1Cube(StatesGroup):
    waiting_bet = State()
    waiting_choice = State()

class Game2Cube(StatesGroup):
    waiting_bet = State()
    waiting_choice = State()

class TGAction(StatesGroup):
    waiting_target = State()
    waiting_message = State()
    waiting_join_link = State()

class MinesGame(StatesGroup):
    waiting_bet = State()
    waiting_mines_count = State()
    waiting_cell = State()

class SlotsGame(StatesGroup):
    waiting_bet = State()
    waiting_combination = State()

# Глобальные словари для данных игр
user_game_data = {}
mines_games = {}

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ========== МИДЛВАРЬ ДЛЯ ПРОВЕРКИ ПОДПИСКИ ==========
@dp.callback_query(lambda c: c.data not in ["check_sub"])
async def subscription_middleware(callback: types.CallbackQuery):
    if not await is_subscribed_to_channel(callback.from_user.id):
        await callback.answer("❌ Подпишитесь на канал @hlspam!", show_alert=True)
        return
    # если подписан – пропускаем дальше
    return

# ========== ОСНОВНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    create_user(message.from_user.id, message.from_user.username or str(message.from_user.id))
    if not await is_subscribed_to_channel(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton(text="✅ Проверить", callback_data="check_sub")]
        ])
        await message.answer(f"Подпишитесь на @{CHANNEL_USERNAME}", reply_markup=kb)
        return
    await message.answer("🎲 Добро пожаловать!\nИспользуйте кнопки меню.", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: types.CallbackQuery):
    if await is_subscribed_to_channel(callback.from_user.id):
        await callback.message.delete()
        await start_cmd(callback.message)
    else:
        await callback.answer("❌ Вы не подписаны", show_alert=True)

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

@dp.callback_query(F.data.startswith("tg_join_"))
async def tg_join_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ссылку или username группы/канала (например, @chat или https://t.me/chat):")
    await state.set_state(TGAction.waiting_join_link)
    await callback.answer()

@dp.message(TGAction.waiting_join_link)
async def tg_join_execute(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file = row[0]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    link = message.text.strip()
    try:
        if "joinchat" in link:
            hash_match = re.search(r'joinchat/([A-Za-z0-9_-]+)', link)
            if hash_match:
                await client(ImportChatInviteRequest(hash_match.group(1)))
                await message.answer(f"✅ Вступил(а) по ссылке-приглашению")
            else:
                raise Exception("Не удалось распознать ссылку-приглашение")
        else:
            entity = await client.get_entity(link)
            await client(JoinChannelRequest(entity))
        await message.answer(f"✅ Вступил(а) в {link}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await client.disconnect()
    await state.clear()

@dp.callback_query(F.data.startswith("tg_leave_"))
async def tg_leave_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username чата/группы (например, -100123456789 или @chat):")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_leave_execute(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file = row[0]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    target = message.text.strip()
    try:
        entity = await client.get_entity(target)
        await client.delete_dialog(entity)
        await message.answer(f"✅ Вышел(а) из чата {target}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await client.disconnect()
    await state.clear()

@dp.callback_query(F.data.startswith("tg_send_msg_"))
async def tg_send_start(callback: types.CallbackQuery, state: FSMContext):
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
    if message.chat.type != ChatType.PRIVATE:
        return
    await state.update_data(target=message.text.strip())
    await message.answer("Введите текст сообщения:")
    await state.set_state(TGAction.waiting_message)

@dp.message(TGAction.waiting_message)
async def tg_send_text(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    text = message.text
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
    row = c.fetchone()
    conn.close()
    if not row:
        await message.answer("Аккаунт не найден")
        await state.clear()
        return
    session_file = row[0]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        entity = await client.get_entity(target)
        await client.send_message(entity, text)
        await message.answer(f"✅ Сообщение отправлено в {target}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
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
    if message.chat.type != ChatType.PRIVATE:
        return
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку между сообщениями (сек, рекомендуется 5):")
    await state.set_state(BroadcastTG.waiting_delay)

@dp.message(BroadcastTG.waiting_delay)
async def broadcast_tg_delay(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        delay = float(message.text.strip())
        if delay < 2:
            await message.answer("⚠️ Слишком маленькая задержка. Установлено 2 сек (минимальная).")
            delay = 2
        data = await state.get_data()
        text = data["text"]
        acc_id = data["acc_id"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT session_file FROM tg_accounts WHERE id=? AND owner_tg_id=?", (acc_id, message.from_user.id))
        row = c.fetchone()
        conn.close()
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file = row[0]
        client = TelegramClient(session_file, API_ID, API_HASH)
        await client.connect()
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_user]
        total = len(targets)
        await message.answer(f"Начинаю рассылку {total} получателям, задержка {delay} сек.")
        sent = 0
        for dialog in targets:
            try:
                await client.send_message(dialog.entity, text)
                sent += 1
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    await client.send_message(dialog.entity, text)
                    sent += 1
                except:
                    continue
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ['user is blocked', 'peer_id_invalid', 'not found', 'cannot write']):
                    continue
                else:
                    continue
        await client.disconnect()
        await message.answer(f"✅ Отправлено {sent} из {total}")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

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
    if message.chat.type != ChatType.PRIVATE:
        return
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку между сообщениями (сек):")
    await state.set_state(BroadcastVK.waiting_delay)

@dp.message(BroadcastVK.waiting_delay)
async def broadcast_vk_delay(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
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

# ========== ПОДПИСКА (ПЛАТНАЯ) ==========
@dp.callback_query(F.data == "buy_sub")
async def buy_sub(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 5$", callback_data="tariff_day")],
        [InlineKeyboardButton(text="1 неделя - 20$", callback_data="tariff_week")],
        [InlineKeyboardButton(text="1 месяц - 40$", callback_data="tariff_month")],
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
        await callback.answer("Ошибка", show_alert=True)
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
        await callback.answer("Ошибка", show_alert=True)
        return
    invoice = await create_crypto_invoice(tariff["price"], f"Подписка на {tariff['name']}")
    if not invoice:
        await callback.answer("Ошибка создания счёта", show_alert=True)
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
    if not is_subscribed(callback.from_user.id):
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
        await message.answer(f"❌ Ошибка: {e}")
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
            await message.answer(f"❌ Ошибка: {e}")
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
        await message.answer(f"❌ Ошибка 2FA: {e}. Попробуйте снова.")
        await client.disconnect()
        await state.clear()

async def show_tg_account_info(message: types.Message, client: TelegramClient, phone: str):
    try:
        if not client.is_connected():
            await client.connect()
        me = await client.get_me()
        spam_status = await check_spambot(client)

        country_map = {
            "7": "🇷🇺 Россия", "380": "🇺🇦 Украина", "375": "🇧🇾 Беларусь",
            "1": "🇺🇸 США", "44": "🇬🇧 Великобритания", "49": "🇩🇪 Германия",
            "90": "🇹🇷 Турция", "86": "🇨🇳 Китай", "91": "🇮🇳 Индия"
        }
        country = "Неизвестно"
        if phone and phone.startswith('+'):
            for code in country_map:
                if phone.startswith('+' + code):
                    country = country_map[code]
                    break

        dialogs = await client.get_dialogs()
        users = [d for d in dialogs if d.is_user]
        total_contacts = len(users)
        total_dialogs = len(dialogs)

        mutual = 0
        for user in users[:50]:
            try:
                async for _ in client.iter_messages(user.entity, limit=1):
                    mutual += 1
                    break
            except:
                pass

        info = (
            f"📱 *Telegram аккаунт*\n"
            f"📞 Номер: `{phone[:4]}****{phone[-3:] if len(phone) > 7 else ''}`\n"
            f"🆔 ID: `{me.id}`\n"
            f"👤 Имя: {me.first_name} {me.last_name or ''}\n"
            f"🌍 Страна: {country}\n"
            f"🔒 *Спам-блок:* {spam_status}\n"
            f"👥 Контактов (всего): {total_contacts}\n"
            f"💬 Диалогов (всего): {total_dialogs}\n"
            f"🤝 Взаимных контактов (приблизительно): {mutual}\n"
            f"✅ Аккаунт успешно подключён!"
        )
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Не удалось получить информацию об аккаунте: {e}")

@dp.callback_query(F.data == "add_vk")
async def add_vk_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if not is_subscribed(callback.from_user.id):
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
        await message.answer(f"❌ Неверный токен или ошибка: {e}")
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
            f"👥 Взаимных друзей: {friends}\n"
            f"👁️ Подписчиков: {followers}\n"
            f"🟢 Друзей онлайн: {online}\n"
            f"✅ Аккаунт успешно подключён!"
        )
        await message.answer(info, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Не удалось получить информацию: {e}")

# ========== ИГРЫ ==========
def save_game_data(user_id, game, bet, choice=None):
    user_game_data[user_id] = {"game": game, "bet": bet, "choice": choice}

def get_game_data(user_id):
    return user_game_data.get(user_id)

@dp.callback_query(F.data == "game_menu")
async def game_menu_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Выберите игру:", reply_markup=game_menu())
    await callback.answer()

@dp.callback_query(F.data == "mini_games")
async def mini_games_callback(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.edit_text("🎲 Мини-игры:", reply_markup=mini_games_menu())
    await callback.answer()

# ---------- 1 куб ----------
@dp.callback_query(F.data == "game_1cube")
async def game_1cube_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Game1Cube.waiting_bet)
    await callback.answer()

@dp.message(Game1Cube.waiting_bet)
async def game_1cube_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Мин 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(bet=bet)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Меньше (1-3)", callback_data="1cube_less")],
            [InlineKeyboardButton(text="Больше (4-6)", callback_data="1cube_more")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
        ])
        await message.answer("Выберите:", reply_markup=keyboard)
        await state.set_state(Game1Cube.waiting_choice)
    except:
        await message.answer("Введите число")

@dp.callback_query(Game1Cube.waiting_choice)
async def game_1cube_choice(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    choice = callback.data
    data = await state.get_data()
    bet = data["bet"]
    user_id = callback.from_user.id
    balance = get_balance(user_id)
    if bet > balance:
        await callback.answer("❌ Не хватает средств, измените ставку", show_alert=True)
        await state.clear()
        return
    save_game_data(user_id, "1cube", bet, choice)
    msg = await callback.message.answer_dice(emoji="🎲")
    roll = msg.dice.value
    await asyncio.sleep(1)
    win = (choice == "1cube_less" and roll <= 3) or (choice == "1cube_more" and roll >= 4)
    if win:
        payout = bet * 2
        update_balance(user_id, payout)
        result_text = f"🎲 Выпало {roll}\n💰 Ставка: {bet}$\n✅ ВЫИГРЫШ: {bet}$ x2 = {payout}$\n💰 Баланс: {get_balance(user_id):.2f}$"
    else:
        update_balance(user_id, -bet)
        result_text = f"🎲 Выпало {roll}\n💰 Ставка: {bet}$\n❌ ПРОИГРЫШ: -{bet}$\n💰 Баланс: {get_balance(user_id):.2f}$"
    await callback.message.answer(result_text, reply_markup=after_game_menu())
    await state.clear()
    await callback.answer()

# ---------- 2 куба ----------
@dp.callback_query(F.data == "game_2cube")
async def game_2cube_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Game2Cube.waiting_bet)
    await callback.answer()

@dp.message(Game2Cube.waiting_bet)
async def game_2cube_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Мин 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(bet=bet)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Сумма 7-", callback_data="2cube_less7")],
            [InlineKeyboardButton(text="Сумма 7", callback_data="2cube_eq7")],
            [InlineKeyboardButton(text="Сумма 7+", callback_data="2cube_more7")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="game_menu")]
        ])
        await message.answer("Выберите вариант:", reply_markup=keyboard)
        await state.set_state(Game2Cube.waiting_choice)
    except:
        await message.answer("Введите число")

@dp.callback_query(Game2Cube.waiting_choice)
async def game_2cube_choice(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    choice = callback.data
    data = await state.get_data()
    bet = data["bet"]
    user_id = callback.from_user.id
    balance = get_balance(user_id)
    if bet > balance:
        await callback.answer("❌ Не хватает средств, измените ставку", show_alert=True)
        await state.clear()
        return
    save_game_data(user_id, "2cube", bet, choice)
    msg1 = await callback.message.answer_dice(emoji="🎲")
    await asyncio.sleep(0.6)
    msg2 = await callback.message.answer_dice(emoji="🎲")
    total = msg1.dice.value + msg2.dice.value
    await asyncio.sleep(0.5)
    win = (choice == "2cube_less7" and total < 7) or (choice == "2cube_eq7" and total == 7) or (choice == "2cube_more7" and total > 7)
    if win:
        payout = bet * 2.4
        update_balance(user_id, payout)
        result_text = f"🎲 {msg1.dice.value}+{msg2.dice.value}={total}\n💰 Ставка: {bet}$\n✅ ВЫИГРЫШ: {bet}$ x2.4 = {payout}$\n💰 Баланс: {get_balance(user_id):.2f}$"
    else:
        update_balance(user_id, -bet)
        result_text = f"🎲 {msg1.dice.value}+{msg2.dice.value}={total}\n💰 Ставка: {bet}$\n❌ ПРОИГРЫШ: -{bet}$\n💰 Баланс: {get_balance(user_id):.2f}$"
    await callback.message.answer(result_text, reply_markup=after_game_menu())
    await state.clear()
    await callback.answer()

# ---------- Ещё раз, повысить/понизить ставку, ва-банк ----------
@dp.callback_query(F.data == "again")
async def again_game(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user_id = callback.from_user.id
    data = get_game_data(user_id)
    if not data:
        await callback.answer("❌ Нет активной игры. Начните новую.", show_alert=True)
        return
    game = data["game"]
    bet = data["bet"]
    choice = data.get("choice")
    balance = get_balance(user_id)
    if bet > balance:
        await callback.answer(f"❌ Не хватает средств. Баланс: {balance:.2f}$, измените ставку", show_alert=True)
        return
    if game == "1cube":
        msg = await callback.message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        win = (choice == "1cube_less" and roll <= 3) or (choice == "1cube_more" and roll >= 4)
        if win:
            payout = bet * 2
            update_balance(user_id, payout)
            result_text = f"🎲 Выпало {roll}\n💰 Ставка: {bet}$\n✅ ВЫИГРЫШ: {bet}$ x2 = {payout}$\n💰 Баланс: {get_balance(user_id):.2f}$"
        else:
            update_balance(user_id, -bet)
            result_text = f"🎲 Выпало {roll}\n💰 Ставка: {bet}$\n❌ ПРОИГРЫШ: -{bet}$\n💰 Баланс: {get_balance(user_id):.2f}$"
        await callback.message.answer(result_text, reply_markup=after_game_menu())
    elif game == "2cube":
        msg1 = await callback.message.answer_dice(emoji="🎲")
        await asyncio.sleep(0.6)
        msg2 = await callback.message.answer_dice(emoji="🎲")
        total = msg1.dice.value + msg2.dice.value
        await asyncio.sleep(0.5)
        win = (choice == "2cube_less7" and total < 7) or (choice == "2cube_eq7" and total == 7) or (choice == "2cube_more7" and total > 7)
        if win:
            payout = bet * 2.4
            update_balance(user_id, payout)
            result_text = f"🎲 {msg1.dice.value}+{msg2.dice.value}={total}\n💰 Ставка: {bet}$\n✅ ВЫИГРЫШ: {bet}$ x2.4 = {payout}$\n💰 Баланс: {get_balance(user_id):.2f}$"
        else:
            update_balance(user_id, -bet)
            result_text = f"🎲 {msg1.dice.value}+{msg2.dice.value}={total}\n💰 Ставка: {bet}$\n❌ ПРОИГРЫШ: -{bet}$\n💰 Баланс: {get_balance(user_id):.2f}$"
        await callback.message.answer(result_text, reply_markup=after_game_menu())
    await callback.answer()

@dp.callback_query(F.data == "inc_bet")
async def inc_bet(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user_id = callback.from_user.id
    data = get_game_data(user_id)
    if not data:
        await callback.answer("❌ Нет активной игры", show_alert=True)
        return
    data["bet"] += 1
    if data["bet"] < 0.1:
        data["bet"] = 0.1
    user_game_data[user_id] = data
    await callback.answer(f"✅ Ставка повышена до {data['bet']:.2f}$", show_alert=True)

@dp.callback_query(F.data == "dec_bet")
async def dec_bet(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user_id = callback.from_user.id
    data = get_game_data(user_id)
    if not data:
        await callback.answer("❌ Нет активной игры", show_alert=True)
        return
    data["bet"] -= 1
    if data["bet"] < 0.1:
        data["bet"] = 0.1
    user_game_data[user_id] = data
    await callback.answer(f"✅ Ставка понижена до {data['bet']:.2f}$", show_alert=True)

@dp.callback_query(F.data == "all_in")
async def all_in(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    user_id = callback.from_user.id
    data = get_game_data(user_id)
    if not data:
        await callback.answer("❌ Нет активной игры", show_alert=True)
        return
    balance = get_balance(user_id)
    if balance < 0.1:
        await callback.answer("❌ Недостаточно средств для ва-банка", show_alert=True)
        return
    data["bet"] = balance
    user_game_data[user_id] = data
    await callback.answer(f"✅ Ва-банк! Ставка установлена на весь баланс: {balance:.2f}$", show_alert=True)

# ========== МИНИ-ИГРЫ ==========
# ---------- Зонтик ----------
@dp.callback_query(F.data == "game_umbrella")
async def umbrella_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Deposit.waiting_amount)  # временно используем состояние
    await state.update_data(game="umbrella")
    await callback.answer()

# ---------- Кено ----------
@dp.callback_query(F.data == "game_keno")
async def keno_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Deposit.waiting_amount)
    await state.update_data(game="keno")
    await callback.answer()

# ---------- Кит ----------
@dp.callback_query(F.data == "game_whale")
async def whale_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Deposit.waiting_amount)
    await state.update_data(game="whale")
    await callback.answer()

# ---------- Автомат ----------
@dp.callback_query(F.data == "game_slots")
async def slots_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(Deposit.waiting_amount)
    await state.update_data(game="slots")
    await callback.answer()

# ---------- Мины ----------
@dp.callback_query(F.data == "game_mines")
async def mines_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(MinesGame.waiting_bet)
    await callback.answer()

@dp.message(MinesGame.waiting_bet)
async def mines_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Мин 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        await state.update_data(bet=bet)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 мина (x1.2)", callback_data="mines_1")],
            [InlineKeyboardButton(text="2 мины (x1.5)", callback_data="mines_2")],
            [InlineKeyboardButton(text="3 мины (x2)", callback_data="mines_3")],
            [InlineKeyboardButton(text="5 мин (x3)", callback_data="mines_5")],
            [InlineKeyboardButton(text="8 мин (x5)", callback_data="mines_8")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="mini_games")]
        ])
        await message.answer("💣 Выберите количество мин:", reply_markup=keyboard)
        await state.set_state(MinesGame.waiting_mines_count)
    except:
        await message.answer("Введите число")

@dp.callback_query(F.data.startswith("mines_"))
async def mines_set_count(callback: types.CallbackQuery, state: FSMContext):
    count = int(callback.data.split("_")[1])
    data = await state.get_data()
    bet = data["bet"]
    multiplier = {1:1.2, 2:1.5, 3:2.0, 5:3.0, 8:5.0}[count]
    total_cells = 9
    mine_positions = set(random.sample(range(9), count))
    safe_cells = [i for i in range(9) if i not in mine_positions]
    mines_games[callback.from_user.id] = {
        "bet": bet,
        "multiplier": multiplier,
        "mines": mine_positions,
        "safe_cells": set(safe_cells),
        "opened": set(),
        "count": count
    }
    await show_mines_field(callback.message, callback.from_user.id)
    await state.clear()
    await callback.answer()

async def show_mines_field(message: types.Message, user_id):
    game = mines_games.get(user_id)
    if not game:
        return
    keyboard = []
    for i in range(9):
        if i in game["opened"]:
            symbol = "💣" if i in game["mines"] else "✅"
        else:
            symbol = "⬜"
        row = i // 3
        if len(keyboard) <= row:
            keyboard.append([])
        keyboard[row].append(InlineKeyboardButton(text=symbol, callback_data=f"mines_cell_{i}"))
    keyboard.append([InlineKeyboardButton(text="🏆 Забрать выигрыш", callback_data="mines_cashout")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="mini_games")])
    await message.edit_text(f"💣 Мины ({game['count']} мин, множитель x{game['multiplier']})", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("mines_cell_"))
async def mines_open_cell(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in mines_games:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    game = mines_games[user_id]
    cell = int(callback.data.split("_")[2])
    if cell in game["opened"]:
        await callback.answer("Клетка уже открыта", show_alert=True)
        return
    if cell in game["mines"]:
        update_balance(user_id, -game["bet"])
        await callback.message.edit_text(f"💣 БАХ! Вы наступили на мину.\n❌ Проигрыш: {game['bet']:.2f}$", reply_markup=back_button("mini_games"))
        del mines_games[user_id]
        await callback.answer()
        return
    game["opened"].add(cell)
    safe_opened = len([c for c in game["opened"] if c not in game["mines"]])
    total_safe = len(game["safe_cells"])
    if safe_opened == total_safe:
        payout = game["bet"] * game["multiplier"]
        update_balance(user_id, payout)
        await callback.message.edit_text(f"🎉 ПОБЕДА! Вы открыли все безопасные клетки!\n✅ Выигрыш: {game['bet']:.2f} x{game['multiplier']} = {payout:.2f}$", reply_markup=back_button("mini_games"))
        del mines_games[user_id]
        await callback.answer()
        return
    await show_mines_field(callback.message, user_id)
    await callback.answer()

@dp.callback_query(F.data == "mines_cashout")
async def mines_cashout(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in mines_games:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    game = mines_games[user_id]
    safe_opened = len([c for c in game["opened"] if c not in game["mines"]])
    if safe_opened == 0:
        await callback.answer("Нет открытых клеток", show_alert=True)
        return
    total_safe = len(game["safe_cells"])
    coeff = (safe_opened / total_safe) * game["multiplier"]
    payout = game["bet"] * coeff
    update_balance(user_id, payout)
    await callback.message.edit_text(f"💰 Вы забрали выигрыш досрочно.\n✅ Выигрыш: {payout:.2f}$", reply_markup=back_button("mini_games"))
    del mines_games[user_id]
    await callback.answer()

# Обработчик ввода ставки для зонтика, кено, кита, автомата
@dp.message(Deposit.waiting_amount)
async def mini_game_bet(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    data = await state.get_data()
    game = data.get("game")
    if not game:
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Мин 0.1$")
            return
        if bet > get_balance(message.from_user.id):
            await message.answer(f"Не хватает. Баланс: {get_balance(message.from_user.id):.2f}$")
            return
        if game == "umbrella":
            win = random.random() < 0.5
            if win:
                payout = bet * 2
                update_balance(message.from_user.id, payout)
                result = f"☂️ Дождь прошёл мимо!\n✅ Выигрыш: {bet:.2f} x2 = {payout:.2f}$"
            else:
                update_balance(message.from_user.id, -bet)
                result = f"☂️ Вы промокли!\n❌ Проигрыш: {bet:.2f}$"
        elif game == "keno":
            chosen = random.randint(1,20)
            drawn = random.randint(1,20)
            if chosen == drawn:
                payout = bet * 10
                update_balance(message.from_user.id, payout)
                result = f"🎰 Кено! Ваше число {chosen}, выпало {drawn}\n✅ ДЖЕКПОТ: {bet:.2f} x10 = {payout:.2f}$"
            else:
                update_balance(message.from_user.id, -bet)
                result = f"🎰 Кено! Ваше число {chosen}, выпало {drawn}\n❌ Проигрыш: {bet:.2f}$"
        elif game == "whale":
            roll = random.randint(1,100)
            if roll == 100:
                payout = bet * 50
                update_balance(message.from_user.id, payout)
                result = f"🐋 КИТ! Выпало 100!\n✅ ДЖЕКПОТ: {bet:.2f} x50 = {payout:.2f}$"
            else:
                update_balance(message.from_user.id, -bet)
                result = f"🐋 Выпало {roll}\n❌ Проигрыш: {bet:.2f}$"
        elif game == "slots":
            symbols = ["🍒", "🍋", "🍊", "7️⃣"]
            result_slots = [random.choice(symbols) for _ in range(3)]
            if result_slots[0] == result_slots[1] == result_slots[2]:
                payout = bet * 60
                update_balance(message.from_user.id, payout)
                result = f"🎰 {result_slots[0]} {result_slots[1]} {result_slots[2]}\n✅ ДЖЕКПОТ: {bet:.2f} x60 = {payout:.2f}$"
            else:
                update_balance(message.from_user.id, -bet)
                result = f"🎰 {result_slots[0]} {result_slots[1]} {result_slots[2]}\n❌ Проигрыш: {bet:.2f}$"
        else:
            result = "❌ Неизвестная игра"
        await message.answer(result, reply_markup=back_button("mini_games"))
        await state.clear()
    except:
        await message.answer("Введите число")

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
    await callback.message.edit_text("Админ-панель", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if callback.from_user.id != ADMIN_ID: return
    users = get_all_users()
    text = "👥 Пользователи:\n"
    for u in users:
        sub = datetime.fromtimestamp(u['sub_until']).strftime('%d.%m.%Y') if u['sub_until'] else "Нет"
        text += f"ID {u['tg_id']} | {u['username']} | Подписка: {sub} | Баланс: {u['balance']:.2f}$\n"
    await callback.message.edit_text(text, reply_markup=back_button("admin_panel"))
    await callback.answer()

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Введите ID пользователя и сумму через пробел (например: 123456 100):")
    await state.set_state(AdminAddBalance.waiting_user_id)
    await callback.answer()

@dp.message(AdminAddBalance.waiting_user_id)
async def admin_add_balance_user(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        parts = message.text.split()
        user_id = int(parts[0])
        amount = float(parts[1])
        user = get_user(user_id)
        if not user:
            await message.answer("Пользователь не найден")
            await state.clear()
            return
        new_balance = user["balance"] + amount
        set_balance(user_id, new_balance)
        await message.answer(f"✅ Начислено {amount}$. Новый баланс: {new_balance:.2f}$")
    except:
        await message.answer("Ошибка. Пример: 123456 100")
    await state.clear()

@dp.callback_query(F.data == "admin_remove_balance")
async def admin_remove_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Введите ID пользователя и сумму списания через пробел:")
    await state.set_state(AdminRemoveBalance.waiting_user_id)
    await callback.answer()

@dp.message(AdminRemoveBalance.waiting_user_id)
async def admin_remove_balance_user(message: types.Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    try:
        parts = message.text.split()
        user_id = int(parts[0])
        amount = float(parts[1])
        user = get_user(user_id)
        if not user:
            await message.answer("Пользователь не найден")
            await state.clear()
            return
        if amount > user["balance"]:
            await message.answer(f"Нельзя списать больше {user['balance']:.2f}$")
            await state.clear()
            return
        new_balance = user["balance"] - amount
        set_balance(user_id, new_balance)
        await message.answer(f"✅ Списано {amount}$. Новый баланс: {new_balance:.2f}$")
    except:
        await message.answer("Ошибка. Пример: 123456 50")
    await state.clear()

@dp.callback_query(F.data == "admin_withdraws")
async def admin_withdraws(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    if callback.from_user.id != ADMIN_ID: return
    pending = get_pending_withdraws()
    if not pending:
        await callback.message.edit_text("Нет заявок", reply_markup=back_button("admin_panel"))
        await callback.answer()
        return
    for req in pending:
        req_id, user_id, amount, wallet = req
        text = f"Заявка #{req_id}\n👤 {user_id}\n💵 {amount}$\n💳 {wallet}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{req_id}"),
             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{req_id}")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_withdraw(callback: types.CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Только в ЛС", show_alert=True)
        return
    req_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, amount FROM withdraw_requests WHERE id=?", (req_id,))
    row = c.fetchone()
    if row:
        user_id, amount = row
        current = get_balance(user_id)
        if current >= amount:
            set_balance(user_id, current - amount)
            update_withdraw_status(req_id, "approved")
            await bot.send_message(user_id, f"✅ Вывод {amount}$ одобрен")
            await callback.message.edit_text(f"✅ Заявка #{req_id} одобрена")
        else:
            await callback.message.edit_text(f"❌ У пользователя недостаточно средств")
    else:
        await callback.message.edit_text("Заявка не найдена")
    conn.close()
    await callback.answer()

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

# ========== ЗАПУСК ==========
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())