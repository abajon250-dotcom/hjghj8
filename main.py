import asyncio
import time
import os
import re
import csv
import io
import logging
from datetime import datetime
import random
import asyncpg
import aiohttp

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
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


logging.basicConfig(level=logging.INFO)

# ------------------- КОНФИГ -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID, API_HASH must be set")

SESSIONS_DIR = "/app/sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

TARIFFS = {
    "day": {"days": 1, "price": 2.5, "name": "1 день"},
    "week": {"days": 7, "price": 9, "name": "1 неделя"},
    "month": {"days": 30, "price": 15, "name": "1 месяц"}
}

SUCCESS_GIF_URL = "https://i.gifer.com/LRP3.gif"
ERROR_GIF_URL = "https://i.gifer.com/84OP.gif"

db_pool = None

# ------------------- БАЗА ДАННЫХ -------------------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), command_timeout=60)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                tg_id BIGINT PRIMARY KEY,
                username TEXT,
                sub_until BIGINT DEFAULT 0,
                balance REAL DEFAULT 0,
                registered_at BIGINT DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tg_accounts (
                id SERIAL PRIMARY KEY,
                owner_tg_id BIGINT NOT NULL,
                phone TEXT,
                session_file TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                name TEXT DEFAULT '',
                last_used BIGINT DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS vk_accounts (
                id SERIAL PRIMARY KEY,
                owner_tg_id BIGINT NOT NULL,
                token TEXT,
                vk_name TEXT,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdraw_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount REAL,
                wallet TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE,
                days INTEGER,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 1
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS used_promocodes (
                user_id BIGINT NOT NULL,
                code_id INTEGER NOT NULL,
                used_at BIGINT,
                PRIMARY KEY (user_id, code_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_type TEXT,
                account_id INTEGER,
                total_contacts INTEGER,
                sent INTEGER,
                errors INTEGER,
                start_time BIGINT,
                end_time BIGINT,
                status TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at BIGINT,
                closed_at BIGINT DEFAULT 0
            )
        ''')

        await conn.execute('ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS closed_at BIGINT DEFAULT 0')
    print("✅ PostgreSQL ready")

# ------------------- ПОЛЬЗОВАТЕЛИ -------------------
async def get_user(tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT tg_id, username, sub_until, balance FROM users WHERE tg_id=$1", tg_id)
        if row:
            return {"tg_id": row["tg_id"], "username": row["username"], "sub_until": row["sub_until"] or 0, "balance": row["balance"]}
    return None

async def create_user(tg_id: int, username: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (tg_id, username, sub_until, balance, registered_at) VALUES ($1,$2,0,0,$3) ON CONFLICT (tg_id) DO NOTHING",
                           tg_id, username, int(time.time()))

async def is_platinum_subscribed(tg_id: int):
    if tg_id == ADMIN_ID:
        return True
    user = await get_user(tg_id)
    return user and user["sub_until"] > int(time.time())

async def set_subscription(tg_id: int, days: int):
    new_time = int(time.time()) + days * 86400
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET sub_until=$1 WHERE tg_id=$2", new_time, tg_id)

async def get_balance(tg_id: int):
    user = await get_user(tg_id)
    return user["balance"] if user else 0.0

async def update_balance(tg_id: int, delta: float):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE tg_id=$2", delta, tg_id)

# ------------------- TELEGRAM АККАУНТЫ -------------------
async def add_tg_account(owner_tg_id: int, phone: str, session_file: str, name: str) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO tg_accounts (owner_tg_id, phone, session_file, name, last_used) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            owner_tg_id, phone, session_file, name, int(time.time())
        )
        return row["id"]

async def get_user_tg_accounts(owner_tg_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, phone, name, is_active FROM tg_accounts WHERE owner_tg_id=$1 ORDER BY last_used DESC", owner_tg_id)
        return [{"id": r["id"], "phone": r["phone"], "name": r["name"], "is_active": r["is_active"]} for r in rows]

async def set_active_tg_account(owner_tg_id: int, account_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tg_accounts SET is_active=FALSE WHERE owner_tg_id=$1", owner_tg_id)
        await conn.execute("UPDATE tg_accounts SET is_active=TRUE, last_used=$1 WHERE id=$2 AND owner_tg_id=$3", int(time.time()), account_id, owner_tg_id)

async def delete_tg_account(owner_tg_id: int, account_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", account_id, owner_tg_id)

async def deactivate_tg_account(owner_tg_id: int, account_id: int, phone: str = ""):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=$1 AND owner_tg_id=$2", account_id, owner_tg_id)

# ------------------- VK АККАУНТЫ -------------------
async def add_vk_account(owner_tg_id: int, token: str, vk_name: str) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO vk_accounts (owner_tg_id, token, vk_name, is_active) VALUES ($1,$2,$3,TRUE) RETURNING id",
            owner_tg_id, token, vk_name
        )
        return row["id"]

async def get_user_vk_accounts(owner_tg_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, vk_name, is_active FROM vk_accounts WHERE owner_tg_id=$1", owner_tg_id)
        return [{"id": r["id"], "name": r["vk_name"], "is_active": r["is_active"]} for r in rows]

async def set_active_vk_account(owner_tg_id: int, account_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE vk_accounts SET is_active=FALSE WHERE owner_tg_id=$1", owner_tg_id)
        await conn.execute("UPDATE vk_accounts SET is_active=TRUE WHERE id=$1 AND owner_tg_id=$2", account_id, owner_tg_id)

async def delete_vk_account(owner_tg_id: int, account_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", account_id, owner_tg_id)

# ------------------- АДМИН -------------------
async def get_all_users():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id, username, sub_until, balance FROM users ORDER BY tg_id")
        return [{"tg_id": r["tg_id"], "username": r["username"], "sub_until": r["sub_until"] or 0, "balance": r["balance"]} for r in rows]

async def add_withdraw_request(user_id: int, amount: float, wallet: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO withdraw_requests (user_id, amount, wallet) VALUES ($1,$2,$3)", user_id, amount, wallet)

async def get_pending_withdraws():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, user_id, amount, wallet FROM withdraw_requests WHERE status='pending'")
        return [(r["id"], r["user_id"], r["amount"], r["wallet"]) for r in rows]

async def update_withdraw_status(req_id: int, status: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE withdraw_requests SET status=$1 WHERE id=$2", status, req_id)

# ------------------- ПРОМОКОДЫ -------------------
async def create_promocode(code: str, days: int, max_uses: int = 1):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO promocodes (code, days, max_uses) VALUES ($1,$2,$3)", code, days, max_uses)

async def get_promocode(code: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, days, uses, max_uses FROM promocodes WHERE code=$1", code)
        if row:
            return {"id": row["id"], "days": row["days"], "uses": row["uses"], "max_uses": row["max_uses"]}
        return None

async def use_promocode(user_id: int, code_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE promocodes SET uses = uses + 1 WHERE id=$1", code_id)
        await conn.execute("INSERT INTO used_promocodes (user_id, code_id, used_at) VALUES ($1,$2,$3)", user_id, code_id, int(time.time()))

async def get_all_promocodes():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, code, days, uses, max_uses FROM promocodes")
        return [{"id": r["id"], "code": r["code"], "days": r["days"], "uses": r["uses"], "max_uses": r["max_uses"]} for r in rows]

async def delete_promocode(code_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM promocodes WHERE id=$1", code_id)

# ------------------- CRYPTOBOT -------------------
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

async def create_crypto_invoice(amount_usd: float, description: str):
    if not CRYPTOBOT_TOKEN:
        return None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(CRYPTOBOT_API_URL + "/createInvoice", headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                                    json={"asset": "USDT", "amount": str(amount_usd), "description": description}) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"pay_url": data["result"]["pay_url"], "invoice_id": data["result"]["invoice_id"]}
        except:
            pass
    return None

async def check_crypto_invoice(invoice_id: str):
    if not CRYPTOBOT_TOKEN:
        return None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(CRYPTOBOT_API_URL + "/getInvoices", headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                                   params={"invoice_ids": invoice_id}) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["items"]:
                    return data["result"]["items"][0]["status"]
        except:
            pass
    return None

# ------------------- ОБЩИЕ -------------------
def get_russian_error(e: Exception) -> str:
    error = str(e)
    if "Cannot find any entity" in error:
        return "Не удалось найти пользователя или чат."
    if "Too many requests" in error or "FloodWaitError" in error:
        return "Слишком много запросов. Подождите."
    if "AuthKeyError" in error or "UnauthorizedError" in error:
        return "Сессия устарела. Аккаунт будет деактивирован."
    return error

async def is_subscribed_to_channel(user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ["member", "creator", "administrator"]
    except:
        return False

async def send_discord_log(title: str, description: str, color: int = 0x00ff00):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Quasar Spam"}
        }
        async with aiohttp.ClientSession() as session:
            await session.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]})
    except Exception as e:
        logging.warning(f"Discord log error: {e}")

# ------------------- КЛАВИАТУРЫ -------------------
def main_menu(tg_id: int):
    kb = [
        [InlineKeyboardButton(text="👤 КАБИНЕТ", callback_data="profile"),
         InlineKeyboardButton(text="🔧 АККАУНТЫ", callback_data="my_accounts")],
        [InlineKeyboardButton(text="❓ ПОМОЩЬ", callback_data="help"),
         InlineKeyboardButton(text="🆘 ПОДДЕРЖКА", callback_data="support")],
    ]
    if tg_id == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="⚙️ АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ПОПОЛНИТЬ", callback_data="deposit"),
         InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw")],
        [InlineKeyboardButton(text="🎁 ПРОМОКОД", callback_data="activate_promo"),
         InlineKeyboardButton(text="💎 ПОДПИСКА", callback_data="buy_sub")],
        [InlineKeyboardButton(text="◀️ НА ГЛАВНУЮ", callback_data="main_menu")]
    ])

def my_accounts_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 TELEGRAM", callback_data="list_tg_accounts")],
        [InlineKeyboardButton(text="📘 VK", callback_data="list_vk_accounts")],
        [InlineKeyboardButton(text="➕ ПОДКЛЮЧИТЬ НОВЫЙ", callback_data="connect_new_account")],
        [InlineKeyboardButton(text="◀️ НА ГЛАВНУЮ", callback_data="main_menu")]
    ])

def connect_new_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 TELEGRAM", callback_data="add_tg")],
        [InlineKeyboardButton(text="📘 VK", callback_data="add_vk")],
        [InlineKeyboardButton(text="📤 МАССОВО VK", callback_data="mass_vk")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_accounts")]
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 ПОЛЬЗОВАТЕЛИ", callback_data="admin_users")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="admin_ext_stats")],
        [InlineKeyboardButton(text="💰 БАЛАНСЫ", callback_data="admin_balance_manage")],
        [InlineKeyboardButton(text="🎁 ВЫДАТЬ ПОДПИСКУ", callback_data="admin_give_sub")],
        [InlineKeyboardButton(text="📢 ГЛОБАЛ РАССЫЛКА", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎫 ПРОМОКОДЫ", callback_data="admin_promocodes")],
        [InlineKeyboardButton(text="💸 ЗАЯВКИ НА ВЫВОД", callback_data="admin_withdraws")],
        [InlineKeyboardButton(text="📥 ЭКСПОРТ CSV", callback_data="admin_export_csv")],
        [InlineKeyboardButton(text="📋 ЛОГИ РАССЫЛОК", callback_data="admin_broadcast_stats")],
        [InlineKeyboardButton(text="◀️ НА ГЛАВНУЮ", callback_data="main_menu")]
    ])

def back_button(callback_data: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data=callback_data)]
    ])

async def tg_accounts_list(user_id: int):
    accounts = await get_user_tg_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']} ({acc['phone']})", callback_data=f"tg_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ TG", callback_data="add_tg")])
    kb.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def vk_accounts_list(user_id: int):
    accounts = await get_user_vk_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']}", callback_data=f"vk_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ VK", callback_data="add_vk")])
    kb.append([InlineKeyboardButton(text="🧹 ОЧИСТИТЬ НЕАКТИВНЫЕ", callback_data="clean_inactive_vk")])
    kb.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ------------------- FSM -------------------
class AddTG(StatesGroup): waiting_phone = State(); waiting_code = State(); waiting_2fa = State()
class AddVK(StatesGroup): waiting_token = State()
class BroadcastTG(StatesGroup): waiting_text = State(); waiting_delay = State()
class BroadcastVK(StatesGroup): waiting_text = State(); waiting_delay = State()
class Deposit(StatesGroup): waiting_amount = State()
class Withdraw(StatesGroup): waiting_amount = State(); waiting_wallet = State()
class AdminGiveSubscription(StatesGroup): waiting_user_id = State(); waiting_days = State()
class AdminAddBalance(StatesGroup): waiting_user_id = State(); waiting_amount = State()
class AdminRemoveBalance(StatesGroup): waiting_user_id = State(); waiting_amount = State()
class AdminCreatePromocode(StatesGroup): waiting_code = State(); waiting_days = State(); waiting_max_uses = State()
class ActivatePromo(StatesGroup): waiting_code = State()
class Support(StatesGroup): waiting_question = State(); waiting_reply = State()
class MassVK(StatesGroup): waiting_tokens = State()
class AdminBroadcast(StatesGroup): waiting_text = State(); waiting_confirm = State()

# ------------------- БОТ -------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ------------------- СТАРТ -------------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    await create_user(message.from_user.id, message.from_user.username or str(message.from_user.id))
    if not await is_subscribed_to_channel(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 ПОДПИСАТЬСЯ", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton(text="✅ ПРОВЕРИТЬ", callback_data="check_sub_start")]
        ])
        await message.answer(f"❌ Подпишитесь на канал @{CHANNEL_USERNAME}", reply_markup=kb)
        return
    try:
        await message.answer_animation(animation="https://i.gifer.com/X63H.gif", caption="🎉 *ДОБРО ПОЖАЛОВАТЬ В Quasar!*", parse_mode="Markdown")
    except:
        pass
    await message.answer("👇 *Главное меню*", reply_markup=main_menu(message.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "check_sub_start")
async def check_sub_start(callback: types.CallbackQuery):
    if await is_subscribed_to_channel(callback.from_user.id):
        await callback.message.delete()
        await start_cmd(callback.message)
    else:
        await callback.answer("❌ Вы не подписаны", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Главное меню", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

# ------------------- ПРОФИЛЬ -------------------
@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = user["balance"]
    sub_until = datetime.fromtimestamp(user["sub_until"]).strftime('%d.%m.%Y') if user["sub_until"] else "—"
    text = (
        "👑 *| ВАШ ПРОФИЛЬ |* 👑\n\n"
        "┌───────────────────┐\n"
        f"│  💰 *БАЛАНС*      │ `{balance:.2f}$`\n"
        "├───────────────────┤\n"
        f"│  💎 *ПОДПИСКА*    │ до `{sub_until}`\n"
        "└───────────────────┘\n\n"
        "▫️ Пополните счёт\n"
        "▫️ Активируйте промокод\n"
        "▫️ Подписка откроет доступ к рассылкам"
    )
    await callback.message.edit_text(text, reply_markup=profile_kb(), parse_mode="Markdown")
    await callback.answer()

# ------------------- АККАУНТЫ -------------------
@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Управление аккаунтами", reply_markup=my_accounts_menu())

@dp.callback_query(F.data == "connect_new_account")
async def connect_new(callback: types.CallbackQuery):
    await callback.message.edit_text("Выберите тип:", reply_markup=connect_new_menu())
    await callback.answer()

@dp.callback_query(F.data == "list_tg_accounts")
async def list_tg_accounts(callback: types.CallbackQuery):
    accounts = await get_user_tg_accounts(callback.from_user.id)
    if not accounts:
        text = "📭 *У вас нет Telegram аккаунтов.*\nНажмите ➕ ДОБАВИТЬ TG, чтобы подключить."
        await callback.message.edit_text(text, reply_markup=back_button("my_accounts"), parse_mode="Markdown")
        return
    text = "📱 *ВАШИ TELEGRAM АККАУНТЫ*"
    await callback.message.edit_text(text, reply_markup=await tg_accounts_list(callback.from_user.id), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "list_vk_accounts")
async def list_vk_accounts(callback: types.CallbackQuery):
    accounts = await get_user_vk_accounts(callback.from_user.id)
    if not accounts:
        text = "📭 *У вас нет VK аккаунтов.*\nНажмите ➕ ДОБАВИТЬ VK, чтобы подключить."
        await callback.message.edit_text(text, reply_markup=back_button("my_accounts"), parse_mode="Markdown")
        return
    text = "📘 *ВАШИ VK АККАУНТЫ*"
    try:
        await callback.message.edit_text(text, reply_markup=await vk_accounts_list(callback.from_user.id), parse_mode="Markdown")
    except Exception as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_acc_"))
async def tg_account_actions(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    accounts = await get_user_tg_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активным" if not acc["is_active"] else "✅ Активен", callback_data=f"tg_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"tg_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_tg_accounts")]
    ])
    await callback.message.edit_text(f"Аккаунт: {acc['name']} ({acc['phone']})", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_set_active_"))
async def tg_set_active(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    await set_active_tg_account(callback.from_user.id, acc_id)
    await callback.answer("✅ Активен", show_alert=True)
    await list_tg_accounts(callback)

@dp.callback_query(F.data.startswith("tg_del_"))
async def tg_delete(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    await delete_tg_account(callback.from_user.id, acc_id)
    await callback.answer("Удалён", show_alert=True)
    await list_tg_accounts(callback)

@dp.callback_query(F.data.startswith("vk_acc_"))
async def vk_account_actions(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    accounts = await get_user_vk_accounts(callback.from_user.id)
    acc = next((a for a in accounts if a["id"] == acc_id), None)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активен" if acc["is_active"] else "❌ Неактивен", callback_data=f"vk_set_active_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data=f"vk_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"vk_del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_vk_accounts")]
    ])
    await callback.message.edit_text(f"Управление VK: {acc['name']}", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_set_active_"))
async def vk_set_active(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    await set_active_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Аккаунт активен", show_alert=True)
    await list_vk_accounts(callback)

@dp.callback_query(F.data.startswith("vk_del_"))
async def vk_delete(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    await delete_vk_account(callback.from_user.id, acc_id)
    await callback.answer("Удалён", show_alert=True)
    await list_vk_accounts(callback)

# ------------------- ДОБАВЛЕНИЕ TG -------------------
@dp.callback_query(F.data == "add_tg")
async def add_tg_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна подписка!", show_alert=True)
        return
    await callback.message.answer("📞 Введите номер телефона (+79991234567):")
    await state.set_state(AddTG.waiting_phone)
    await callback.answer()

@dp.message(AddTG.waiting_phone)
async def add_tg_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    session_file = os.path.join(SESSIONS_DIR, f"{message.from_user.id}_{phone}.session")
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.send_code_request(phone)
        await state.update_data(phone=phone, session_file=session_file, client=client)
        await message.answer("🔑 Введите код из SMS:")
        await state.set_state(AddTG.waiting_code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

@dp.message(AddTG.waiting_code)
async def add_tg_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    phone = data["phone"]
    try:
        await client.sign_in(phone, code)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        acc_id = await add_tg_account(message.from_user.id, phone, data["session_file"], name)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ ДОБАВИТЬ ЕЩЁ TG", callback_data="add_tg"),
             InlineKeyboardButton(text="📨 НАЧАТЬ РАССЫЛКУ", callback_data=f"tg_broadcast_{acc_id}")],
            [InlineKeyboardButton(text="◀️ МОИ АККАУНТЫ", callback_data="my_accounts")]
        ])
        await message.answer(f"✅ Аккаунт {name} добавлен!", reply_markup=kb)
        await client.disconnect()
        await state.clear()
        await send_discord_log("➕ Добавлен Telegram аккаунт", f"Пользователь: {message.from_user.id}\nАккаунт: {name}", 0x00ff00)
    except SessionPasswordNeededError:
        await message.answer("🔒 Введите 2FA пароль:")
        await state.set_state(AddTG.waiting_2fa)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

@dp.message(AddTG.waiting_2fa)
async def add_tg_2fa(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    client = data["client"]
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        name = f"{me.first_name} {me.last_name or ''}".strip() or me.username or str(me.id)
        acc_id = await add_tg_account(message.from_user.id, data["phone"], data["session_file"], name)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ ДОБАВИТЬ ЕЩЁ TG", callback_data="add_tg"),
             InlineKeyboardButton(text="📨 НАЧАТЬ РАССЫЛКУ", callback_data=f"tg_broadcast_{acc_id}")],
            [InlineKeyboardButton(text="◀️ МОИ АККАУНТЫ", callback_data="my_accounts")]
        ])
        await message.answer(f"✅ Аккаунт {name} добавлен (2FA)!", reply_markup=kb)
        await client.disconnect()
        await state.clear()
        await send_discord_log("➕ Добавлен Telegram аккаунт (2FA)", f"Пользователь: {message.from_user.id}\nАккаунт: {name}", 0x00ff00)
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {get_russian_error(e)}")
        await state.clear()

# ------------------- ДОБАВЛЕНИЕ VK -------------------
@dp.callback_query(F.data == "add_vk")
async def add_vk_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна подписка!", show_alert=True)
        return
    await callback.message.answer("🔑 Введите токен VK (access_token):")
    await state.set_state(AddVK.waiting_token)
    await callback.answer()

@dp.message(AddVK.waiting_token)
async def add_vk_token(message: types.Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым.")
        await state.clear()
        return

    try:
        # Создаём сессию и получаем объект API
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()          # <-- ЭТА СТРОКА ОБЯЗАТЕЛЬНА
        user = vk.users.get()[0]           # <-- ТЕПЕРЬ РАБОТАЕТ

        name = f"{user['first_name']} {user['last_name']}"
        acc_id = await add_vk_account(message.from_user.id, token, name)

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ ДОБАВИТЬ ЕЩЁ VK", callback_data="add_vk"),
                InlineKeyboardButton(text="📨 НАЧАТЬ РАССЫЛКУ", callback_data=f"vk_broadcast_{acc_id}")
            ],
            [InlineKeyboardButton(text="◀️ МОИ АККАУНТЫ", callback_data="my_accounts")]
        ])

        await message.answer(f"✅ VK аккаунт **{name}** добавлен!", reply_markup=kb, parse_mode="Markdown")
        await state.clear()
        await send_discord_log("➕ Добавлен VK аккаунт", f"Пользователь: {message.from_user.id}\nАккаунт: {name}", 0x00ff00)

    except vk_api.exceptions.ApiError as e:
        if "invalid access_token" in str(e) or "5" in str(e):
            await message.answer("❌ Неверный токен. Получите новый через https://vkhost.github.io (включите `messages`).")
        else:
            await message.answer(f"❌ Ошибка VK: {e}")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

@dp.callback_query(F.data == "mass_vk")
async def mass_vk_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_platinum_subscribed(callback.from_user.id):
        await callback.answer("❌ Нужна подписка!", show_alert=True)
        return
    await callback.message.answer("📎 Введите список VK токенов через запятую (можно с пробелами).")
    await state.set_state(MassVK.waiting_tokens)
    await callback.answer()

@dp.message(MassVK.waiting_tokens)
async def mass_vk_process(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        await message.answer("❌ Не найдено ни одного токена.")
        await state.clear()
        return
    added = 0
    errors = []
    for token in tokens:
        try:
            vk_session = vk_api.VkApi(token=token)
            vk = vk_session.get_api()
            user = vk.users.get()[0]
            name = f"{user['first_name']} {user['last_name']}"
            await add_vk_account(message.from_user.id, token, name)
            added += 1
        except Exception as e:
            errors.append(f"{token[:10]}...: {str(e)}")
        await asyncio.sleep(0.5)
    await message.answer(f"✅ Добавлено: {added}\n❌ Ошибок: {len(errors)}")
    await state.clear()

# ------------------- ОЧИСТКА НЕАКТИВНЫХ VK -------------------
async def clean_invalid_vk_accounts(user_id: int) -> int:
    deleted = 0
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, token, vk_name FROM vk_accounts WHERE owner_tg_id=$1", user_id)
        for row in rows:
            try:
                vk = vk_api.VkApi(token=row["token"])
                vk.users.get()
            except:
                await delete_vk_account(user_id, row["id"])
                deleted += 1
    return deleted

@dp.callback_query(F.data == "clean_inactive_vk")
async def clean_inactive_vk(callback: types.CallbackQuery):
    await callback.answer("🔄 Проверка...", show_alert=False)
    deleted = await clean_invalid_vk_accounts(callback.from_user.id)
    await callback.answer(f"🧹 Удалено неактивных: {deleted}", show_alert=True)
    await callback.message.delete()
    await list_vk_accounts(callback)

# ------------------- РАССЫЛКИ TG -------------------
@dp.callback_query(F.data.startswith("tg_broadcast_"))
async def tg_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(BroadcastTG.waiting_text)
    await callback.answer()

@dp.message(BroadcastTG.waiting_text)
async def broadcast_tg_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("Задержка (сек):")
    await state.set_state(BroadcastTG.waiting_delay)

@dp.message(BroadcastTG.waiting_delay)
async def broadcast_tg_delay(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if not raw:
        await message.answer("❌ Введите число.")
        return
    try:
        delay = float(raw.replace(',', '.'))
        if delay < 1:
            delay = 1
    except:
        await message.answer("❌ Нужно число, например 5")
        return

    data = await state.get_data()
    text = data["text"]
    acc_id = data["acc_id"]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone, name FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ Аккаунт не найден")
            await state.clear()
            return
        session_file, phone, acc_name = row["session_file"], row["phone"], row["name"]

    status_msg = await message.answer(f"📲 *Аккаунт {acc_name} загружается...*", parse_mode="Markdown")
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_user]
        total = len(targets)
        if total == 0:
            await status_msg.edit_text("❌ Нет диалогов.")
            return
        sent = 0
        errors = 0
        start_time = time.time()
        for dialog in targets:
            try:
                await client.send_message(dialog.entity, text)
                sent += 1
            except:
                errors += 1
            await asyncio.sleep(delay)
        elapsed = time.time() - start_time
        success_rate = (sent / (sent+errors))*100 if sent+errors else 0
        report = f"✅ *Рассылка TG завершена*\n📤 Отправлено: {sent}/{total}\n❌ Ошибок: {errors}\n📈 Успешность: {success_rate:.1f}%\n⏱️ Затрачено: {elapsed:.1f} сек."
        await status_msg.edit_text(report, parse_mode="Markdown")
        await send_discord_log("📨 TG рассылка", f"Аккаунт: {acc_name}\nОтправлено: {sent}/{total}\nОшибок: {errors}", 0x00ff00 if errors==0 else 0xffaa00)
    except (AuthKeyError, UnauthorizedError):
        await deactivate_tg_account(message.from_user.id, acc_id, phone)
        await status_msg.edit_text(f"❌ Аккаунт {phone} деактивирован.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# ------------------- РАССЫЛКИ VK -------------------
@dp.callback_query(F.data.startswith("vk_broadcast_"))
async def vk_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(BroadcastVK.waiting_text)
    await callback.answer()

@dp.message(BroadcastVK.waiting_text)
async def broadcast_vk_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("Задержка (сек):")
    await state.set_state(BroadcastVK.waiting_delay)

@dp.message(BroadcastVK.waiting_delay)
async def broadcast_vk_delay(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if not raw:
        await message.answer("❌ Введите число.")
        return
    try:
        delay = float(raw.replace(',', '.'))
        if delay < 1:
            delay = 1
    except:
        await message.answer("❌ Нужно число, например 5")
        return

    data = await state.get_data()
    text = data["text"]
    acc_id = data["acc_id"]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token, vk_name, is_active FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ Аккаунт не найден")
            await state.clear()
            return
        token, vk_name, is_active = row["token"], row["vk_name"], row["is_active"]

    if not is_active:
        await message.answer(f"❌ Аккаунт {vk_name} неактивен.")
        await state.clear()
        return

    status_msg = await message.answer(f"📲 *Аккаунт {vk_name} загружается...*", parse_mode="Markdown")

    # ---- Правильное создание API ----
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()

    # Проверка токена
    try:
        vk.users.get()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка авторизации VK: {get_russian_error(e)}")
        await state.clear()
        return

    # Собираем только друзей (беседы убираем, чтобы меньше ошибок)
    try:
        friends = vk.friends.get()["items"]
        total = len(friends)
        if total == 0:
            await status_msg.edit_text("❌ Нет друзей для рассылки.")
            await state.clear()
            return
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка получения друзей: {get_russian_error(e)}")
        await state.clear()
        return

    await status_msg.edit_text(
        f"🚀 *Рассылка VK запущена*\n"
        f"👥 Друзей: {total}\n"
        f"⏱️ Задержка: {delay} сек\n\n"
        f"✅ Отправлено: 0/{total} (0%)"
    )

    sent = 0
    errors = 0
    skipped = 0
    start_time = time.time()
    last_update = start_time

    async def update_progress():
        nonlocal sent, errors, skipped, total, start_time
        elapsed = time.time() - start_time
        processed = sent + errors + skipped
        percent = (processed / total) * 100 if total else 0
        speed = sent / elapsed * 60 if elapsed > 0 else 0
        remaining_sec = (total - processed) / (speed / 60) if speed > 0 else 0
        bar_len = 20
        filled = int(bar_len * sent / total) if total else 0
        bar = "🟩" * filled + "⬜" * (bar_len - filled)
        text_status = (
            f"📤 *Рассылка VK в процессе*\n\n"
            f"👥 Всего: {total}\n"
            f"✅ Отправлено: {sent}\n"
            f"❌ Ошибок: {errors}\n"
            f"⏭️ Пропущено: {skipped}\n"
            f"📊 Прогресс: {percent:.1f}%\n"
            f"{bar}\n"
            f"⚡ Скорость: {speed:.1f} сообщ/мин\n"
            f"⏲️ Осталось ~ {remaining_sec:.0f} сек"
        )
        await status_msg.edit_text(text_status, parse_mode="Markdown")

    await update_progress()

    for friend_id in friends:
        try:
            vk.messages.send(user_id=friend_id, message=text, random_id=0)
            sent += 1
        except vk_api.exceptions.ApiError as e:
            err_str = str(e).lower()
            # Пропускаем недоступных – они не считаются ошибками
            if "user deactivated" in err_str or "cannot send" in err_str or "access denied" in err_str or "privacy settings" in err_str:
                skipped += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1

        await asyncio.sleep(delay)

        if time.time() - last_update >= 5:
            await update_progress()
            last_update = time.time()

    elapsed = time.time() - start_time
    success_rate = (sent / (sent + errors + skipped)) * 100 if (sent + errors + skipped) else 0
    final_report = (
        f"✅ *Рассылка VK завершена*\n"
        f"📤 Отправлено: {sent}\n"
        f"❌ Ошибок: {errors}\n"
        f"⏭️ Пропущено (недоступно): {skipped}\n"
        f"📈 Успешность: {success_rate:.1f}%\n"
        f"⏱️ Затрачено: {elapsed:.1f} сек."
    )
    await status_msg.edit_text(final_report, parse_mode="Markdown")
    gif = SUCCESS_GIF_URL if errors == 0 else ERROR_GIF_URL
    caption = "🎉 Успешно!" if errors == 0 else "⚠️ С ошибками"
    try:
        await message.answer_animation(animation=gif, caption=caption, parse_mode="Markdown")
    except:
        pass
    await send_discord_log("📘 VK рассылка", f"Аккаунт: {vk_name}\nОтправлено: {sent}\nОшибок: {errors}\nПропущено: {skipped}", 0x00ff00 if errors == 0 else 0xffaa00)
    await state.clear()

# ------------------- ПОДПИСКА, БАЛАНС, ВЫВОД -------------------
@dp.callback_query(F.data == "buy_sub")
async def buy_sub(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день - 2.5$", callback_data="tariff_day")],
        [InlineKeyboardButton(text="1 неделя - 9$", callback_data="tariff_week")],
        [InlineKeyboardButton(text="1 месяц - 15$", callback_data="tariff_month")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
    ])
    await callback.message.edit_text("Выберите тариф:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: types.CallbackQuery, state: FSMContext):
    tariff_key = callback.data.split("_")[1]
    tariff = TARIFFS[tariff_key]
    await state.update_data(tariff=tariff)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Оплатить с баланса", callback_data="pay_balance")],
        [InlineKeyboardButton(text="💳 Оплатить через CryptoBot", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Тариф: {tariff['name']} - {tariff['price']}$", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "pay_balance")
async def pay_balance(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка", show_alert=True)
        return
    balance = await get_balance(callback.from_user.id)
    if balance >= tariff["price"]:
        await update_balance(callback.from_user.id, -tariff["price"])
        await set_subscription(callback.from_user.id, tariff["days"])
        await callback.message.edit_text(f"✅ Подписка на {tariff['name']} активирована!", reply_markup=main_menu(callback.from_user.id))
    else:
        await callback.answer(f"Не хватает. Нужно {tariff['price']}$", show_alert=True)
    await state.clear()
    await callback.answer()

crypto_pending = {}

@dp.callback_query(F.data == "pay_crypto")
async def pay_crypto(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tariff = data.get("tariff")
    if not tariff:
        await callback.answer("Ошибка", show_alert=True)
        return
    invoice = await create_crypto_invoice(tariff["price"], f"Подписка {tariff['name']}")
    if not invoice:
        await callback.answer("Ошибка создания счёта", show_alert=True)
        return
    crypto_pending[callback.from_user.id] = {"invoice_id": invoice["invoice_id"], "days": tariff["days"]}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_sub_{invoice['invoice_id']}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_sub")]
    ])
    await callback.message.edit_text(f"Оплатите {tariff['price']} USDT", reply_markup=kb)
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("check_sub_"))
async def check_sub_payment(callback: types.CallbackQuery):
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in crypto_pending:
            days = crypto_pending[callback.from_user.id]["days"]
            await set_subscription(callback.from_user.id, days)
            del crypto_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Подписка активирована на {days} дней!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Оплата подтверждена", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 Сумма пополнения (мин 1$):")
    await state.set_state(Deposit.waiting_amount)
    await callback.answer()

@dp.message(Deposit.waiting_amount)
async def deposit_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 1:
            await message.answer("Мин 1$")
            return
        invoice = await create_crypto_invoice(amount, f"Пополнение на {amount}$")
        if not invoice:
            await message.answer("Ошибка создания счёта", reply_markup=back_button("profile"))
            await state.clear()
            return
        deposit_pending[message.from_user.id] = {"invoice_id": invoice["invoice_id"], "amount": amount}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Оплатить", url=invoice["pay_url"])],
            [InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_dep_{invoice['invoice_id']}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
        ])
        await message.answer(f"Счёт на {amount} USDT", reply_markup=kb)
    except:
        await message.answer("Введите число")
    await state.clear()

deposit_pending = {}

@dp.callback_query(F.data.startswith("check_dep_"))
async def check_dep_payment(callback: types.CallbackQuery):
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in deposit_pending:
            amount = deposit_pending[callback.from_user.id]["amount"]
            await update_balance(callback.from_user.id, amount)
            await send_discord_log("💎 Пополнение", f"Пользователь: {callback.from_user.id}\nСумма: {amount}$", 0x00ff00)
            del deposit_pending[callback.from_user.id]
            await callback.message.edit_text(f"✅ Пополнение на {amount}$ успешно!", reply_markup=main_menu(callback.from_user.id))
        else:
            await callback.message.edit_text("Ошибка", reply_markup=main_menu(callback.from_user.id))
    elif status == "pending":
        await callback.answer("⏳ Платёж не обработан", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 Сумма вывода (мин 1$):")
    await state.set_state(Withdraw.waiting_amount)
    await callback.answer()

@dp.message(Withdraw.waiting_amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 1:
            await message.answer("❌ Минимальная сумма вывода 1$")
            return
        balance = await get_balance(message.from_user.id)
        if amount > balance:
            await message.answer(f"❌ Не хватает. Баланс: {balance:.2f}$")
            return
        await state.update_data(amount=amount)
        await message.answer("💳 Адрес кошелька USDT TRC20:")
        await state.set_state(Withdraw.waiting_wallet)
    except:
        await message.answer("❌ Введите число")

@dp.message(Withdraw.waiting_wallet)
async def withdraw_wallet(message: types.Message, state: FSMContext):
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data["amount"]
    await add_withdraw_request(message.from_user.id, amount, wallet)
    await message.answer(f"✅ Заявка на вывод {amount}$ создана", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(ADMIN_ID, f"📥 Заявка от {message.from_user.id}\nСумма: {amount}$\nКошелёк: {wallet}")
    await send_discord_log("💰 Заявка на вывод", f"Пользователь: {message.from_user.id}\nСумма: {amount}$", 0xffa500)
    await state.clear()

# ------------------- АДМИН-ПАНЕЛЬ -------------------
@dp.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("👑 Админ-панель", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_give_sub")
async def admin_give_sub_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Введите ID пользователя:")
    await state.set_state(AdminGiveSubscription.waiting_user_id)
    await callback.answer()

@dp.message(AdminGiveSubscription.waiting_user_id)
async def admin_give_sub_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(user_id=user_id)
        await message.answer("Введите количество ДНЕЙ подписки:")
        await state.set_state(AdminGiveSubscription.waiting_days)
    except:
        await message.answer("❌ Введите число (ID)")

@dp.message(AdminGiveSubscription.waiting_days)
async def admin_give_sub_days(message: types.Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
        data = await state.get_data()
        user_id = data["user_id"]
        await set_subscription(user_id, days)
        await message.answer(f"✅ Пользователю {user_id} выдана подписка на {days} дней.")
        await bot.send_message(user_id, f"🎁 Администратор выдал подписку на {days} дней!")
        await state.clear()
    except:
        await message.answer("❌ Введите положительное число")

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Введите ID пользователя:")
    await state.set_state(AdminAddBalance.waiting_user_id)
    await callback.answer()

@dp.message(AdminAddBalance.waiting_user_id)
async def admin_add_balance_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(user_id=user_id)
        await message.answer("Введите сумму пополнения (в долларах):")
        await state.set_state(AdminAddBalance.waiting_amount)
    except:
        await message.answer("❌ Введите число (ID)")

@dp.message(AdminAddBalance.waiting_amount)
async def admin_add_balance_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        user_id = data["user_id"]
        await update_balance(user_id, amount)
        await message.answer(f"✅ Пользователю {user_id} начислено {amount}$")
        await bot.send_message(user_id, f"💰 Вам начислено {amount}$")
        await state.clear()
    except:
        await message.answer("❌ Введите положительное число")

@dp.callback_query(F.data == "admin_remove_balance")
async def admin_remove_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Введите ID пользователя:")
    await state.set_state(AdminRemoveBalance.waiting_user_id)
    await callback.answer()

@dp.message(AdminRemoveBalance.waiting_user_id)
async def admin_remove_balance_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(user_id=user_id)
        await message.answer("Введите сумму списания (в долларах):")
        await state.set_state(AdminRemoveBalance.waiting_amount)
    except:
        await message.answer("❌ Введите число (ID)")

@dp.message(AdminRemoveBalance.waiting_amount)
async def admin_remove_balance_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        user_id = data["user_id"]
        current = await get_balance(user_id)
        if current < amount:
            await message.answer(f"❌ Недостаточно. Баланс: {current}$")
            return
        await update_balance(user_id, -amount)
        await message.answer(f"✅ У пользователя {user_id} списано {amount}$")
        await bot.send_message(user_id, f"⚠️ Списано {amount}$")
        await state.clear()
    except:
        await message.answer("❌ Введите положительное число")

@dp.callback_query(F.data == "admin_withdraws")
async def admin_withdraws(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    reqs = await get_pending_withdraws()
    if not reqs:
        await callback.message.edit_text("Нет заявок", reply_markup=back_button("admin_panel"))
        return
    text = "💰 Заявки на вывод:\n\n"
    for r in reqs:
        text += f"#{r[0]} | Пользователь: {r[1]} | {r[2]}$ | {r[3]}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data="withdraw_approve"), InlineKeyboardButton(text="❌ Отклонить", callback_data="withdraw_reject")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "withdraw_approve")
async def withdraw_approve(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    reqs = await get_pending_withdraws()
    if not reqs:
        await callback.answer("Нет заявок", show_alert=True)
        return
    req_id, user_id, amount, wallet = reqs[0]
    await update_withdraw_status(req_id, "approved")
    await bot.send_message(user_id, f"✅ Ваша заявка на вывод {amount}$ одобрена.")
    await callback.answer("Одобрено", show_alert=True)
    await admin_withdraws(callback)

@dp.callback_query(F.data == "withdraw_reject")
async def withdraw_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    reqs = await get_pending_withdraws()
    if not reqs:
        await callback.answer("Нет заявок", show_alert=True)
        return
    req_id, user_id, amount, wallet = reqs[0]
    await update_withdraw_status(req_id, "rejected")
    await update_balance(user_id, amount)
    await bot.send_message(user_id, f"❌ Заявка на вывод {amount}$ отклонена. Средства возвращены.")
    await callback.answer("Отклонено", show_alert=True)
    await admin_withdraws(callback)

@dp.callback_query(F.data == "admin_promocodes")
async def admin_promocodes_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_promos")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text("🎫 Промокоды", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_create_promo")
async def create_promo_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Введите код промокода:")
    await state.set_state(AdminCreatePromocode.waiting_code)
    await callback.answer()

@dp.message(AdminCreatePromocode.waiting_code)
async def create_promo_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    await state.update_data(code=code)
    await message.answer("Введите количество дней:")
    await state.set_state(AdminCreatePromocode.waiting_days)

@dp.message(AdminCreatePromocode.waiting_days)
async def create_promo_days(message: types.Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
        await state.update_data(days=days)
        await message.answer("Макс. использований:")
        await state.set_state(AdminCreatePromocode.waiting_max_uses)
    except:
        await message.answer("Введите число >0")

@dp.message(AdminCreatePromocode.waiting_max_uses)
async def create_promo_max_uses(message: types.Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 1:
            raise ValueError
        data = await state.get_data()
        await create_promocode(data["code"], data["days"], max_uses)
        await message.answer(f"✅ Промокод {data['code']} создан!")
        await state.clear()
    except:
        await message.answer("Введите число от 1")

@dp.callback_query(F.data == "admin_list_promos")
async def list_promocodes_admin(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    promos = await get_all_promocodes()
    if not promos:
        await callback.message.edit_text("Нет промокодов.", reply_markup=back_button("admin_promocodes"))
        return
    text = "Список промокодов:\n"
    for p in promos:
        text += f"🔹 {p['code']} – {p['days']} дней, {p['uses']}/{p['max_uses']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_delete_promo")], [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_promocodes")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_delete_promo")
async def delete_promo_start(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    promos = await get_all_promocodes()
    if not promos:
        await callback.answer("Нет промокодов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"❌ {p['code']}", callback_data=f"del_promo_{p['id']}")] for p in promos] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_promocodes")]])
    await callback.message.edit_text("Выберите промокод для удаления:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_promo_"))
async def delete_promo_exec(callback: types.CallbackQuery):
    promo_id = int(callback.data.split("_")[2])
    await delete_promocode(promo_id)
    await callback.answer("Удалён", show_alert=True)
    await admin_promocodes_menu(callback)

@dp.callback_query(F.data == "activate_promo")
async def activate_promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите промокод:")
    await state.set_state(ActivatePromo.waiting_code)
    await callback.answer()

@dp.message(ActivatePromo.waiting_code)
async def activate_promo_exec(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    promo = await get_promocode(code)
    user_id = message.from_user.id
    if not promo:
        await message.answer("❌ Неверный промокод")
        await state.clear()
        return
    async with db_pool.acquire() as conn:
        used = await conn.fetchval("SELECT 1 FROM used_promocodes WHERE user_id=$1 AND code_id=$2", user_id, promo["id"])
    if used:
        await message.answer("❌ Вы уже активировали этот промокод")
        await state.clear()
        return
    if promo["uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод больше не действителен")
        await state.clear()
        return
    await use_promocode(user_id, promo["id"])
    await set_subscription(user_id, promo["days"])
    await message.answer(f"✅ Активирован! +{promo['days']} дней подписки.")
    await bot.send_message(ADMIN_ID, f"🎫 {user_id} активировал {code}")
    await send_discord_log("🎫 Промокод", f"Пользователь: {user_id}\nКод: {code}", 0x00aaFF)
    await state.clear()

# ------------------- РАСШИРЕННАЯ СТАТИСТИКА -------------------
async def get_registration_stats():
    now = int(time.time())
    day_ago = now - 86400
    week_ago = now - 7*86400
    month_ago = now - 30*86400
    async with db_pool.acquire() as conn:
        day = await conn.fetchval("SELECT COUNT(*) FROM users WHERE registered_at >= $1", day_ago)
        week = await conn.fetchval("SELECT COUNT(*) FROM users WHERE registered_at >= $1", week_ago)
        month = await conn.fetchval("SELECT COUNT(*) FROM users WHERE registered_at >= $1", month_ago)
        return day, week, month

async def get_balance_stats():
    async with db_pool.acquire() as conn:
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        avg_balance = await conn.fetchval("SELECT COALESCE(AVG(balance),0) FROM users")
        positive_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE balance > 0")
        return total_balance, avg_balance, positive_count

async def get_withdraw_stats():
    async with db_pool.acquire() as conn:
        approved = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE status='approved'")
        pending = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE status='pending'")
        return approved, pending

async def get_promocode_stats():
    async with db_pool.acquire() as conn:
        total_used = await conn.fetchval("SELECT COUNT(*) FROM used_promocodes")
        avg_days = await conn.fetchval("SELECT AVG(p.days) FROM used_promocodes u JOIN promocodes p ON u.code_id = p.id")
        return total_used, avg_days or 0

async def get_top_users_by_balance(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id, username, balance FROM users ORDER BY balance DESC LIMIT $1", limit)
        return rows

async def get_top_subscriptions(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id, username, sub_until FROM users WHERE sub_until > $1 ORDER BY sub_until DESC LIMIT $2", int(time.time()), limit)
        return rows

async def get_all_users_for_csv():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id, username, to_timestamp(registered_at) as registered, balance, to_timestamp(sub_until) as sub_until FROM users ORDER BY tg_id")
        return rows

@dp.callback_query(F.data == "admin_ext_stats")
async def admin_extended_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    total_users = len(await get_all_users())
    active_subs = sum(1 for u in await get_all_users() if u["sub_until"] > int(time.time()))
    day_reg, week_reg, month_reg = await get_registration_stats()
    total_bal, avg_bal, pos_bal = await get_balance_stats()
    approved_withdraw, pending_withdraw = await get_withdraw_stats()
    promo_used, avg_promo_days = await get_promocode_stats()
    async with db_pool.acquire() as conn:
        tg_total = await conn.fetchval("SELECT COUNT(*) FROM tg_accounts")
        tg_active = await conn.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE is_active=True")
        vk_total = await conn.fetchval("SELECT COUNT(*) FROM vk_accounts")
        vk_active = await conn.fetchval("SELECT COUNT(*) FROM vk_accounts WHERE is_active=True")
    text = (
        f"📊 **Расширенная статистика бота**\n\n"
        f"👥 **Пользователи:**\n"
        f"• Всего: {total_users}\n"
        f"• Активных подписок: {active_subs}\n"
        f"• Зарегистрировались за 24ч: {day_reg}\n"
        f"• За 7 дней: {week_reg}\n"
        f"• За 30 дней: {month_reg}\n\n"
        f"💰 **Балансы:**\n"
        f"• Общая сумма: {total_bal:.2f}$\n"
        f"• Средний баланс: {avg_bal:.2f}$\n"
        f"• С положительным балансом: {pos_bal}\n\n"
        f"💸 **Выводы:**\n"
        f"• Выплачено всего: {approved_withdraw:.2f}$\n"
        f"• Ожидает выплаты: {pending_withdraw:.2f}$\n\n"
        f"🎫 **Промокоды:**\n"
        f"• Активаций: {promo_used}\n"
        f"• Средняя длительность: {avg_promo_days:.1f} дней\n\n"
        f"📱 **Telegram аккаунты:**\n"
        f"• Всего: {tg_total}, активных: {tg_active}\n\n"
        f"📘 **VK аккаунты:**\n"
        f"• Всего: {vk_total}, активных: {vk_active}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Топ по балансу", callback_data="admin_top_balance"),
         InlineKeyboardButton(text="⏳ Топ подписок", callback_data="admin_top_sub")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_top_balance")
async def admin_top_balance(callback: types.CallbackQuery):
    top = await get_top_users_by_balance(10)
    if not top:
        text = "Нет пользователей с балансом > 0"
    else:
        text = "🏆 **Топ-10 по балансу**\n\n"
        for i, u in enumerate(top, 1):
            text += f"{i}. {u['username'] or u['tg_id']} — {u['balance']:.2f}$\n"
    await callback.message.edit_text(text, reply_markup=back_button("admin_ext_stats"))
    await callback.answer()

@dp.callback_query(F.data == "admin_top_sub")
async def admin_top_subscription(callback: types.CallbackQuery):
    top = await get_top_subscriptions(10)
    if not top:
        text = "Нет активных подписок"
    else:
        text = "⏳ **Топ-10 по длительности подписки**\n\n"
        for i, u in enumerate(top, 1):
            until = datetime.fromtimestamp(u["sub_until"]).strftime('%d.%m.%Y')
            text += f"{i}. {u['username'] or u['tg_id']} — до {until}\n"
    await callback.message.edit_text(text, reply_markup=back_button("admin_ext_stats"))
    await callback.answer()

@dp.callback_query(F.data == "admin_export_csv")
async def admin_export_csv(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    rows = await get_all_users_for_csv()
    if not rows:
        await callback.answer("Нет данных", show_alert=True)
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["tg_id", "username", "registered_at", "balance", "subscription_until"])
    for r in rows:
        writer.writerow([r["tg_id"], r["username"], r["registered"], r["balance"], r["sub_until"]])
    csv_content = output.getvalue().encode('utf-8')
    await callback.message.answer_document(BufferedInputFile(csv_content, filename="users_export.csv"), caption="📥 Экспорт пользователей")
    await callback.answer()

@dp.callback_query(F.data == "admin_balance_manage")
async def admin_balance_manage(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать баланс", callback_data="admin_add_balance"),
         InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin_remove_balance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text("Управление балансами пользователей", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast_stats")
async def admin_broadcast_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        total_broadcasts = await conn.fetchval("SELECT COUNT(*) FROM broadcast_logs")
        total_contacts = await conn.fetchval("SELECT COALESCE(SUM(total_contacts),0) FROM broadcast_logs")
        total_sent = await conn.fetchval("SELECT COALESCE(SUM(sent),0) FROM broadcast_logs")
        total_friends = await conn.fetchval("SELECT COALESCE(SUM(friends_count),0) FROM broadcast_logs WHERE account_type='vk'")
        total_chats = await conn.fetchval("SELECT COALESCE(SUM(chats_count),0) FROM broadcast_logs WHERE account_type='vk'")
        text = (f"📊 **Статистика всех рассылок**\n\n"
                f"📨 Всего запусков: {total_broadcasts}\n"
                f"👥 Всего контактов обработано: {total_contacts}\n"
                f"✅ Отправлено сообщений: {total_sent}\n"
                f"📘 Из них:\n"
                f"   • Друзей VK: {total_friends}\n"
                f"   • Бесед VK: {total_chats}\n")
        top_users = await conn.fetch("SELECT user_id, SUM(total_contacts) as total FROM broadcast_logs GROUP BY user_id ORDER BY total DESC LIMIT 5")
        if top_users:
            text += "\n🏆 **Топ пользователей по проливу:**\n"
            for u in top_users:
                text += f"• ID {u['user_id']} — {u['total']} контактов\n"
    await callback.message.edit_text(text, reply_markup=back_button("admin_panel"))
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(AdminBroadcast.waiting_text)
    await callback.answer()

@dp.message(AdminBroadcast.waiting_text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(text=message.text)
    await message.answer("Подтвердите (да/нет):")
    await state.set_state(AdminBroadcast.waiting_confirm)

@dp.message(AdminBroadcast.waiting_confirm)
async def admin_broadcast_confirm(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text.lower() != "да":
        await message.answer("Отменено")
        await state.clear()
        return
    data = await state.get_data()
    text = data.get("text", "")
    users = await get_all_users()
    total = len(users)
    sent = 0
    await message.answer(f"Начинаю рассылку {total} пользователям...")
    for u in users:
        try:
            await bot.send_message(u["tg_id"], text)
            sent += 1
        except:
            pass
        await asyncio.sleep(0.5)
    await message.answer(f"✅ Отправлено {sent} из {total}")
    await send_discord_log("📢 Глобальная рассылка", f"Отправлено {sent} из {total}", 0x9b59b6)
    await state.clear()

# ------------------- ПОМОЩЬ И ПОДДЕРЖКА -------------------
@dp.callback_query(F.data == "help")
async def help_menu(callback: types.CallbackQuery):
    text = (
        "❓ *ПОМОЩЬ И ИНСТРУКЦИЯ*\n\n"
        "┌─────────────────────────────────┐\n"
        "│  💰 *БАЛАНС*                       │\n"
        "│  Пополнение через CryptoBot      │\n"
        "│  (USDT). Вывод от 1$ на кошелёк. │\n"
        "├─────────────────────────────────┤\n"
        "│  📢 *РАССЫЛКИ*                    │\n"
        "│  Добавь свои Telegram/VK аккаунты,│\n"
        "│  пиши сообщения, настраивай задержку.│\n"
        "├─────────────────────────────────┤\n"
        "│  💎 *ПОДПИСКА*                    │\n"
        "│  Даёт доступ к рассылкам.        │\n"
        "├─────────────────────────────────┤\n"
        "│  🔧 *ТЕХПОДДЕРЖКА*                │\n"
        "│  @bloodworn                      │\n"
        "└─────────────────────────────────┘\n\n"
        "✨ *Удачи в использовании!*"
    )
    await callback.message.edit_text(text, reply_markup=back_button("main_menu"), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("✍️ **Напишите ваш вопрос.**\nАдминистратор ответит в этом чате.")
    await state.set_state(Support.waiting_question)
    await callback.answer()

@dp.message(Support.waiting_question)
async def support_send_question(message: types.Message, state: FSMContext):
    async with db_pool.acquire() as conn:
        ticket_id = await conn.fetchval("INSERT INTO support_tickets (user_id, created_at) VALUES ($1, $2) RETURNING id", message.from_user.id, int(time.time()))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 ОТВЕТИТЬ", callback_data=f"reply_ticket_{ticket_id}_{message.from_user.id}")]])
    await bot.send_message(ADMIN_ID, f"🆕 **Новый тикет #{ticket_id}**\nОт: {message.from_user.id}\n\n{message.text}", reply_markup=kb)
    await message.answer(f"✅ Ваше сообщение отправлено администратору (Тикет #{ticket_id}).\nОжидайте ответа.")
    await state.clear()

@dp.callback_query(F.data.startswith("reply_ticket_"))
async def reply_ticket_start(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    ticket_id = int(parts[2])
    user_id = int(parts[3])
    await state.update_data(ticket_id=ticket_id, user_id=user_id)
    await callback.message.answer("✍️ Введите ответ пользователю:")
    await state.set_state(Support.waiting_reply)
    await callback.answer()

@dp.message(Support.waiting_reply, F.chat.id == ADMIN_ID)
async def reply_ticket_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    user_id = data.get("user_id")
    if not user_id:
        await message.answer("Ошибка: не найден пользователь.")
        await state.clear()
        return
    await bot.send_message(user_id, f"🛎️ **Ответ администратора по тикету #{ticket_id}:**\n\n{message.text}")
    await message.answer("✅ Ответ отправлен пользователю.")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE support_tickets SET status='closed', closed_at=$1 WHERE id=$2", int(time.time()), ticket_id)
    await state.clear()

# ------------------- ЗАПУСК -------------------
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())