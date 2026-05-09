import asyncio
import time
import os
import re
from datetime import datetime
import random
import logging
import asyncpg

# Где-то в начале файла (после импортов)
SUCCESS_GIF_URL = "https://i.gifer.com/LRP3.gif"
ERROR_GIF_URL = "https://i.gifer.com/84OP.gif"

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
import aiohttp
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)

# ========== КОНФИГ (переменные окружения) ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID, API_HASH must be set")

SESSIONS_DIR = "/app/sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

TARIFFS = {
    "day": {"days": 1, "price": 2.5, "name": "1 день"},
    "week": {"days": 7, "price": 9, "name": "1 неделя"},
    "month": {"days": 30, "price": 15, "name": "1 месяц"}
}

SAFETY_CONFIG = {
    "vk": {
        "min_delay": 2,          # минимальная задержка (сек)
        "max_delay": 5,          # максимальная задержка (сек) - случайная
        "messages_per_hour": 60, # не более 60 сообщений в час
        "messages_per_day": 500, # не более 500 сообщений в день
        "max_recipients": 200,   # максимум получателей за одну рассылку
        "pause_after_batch": 300 # пауза 5 минут после каждых 50 сообщений
    },
    "tg": {
        "min_delay": 3,
        "max_delay": 7,
        "messages_per_hour": 50,
        "messages_per_day": 400,
        "max_recipients": 150,
        "pause_after_batch": 600
    }
}

db_pool = None

# Глобальный словарь для игр (обход FSM). Здесь будут храниться данные для куба, баскетбола и т.д.
user_games = {}

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), command_timeout=60)
    async with db_pool.acquire() as conn:
        # существующие таблицы...
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                tg_id BIGINT PRIMARY KEY,
                username TEXT,
                sub_until BIGINT DEFAULT 0,
                balance REAL DEFAULT 0
            )
        ''')
        # Добавляем колонку registered_at, если её ещё нет
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS registered_at BIGINT DEFAULT 0')
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
            CREATE TABLE IF NOT EXISTS vk_templates (
                id SERIAL PRIMARY KEY,
                owner_tg_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at BIGINT DEFAULT 0
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_type TEXT,
                account_id INTEGER,
                total_contacts INTEGER,
                friends_count INTEGER,
                chats_count INTEGER,
                sent_count INTEGER,
                start_time BIGINT,
                end_time BIGINT,
                status TEXT
            )
        ''')

        # В init_db() добавьте:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS send_limits (
                id SERIAL PRIMARY KEY,
                account_type TEXT,
                account_id INTEGER,
                hour_start BIGINT,
                hour_count INTEGER,
                day_start BIGINT,
                day_count INTEGER
            )
        ''')
        # Добавляем колонку registered_at, если её нет
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS registered_at BIGINT DEFAULT 0')
    print("✅ PostgreSQL ready")

# ----- Функции работы с пользователями -----
async def get_user(tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT tg_id, username, sub_until, balance FROM users WHERE tg_id=$1", tg_id)
        if row:
            return {"tg_id": row["tg_id"], "username": row["username"], "sub_until": row["sub_until"] or 0, "balance": row["balance"]}
    return None

async def create_user(tg_id: int, username: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (tg_id, username, sub_until, balance, registered_at) 
            VALUES ($1, $2, 0, 0, $3) 
            ON CONFLICT (tg_id) DO NOTHING
        """, tg_id, username, int(time.time()))

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

# ----- Telegram аккаунты -----
async def add_tg_account(owner_tg_id: int, phone: str, session_file: str, name: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO tg_accounts (owner_tg_id, phone, session_file, name, last_used) VALUES ($1,$2,$3,$4,$5)",
                           owner_tg_id, phone, session_file, name, int(time.time()))

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

async def deactivate_tg_account(owner_tg_id: int, account_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tg_accounts SET is_active=FALSE WHERE id=$1 AND owner_tg_id=$2", account_id, owner_tg_id)

# ----- VK аккаунты -----
async def add_vk_account(owner_tg_id: int, token: str, vk_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO vk_accounts (owner_tg_id, token, vk_name, is_active) VALUES ($1,$2,$3,TRUE)", owner_tg_id, token, vk_name)

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

# ----- Админские функции -----
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

# ----- Промокоды -----
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

# ----- CryptoBot -----
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

# ----- Обработка ошибок сессий -----
async def handle_session_error(user_id: int, account_id: int, phone: str):
    await deactivate_tg_account(user_id, account_id)
    await bot.send_message(user_id, f"❌ Аккаунт {phone} был автоматически деактивирован из-за слетевшей сессии.")

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

# ========== КЛАВИАТУРЫ ==========
# ========== ПРЕМИУМ-КЛАВИАТУРЫ ==========

# ========== ПРЕМИУМ-КЛАВИАТУРЫ ==========

def main_menu(tg_id: int):
    """Главное меню — золотой стиль"""
    kb = [
        [
            InlineKeyboardButton(text="🎲 ИГРАТЬ", callback_data="game_menu"),
            InlineKeyboardButton(text="👤 КАБИНЕТ", callback_data="profile")
        ],
        [
            InlineKeyboardButton(text="🔧 АККАУНТЫ", callback_data="my_accounts"),
            InlineKeyboardButton(text="❓ ПОМОЩЬ", callback_data="help")
        ],
    ]
    if tg_id == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="⚙️ АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def game_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 КУБ   |  x2–x6", callback_data="game_cube")],
        [InlineKeyboardButton(text="🏀 БАСКЕТБОЛ | x1.5–x7", callback_data="game_basketball")],
        [InlineKeyboardButton(text="🎯 ДАРТС   |  x5–x10", callback_data="game_darts")],
        [InlineKeyboardButton(text="⚽ ФУТБОЛ  |  x1.5–x8", callback_data="game_football")],
        [InlineKeyboardButton(text="🏠 НА ГЛАВНУЮ", callback_data="main_menu")]
    ])

def cube_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 МЕНЬШЕ / БОЛЬШЕ (1-3 / 4-6)     x2", callback_data="mode:less_more")],
        [InlineKeyboardButton(text="🔢 ЧЁТ / НЕЧЕТ                        x2", callback_data="mode:even_odd")],
        [InlineKeyboardButton(text="🎯 УГАДАЙ ЧИСЛО (от 1 до 6)           x6", callback_data="mode:exact")],
        [InlineKeyboardButton(text="🎲 ДИАПАЗОН (например 2-4)            x? ", callback_data="mode:range")],
        [InlineKeyboardButton(text="⚖️ БОЛЬШЕ 3.5 / МЕНЬШЕ 3.5            x2", callback_data="mode:35")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="game_menu")]
    ])

def after_game_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 ИГРАТЬ ЕЩЁ", callback_data="again"),
            InlineKeyboardButton(text="💰 +1$", callback_data="inc_bet"),
            InlineKeyboardButton(text="💸 -1$", callback_data="dec_bet"),
            InlineKeyboardButton(text="🔥 ВА-БАНК", callback_data="all_in")
        ],
        [InlineKeyboardButton(text="🏠 В МЕНЮ", callback_data="game_menu")]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 ПОПОЛНИТЬ", callback_data="deposit"),
            InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw")
        ],
        [
            InlineKeyboardButton(text="🎁 ПРОМОКОД", callback_data="activate_promo"),
            InlineKeyboardButton(text="💎 ПОДПИСКА", callback_data="buy_sub")
        ],
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

# ========== FSM состояния ==========
class AddTG(StatesGroup): waiting_phone = State(); waiting_code = State(); waiting_2fa = State()
class AddVK(StatesGroup): waiting_token = State()
class BroadcastTG(StatesGroup): waiting_text = State(); waiting_delay = State()
class BroadcastVK(StatesGroup): waiting_text = State(); waiting_delay = State()
class AdminAddBalance(StatesGroup): waiting_user_id = State(); waiting_amount = State()
class AdminRemoveBalance(StatesGroup): waiting_user_id = State(); waiting_amount = State()
class AdminGiveSubscription(StatesGroup): waiting_user_id = State(); waiting_days = State()
class Withdraw(StatesGroup): waiting_amount = State(); waiting_wallet = State()
class Deposit(StatesGroup): waiting_amount = State()
class GameBet(StatesGroup): waiting_bet = State()
class GameCube(StatesGroup): waiting_choice = State(); waiting_exact = State(); waiting_range = State()
class GameBasketball(StatesGroup): waiting_choice = State()
class GameDarts(StatesGroup): waiting_choice = State()
class GameFootball(StatesGroup): waiting_choice = State()
class ManageTG(StatesGroup): waiting_new_avatar = State(); waiting_cloud_password = State(); waiting_code_for_login = State(); waiting_new_name = State(); waiting_new_username = State()
class TGAction(StatesGroup): waiting_target = State(); waiting_message = State(); waiting_join_link = State(); waiting_photo = State(); waiting_file = State(); waiting_schedule_delay = State()
class AdminCreatePromocode(StatesGroup): waiting_code = State(); waiting_days = State(); waiting_max_uses = State()
class AdminBroadcast(StatesGroup): waiting_text = State(); waiting_photo = State(); waiting_confirm = State()
class ActivatePromo(StatesGroup): waiting_code = State()
class VKManage(StatesGroup): waiting_new_name = State(); waiting_new_status = State(); waiting_template_name = State(); waiting_template_content = State(); waiting_new_lastname = State(); waiting_new_avatar = State()
class VKTemplate(StatesGroup): waiting_name = State(); waiting_text = State(); waiting_select = State()
class BroadcastVKTarget(StatesGroup): waiting_target_choice = State()
class VKBroadcastState(StatesGroup): waiting_choice = State(); active = State()


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_last_game = {}
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

    # ГИФКА (первое сообщение)
    try:
        await message.answer_animation(
            animation="https://i.gifer.com/X63H.gif",
            caption="🎉 *ДОБРО ПОЖАЛОВАТЬ В Quasar!*",
            parse_mode="Markdown"
        )
    except:
        pass

    # ВТОРОЕ СООБЩЕНИЕ — только кнопки, без лишнего текста
    await message.answer(
        "👇 *Главное меню*",
        reply_markup=main_menu(message.from_user.id),
        parse_mode="Markdown"
    )

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
        "▫️ Пополните счёт, чтобы начать игру\n"
        "▫️ Активируйте промокод для бонусных дней\n"
        "▫️ Подписка откроет доступ к рассылкам"
    )
    await callback.message.edit_text(text, reply_markup=profile_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: types.CallbackQuery):
    await callback.message.edit_text("Управление аккаунтами", reply_markup=my_accounts_menu())
    await callback.answer()

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

# ========== УПРАВЛЕНИЕ АККАУНТОМ (аватар, пароль, имя, username) ==========
@dp.callback_query(F.data.startswith("tg_change_avatar_"))
async def tg_change_avatar_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Пришлите новое фото:")
    await state.set_state(ManageTG.waiting_new_avatar)
    await callback.answer()

@dp.message(ManageTG.waiting_new_avatar, F.photo)
async def tg_change_avatar_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await message.bot.download_file(file.file_path, file_path)
        await client(UploadProfilePhotoRequest(file=await client.upload_file(file_path)))
        await message.answer("✅ Аватарка изменена!")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_cloud_password_"))
async def tg_cloud_password_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("🔐 Введите облачный пароль:")
    await state.set_state(ManageTG.waiting_cloud_password)
    await callback.answer()

@dp.message(ManageTG.waiting_cloud_password)
async def tg_cloud_password_set(message: types.Message, state: FSMContext):
    password = message.text.strip()
    if not password:
        await message.answer("Пароль не может быть пустым")
        return
    data = await state.get_data()
    acc_id = data["acc_id"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client.edit_2fa(new_password=password)
        await message.answer("✅ 2FA пароль установлен!")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_request_code_"))
async def tg_request_code(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        if me:
            await callback.message.answer("✅ Аккаунт уже активен")
            return
    except (AuthKeyError, UnauthorizedError):
        try:
            await client.send_code_request(phone)
            await callback.message.answer(f"📲 Код отправлен на {phone}. Введите его:")
            await state.update_data(acc_id=acc_id, phone=phone, session_file=session_file)
            await state.set_state(ManageTG.waiting_code_for_login)
        except Exception as e:
            await callback.message.answer(f"❌ {get_russian_error(e)}")
    except Exception as e:
        await callback.message.answer(f"❌ {get_russian_error(e)}")
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
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_name_"))
async def tg_change_name_start(callback: types.CallbackQuery, state: FSMContext):
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
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateProfileRequest(first_name=new_name))
        await message.answer(f"✅ Имя изменено на {new_name}")
        await conn.execute("UPDATE tg_accounts SET name=$1 WHERE id=$2", new_name, acc_id)
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_change_username_"))
async def tg_change_username_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите username (без @):")
    await state.set_state(ManageTG.waiting_new_username)
    await callback.answer()

@dp.message(ManageTG.waiting_new_username)
async def tg_change_username(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    new_username = message.text.strip()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client(UpdateUsernameRequest(username=new_username))
        await message.answer(f"✅ Username изменён на @{new_username}")
    except (AuthKeyError, UnauthorizedError):
        await handle_session_error(message.from_user.id, acc_id, phone)
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

# ========== ОТПРАВКА СООБЩЕНИЙ, ФОТО, ДОКУМЕНТОВ, ОТЛОЖЕННАЯ ОТПРАВКА ==========
@dp.callback_query(F.data.startswith("tg_send_msg_"))
async def tg_send_msg_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_send_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    target = raw if raw.replace('-', '').isdigit() else raw
    await state.update_data(target=target)
    await message.answer("Введите текст сообщения:")
    await state.set_state(TGAction.waiting_message)

@dp.message(TGAction.waiting_message)
async def tg_send_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    text = message.text
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ Аккаунт не найден")
            await state.clear()
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        entity = await client.get_entity(target)
        await client.send_message(entity, text)
        await message.answer(f"✅ Сообщение отправлено в {target}")
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_send_photo_"))
async def tg_send_photo_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_photo_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    target = raw if raw.replace('-', '').isdigit() else raw
    await state.update_data(target=target)
    await message.answer("Пришлите фото (можно с подписью):")
    await state.set_state(TGAction.waiting_photo)

@dp.message(TGAction.waiting_photo, F.photo)
async def tg_send_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ Аккаунт не найден")
            await state.clear()
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        entity = await client.get_entity(target)
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f"/tmp/{photo.file_id}.jpg"
        await message.bot.download_file(file.file_path, file_path)
        caption = message.caption if message.caption else None
        await client.send_file(entity, file_path, caption=caption)
        await message.answer(f"✅ Фото отправлено в {target}")
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_send_doc_"))
async def tg_send_doc_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_doc_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    target = raw if raw.replace('-', '').isdigit() else raw
    await state.update_data(target=target)
    await message.answer("Пришлите документ (файл):")
    await state.set_state(TGAction.waiting_file)

@dp.message(TGAction.waiting_file, F.document)
async def tg_send_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = data["target"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ Аккаунт не найден")
            await state.clear()
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        entity = await client.get_entity(target)
        doc = message.document
        file = await message.bot.get_file(doc.file_id)
        file_path = f"/tmp/{doc.file_id}"
        await message.bot.download_file(file.file_path, file_path)
        caption = message.caption if message.caption else None
        await client.send_file(entity, file_path, caption=caption)
        await message.answer(f"✅ Документ отправлен в {target}")
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_schedule_"))
async def tg_schedule_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username получателя:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_schedule_target(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    target = raw if raw.replace('-', '').isdigit() else raw
    await state.update_data(target=target)
    await message.answer("Введите текст сообщения:")
    await state.set_state(TGAction.waiting_message)

@dp.message(TGAction.waiting_message)
async def tg_schedule_text(message: types.Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    await message.answer("Введите задержку (сек):")
    await state.set_state(TGAction.waiting_schedule_delay)

@dp.message(TGAction.waiting_schedule_delay)
async def tg_schedule_delay(message: types.Message, state: FSMContext):
    try:
        delay = int(message.text.strip())
        if delay <= 0: raise ValueError
        data = await state.get_data()
        acc_id = data["acc_id"]
        target = data["target"]
        text = data["message_text"]
        await message.answer(f"⏳ Отправлю через {delay} сек.")
        await asyncio.sleep(delay)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
            if not row:
                await message.answer("Аккаунт не найден")
                await state.clear()
                return
            session_file = row["session_file"]
        client = TelegramClient(session_file, API_ID, API_HASH)
        await client.connect()
        try:
            await client.get_me()
            entity = await client.get_entity(target)
            await client.send_message(entity, text)
            await message.answer(f"✅ Отправлено в {target}")
        except Exception as e:
            await message.answer(f"❌ {get_russian_error(e)}")
        finally:
            await client.disconnect()
    except:
        await message.answer("Введите число секунд")
    await state.clear()

@dp.callback_query(F.data.startswith("tg_dialogs_"))
async def tg_dialogs_start(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        dialogs = await client.get_dialogs()
        text = "📋 Диалоги:\n" + "\n".join([f"{i+1}. {d.name or d.entity.id}" for i, d in enumerate(dialogs[:20])])
        await callback.message.answer(text)
    except Exception as e:
        await callback.message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_terminate_"))
async def tg_terminate_sessions(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file, phone FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        session_file, phone = row["session_file"], row["phone"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        await client.log_out()
        await callback.message.answer("✅ Все сессии завершены. Аккаунт деактивирован.")
        await deactivate_tg_account(callback.from_user.id, acc_id)
    except Exception as e:
        await callback.message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_refresh_info_"))
async def tg_refresh_info(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        await conn.execute("UPDATE tg_accounts SET name=$1 WHERE id=$2", name, acc_id)
        await callback.message.answer(f"✅ Информация обновлена: {name}")
    except Exception as e:
        await callback.message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
    await callback.answer()

@dp.callback_query(F.data.startswith("tg_join_"))
async def tg_join_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ссылку или username:")
    await state.set_state(TGAction.waiting_join_link)
    await callback.answer()

@dp.message(TGAction.waiting_join_link)
async def tg_join_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    link = message.text.strip()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        if "joinchat" in link:
            hash_match = re.search(r'joinchat/([A-Za-z0-9_-]+)', link)
            if hash_match:
                await client(ImportChatInviteRequest(hash_match.group(1)))
            else:
                raise Exception("Неверная ссылка")
        else:
            entity = await client.get_entity(link)
            await client(JoinChannelRequest(entity))
        await message.answer(f"✅ Вступил в {link}")
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

@dp.callback_query(F.data.startswith("tg_leave_"))
async def tg_leave_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите ID или username чата:")
    await state.set_state(TGAction.waiting_target)
    await callback.answer()

@dp.message(TGAction.waiting_target)
async def tg_leave_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    target = message.text.strip()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT session_file FROM tg_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        session_file = row["session_file"]
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        entity = await client.get_entity(target)
        await client.delete_dialog(entity)
        await message.answer(f"✅ Вышел из {target}")
    except Exception as e:
        await message.answer(f"❌ {get_russian_error(e)}")
    finally:
        await client.disconnect()
        await state.clear()

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


SUCCESS_GIF_URL = "https://i.gifer.com/LRP3.gif"
ERROR_GIF_URL = "https://i.gifer.com/84OP.gif"

@dp.message(BroadcastTG.waiting_delay)
async def broadcast_tg_delay(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if not raw:
        await message.answer("❌ Введите задержку в секундах (число).")
        return
    try:
        delay = float(raw.replace(',', '.'))
        if delay < 1:
            delay = 1
    except ValueError:
        await message.answer("❌ Нужно число, например 5")
        return

    data = await state.get_data()
    text = data.get("text")
    acc_id = data.get("acc_id")

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
        me = await client.get_me()
        dialogs = await client.get_dialogs()
        targets = [d for d in dialogs if d.is_user]
        total = len(targets)
        if total == 0:
            await status_msg.edit_text("❌ Нет диалогов (пользователей) для рассылки.")
            return

        sent = 0
        errors = 0
        start_time = time.time()
        last_update = start_time

        async def update_progress():
            nonlocal sent, errors, total, start_time
            elapsed = time.time() - start_time
            processed = sent + errors
            percent = (processed / total) * 100 if total else 0
            speed = sent / elapsed * 60 if elapsed > 0 else 0
            remaining_sec = (total - processed) / (speed / 60) if speed > 0 else 0
            bar_len = 20
            filled = int(bar_len * processed / total) if total else 0
            bar = "🟩" * filled + "⬜" * (bar_len - filled)
            text_status = (
                f"📲 *Аккаунт {acc_name} загружен!*\n\n"
                f"👤 Имя: {acc_name}\n"
                f"🤙 Телефон: {phone}\n"
                f"📂 Всего чатов: {total} шт.\n"
                f"   ┣ Диалоги: {len(dialogs)}\n"
                f"   ┗ Контакты: {total}\n"
                f"🔄 Прогресс — {percent:.1f}%\n"
                f"{bar}\n"
                f"⏲️ Осталось {remaining_sec:.1f} с."
            )
            await status_msg.edit_text(text_status, parse_mode="Markdown")

        await update_progress()

        for dialog in targets:
            try:
                await client.send_message(dialog.entity, text)
                sent += 1
            except Exception as e:
                errors += 1
                logging.warning(f"Ошибка отправки {dialog.entity}: {e}")
            now = time.time()
            if now - last_update >= 5:
                await update_progress()
                last_update = now
            await asyncio.sleep(delay)

        elapsed_total = time.time() - start_time
        success_rate = (sent / (sent + errors)) * 100 if (sent + errors) > 0 else 0
        final_report = (
            f"📲 *Спам Telegram завершен!*\n\n"
            f"📝 Отправлено: {sent}/{total}\n"
            f"   ┣ ✅ Успешно: {sent}\n"
            f"   ┗ ❌ Ошибки: {errors}\n"
            f"❌ Успешность: {success_rate:.1f}%\n"
            f"⏲️ Время: {elapsed_total:.1f} сек."
        )
        await status_msg.edit_text(final_report, parse_mode="Markdown")

        # Отправляем гифку в зависимости от успешности
        gif_url = SUCCESS_GIF_URL if success_rate >= 80 else ERROR_GIF_URL
        try:
            await message.answer_animation(animation=gif_url, caption="🎉 *РАССЫЛКА ЗАВЕРШЕНА!* 🎉", parse_mode="Markdown")
        except Exception as gif_err:
            logging.warning(f"Не удалось отправить гифку: {gif_err}")

    except (AuthKeyError, UnauthorizedError):
        await deactivate_tg_account(message.from_user.id, acc_id)
        await status_msg.edit_text(f"❌ Аккаунт {phone} деактивирован (сессия устарела).")
        try:
            await message.answer_animation(animation=ERROR_GIF_URL, caption="⚠️ *АККАУНТ ДЕАКТИВИРОВАН*", parse_mode="Markdown")
        except:
            pass
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {get_russian_error(e)}")
        try:
            await message.answer_animation(animation=ERROR_GIF_URL, caption="❌ *ОШИБКА РАССЫЛКИ*", parse_mode="Markdown")
        except:
            pass
    finally:
        await client.disconnect()
        await state.clear()

# ========== VK АККАУНТЫ ==========
@dp.callback_query(F.data == "list_vk_accounts")
async def list_vk_accounts(callback: types.CallbackQuery):
    accounts = await get_user_vk_accounts(callback.from_user.id)
    if not accounts:
        text = "📭 *У вас нет VK аккаунтов.*\nНажмите ➕ ДОБАВИТЬ VK, чтобы подключить."
        await callback.message.edit_text(text, reply_markup=back_button("my_accounts"), parse_mode="Markdown")
        return
    text = "📘 *ВАШИ VK АККАУНТЫ*"
    await callback.message.edit_text(text, reply_markup=await vk_accounts_list(callback.from_user.id), parse_mode="Markdown")
    await callback.answer()

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
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"vk_stats_{acc_id}")],
        [InlineKeyboardButton(text="📨 Рассылка (обычная)", callback_data=f"vk_broadcast_{acc_id}")],
        [InlineKeyboardButton(text="📝 Рассылка по шаблону", callback_data=f"vk_broadcast_template_{acc_id}")],
        [InlineKeyboardButton(text="✏️ Сменить имя/фамилию", callback_data=f"vk_edit_name_{acc_id}")],
        [InlineKeyboardButton(text="🖼️ Сменить аватарку", callback_data=f"vk_edit_avatar_{acc_id}")],
        [InlineKeyboardButton(text="📝 Сменить статус", callback_data=f"vk_edit_status_{acc_id}")],
        [InlineKeyboardButton(text="👥 Список друзей (первые 10)", callback_data=f"vk_friends_{acc_id}")],
        [InlineKeyboardButton(text="💬 Мои беседы", callback_data=f"vk_convs_{acc_id}")],
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

@dp.callback_query(F.data.startswith("vk_broadcast_"))
async def vk_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Текст рассылки:")
    await state.set_state(BroadcastVK.waiting_text)
    await callback.answer()

@dp.message(BroadcastVK.waiting_text)
async def broadcast_vk_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("Задержка (сек):")
    await state.set_state(BroadcastVK.waiting_delay)

SUCCESS_GIF_URL = "https://i.gifer.com/LRP3.gif"
ERROR_GIF_URL = "https://i.gifer.com/84OP.gif"

@dp.message(BroadcastVK.waiting_delay)
async def broadcast_vk_delay(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if not raw:
        await message.answer("❌ Введите задержку в секундах.")
        return
    try:
        delay = float(raw.replace(',', '.'))
        if delay < 1:
            delay = 1
    except ValueError:
        await message.answer("❌ Нужно число, например 5")
        return

    data = await state.get_data()
    text = data.get("text")
    acc_id = data.get("acc_id")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token, vk_name FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("❌ VK аккаунт не найден")
            await state.clear()
            return
        token, vk_name = row["token"], row["vk_name"]

    status_msg = await message.answer(f"📲 *Аккаунт {vk_name} загружается...*", parse_mode="Markdown")
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    try:
        vk.users.get()
    except Exception as e:
        if "invalid token" in str(e).lower():
            await delete_vk_account(message.from_user.id, acc_id)
            await status_msg.edit_text(f"❌ Аккаунт {vk_name} заблокирован. Удалён.")
            try:
                await message.answer_animation(animation=ERROR_GIF_URL, caption="⚠️ *АККАУНТ УДАЛЁН*", parse_mode="Markdown")
            except:
                pass
            await state.clear()
            return
        else:
            await status_msg.edit_text(f"❌ Ошибка VK: {e}")
            await state.clear()
            return

    try:
        friends = vk.friends.get()["items"]
        convs = vk.messages.getConversations(count=200)["items"]
        total_friends = len(friends)
        total_chats = len(convs)
        targets = friends + [c["conversation"]["peer"]["id"] for c in convs]
        total = len(targets)
        if total == 0:
            await status_msg.edit_text("❌ Нет получателей (друзей или бесед).")
            return

        sent = 0
        errors = 0
        start_time = time.time()
        last_update = start_time

        async def update_progress():
            nonlocal sent, errors, total, start_time
            elapsed = time.time() - start_time
            processed = sent + errors
            percent = (processed / total) * 100 if total else 0
            speed = sent / elapsed * 60 if elapsed > 0 else 0
            remaining_sec = (total - processed) / (speed / 60) if speed > 0 else 0
            bar_len = 20
            filled = int(bar_len * processed / total) if total else 0
            bar = "🟩" * filled + "⬜" * (bar_len - filled)
            text_status = (
                f"📲 *Аккаунт {vk_name} загружен!*\n\n"
                f"👤 Имя: {vk_name}\n"
                f"📂 Всего получателей: {total}\n"
                f"   ┣ Друзей: {total_friends}\n"
                f"   ┗ Бесед: {total_chats}\n"
                f"🔄 Прогресс — {percent:.1f}%\n"
                f"{bar}\n"
                f"⏲️ Осталось {remaining_sec:.1f} с."
            )
            await status_msg.edit_text(text_status, parse_mode="Markdown")

        await update_progress()

        for target in targets:
            try:
                if isinstance(target, int):
                    vk.messages.send(user_id=target, message=text, random_id=0)
                else:
                    vk.messages.send(peer_id=target, message=text, random_id=0)
                sent += 1
            except Exception as e:
                errors += 1
                logging.warning(f"VK ошибка {target}: {e}")
            now = time.time()
            if now - last_update >= 5:
                await update_progress()
                last_update = now
            await asyncio.sleep(delay)

        elapsed_total = time.time() - start_time
        success_rate = (sent / (sent + errors)) * 100 if (sent + errors) > 0 else 0
        final_report = (
            f"📲 *Спам MAX завершен!*\n\n"
            f"📝 Отправлено: {sent}/{total}\n"
            f"   ┣ ✅ Успешно: {sent}\n"
            f"   ┗ ❌ Ошибки: {errors}\n"
            f"❌ Успешность: {success_rate:.1f}%\n"
            f"⏲️ Время: {elapsed_total:.1f} сек."
        )
        await status_msg.edit_text(final_report, parse_mode="Markdown")

        # Отправляем гифку
        gif_url = SUCCESS_GIF_URL if success_rate >= 80 else ERROR_GIF_URL
        try:
            await message.answer_animation(animation=gif_url, caption="🎉 *VK РАССЫЛКА ЗАВЕРШЕНА!* 🎉", parse_mode="Markdown")
        except:
            pass

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка VK рассылки: {e}")
        try:
            await message.answer_animation(animation=ERROR_GIF_URL, caption="❌ *ОШИБКА VK РАССЫЛКИ*", parse_mode="Markdown")
        except:
            pass
    finally:
        await state.clear()

# ========== ПОДКЛЮЧЕНИЕ НОВЫХ АККАУНТОВ ==========
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
        await add_tg_account(message.from_user.id, phone, data["session_file"], name)
        await message.answer(f"✅ Аккаунт {name} добавлен!")
        await client.disconnect()
        await state.clear()
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
        await add_tg_account(message.from_user.id, data["phone"], data["session_file"], name)
        await message.answer(f"✅ Аккаунт {name} добавлен (2FA)!")
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {get_russian_error(e)}")
        await state.clear()

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
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        user = vk.users.get()[0]
        name = f"{user['first_name']} {user['last_name']}"
        await add_vk_account(message.from_user.id, token, name)
        await message.answer(f"✅ VK аккаунт {name} добавлен!")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
        await state.clear()

# ========== ПОДПИСКА ==========
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
    user_id = callback.from_user.id
    balance = await get_balance(user_id)
    if balance >= tariff["price"]:
        await update_balance(user_id, -tariff["price"])
        await set_subscription(user_id, tariff["days"])
        await callback.message.edit_text(f"✅ Подписка на {tariff['name']} активирована!", reply_markup=main_menu(user_id))
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

# ========== ПОПОЛНЕНИЕ / ВЫВОД (мин 1$) ==========
deposit_pending = {}

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

@dp.callback_query(F.data.startswith("check_dep_"))
async def check_dep_payment(callback: types.CallbackQuery):
    invoice_id = callback.data.split("_")[2]
    status = await check_crypto_invoice(invoice_id)
    if status == "paid":
        if callback.from_user.id in deposit_pending:
            amount = deposit_pending[callback.from_user.id]["amount"]
            await update_balance(callback.from_user.id, amount)
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
    if message.chat.type != ChatType.PRIVATE:
        return
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
        await message.answer("❌ Введите число (сумму вывода)")

@dp.message(Withdraw.waiting_wallet)
async def withdraw_wallet(message: types.Message, state: FSMContext):
    wallet = message.text.strip()
    data = await state.get_data()
    amount = data["amount"]
    await add_withdraw_request(message.from_user.id, amount, wallet)
    await message.answer(f"✅ Заявка на вывод {amount}$ создана", reply_markup=main_menu(message.from_user.id))
    await bot.send_message(ADMIN_ID, f"📥 Заявка от {message.from_user.id}\nСумма: {amount}$\nКошелёк: {wallet}")
    await state.clear()# ========== ИГРЫ ==========
# Вспомогательная функция повторной игры
async def repeat_game(call_or_msg, state, new_bet=None):
    user_id = call_or_msg.from_user.id
    if user_id not in user_games or 'last_game_type' not in user_games[user_id]:
        await call_or_msg.answer("❌ Нет сохранённой игры. Начните сначала через меню.")
        return False
    game_type = user_games[user_id]['last_game_type']
    bet = new_bet if new_bet is not None else user_games[user_id].get('last_bet', 0)
    if bet < 0.1:
        await call_or_msg.answer("❌ Ставка не может быть меньше 0.1$")
        return False
    balance = await get_balance(user_id)
    if bet > balance:
        await call_or_msg.answer(f"❌ Не хватает средств. Баланс: {balance:.2f}$")
        return False
    user_games[user_id]['bet'] = bet
    if game_type == 'cube':
        mode = user_games[user_id].get('last_cube_mode')
        if not mode:
            await call_or_msg.answer("❌ Ошибка: режим куба не сохранён")
            return False
        if mode in ('less_more', 'even_odd', '35'):
            if mode == 'less_more':
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меньше (1-3)", callback_data=f"choice:{mode}:less"), InlineKeyboardButton(text="Больше (4-6)", callback_data=f"choice:{mode}:more")]])
            elif mode == 'even_odd':
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Чёт", callback_data=f"choice:{mode}:even"), InlineKeyboardButton(text="Нечет", callback_data=f"choice:{mode}:odd")]])
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Больше 3.5", callback_data=f"choice:{mode}:gt35"), InlineKeyboardButton(text="Меньше 3.5", callback_data=f"choice:{mode}:lt35")]])
            await call_or_msg.answer("Выберите вариант:", reply_markup=kb)
            await state.set_state(GameCube.waiting_choice)
            return True
        elif mode == 'exact':
            await call_or_msg.answer("Введите число от 1 до 6:")
            await state.set_state(GameCube.waiting_exact)
            return True
        elif mode == 'range':
            await call_or_msg.answer("Введите диапазон (например, 2-4):")
            await state.set_state(GameCube.waiting_range)
            return True
    elif game_type == 'basketball':
        mode = user_games[user_id].get('last_basketball_mode')
        if mode:
            await state.set_state(GameBasketball.waiting_choice)
            await game_basketball_choice_after_bet(call_or_msg, state, bet)
            return True
    elif game_type == 'darts':
        mode = user_games[user_id].get('last_darts_mode')
        if mode:
            await state.set_state(GameDarts.waiting_choice)
            await game_darts_choice_after_bet(call_or_msg, state, bet)
            return True
    elif game_type == 'football':
        mode = user_games[user_id].get('last_football_mode')
        if mode:
            await state.set_state(GameFootball.waiting_choice)
            await game_football_choice_after_bet(call_or_msg, state, bet)
            return True
    return False

@dp.callback_query(F.data == "game_menu")
async def game_menu_callback(callback: types.CallbackQuery):
    await callback.message.edit_text("🎲 Выберите игру:", reply_markup=game_menu())
    await callback.answer()

# -------- КУБ ----------
@dp.callback_query(F.data == "game_cube")
async def game_cube_menu(callback: types.CallbackQuery):
    await callback.message.edit_text("🎲 Режимы куба:", reply_markup=cube_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("mode:"))
async def cube_mode_selected(callback: types.CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    user_id = callback.from_user.id
    user_games[user_id] = {"type": "cube", "mode": mode, "bet": None}
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

# Общий обработчик ставки
@dp.message(GameBet.waiting_bet)
async def game_bet(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_games or user_games[user_id].get('mode') is None:
        await message.answer("❌ Сначала выберите игру через меню.")
        await state.clear()
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("❌ Минимальная ставка 0.1$")
            return
        balance = await get_balance(user_id)
        if bet > balance:
            await message.answer(f"❌ Не хватает. Баланс: {balance:.2f}$")
            return
        user_games[user_id]['bet'] = bet
        game_type = user_games[user_id]['type']
        mode = user_games[user_id]['mode']

        if game_type == 'cube':
            if mode in ('less_more', 'even_odd', '35'):
                if mode == 'less_more':
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меньше (1-3)", callback_data="choice:less_more:less"), InlineKeyboardButton(text="Больше (4-6)", callback_data="choice:less_more:more")]])
                elif mode == 'even_odd':
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Чёт", callback_data="choice:even_odd:even"), InlineKeyboardButton(text="Нечет", callback_data="choice:even_odd:odd")]])
                else:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Больше 3.5", callback_data="choice:35:gt35"), InlineKeyboardButton(text="Меньше 3.5", callback_data="choice:35:lt35")]])
                await message.answer("Выберите вариант:", reply_markup=kb)
                await state.set_state(GameCube.waiting_choice)
                return
            elif mode == 'exact':
                await message.answer("Введите число от 1 до 6:")
                await state.set_state(GameCube.waiting_exact)
                return
            elif mode == 'range':
                await message.answer("Введите диапазон (например, 2-4):")
                await state.set_state(GameCube.waiting_range)
                return
            else:
                await message.answer("❌ Неизвестный режим куба")
                await state.clear()
                return
        elif game_type == 'basketball':
            await state.set_state(GameBasketball.waiting_choice)
            await game_basketball_choice_after_bet(message, state)
        elif game_type == 'darts':
            await state.set_state(GameDarts.waiting_choice)
            await game_darts_choice_after_bet(message, state)
        elif game_type == 'football':
            await state.set_state(GameFootball.waiting_choice)
            await game_football_choice_after_bet(message, state)
    except:
        await message.answer("❌ Введите число")

# ---- Кубик: выбор в режимах (less/more, even/odd, 35) ----
@dp.callback_query(F.data.startswith("choice:"))
async def cube_choice_handler(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id not in user_games or user_games[user_id].get('bet') is None:
        await callback.answer("❌ Ошибка, начните сначала", show_alert=True)
        await state.clear()
        return
    bet = user_games[user_id]['bet']
    mode = user_games[user_id]['mode']
    choice_parts = callback.data.split(':')
    choice = choice_parts[2] if len(choice_parts) > 2 else choice_parts[1]

    msg = await callback.message.answer_dice(emoji="🎲")
    roll = msg.dice.value
    await asyncio.sleep(1.2)

    win = False
    if mode == 'less_more':
        win = (choice == 'less' and roll <= 3) or (choice == 'more' and roll >= 4)
    elif mode == 'even_odd':
        win = (choice == 'even' and roll % 2 == 0) or (choice == 'odd' and roll % 2 == 1)
    elif mode == '35':
        win = (choice == 'gt35' and roll > 3.5) or (choice == 'lt35' and roll < 3.5)

    if win:
        payout = bet * 2
        await update_balance(user_id, payout)
        new_balance = await get_balance(user_id)
        result = (
            "🎉 *┌────────────────────────────┐* 🎉\n"
            "   *│        ВЫ ВЫИГРАЛИ!        │*\n"
            f"   *│      СТАВКА : {bet:.2f}$          │*\n"
            f"   *│      ВЫИГРЫШ: {payout:.2f}$ ×2      │*\n"
            f"   *│      БАЛАНС : {new_balance:.2f}$      │*\n"
            "   *└────────────────────────────┘*\n"
            f"🎲 *Вам выпало число {roll}*\n"
            "✨ *Поздравляем! Сыграйте ещё!* ✨"
        )
    else:
        await update_balance(user_id, -bet)
        new_balance = await get_balance(user_id)
        result = (
            "💔 *┌────────────────────────────┐* 💔\n"
            "   *│         ПРОИГРЫШ :(         │*\n"
            f"   *│      СТАВКА : {bet:.2f}$          │*\n"
            f"   *│      ПОТЕРЯНО  : {bet:.2f}$          │*\n"
            f"   *│      БАЛАНС : {new_balance:.2f}$      │*\n"
            "   *└────────────────────────────┘*\n"
            f"🎲 *Выпало число {roll}*\n"
            "🔄 *Удача придёт в следующий раз!*"
        )

    # Сохраняем данные для кнопок "ещё раз"
    user_games[user_id]['last_game_type'] = 'cube'
    user_games[user_id]['last_bet'] = bet
    user_games[user_id]['last_cube_mode'] = mode

    await callback.message.answer(result, reply_markup=after_game_menu(), parse_mode="Markdown")
    await state.clear()
    await callback.answer()

# ---- Кубик: exact (угадать число) ----
@dp.message(GameCube.waiting_exact)
async def cube_exact_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_games or user_games[user_id].get('bet') is None:
        await message.answer("Ошибка, начните сначала")
        await state.clear()
        return
    try:
        num = int(message.text.strip())
        if num < 1 or num > 6:
            raise ValueError
        bet = user_games[user_id]['bet']
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if roll == num:
            payout = bet * 6
            await update_balance(user_id, payout)
            new_balance = await get_balance(user_id)
            result = f"🎲 Выпало {roll}\n✅ УГАДАЛ! Выигрыш: {bet}$ x6 = {payout:.2f}$\n💰 Баланс: {new_balance:.2f}$"
        else:
            await update_balance(user_id, -bet)
            new_balance = await get_balance(user_id)
            result = f"🎲 Выпало {roll}\n❌ НЕ УГАДАЛ. Проигрыш: {bet}$\n💰 Баланс: {new_balance:.2f}$"
        user_games[user_id]['last_game_type'] = 'cube'
        user_games[user_id]['last_bet'] = bet
        user_games[user_id]['last_cube_mode'] = 'exact'
        await message.answer(result, reply_markup=after_game_menu())
    except:
        await message.answer("❌ Введите число от 1 до 6")
        user_last_game[user_id] = {"bet": bet, "mode": "exact"}
    finally:
        await state.clear()

# ---- Кубик: диапазон ----
@dp.message(GameCube.waiting_range)
async def cube_range_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in user_games or user_games[user_id].get('bet') is None:
        await message.answer("Ошибка, начните сначала")
        await state.clear()
        return
    try:
        parts = message.text.strip().split('-')
        low = int(parts[0]); high = int(parts[1])
        if low < 1 or high > 6 or low > high:
            raise ValueError
        count = high - low + 1
        if count == 6:
            coeff = 1.0
        else:
            coeff = round(6.0 / count, 1)
            if coeff < 1.2:
                coeff = 1.2
        bet = user_games[user_id]['bet']
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if low <= roll <= high:
            payout = bet * coeff
            await update_balance(user_id, payout)
            new_balance = await get_balance(user_id)
            result = f"🎲 Выпало {roll}\n✅ ПОПАЛ! Коэф: {coeff}x, Выигрыш: {bet}$ x{coeff} = {payout:.2f}$\n💰 Баланс: {new_balance:.2f}$"
        else:
            await update_balance(user_id, -bet)
            new_balance = await get_balance(user_id)
            result = f"🎲 Выпало {roll}\n❌ МИМО. Проигрыш: {bet}$\n💰 Баланс: {new_balance:.2f}$"
        user_games[user_id]['last_game_type'] = 'cube'
        user_games[user_id]['last_bet'] = bet
        user_games[user_id]['last_cube_mode'] = 'range'
        await message.answer(result, reply_markup=after_game_menu())
    except:
        await message.answer("❌ Пример: 2-4")
        user_last_game[user_id] = {"bet": bet, "mode": "range"}
    finally:
        await state.clear()

# -------- БАСКЕТБОЛ ----------
@dp.callback_query(F.data == "game_basketball")
async def game_basketball_menu(callback: types.CallbackQuery):
    await callback.message.edit_text("🏀 Выберите исход:", reply_markup=basketball_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("basket_"))
async def game_basketball_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_games[user_id] = {"type": "basketball", "mode": callback.data, "bet": None}
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_basketball_choice_after_bet(message, state, custom_bet=None):
    user_id = message.from_user.id if hasattr(message, 'from_user') else message.chat.id
    if user_id not in user_games:
        await message.answer("Ошибка, начните заново")
        return
    bet = custom_bet if custom_bet is not None else user_games[user_id].get('bet')
    mode = user_games[user_id]['mode']
    msg = await message.answer_dice(emoji="🏀")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "basket_exact": (value == 5, 7, "🎯 Точное попадание"),
        "basket_ring": (value == 4, 3, "🏀 Попадание в кольцо"),
        "basket_miss": (value <= 2, 1.5, "❌ Мимо"),
        "basket_board": (value == 3, 2.5, "🛡️ Щит")
    }
    win, mult, name = outcomes.get(mode, (False, 1, "Неизвестно"))
    if win:
        payout = bet * mult
        await update_balance(user_id, payout)
        new_balance = await get_balance(user_id)
        result = f"🏀 {name}! Выигрыш: {bet}$ x{mult} = {payout:.2f}$\n💰 Баланс: {new_balance:.2f}$"
    else:
        await update_balance(user_id, -bet)
        new_balance = await get_balance(user_id)
        result = f"🏀 {name} не удалось. Проигрыш: {bet}$\n💰 Баланс: {new_balance:.2f}$"
    user_games[user_id]['last_game_type'] = 'basketball'
    user_games[user_id]['last_bet'] = bet
    user_games[user_id]['last_basketball_mode'] = mode
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# -------- ДАРТС ----------
@dp.callback_query(F.data == "game_darts")
async def game_darts_menu(callback: types.CallbackQuery):
    await callback.message.edit_text("🎯 Выберите исход:", reply_markup=darts_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("darts_"))
async def game_darts_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_games[user_id] = {"type": "darts", "mode": callback.data, "bet": None}
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_darts_choice_after_bet(message, state, custom_bet=None):
    user_id = message.from_user.id if hasattr(message, 'from_user') else message.chat.id
    if user_id not in user_games:
        await message.answer("Ошибка, начните заново")
        return
    bet = custom_bet if custom_bet is not None else user_games[user_id].get('bet')
    mode = user_games[user_id]['mode']
    msg = await message.answer_dice(emoji="🎯")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "darts_bullseye": (value == 6, 10, "🎯 Яблочко"),
        "darts_20": (value == 5, 5, "🎯 20")
    }
    win, mult, name = outcomes.get(mode, (False, 1, "Неизвестно"))
    if win:
        payout = bet * mult
        await update_balance(user_id, payout)
        new_balance = await get_balance(user_id)
        result = f"🎯 {name}! Выигрыш: {bet}$ x{mult} = {payout:.2f}$\n💰 Баланс: {new_balance:.2f}$"
    else:
        await update_balance(user_id, -bet)
        new_balance = await get_balance(user_id)
        result = f"🎯 {name} не выпало. Проигрыш: {bet}$\n💰 Баланс: {new_balance:.2f}$"
    user_games[user_id]['last_game_type'] = 'darts'
    user_games[user_id]['last_bet'] = bet
    user_games[user_id]['last_darts_mode'] = mode
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# -------- ФУТБОЛ ----------
@dp.callback_query(F.data == "game_football")
async def game_football_menu(callback: types.CallbackQuery):
    await callback.message.edit_text("⚽ Выберите исход:", reply_markup=football_menu())
    await callback.answer()

@dp.callback_query(F.data.startswith("foot_"))
async def game_football_start(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_games[user_id] = {"type": "football", "mode": callback.data, "bet": None}
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await state.set_state(GameBet.waiting_bet)
    await callback.answer()

async def game_football_choice_after_bet(message, state, custom_bet=None):
    user_id = message.from_user.id if hasattr(message, 'from_user') else message.chat.id
    if user_id not in user_games:
        await message.answer("Ошибка, начните заново")
        return
    bet = custom_bet if custom_bet is not None else user_games[user_id].get('bet')
    mode = user_games[user_id]['mode']
    msg = await message.answer_dice(emoji="⚽")
    value = msg.dice.value
    await asyncio.sleep(1)
    outcomes = {
        "foot_nine": (value == 5, 8, "⚽ Гол в девятку"),
        "foot_target": (value == 4, 3, "⚽ Гол в створ"),
        "foot_miss": (value <= 2, 1.5, "❌ Мимо"),
        "foot_post": (value == 3, 5, "🥅 Штанга/перекладина")
    }
    win, mult, name = outcomes.get(mode, (False, 1, "Неизвестно"))
    if win:
        payout = bet * mult
        await update_balance(user_id, payout)
        new_balance = await get_balance(user_id)
        result = f"⚽ {name}! Выигрыш: {bet}$ x{mult} = {payout:.2f}$\n💰 Баланс: {new_balance:.2f}$"
    else:
        await update_balance(user_id, -bet)
        new_balance = await get_balance(user_id)
        result = f"⚽ {name} не забит. Проигрыш: {bet}$\n💰 Баланс: {new_balance:.2f}$"
    user_games[user_id]['last_game_type'] = 'football'
    user_games[user_id]['last_bet'] = bet
    user_games[user_id]['last_football_mode'] = mode
    await message.answer(result, reply_markup=after_game_menu())
    await state.clear()

# -------- КНОПКИ ПОСЛЕ ИГРЫ ----------
@dp.callback_query(F.data == "again")
async def again_game(callback: types.CallbackQuery, state: FSMContext):
    await repeat_game(callback, state)

@dp.callback_query(F.data == "inc_bet")
async def inc_bet(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id not in user_games or 'last_bet' not in user_games[user_id]:
        await callback.answer("❌ Нет активной игры", show_alert=True)
        return
    old_bet = user_games[user_id]['last_bet']
    new_bet = old_bet + 1.0
    balance = await get_balance(user_id)
    if new_bet > balance:
        await callback.answer(f"❌ Не хватает. Баланс: {balance:.2f}$", show_alert=True)
        return
    await repeat_game(callback, state, new_bet)
    await callback.answer()

@dp.callback_query(F.data == "dec_bet")
async def dec_bet(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id not in user_games or 'last_bet' not in user_games[user_id]:
        await callback.answer("❌ Нет активной игры", show_alert=True)
        return
    old_bet = user_games[user_id]['last_bet']
    new_bet = old_bet - 1.0
    if new_bet < 0.1:
        new_bet = 0.1
    await repeat_game(callback, state, new_bet)
    await callback.answer()

@dp.callback_query(F.data == "all_in")
async def all_in(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    balance = await get_balance(user_id)
    if balance < 0.1:
        await callback.answer("❌ Баланс слишком мал для игры", show_alert=True)
        return
    await repeat_game(callback, state, balance)
    await callback.answer()# ========== АДМИН-ПАНЕЛЬ ==========
@dp.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("👑 Админ-панель", reply_markup=admin_menu())
    await callback.answer()

# --- Выдача подписки ---
@dp.callback_query(F.data == "admin_give_sub")
async def admin_give_sub_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
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
        if days <= 0: raise ValueError
        data = await state.get_data()
        user_id = data["user_id"]
        await set_subscription(user_id, days)
        await message.answer(f"✅ Пользователю {user_id} выдана подписка на {days} дней.")
        await bot.send_message(user_id, f"🎁 Администратор выдал вам подписку на {days} дней!")
        await state.clear()
    except:
        await message.answer("❌ Введите положительное число")

# --- Выдача / списание баланса ---
@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
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
        if amount <= 0: raise ValueError
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
    if callback.from_user.id != ADMIN_ID: return
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
        if amount <= 0: raise ValueError
        data = await state.get_data()
        user_id = data["user_id"]
        current = await get_balance(user_id)
        if current < amount:
            await message.answer(f"❌ Недостаточно. Баланс: {current}$")
            return
        await update_balance(user_id, -amount)
        await message.answer(f"✅ У пользователя {user_id} списано {amount}$")
        await bot.send_message(user_id, f"⚠️ С вашего баланса списано {amount}$")
        await state.clear()
    except:
        await message.answer("❌ Введите положительное число")

# --- Статистика ---
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    users = await get_all_users()
    total = len(users)
    active = sum(1 for u in users if u["sub_until"] > int(time.time()))
    total_balance = sum(u["balance"] for u in users)
    async with db_pool.acquire() as conn:
        tg_count = await conn.fetchval("SELECT COUNT(*) FROM tg_accounts")
        vk_count = await conn.fetchval("SELECT COUNT(*) FROM vk_accounts")
    text = (f"📊 Статистика:\n👥 Пользователей: {total}\n💎 Активных подписок: {active}\n💰 Общий баланс: {total_balance:.2f}$\n"
            f"📱 TG аккаунтов: {tg_count}\n📘 VK аккаунтов: {vk_count}")
    try:
        await callback.message.edit_text(text, reply_markup=back_button("admin_panel"))
    except:
        await callback.message.answer(text, reply_markup=back_button("admin_panel"))
    await callback.answer()

# --- Заявки на вывод ---
@dp.callback_query(F.data == "admin_withdraws")
async def admin_withdraws(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
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
    if callback.from_user.id != ADMIN_ID: return
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
    if callback.from_user.id != ADMIN_ID: return
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

# --- Промокоды (админ) ---
@dp.callback_query(F.data == "admin_promocodes")
async def admin_promocodes_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список", callback_data="admin_list_promos")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text("🎫 Промокоды", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_create_promo")
async def create_promo_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
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
        if days <= 0: raise ValueError
        await state.update_data(days=days)
        await message.answer("Макс. использований:")
        await state.set_state(AdminCreatePromocode.waiting_max_uses)
    except:
        await message.answer("Введите число >0")

@dp.message(AdminCreatePromocode.waiting_max_uses)
async def create_promo_max_uses(message: types.Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 1: raise ValueError
        data = await state.get_data()
        await create_promocode(data["code"], data["days"], max_uses)
        await message.answer(f"✅ Промокод {data['code']} создан!")
        await state.clear()
    except:
        await message.answer("Введите число от 1")

@dp.callback_query(F.data == "admin_list_promos")
async def list_promocodes_admin(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
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
    if callback.from_user.id != ADMIN_ID: return
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

# --- Активация промокода пользователем (только 1 раз) ---
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
    remaining = promo["max_uses"] - promo["uses"] - 1
    await message.answer(f"✅ Активирован! +{promo['days']} дней подписки. Осталось использований: {remaining}")
    await bot.send_message(ADMIN_ID, f"🎫 {user_id} активировал {code}")
    await state.clear()

# --- Пагинация пользователей ---
@dp.callback_query(F.data == "admin_users_page")
async def admin_users_page(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.update_data(page=0)
    await show_users_page(callback.message, state)

async def show_users_page(message: types.Message, state: FSMContext):
    data = await state.get_data()
    page = data.get("page", 0)
    users = await get_all_users()
    per_page = 10
    total = max(1, (len(users) + per_page - 1) // per_page)
    if page >= total: page = total - 1
    if page < 0: page = 0
    await state.update_data(page=page)
    start = page * per_page
    end = start + per_page
    current = users[start:end]
    text = "👥 Пользователи:\n\n"
    for u in current:
        sub = datetime.fromtimestamp(u["sub_until"]).strftime('%d.%m.%Y') if u["sub_until"] else "Нет"
        text += f"🆔 {u['tg_id']} | {u['username']}\n💵 {u['balance']:.2f}$ | Подписка до: {sub}\n\n"
    kb = []
    if page > 0:
        kb.append(InlineKeyboardButton(text="◀️ Назад", callback_data="users_page_prev"))
    if page < total - 1:
        kb.append(InlineKeyboardButton(text="Вперед ▶️", callback_data="users_page_next"))
    kb.append(InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel"))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[kb])
    await message.edit_text(text, reply_markup=keyboard)

@dp.callback_query(F.data == "users_page_prev")
async def users_page_prev(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    page = (await state.get_data()).get("page", 0)
    if page > 0:
        await state.update_data(page=page - 1)
        await show_users_page(callback.message, state)
    await callback.answer()

@dp.callback_query(F.data == "users_page_next")
async def users_page_next(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    page = (await state.get_data()).get("page", 0)
    users = await get_all_users()
    total = max(1, (len(users) + 9) // 10)
    if page < total - 1:
        await state.update_data(page=page + 1)
        await show_users_page(callback.message, state)
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users_list(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    users = await get_all_users()
    if not users:
        await callback.message.edit_text("Нет пользователей", reply_markup=back_button("admin_panel"))
        return
    await admin_users_page(callback, state)

# --- Глобальная рассылка админа ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(AdminBroadcast.waiting_text)
    await callback.answer()

@dp.message(AdminBroadcast.waiting_text)
async def admin_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.update_data(text=message.text)
    await message.answer("Подтвердите (да/нет):")
    await state.set_state(AdminBroadcast.waiting_confirm)

@dp.message(AdminBroadcast.waiting_confirm)
async def admin_broadcast_confirm(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
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
    await state.clear()

# Кнопки после игры (вставьте после всех хендлеров куба)
@dp.callback_query(F.data == "cube_again")
async def cube_again(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    last = user_last_game.get(user_id)
    if not last:
        await callback.answer("Нет предыдущей игры. Сначала сыграйте.", show_alert=True)
        return
    last_bet = last["bet"]
    last_mode = last["mode"]

    balance = await get_balance(user_id)
    if last_bet > balance:
        await callback.answer(f"❌ Не хватает. Баланс: {balance:.2f}$", show_alert=True)
        return

    # Сбрасываем состояние для новой игры
    await state.clear()
    await state.update_data(bet=last_bet, cube_mode=last_mode)

    if last_mode in ("less_more", "even_odd", "35"):
        if last_mode == "less_more":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Меньше (1-3)", callback_data="cube_choice:less"),
                 InlineKeyboardButton(text="Больше (4-6)", callback_data="cube_choice:more")]])
        elif last_mode == "even_odd":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Чёт", callback_data="cube_choice:even"),
                 InlineKeyboardButton(text="Нечет", callback_data="cube_choice:odd")]])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Больше 3.5", callback_data="cube_choice:gt35"),
                 InlineKeyboardButton(text="Меньше 3.5", callback_data="cube_choice:lt35")]])
        await callback.message.answer("Выберите вариант:", reply_markup=kb)
        await state.set_state(GameCube.waiting_choice)
    elif last_mode == "exact":
        await callback.message.answer("Введите число от 1 до 6:")
        await state.set_state(GameCube.waiting_exact)
    elif last_mode == "range":
        await callback.message.answer("Введите диапазон (пример: 2-4):")
        await state.set_state(GameCube.waiting_range)
    await callback.answer()

@dp.callback_query(F.data == "cube_inc")
async def cube_inc(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    old_bet = data.get("last_bet", 0)
    if old_bet < 0.1:
        await callback.answer("Нет предыдущей игры", show_alert=True)
        return
    new_bet = old_bet + 1.0
    balance = await get_balance(user_id)
    if new_bet > balance:
        await callback.answer(f"❌ Не хватает. Баланс: {balance:.2f}$", show_alert=True)
        return
    await state.update_data(last_bet=new_bet, bet=new_bet)
    await cube_again(callback, state)

@dp.callback_query(F.data == "cube_dec")
async def cube_dec(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    old_bet = data.get("last_bet", 0)
    if old_bet < 0.1:
        await callback.answer("Нет предыдущей игры", show_alert=True)
        return
    new_bet = old_bet - 1.0
    if new_bet < 0.1:
        new_bet = 0.1
    await state.update_data(last_bet=new_bet, bet=new_bet)
    await cube_again(callback, state)

@dp.callback_query(F.data == "cube_allin")
async def cube_allin(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    balance = await get_balance(user_id)
    if balance < 0.1:
        await callback.answer("❌ Баланс слишком мал для игры", show_alert=True)
        return
    await state.update_data(last_bet=balance, bet=balance)
    await cube_again(callback, state)


@dp.callback_query(F.data == "cube_again")
async def cube_again(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    game_data = user_last_game.get(user_id)
    if not game_data:
        await callback.answer("Нет предыдущей игры. Начните новую через меню.", show_alert=True)
        return
    last_bet = game_data["bet"]
    last_mode = game_data["mode"]

    # Проверка баланса
    balance = await get_balance(user_id)
    if last_bet > balance:
        await callback.answer(f"❌ Не хватает средств. Баланс: {balance:.2f}$", show_alert=True)
        return

    # Сохраняем в состояние для текущей игры
    await state.update_data(bet=last_bet, cube_mode=last_mode)

    if last_mode in ("less_more", "even_odd", "35"):
        if last_mode == "less_more":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Меньше (1-3)", callback_data="cube_choice:less"),
                 InlineKeyboardButton(text="Больше (4-6)", callback_data="cube_choice:more")]])
        elif last_mode == "even_odd":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Чёт", callback_data="cube_choice:even"),
                 InlineKeyboardButton(text="Нечет", callback_data="cube_choice:odd")]])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Больше 3.5", callback_data="cube_choice:gt35"),
                 InlineKeyboardButton(text="Меньше 3.5", callback_data="cube_choice:lt35")]])
        await callback.message.answer("Выберите вариант:", reply_markup=kb)
        await state.set_state(GameCube.waiting_choice)
    elif last_mode == "exact":
        await callback.message.answer("Введите число от 1 до 6:")
        await state.set_state(GameCube.waiting_exact)
    elif last_mode == "range":
        await callback.message.answer("Введите диапазон (пример: 2-4):")
        await state.set_state(GameCube.waiting_range)
    await callback.answer()

async def get_registration_stats():
    """Возвращает количество регистраций за последние 24ч, 7 дней, 30 дней"""
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
    """Сумма балансов, средний баланс, кол-во с положительным балансом"""
    async with db_pool.acquire() as conn:
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        avg_balance = await conn.fetchval("SELECT COALESCE(AVG(balance),0) FROM users")
        positive_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE balance > 0")
        return total_balance, avg_balance, positive_count

async def get_withdraw_stats():
    """Сумма одобренных выводов и сумма ожидающих"""
    async with db_pool.acquire() as conn:
        approved = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE status='approved'")
        pending = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE status='pending'")
        return approved, pending

async def get_promocode_stats():
    """Количество активаций промокодов и среднее количество дней"""
    async with db_pool.acquire() as conn:
        total_used = await conn.fetchval("SELECT COUNT(*) FROM used_promocodes")
        # Среднее количество дней по активированным промокодам
        avg_days = await conn.fetchval("""
            SELECT AVG(p.days) FROM used_promocodes u 
            JOIN promocodes p ON u.code_id = p.id
        """)
        return total_used, avg_days or 0

async def get_top_users_by_balance(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tg_id, username, balance FROM users 
            ORDER BY balance DESC LIMIT $1
        """, limit)
        return rows

async def get_top_subscriptions(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tg_id, username, sub_until FROM users 
            WHERE sub_until > $1
            ORDER BY sub_until DESC LIMIT $2
        """, int(time.time()), limit)
        return rows

async def get_all_users_for_csv():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tg_id, username, 
                   to_timestamp(registered_at) as registered,
                   balance, 
                   to_timestamp(sub_until) as sub_until
            FROM users ORDER BY tg_id
        """)
        return rows

@dp.callback_query(F.data == "admin_ext_stats")
async def admin_extended_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    # Получаем все данные
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
    if callback.from_user.id != ADMIN_ID:
        return
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
    if callback.from_user.id != ADMIN_ID:
        return
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
    # Формируем CSV строку
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["tg_id", "username", "registered_at", "balance", "subscription_until"])
    for r in rows:
        writer.writerow([r["tg_id"], r["username"], r["registered"], r["balance"], r["sub_until"]])
    csv_content = output.getvalue().encode('utf-8')
    # Отправляем файлом
    await callback.message.answer_document(
        types.BufferedInputFile(csv_content, filename="users_export.csv"),
        caption="📥 Экспорт пользователей"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_balance_manage")
async def admin_balance_manage(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать баланс", callback_data="admin_add_balance"),
         InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin_remove_balance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text("Управление балансами пользователей", reply_markup=kb)
    await callback.answer()

# ----- VK расширенные функции -----
async def get_vk_account_info(access_token: str):
    """Получает полную информацию о VK аккаунте"""
    vk_session = vk_api.VkApi(token=access_token)
    vk = vk_session.get_api()
    user = vk.users.get(fields="counters,sex,bdate,city,country,status,last_seen,photo_max_orig")[0]
    counters = user.get("counters", {})
    return {
        "id": user["id"],
        "name": f"{user['first_name']} {user['last_name']}",
        "status": user.get("status", ""),
        "friends": counters.get("friends", 0),
        "followers": counters.get("followers", 0),
        "groups": counters.get("groups", 0),
        "photos": counters.get("photos", 0),
        "videos": counters.get("videos", 0),
        "audios": counters.get("audios", 0),
        "sex": "Мужской" if user.get("sex") == 2 else "Женский" if user.get("sex") == 1 else "Не указан",
        "bdate": user.get("bdate", "Не указана"),
        "city": user.get("city", {}).get("title", "Не указан"),
        "country": user.get("country", {}).get("title", "Не указан"),
        "last_seen": user.get("last_seen", {}).get("time", 0),
        "photo": user.get("photo_max_orig", "")
    }

async def get_vk_friends_list(access_token: str, limit=100):
    """Получает список друзей"""
    vk_session = vk_api.VkApi(token=access_token)
    vk = vk_session.get_api()
    friends = vk.friends.get(fields="first_name,last_name", count=limit)
    return friends["items"]

async def get_vk_groups_list(access_token: str, limit=100):
    """Получает список групп, в которых состоит пользователь"""
    vk_session = vk_api.VkApi(token=access_token)
    vk = vk_session.get_api()
    groups = vk.groups.get(count=limit, extended=1)
    return groups["items"]

async def change_vk_profile_name(access_token: str, first_name: str, last_name: str = ""):
    """Изменяет имя и фамилию в VK"""
    vk_session = vk_api.VkApi(token=access_token)
    vk = vk_session.get_api()
    params = {"first_name": first_name}
    if last_name:
        params["last_name"] = last_name
    return vk.account.saveProfileInfo(**params)

async def change_vk_status(access_token: str, status: str):
    """Изменяет статус в VK"""
    vk_session = vk_api.VkApi(token=access_token)
    vk = vk_session.get_api()
    return vk.status.set(text=status)

async def upload_vk_avatar(access_token: str, photo_path: str):
    """Загружает новую аватарку в VK (требуется photo_path)"""
    # Упрощённая версия: полная требует загрузки на сервер VK
    # Для бота проще предложить пользователю использовать ссылку на фото
    return "Полная смена аватарки требует прямой загрузки файла. Используйте официальное приложение VK."

# Шаблоны
async def add_vk_template(owner_tg_id: int, name: str, content: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vk_templates (owner_tg_id, name, content, created_at) VALUES ($1,$2,$3,$4)",
            owner_tg_id, name, content, int(time.time())
        )

async def get_vk_templates(owner_tg_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, content FROM vk_templates WHERE owner_tg_id=$1 ORDER BY id", owner_tg_id)
        return rows

async def delete_vk_template(template_id: int, owner_tg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM vk_templates WHERE id=$1 AND owner_tg_id=$2", template_id, owner_tg_id)

async def get_vk_template(template_id: int, owner_tg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name, content FROM vk_templates WHERE id=$1 AND owner_tg_id=$2", template_id, owner_tg_id)
        return row

# Информация об аккаунте
@dp.callback_query(F.data.startswith("vk_info_"))
async def vk_account_info(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token, vk_name FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        token = row["token"]
    try:
        info = await get_vk_account_info(token)
        last_seen = datetime.fromtimestamp(info["last_seen"]).strftime('%d.%m.%Y %H:%M') if info["last_seen"] else "никогда"
        text = (
            f"📊 **Информация о VK аккаунте**\n\n"
            f"👤 Имя: {info['name']}\n"
            f"🆔 ID: {info['id']}\n"
            f"💬 Статус: {info['status'][:50]}\n"
            f"👫 Друзья: {info['friends']}\n"
            f"👥 Подписчики: {info['followers']}\n"
            f"📁 Групп: {info['groups']}\n"
            f"📸 Фото: {info['photos']}\n"
            f"🎬 Видео: {info['videos']}\n"
            f"🎵 Аудио: {info['audios']}\n"
            f"🚻 Пол: {info['sex']}\n"
            f"🎂 Дата рождения: {info['bdate']}\n"
            f"🏙️ Город: {info['city']}\n"
            f"🌍 Страна: {info['country']}\n"
            f"🕒 Был в сети: {last_seen}"
        )
        await callback.message.edit_text(text, reply_markup=back_button(f"vk_acc_{acc_id}"))
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {get_russian_error(e)}", reply_markup=back_button(f"vk_acc_{acc_id}"))
    await callback.answer()

# Список друзей
@dp.callback_query(F.data.startswith("vk_friends_"))
async def vk_show_friends(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        token = row["token"]
    try:
        friends = await get_vk_friends_list(token, 10)
        if not friends:
            text = "Нет друзей"
        else:
            text = "👥 **Первые 10 друзей:**\n"
            for f in friends:
                text += f"• {f['first_name']} {f['last_name']} (ID:{f['id']})\n"
        await callback.message.answer(text, reply_markup=back_button(f"vk_acc_{acc_id}"))
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_convs_"))
async def vk_show_conversations(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        token = row["token"]
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    try:
        convs = vk.messages.getConversations(count=10)["items"]
        if not convs:
            text = "Нет активных бесед"
        else:
            text = "💬 **Последние беседы:**\n"
            for c in convs:
                peer_id = c["conversation"]["peer"]["id"]
                text += f"• Беседа ID: {peer_id}\n"
        await callback.message.answer(text, reply_markup=back_button(f"vk_acc_{acc_id}"))
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
    await callback.answer()

# Список групп
@dp.callback_query(F.data.startswith("vk_groups_"))
async def vk_groups_list(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, callback.from_user.id)
        if not row:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        token = row["token"]
    try:
        groups = await get_vk_groups_list(token, 30)
        if not groups:
            text = "Нет групп."
        else:
            text = "📁 **Список групп (первые 30):**\n\n"
            for g in groups:
                text += f"• {g['name']} (участников: {g.get('members_count', '?')})\n"
        await callback.message.edit_text(text, reply_markup=back_button(f"vk_acc_{acc_id}"))
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {get_russian_error(e)}", reply_markup=back_button(f"vk_acc_{acc_id}"))
    await callback.answer()

# Изменение имени
@dp.callback_query(F.data.startswith("vk_change_name_"))
async def vk_change_name_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(vk_acc_id=acc_id)
    await callback.message.answer("Введите новое имя и фамилию через пробел (например: Иван Иванов):")
    await state.set_state(VKManage.waiting_new_name)
    await callback.answer()

@dp.message(VKManage.waiting_new_name)
async def vk_change_name_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["vk_acc_id"]
    name_parts = message.text.strip().split()
    if len(name_parts) < 1:
        await message.answer("❌ Введите хотя бы имя")
        return
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row["token"]
    try:
        res = await change_vk_profile_name(token, first_name, last_name)
        if "name_changed" in res:
            await message.answer(f"✅ Имя изменено на {first_name} {last_name}")
            await conn.execute("UPDATE vk_accounts SET vk_name=$1 WHERE id=$2", f"{first_name} {last_name}", acc_id)
        else:
            await message.answer("❌ Не удалось изменить имя. Возможно, частое изменение.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await state.clear()

# Изменение статуса
@dp.callback_query(F.data.startswith("vk_change_status_"))
async def vk_change_status_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(vk_acc_id=acc_id)
    await callback.message.answer("Введите новый статус (макс 140 символов):")
    await state.set_state(VKManage.waiting_new_status)
    await callback.answer()

@dp.message(VKManage.waiting_new_status)
async def vk_change_status_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["vk_acc_id"]
    new_status = message.text.strip()[:140]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row["token"]
    try:
        await change_vk_status(token, new_status)
        await message.answer(f"✅ Статус изменён на: {new_status}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await state.clear()

# Шаблоны рассылки VK
@dp.callback_query(F.data.startswith("vk_templates_"))
async def vk_templates_menu(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[2])
    templates = await get_vk_templates(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    if templates:
        for t in templates:
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"📝 {t['name']}", callback_data=f"vk_use_template_{t['id']}_{acc_id}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Создать шаблон", callback_data=f"vk_create_template_{acc_id}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"vk_acc_{acc_id}")])
    await callback.message.edit_text("📝 Ваши шаблоны текста для рассылки:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("vk_create_template_"))
async def vk_create_template_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(vk_acc_id=acc_id)
    await callback.message.answer("Введите название шаблона:")
    await state.set_state(VKManage.waiting_template_name)
    await callback.answer()

@dp.message(VKManage.waiting_template_name)
async def vk_create_template_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым")
        return
    await state.update_data(template_name=name)
    await message.answer("Введите текст шаблона (можно с эмодзи и переносами):")
    await state.set_state(VKManage.waiting_template_content)

@dp.message(VKManage.waiting_template_content)
async def vk_create_template_content(message: types.Message, state: FSMContext):
    content = message.text
    data = await state.get_data()
    name = data["template_name"]
    await add_vk_template(message.from_user.id, name, content)
    await message.answer(f"✅ Шаблон «{name}» сохранён!")
    await state.clear()

@dp.callback_query(F.data.startswith("vk_use_template_"))
async def vk_use_template(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    template_id = int(parts[3])
    acc_id = int(parts[4])
    template = await get_vk_template(template_id, callback.from_user.id)
    if not template:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await state.update_data(acc_id=acc_id, text=template["content"])
    await callback.message.answer(f"📝 Использую шаблон «{template['name']}»:\n\n{template['content']}\n\nВведите задержку (сек):")
    await state.set_state(BroadcastVK.waiting_delay)
    await callback.answer()

async def get_vk_user_info(token: str):
    """Возвращает информацию о VK-пользователе"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    user = vk.users.get(fields="status,photo_max,first_name,last_name")[0]
    return user

async def update_vk_profile(token: str, first_name=None, last_name=None, status=None):
    """Обновляет имя, фамилию и статус VK"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    params = {}
    if first_name: params["first_name"] = first_name
    if last_name: params["last_name"] = last_name
    if status: params["status"] = status
    if params:
        vk.account.saveProfileInfo(**params)

async def upload_vk_avatar(token: str, photo_path: str):
    """Загружает новую аватарку VK"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    upload_url = vk.photos.getOwnerPhotoUploadServer()["upload_url"]
    import aiohttp
    async with aiohttp.ClientSession() as session:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            response = await session.post(upload_url, files=files)
            data = await response.json()
            vk.photos.saveOwnerPhoto(server=data["server"], photo=data["photo"], hash=data["hash"])

async def get_vk_friends_count(token: str):
    """Количество друзей"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    return vk.friends.get()["count"]

async def get_vk_groups_count(token: str):
    """Количество групп (подписок)"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    return vk.groups.get()["count"]

async def get_vk_conversations_count(token: str):
    """Количество активных бесед"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    convs = vk.messages.getConversations(count=200)
    return convs["count"]

async def get_vk_friends_list(token: str, limit=10):
    """Возвращает список друзей (первые limit)"""
    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    friends = vk.friends.get(fields="first_name,last_name", count=limit)
    return friends["items"]

# Работа с шаблонами
async def add_vk_template(owner_tg_id: int, name: str, text: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO vk_templates (owner_tg_id, name, text) VALUES ($1, $2, $3)", owner_tg_id, name, text)

async def get_user_templates(owner_tg_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, text FROM vk_templates WHERE owner_tg_id=$1 ORDER BY created_at DESC", owner_tg_id)
        return rows

async def delete_template(template_id: int, owner_tg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM vk_templates WHERE id=$1 AND owner_tg_id=$2", template_id, owner_tg_id)

# Логирование рассылки
async def log_broadcast(user_id, account_type, account_id, total, friends, chats, sent, status="completed"):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO broadcast_logs (user_id, account_type, account_id, total_contacts, friends_count, chats_count, sent_count, start_time, end_time, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """, user_id, account_type, account_id, total, friends, chats, sent, int(time.time()), int(time.time()), status)

@dp.callback_query(F.data.startswith("vk_edit_name_"))
async def vk_edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новое ИМЯ:")
    await state.set_state(VKManage.waiting_new_name)
    await callback.answer()

@dp.message(VKManage.waiting_new_name)
async def vk_edit_name_first(message: types.Message, state: FSMContext):
    await state.update_data(first_name=message.text.strip())
    await message.answer("Введите новую ФАМИЛИЮ:")
    await state.set_state(VKManage.waiting_new_lastname)

@dp.message(VKManage.waiting_new_lastname)
async def vk_edit_name_last(message: types.Message, state: FSMContext):
    last_name = message.text.strip()
    data = await state.get_data()
    acc_id = data["acc_id"]
    first_name = data["first_name"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row["token"]
    try:
        await update_vk_profile(token, first_name=first_name, last_name=last_name)
        await message.answer(f"✅ Имя изменено на {first_name} {last_name}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("vk_edit_status_"))
async def vk_edit_status_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Введите новый статус:")
    await state.set_state(VKManage.waiting_new_status)
    await callback.answer()

@dp.message(VKManage.waiting_new_status)
async def vk_edit_status_execute(message: types.Message, state: FSMContext):
    status = message.text.strip()
    data = await state.get_data()
    acc_id = data["acc_id"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row["token"]
    try:
        await update_vk_profile(token, status=status)
        await message.answer("✅ Статус обновлён!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    await state.clear()

@dp.callback_query(F.data.startswith("vk_edit_avatar_"))
async def vk_edit_avatar_start(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    await state.update_data(acc_id=acc_id)
    await callback.message.answer("Пришлите новое фото для аватарки:")
    await state.set_state(VKManage.waiting_new_avatar)
    await callback.answer()

@dp.message(VKManage.waiting_new_avatar, F.photo)
async def vk_edit_avatar_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token FROM vk_accounts WHERE id=$1 AND owner_tg_id=$2", acc_id, message.from_user.id)
        if not row:
            await message.answer("Аккаунт не найден")
            await state.clear()
            return
        token = row["token"]
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_path = f"/tmp/{photo.file_id}.jpg"
    await message.bot.download_file(file.file_path, file_path)
    try:
        await upload_vk_avatar(token, file_path)
        await message.answer("✅ Аватарка изменена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {get_russian_error(e)}")
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("vk_broadcast_template_"))
async def vk_broadcast_template(callback: types.CallbackQuery, state: FSMContext):
    acc_id = int(callback.data.split("_")[3])
    templates = await get_user_templates(callback.from_user.id)
    if not templates:
        await callback.message.answer("У вас нет шаблонов. Создать? (напишите /new_template)")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t["name"], callback_data=f"template_use_{t['id']}_{acc_id}")] for t in templates
    ] + [[InlineKeyboardButton(text="➕ Новый шаблон", callback_data="vk_create_template")],
         [InlineKeyboardButton(text="🔙 Назад", callback_data=f"vk_acc_{acc_id}")]])
    await callback.message.edit_text("Выберите шаблон:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("template_use_"))
async def vk_use_template(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    template_id = int(parts[2])
    acc_id = int(parts[3])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT text FROM vk_templates WHERE id=$1 AND owner_tg_id=$2", template_id, callback.from_user.id)
        if not row:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        text = row["text"]
    await state.update_data(acc_id=acc_id, text=text)
    await callback.message.answer(f"Текст шаблона:\n{text}\n\nВведите задержку (сек):")
    await state.set_state(BroadcastVK.waiting_delay)
    await callback.answer()

@dp.callback_query(F.data == "vk_create_template")
async def vk_create_template_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите НАЗВАНИЕ шаблона:")
    await state.set_state(VKTemplate.waiting_name)
    await callback.answer()

@dp.message(VKTemplate.waiting_name)
async def vk_template_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите ТЕКСТ шаблона (можно Emoji, текст):")
    await state.set_state(VKTemplate.waiting_text)

@dp.message(VKTemplate.waiting_text)
async def vk_template_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    text = message.text
    await add_vk_template(message.from_user.id, name, text)
    await message.answer(f"✅ Шаблон «{name}» сохранён!")
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast_stats")
async def admin_broadcast_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        # Общая статистика по всем рассылкам
        total_broadcasts = await conn.fetchval("SELECT COUNT(*) FROM broadcast_logs")
        total_contacts = await conn.fetchval("SELECT COALESCE(SUM(total_contacts),0) FROM broadcast_logs")
        total_sent = await conn.fetchval("SELECT COALESCE(SUM(sent_count),0) FROM broadcast_logs")
        total_friends = await conn.fetchval("SELECT COALESCE(SUM(friends_count),0) FROM broadcast_logs WHERE account_type='vk'")
        total_chats = await conn.fetchval("SELECT COALESCE(SUM(chats_count),0) FROM broadcast_logs WHERE account_type='vk'")
        # Статистика по TG (если будете логировать)
        text = (f"📊 **Статистика всех рассылок**\n\n"
                f"📨 Всего запусков: {total_broadcasts}\n"
                f"👥 Всего контактов обработано: {total_contacts}\n"
                f"✅ Отправлено сообщений: {total_sent}\n"
                f"📘 Из них:\n"
                f"   • Друзей VK: {total_friends}\n"
                f"   • Бесед VK: {total_chats}\n")
        # Топ-пользователей по количеству контактов
        top_users = await conn.fetch("""
            SELECT user_id, SUM(total_contacts) as total 
            FROM broadcast_logs 
            GROUP BY user_id 
            ORDER BY total DESC LIMIT 5
        """)
        if top_users:
            text += "\n🏆 **Топ пользователей по проливу:**\n"
            for u in top_users:
                text += f"• ID {u['user_id']} — {u['total']} контактов\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

async def check_vk_account_valid(owner_tg_id: int, acc_id: int, token: str, vk_name: str) -> bool:
    """Проверяет, жив ли VK аккаунт. Если нет — удаляет из БД и уведомляет."""
    try:
        vk_session = vk_api.VkApi(token=token)
        vk = vk_session.get_api()
        vk.users.get()  # пробуем получить свои данные
        return True
    except Exception as e:
        error_msg = str(e).lower()
        if "invalid token" in error_msg or "access denied" in error_msg or "authorization failed" in error_msg:
            # Удаляем аккаунт из БД
            await delete_vk_account(owner_tg_id, acc_id)
            # Отправляем уведомление пользователю
            await bot.send_message(owner_tg_id,
                f"⚠️ **VK аккаунт «{vk_name}» автоматически удалён**\n"
                f"Причина: токен недействителен или аккаунт заблокирован.\n"
                f"Вы можете добавить его заново в разделе «Мои аккаунты».")
            return False
        else:
            # Другая ошибка (сеть, таймаут) — не удаляем, но сообщаем
            await bot.send_message(owner_tg_id,
                f"❌ Ошибка при проверке VK аккаунта «{vk_name}»:\n{get_russian_error(e)}")
            return False

async def check_tg_account_valid(owner_tg_id: int, acc_id: int, session_file: str, phone: str) -> bool:
    """Проверяет, жива ли сессия Telegram. Если нет — деактивирует аккаунт."""
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    try:
        await client.get_me()
        return True
    except (AuthKeyError, UnauthorizedError) as e:
        await deactivate_tg_account(owner_tg_id, acc_id)
        await bot.send_message(owner_tg_id,
            f"⚠️ **Telegram аккаунт {phone} автоматически деактивирован**\n"
            f"Причина: сессия устарела или аккаунт заблокирован.\n"
            f"Удалите его и добавьте заново в разделе «Мои аккаунты».")
        return False
    except Exception as e:
        await bot.send_message(owner_tg_id,
            f"❌ Ошибка при проверке Telegram аккаунта {phone}:\n{get_russian_error(e)}")
        return False
    finally:
        await client.disconnect()

async def periodic_account_check():
    while True:
        await asyncio.sleep(3600)  # каждый час
        async with db_pool.acquire() as conn:
            # Проверяем VK аккаунты
            vk_rows = await conn.fetch("SELECT id, owner_tg_id, token, vk_name FROM vk_accounts WHERE is_active=True")
            for row in vk_rows:
                await check_vk_account_valid(row["owner_tg_id"], row["id"], row["token"], row["vk_name"])
                await asyncio.sleep(1)  # задержка, чтобы не спамить
            # Проверяем Telegram аккаунты
            tg_rows = await conn.fetch("SELECT id, owner_tg_id, session_file, phone FROM tg_accounts WHERE is_active=True")
            for row in tg_rows:
                await check_tg_account_valid(row["owner_tg_id"], row["id"], row["session_file"], row["phone"])
                await asyncio.sleep(1)

# Запустить в main() после инициализации:
async def main():
    await init_db()
    asyncio.create_task(periodic_account_check())  # фоновая проверка
    await dp.start_polling(bot)

async def check_and_update_limit(account_type: str, account_id: int, limit_cfg: dict) -> bool:
    """Проверяет, не превышен ли лимит. Если нет - увеличивает счётчик. Возвращает True, если можно отправлять."""
    now = int(time.time())
    hour_window = now - 3600
    day_window = now - 86400
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hour_start, hour_count, day_start, day_count FROM send_limits WHERE account_type=$1 AND account_id=$2",
            account_type, account_id
        )
        if row:
            hour_start, hour_count, day_start, day_count = row
            if hour_start < hour_window:
                hour_start = now
                hour_count = 0
            if day_start < day_window:
                day_start = now
                day_count = 0
            if hour_count >= limit_cfg["messages_per_hour"]:
                return False
            if day_count >= limit_cfg["messages_per_day"]:
                return False
            # Обновляем
            await conn.execute(
                "UPDATE send_limits SET hour_start=$1, hour_count=$2, day_start=$3, day_count=$4 WHERE account_type=$5 AND account_id=$6",
                hour_start, hour_count+1, day_start, day_count+1, account_type, account_id
            )
        else:
            await conn.execute(
                "INSERT INTO send_limits (account_type, account_id, hour_start, hour_count, day_start, day_count) VALUES ($1,$2,$3,$4,$5,$6)",
                account_type, account_id, now, 1, now, 1
            )
    return True
@dp.callback_query(F.data == "help")
async def help_menu(callback: types.CallbackQuery):
    text = (
        "❓ *ПОМОЩЬ И ИНСТРУКЦИЯ*\n\n"
        "┌─────────────────────────────────┐\n"
        "│  🎲 *ИГРЫ*                         │\n"
        "│  Выбери игру, сделай ставку      │\n"
        "│  от 0.1$. Коэффициенты до x10.   │\n"
        "├─────────────────────────────────┤\n"
        "│  💰 *БАЛАНС*                       │\n"
        "│  Пополнение через CryptoBot      │\n"
        "│  (USDT). Вывод от 1$ на кошелёк. │\n"
        "├─────────────────────────────────┤\n"
        "│  📢 *РАССЫЛКИ*                    │\n"
        "│  Добавь свои Telegram/VK аккаунты,│\n"
        "│  пиши сообщения, настраивай задержку  │\n"
        "│  для добавления аккаунтов.       │\n"
        "├─────────────────────────────────┤\n"
        "│  💎 *ПОДПИСКА*                    │\n"
        "│  Даёт доступ к рассылкам, шабло- │\n"
        "│  нам и расширенной статистике.   │\n"
        "├─────────────────────────────────┤\n"
        "│  🔧 *ТЕХПОДДЕРЖКА*                │\n"
        "│  @bloodworn   │\n"
        "└─────────────────────────────────┘\n\n"
        "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
        "✨ *Удачи в игре и высоких выигрышей!*"
    )
    await callback.message.edit_text(text, reply_markup=back_button("main_menu"), parse_mode="Markdown")
    await callback.answer()

async def tg_accounts_list(user_id: int):
    """Возвращает клавиатуру со списком Telegram аккаунтов пользователя"""
    accounts = await get_user_tg_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']} ({acc['phone']})", callback_data=f"tg_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ TG", callback_data="add_tg")])
    kb.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def vk_accounts_list(user_id: int):
    """Возвращает клавиатуру со списком VK аккаунтов пользователя"""
    accounts = await get_user_vk_accounts(user_id)
    kb = []
    for acc in accounts:
        status = "✅" if acc["is_active"] else "❌"
        kb.append([InlineKeyboardButton(text=f"{status} {acc['name']}", callback_data=f"vk_acc_{acc['id']}")])
    kb.append([InlineKeyboardButton(text="➕ ДОБАВИТЬ VK", callback_data="add_vk")])
    kb.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())