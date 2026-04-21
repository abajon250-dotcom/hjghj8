import sqlite3
import time

DB_PATH = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        sub_until INTEGER DEFAULT 0,
        casino_balance INTEGER DEFAULT 100
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tg_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_tg_id INTEGER,
        phone TEXT,
        session_file TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vk_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_tg_id INTEGER,
        token TEXT,
        vk_user_id INTEGER,
        vk_name TEXT
    )''')
    conn.commit()
    conn.close()

def get_user(tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"tg_id": row[0], "username": row[1], "sub_until": row[2], "casino_balance": row[3]}
    return None

def create_user(tg_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (tg_id, username, sub_until, casino_balance) VALUES (?, ?, 0, 100)", (tg_id, username))
    conn.commit()
    conn.close()

def is_subscribed(tg_id):
    user = get_user(tg_id)
    if not user: return False
    return user["sub_until"] > int(time.time())

def set_subscription(tg_id, days):
    new_time = int(time.time()) + days * 86400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET sub_until=? WHERE tg_id=?", (new_time, tg_id))
    conn.commit()
    conn.close()

def get_balance(tg_id):
    user = get_user(tg_id)
    return user["casino_balance"] if user else 0

def update_balance(tg_id, delta):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET casino_balance = casino_balance + ? WHERE tg_id=?", (delta, tg_id))
    conn.commit()
    conn.close()

# TG аккаунты
def add_tg_account(owner_tg_id, phone, session_file):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tg_accounts (owner_tg_id, phone, session_file) VALUES (?,?,?)", (owner_tg_id, phone, session_file))
    conn.commit()
    conn.close()

def get_active_tg_account(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_file FROM tg_accounts WHERE owner_tg_id=? AND is_active=1 ORDER BY id DESC LIMIT 1", (owner_tg_id,))
    row = c.fetchone()
    conn.close()
    return {"session_file": row[0]} if row else None

# VK аккаунты
def add_vk_account(owner_tg_id, token, vk_user_id, vk_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO vk_accounts (owner_tg_id, token, vk_user_id, vk_name) VALUES (?,?,?,?)", (owner_tg_id, token, vk_user_id, vk_name))
    conn.commit()
    conn.close()

def get_active_vk_account(owner_tg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token, vk_name FROM vk_accounts WHERE owner_tg_id=? ORDER BY id DESC LIMIT 1", (owner_tg_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"token": row[0], "vk_name": row[1]}
    return None

# Для админа: список всех пользователей
def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, sub_until, casino_balance FROM users")
    rows = c.fetchall()
    conn.close()
    return [{"tg_id": r[0], "username": r[1], "sub_until": r[2], "balance": r[3]} for r in rows]