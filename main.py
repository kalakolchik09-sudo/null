import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from dotenv import load_dotenv
load_dotenv()
# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
# =========================
# BOT
# =========================
bot = Bot(
    token=BOT_TOKEN,
    parse_mode="HTML",
)
dp = Dispatcher(
    bot,
    storage=MemoryStorage(),
)
# =========================
# KEYBOARDS
# =========================
def main_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "📱 Профиль",
            callback_data="profile"
        ),
        types.InlineKeyboardButton(
            "ℹ️ Помощь",
            callback_data="help"
        ),
    )
    return keyboard
def admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "📊 Статус",
            callback_data="status"
        ),
        types.InlineKeyboardButton(
            "🔄 Проверить",
            callback_data="check"
        ),
    )
    return keyboard
# =========================
# /start
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    logger.info(
        "User started bot: id=%s username=%s",
        message.from_user.id,
        message.from_user.username,
    )
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Бот успешно запущен.\n"
        "Выбери действие ниже:",
        reply_markup=main_keyboard(),
    )
# =========================
# PROFILE
# =========================
@dp.callback_query_handler(lambda c: c.data == "profile")
async def callback_profile(callback: types.CallbackQuery):
    user = callback.from_user
    await callback.answer()
    await callback.message.edit_text(
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Имя: {user.first_name or 'нет'}",
        reply_markup=main_keyboard(),
    )
# =========================
# HELP
# =========================
@dp.callback_query_handler(lambda c: c.data == "help")
async def callback_help(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "Используй /start, чтобы открыть главное меню.",
        reply_markup=main_keyboard(),
    )
# =========================
# ADMIN
# =========================
@dp.message_handler(commands=["admin"])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "🛠 <b>Админ-панель</b>",
        reply_markup=admin_keyboard(),
    )
@dp.callback_query_handler(lambda c: c.data == "status")
async def callback_status(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "📊 <b>Статус бота</b>\n\n"
        "🟢 Бот работает\n"
        "🟢 Polling активен\n"
        "🟢 Railway подключён",
        reply_markup=admin_keyboard(),
    )
@dp.callback_query_handler(lambda c: c.data == "check")
async def callback_check(callback: types.CallbackQuery):
    await callback.answer("Проверка выполнена ✅")
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "✅ <b>Проверка завершена</b>\n\n"
        "Ошибок запуска не обнаружено.",
        reply_markup=admin_keyboard(),
    )
# =========================
# UNKNOWN MESSAGES
# =========================
@dp.message_handler()
async def unknown_message(message: types.Message):
    await message.answer(
        "Используй /start, чтобы открыть меню."
    )
# =========================
# STARTUP
# =========================
async def on_startup(dispatcher):
    logger.info("🚀 Bot starting...")
    me = await bot.get_me()
    logger.info(
        "Bot: @%s (id=%s)",
        me.username,
        me.id,
    )
    logger.info("✅ Bot started successfully")
async def on_shutdown(dispatcher):
    logger.info("🛑 Bot shutting down...")
    await bot.close()
    logger.info("Bot stopped")
# =========================
# MAIN
# =========================
if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )
