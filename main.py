import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

BOT_TOKEN = "8354010714:AAG4BfVBaeSGxCaCINEc1ByWE07_ctWJg3o"  # ВСТАВЬТЕ

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище: { user_id: {"mode": str, "bet": float, "last_bet": float, "last_mode": str} }
users = {}

@dp.message(Command("start"))
async def start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Куб", callback_data="cube_menu")],
    ])
    await message.answer("Выберите игру:", reply_markup=kb)

@dp.callback_query(F.data == "cube_menu")
async def cube_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Больше/Меньше (x2)", callback_data="mode:less_more")],
        [InlineKeyboardButton(text="Чёт/Нечет (x2)", callback_data="mode:even_odd")],
        [InlineKeyboardButton(text="Угадать число (x6)", callback_data="mode:exact")],
        [InlineKeyboardButton(text="Диапазон (x?)", callback_data="mode:range")],
        [InlineKeyboardButton(text="Больше 3.5 / Меньше 3.5 (x2)", callback_data="mode:35")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")]
    ])
    await callback.message.edit_text("Режимы куба:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    await start(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("mode:"))
async def mode_selected(callback: types.CallbackQuery):
    mode = callback.data.split(":")[1]
    user_id = callback.from_user.id
    users[user_id] = {"mode": mode}
    await callback.message.answer("💰 Введите ставку (мин 0.1$):")
    await callback.answer()

@dp.message(F.text)
async def handle_bet(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users or "mode" not in users[user_id]:
        await message.answer("Сначала выберите режим через меню.")
        return
    try:
        bet = float(message.text.strip())
        if bet < 0.1:
            await message.answer("Минимум 0.1$")
            return
        users[user_id]["bet"] = bet
        mode = users[user_id]["mode"]

        if mode in ("less_more", "even_odd", "35"):
            if mode == "less_more":
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меньше (1-3)", callback_data="choice:less"), InlineKeyboardButton(text="Больше (4-6)", callback_data="choice:more")]])
            elif mode == "even_odd":
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Чёт", callback_data="choice:even"), InlineKeyboardButton(text="Нечет", callback_data="choice:odd")]])
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Больше 3.5", callback_data="choice:gt35"), InlineKeyboardButton(text="Меньше 3.5", callback_data="choice:lt35")]])
            await message.answer("Выберите вариант:", reply_markup=kb)
        elif mode == "exact":
            await message.answer("Введите число от 1 до 6:")
            users[user_id]["await_exact"] = True
        elif mode == "range":
            await message.answer("Введите диапазон (пример: 2-4):")
            users[user_id]["await_range"] = True
    except:
        await message.answer("Введите число")

@dp.callback_query(F.data.startswith("choice:"))
async def choice_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in users or "bet" not in users[user_id]:
        await callback.answer("Ошибка, начните сначала", show_alert=True)
        return
    bet = users[user_id]["bet"]
    mode = users[user_id]["mode"]
    choice = callback.data.split(":")[1]
    msg = await callback.message.answer_dice(emoji="🎲")
    roll = msg.dice.value
    await asyncio.sleep(1)
    win = False
    if mode == "less_more":
        win = (choice == "less" and roll <= 3) or (choice == "more" and roll >= 4)
    elif mode == "even_odd":
        win = (choice == "even" and roll % 2 == 0) or (choice == "odd" and roll % 2 == 1)
    elif mode == "35":
        win = (choice == "gt35" and roll > 3.5) or (choice == "lt35" and roll < 3.5)
    if win:
        payout = bet * 2
        result = f"🎲 Выпало {roll}\n✅ ВЫИГРЫШ: {bet}$ x2 = {payout}$"
    else:
        payout = -bet
        result = f"🎲 Выпало {roll}\n❌ ПРОИГРЫШ: {bet}$"
    users[user_id]["last_bet"] = bet
    users[user_id]["last_mode"] = mode
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Ещё раз", callback_data="again"),
         InlineKeyboardButton(text="⬆️ +1$", callback_data="inc"),
         InlineKeyboardButton(text="⬇️ -1$", callback_data="dec"),
         InlineKeyboardButton(text="💰 Ва-банк", callback_data="all_in")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_start")]
    ])
    await callback.message.answer(result, reply_markup=kb)
    await callback.answer()

@dp.message(F.text)
async def exact_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users or not users[user_id].get("await_exact"):
        return
    try:
        num = int(message.text.strip())
        if num < 1 or num > 6:
            raise ValueError
        bet = users[user_id]["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if roll == num:
            payout = bet * 6
            result = f"🎲 Выпало {roll}\n✅ УГАДАЛ! Выигрыш: {bet}$ x6 = {payout}$"
        else:
            payout = -bet
            result = f"🎲 Выпало {roll}\n❌ НЕ УГАДАЛ. Проигрыш: {bet}$"
        users[user_id]["last_bet"] = bet
        users[user_id]["last_mode"] = "exact"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Ещё раз", callback_data="again"),
             InlineKeyboardButton(text="⬆️ +1$", callback_data="inc"),
             InlineKeyboardButton(text="⬇️ -1$", callback_data="dec"),
             InlineKeyboardButton(text="💰 Ва-банк", callback_data="all_in")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_start")]
        ])
        await message.answer(result, reply_markup=kb)
    except:
        await message.answer("Введите число от 1 до 6")
    finally:
        users[user_id]["await_exact"] = False

@dp.message(F.text)
async def range_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id not in users or not users[user_id].get("await_range"):
        return
    try:
        parts = message.text.strip().split('-')
        low, high = int(parts[0]), int(parts[1])
        if low < 1 or high > 6 or low > high:
            raise ValueError
        count = high - low + 1
        coeff = 6.0 / count
        if coeff < 1.2:
            coeff = 1.2
        bet = users[user_id]["bet"]
        msg = await message.answer_dice(emoji="🎲")
        roll = msg.dice.value
        await asyncio.sleep(1)
        if low <= roll <= high:
            payout = bet * coeff
            result = f"🎲 Выпало {roll}\n✅ ПОПАЛ! Коэф: {coeff:.1f}x, Выигрыш: {bet}$ x{coeff:.1f} = {payout:.2f}$"
        else:
            payout = -bet
            result = f"🎲 Выпало {roll}\n❌ МИМО. Проигрыш: {bet}$"
        users[user_id]["last_bet"] = bet
        users[user_id]["last_mode"] = "range"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Ещё раз", callback_data="again"),
             InlineKeyboardButton(text="⬆️ +1$", callback_data="inc"),
             InlineKeyboardButton(text="⬇️ -1$", callback_data="dec"),
             InlineKeyboardButton(text="💰 Ва-банк", callback_data="all_in")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_start")]
        ])
        await message.answer(result, reply_markup=kb)
    except:
        await message.answer("Пример: 2-4")
    finally:
        users[user_id]["await_range"] = False

@dp.callback_query(F.data == "again")
async def again_game(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in users or "last_bet" not in users[user_id]:
        await callback.answer("Нет предыдущей игры", show_alert=True)
        return
    bet = users[user_id]["last_bet"]
    mode = users[user_id]["last_mode"]
    users[user_id]["bet"] = bet
    if mode in ("less_more", "even_odd", "35"):
        if mode == "less_more":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Меньше (1-3)", callback_data="choice:less"), InlineKeyboardButton(text="Больше (4-6)", callback_data="choice:more")]])
        elif mode == "even_odd":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Чёт", callback_data="choice:even"), InlineKeyboardButton(text="Нечет", callback_data="choice:odd")]])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Больше 3.5", callback_data="choice:gt35"), InlineKeyboardButton(text="Меньше 3.5", callback_data="choice:lt35")]])
        await callback.message.answer("Выберите вариант:", reply_markup=kb)
    elif mode == "exact":
        await callback.message.answer("Введите число от 1 до 6:")
        users[user_id]["await_exact"] = True
    elif mode == "range":
        await callback.message.answer("Введите диапазон (пример: 2-4):")
        users[user_id]["await_range"] = True
    await callback.answer()

@dp.callback_query(F.data == "inc")
async def inc_bet(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in users or "last_bet" not in users[user_id]:
        await callback.answer("Нет игры", show_alert=True)
        return
    new_bet = users[user_id]["last_bet"] + 1
    users[user_id]["bet"] = new_bet
    users[user_id]["last_bet"] = new_bet
    await again_game(callback)

@dp.callback_query(F.data == "dec")
async def dec_bet(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in users or "last_bet" not in users[user_id]:
        await callback.answer("Нет игры", show_alert=True)
        return
    new_bet = users[user_id]["last_bet"] - 1
    if new_bet < 0.1:
        new_bet = 0.1
    users[user_id]["bet"] = new_bet
    users[user_id]["last_bet"] = new_bet
    await again_game(callback)

@dp.callback_query(F.data == "all_in")
async def all_in(callback: types.CallbackQuery):
    # Здесь можно вставить реальный баланс, пока для демонстрации ставим 100
    user_id = callback.from_user.id
    users[user_id]["bet"] = 100
    users[user_id]["last_bet"] = 100
    await again_game(callback)

async def main():
    print("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())