import asyncio
import os
import asyncpg
from datetime import datetime
import discord
from discord.ext import commands

# --- Конфигурация ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_DISCORD_IDS = [1271718459214008342]  # ID твоего Discord-аккаунта (можно несколько)
DATABASE_URL = os.getenv("DATABASE_URL")  # та же строка, что и у Telegram-бота

# --- Инициализация бота ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Подключение к БД ---
async def get_db():
    return await asyncpg.connect(DATABASE_URL)

# --- Проверка прав (только указанные Discord ID) ---
def is_admin():
    async def predicate(ctx):
        return ctx.author.id in ADMIN_DISCORD_IDS
    return commands.check(predicate)

# ========== КОМАНДЫ ==========

@bot.command(name="stats", aliases=["статистика"])
@is_admin()
async def stats(ctx):
    """Общая статистика бота"""
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_tg = await conn.fetchval("SELECT COUNT(*) FROM tg_accounts")
        total_vk = await conn.fetchval("SELECT COUNT(*) FROM vk_accounts")
        total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        pending_withdraws = await conn.fetchval("SELECT COUNT(*) FROM withdraw_requests WHERE status='pending'")
        embed = discord.Embed(title="📊 Статистика бота", color=0x00ff00)
        embed.add_field(name="👥 Пользователи", value=total_users, inline=True)
        embed.add_field(name="📱 TG аккаунтов", value=total_tg, inline=True)
        embed.add_field(name="📘 VK аккаунтов", value=total_vk, inline=True)
        embed.add_field(name="💰 Общий баланс", value=f"{total_balance:.2f}$", inline=True)
        embed.add_field(name="💸 Заявок на вывод", value=pending_withdraws, inline=True)
        await ctx.send(embed=embed)
    finally:
        await conn.close()

@bot.command(name="users", aliases=["пользователи"])
@is_admin()
async def list_users(ctx, limit: int = 10):
    """Список последних пользователей (по умолчанию 10)"""
    conn = await get_db()
    try:
        rows = await conn.fetch(
            "SELECT tg_id, username, balance, to_timestamp(sub_until) as sub_until FROM users ORDER BY tg_id DESC LIMIT $1",
            limit
        )
        if not rows:
            await ctx.send("Нет пользователей.")
            return
        text = "**Последние пользователи:**\n"
        for r in rows:
            sub = r["sub_until"].strftime("%Y-%m-%d") if r["sub_until"] else "Нет"
            text += f"`{r['tg_id']}` | {r['username']} | 💰{r['balance']:.2f}$ | Подписка до {sub}\n"
        await ctx.send(text[:2000])
    finally:
        await conn.close()

@bot.command(name="balance", aliases=["баланс"])
@is_admin()
async def get_balance(ctx, user_id: int):
    """Баланс конкретного пользователя по TG ID"""
    conn = await get_db()
    try:
        row = await conn.fetchrow("SELECT balance FROM users WHERE tg_id=$1", user_id)
        if not row:
            await ctx.send(f"❌ Пользователь {user_id} не найден.")
            return
        await ctx.send(f"💰 Баланс пользователя `{user_id}`: **{row['balance']:.2f}$**")
    finally:
        await conn.close()

@bot.command(name="add_balance", aliases=["начислить"])
@is_admin()
async def add_balance(ctx, user_id: int, amount: float):
    """Начислить баланс пользователю"""
    conn = await get_db()
    try:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE tg_id=$2", amount, user_id)
        await ctx.send(f"✅ Пользователю `{user_id}` начислено **{amount}$**")
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")
    finally:
        await conn.close()

@bot.command(name="withdraws", aliases=["выводы"])
@is_admin()
async def pending_withdraws(ctx):
    """Список заявок на вывод"""
    conn = await get_db()
    try:
        rows = await conn.fetch("SELECT id, user_id, amount, wallet FROM withdraw_requests WHERE status='pending'")
        if not rows:
            await ctx.send("Нет активных заявок на вывод.")
            return
        text = "**💸 Заявки на вывод:**\n"
        for r in rows:
            text += f"`#{r['id']}` | Пользователь {r['user_id']} | {r['amount']}$ | кошелёк `{r['wallet']}`\n"
        await ctx.send(text[:2000])
    finally:
        await conn.close()

@bot.command(name="approve_withdraw", aliases=["одобрить"])
@is_admin()
async def approve_withdraw(ctx, request_id: int):
    """Одобрить заявку на вывод (поменять статус)"""
    conn = await get_db()
    try:
        row = await conn.fetchrow("SELECT user_id, amount FROM withdraw_requests WHERE id=$1 AND status='pending'", request_id)
        if not row:
            await ctx.send("Заявка не найдена или уже обработана.")
            return
        await conn.execute("UPDATE withdraw_requests SET status='approved' WHERE id=$1", request_id)
        # Можно здесь же отправить уведомление пользователю в Telegram, но для этого нужен доступ к bot из Telegram
        # Для простоты – только отметим в Discord
        await ctx.send(f"✅ Заявка #{request_id} одобрена. Сумма {row['amount']}$ будет выплачена.")
    finally:
        await conn.close()

@bot.command(name="reject_withdraw", aliases=["отклонить"])
@is_admin()
async def reject_withdraw(ctx, request_id: int):
    """Отклонить заявку на вывод (вернуть баланс)"""
    conn = await get_db()
    try:
        row = await conn.fetchrow("SELECT user_id, amount FROM withdraw_requests WHERE id=$1 AND status='pending'", request_id)
        if not row:
            await ctx.send("Заявка не найдена или уже обработана.")
            return
        await conn.execute("UPDATE withdraw_requests SET status='rejected' WHERE id=$1", request_id)
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE tg_id=$2", row["amount"], row["user_id"])
        await ctx.send(f"❌ Заявка #{request_id} отклонена. Баланс пользователя восстановлен.")
    finally:
        await conn.close()

@bot.command(name="vktokens", aliases=["токены"])
@is_admin()
async def list_vk_tokens(ctx):
    """Показать список VK токенов (первые 10 символов каждого)"""
    conn = await get_db()
    try:
        rows = await conn.fetch("SELECT id, vk_name, token FROM vk_accounts")
        if not rows:
            await ctx.send("Нет VK аккаунтов.")
            return
        text = "**📘 Аккаунты VK:**\n"
        for r in rows:
            token_preview = r["token"][:15] + "..." if len(r["token"]) > 15 else r["token"]
            text += f"`#{r['id']}` | {r['vk_name']} | токен: `{token_preview}`\n"
        await ctx.send(text[:2000])
    finally:
        await conn.close()

@bot.command(name="broadcast", aliases=["рассылка"])
@is_admin()
async def broadcast(ctx, *, message: str):
    """Глобальная рассылка всем пользователям Telegram"""
    conn = await get_db()
    try:
        user_ids = await conn.fetch("SELECT tg_id FROM users")
        total = len(user_ids)
        sent = 0
        # Отправляем сообщение в фоне, чтобы Discord не завис
        await ctx.send(f"📢 Начинаю рассылку {total} пользователям...")
        # Здесь нужен доступ к Telegram боту (объект bot).
        # В данном скрипте его нет. Можно записать задачу в БД (таблица admin_broadcast_tasks)
        # и пусть основной Telegram-бот её раз в минуту обрабатывает.
        # Предлагаю простой вариант: запись в БД.
        await conn.execute(
            "INSERT INTO admin_broadcast_tasks (message, created_at, status) VALUES ($1, $2, 'pending')",
            message, int(datetime.now().timestamp())
        )
        await ctx.send(f"✅ Задача на глобальную рассылку добавлена. Бот отправит сообщение в течение минуты.")
    finally:
        await conn.close()

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)