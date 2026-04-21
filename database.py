import sqlite3
from config import DB_NAME

conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cursor = conn.cursor()

# Таблицы
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    is_premium INTEGER DEFAULT 0,
    casino_balance INTEGER DEFAULT 100
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS tg_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_tg_id INTEGER,
    phone TEXT,
    session_file TEXT,
    is_active INTEGER DEFAULT 1
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS vk_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_tg_id INTEGER,
    token TEXT,
    vk_user_id INTEGER,
    vk_name TEXT
)
""")
conn.commit()

# ---- Users ----
def register_user(tg_id, username):
    cursor.execute("INSERT OR IGNORE INTO users (tg_id, username) VALUES (?, ?)", (tg_id, username))
    conn.commit()

def is_subscribed(tg_id):
    cursor.execute("SELECT is_premium FROM users WHERE tg_id=?", (tg_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

def set_premium(tg_id):
    cursor.execute("UPDATE users SET is_premium = 1 WHERE tg_id=?", (tg_id,))
    conn.commit()

def get_casino_balance(tg_id):
    cursor.execute("SELECT casino_balance FROM users WHERE tg_id=?", (tg_id,))
    row = cursor.fetchone()
    return row[0] if row else 100

def update_casino_balance(tg_id, delta):
    cursor.execute("UPDATE users SET casino_balance = casino_balance + ? WHERE tg_id=?", (delta, tg_id))
    conn.commit()

# ---- Telegram accounts ----
def add_tg_account(owner, phone, session_file):
    cursor.execute("INSERT INTO tg_accounts (owner_tg_id, phone, session_file) VALUES (?,?,?)",
                   (owner, phone, session_file))
    conn.commit()

def get_tg_account(owner):
    cursor.execute("SELECT session_file FROM tg_accounts WHERE owner_tg_id=? AND is_active=1", (owner,))
    row = cursor.fetchone()
    return row[0] if row else None

def deactivate_tg_account(owner):
    cursor.execute("UPDATE tg_accounts SET is_active=0 WHERE owner_tg_id=?", (owner,))
    conn.commit()

# ---- VK accounts ----
def add_vk_account(owner, token, vk_id, name):
    cursor.execute("INSERT INTO vk_accounts (owner_tg_id, token, vk_user_id, vk_name) VALUES (?,?,?,?)",
                   (owner, token, vk_id, name))
    conn.commit()

def get_vk_token(owner):
    cursor.execute("SELECT token FROM vk_accounts WHERE owner_tg_id=?", (owner,))
    row = cursor.fetchone()
    return row[0] if row else None

# ---- Admin ----
def get_all_users():
    cursor.execute("SELECT tg_id, is_premium, casino_balance FROM users")
    return cursor.fetchall()

def get_stats():
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tg_accounts")
    tg_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM vk_accounts")
    vk_count = cursor.fetchone()[0]
    return user_count, tg_count, vk_count