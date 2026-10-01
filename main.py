# main.py
# ============================================================
# Telegram-рассыльщик для Railway (монолитный)
# - Telethon userbot: рассылка с вашего аккаунта (сессия в Volume)
# - aiogram: админ-панель + активация ключей
# - SQLite в Volume (/data)
# - HTTP healthcheck для Railway
# ============================================================

import asyncio
import logging
import os
import random
import secrets
import shutil
import string
from datetime import datetime, timedelta

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telethon import TelegramClient

# ============================================================
# КОНФИГ (из переменных Railway)
# ============================================================
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

# Файл сессии Telethon. Telethon сам добавит ".session".
# На Railway /data — это точка монтирования Volume.
SESSION_PATH = os.environ.get("SESSION_PATH", "/data/sender")

# Файл базы данных — тоже в Volume
DB_PATH = os.environ.get("DB_PATH", "/data/data.db")

# Лимиты безопасности рассылки
MAX_MESSAGES_PER_HOUR = int(os.environ.get("MAX_MESSAGES_PER_HOUR", "20"))
MIN_INTERVAL_SECONDS = int(os.environ.get("MIN_INTERVAL_SECONDS", "60"))

# Допустимые сроки ключей
ALLOWED_KEY_DAYS = {1, 2, 3, 4, 30, 360, -1}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sender")


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activation_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                days INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_by INTEGER,
                used_at TIMESTAMP,
                expires_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                activated_key TEXT,
                expires_at TIMESTAMP,
                is_active INTEGER DEFAULT 0
            )
        """)
        await db.commit()


async def db_create_key(key: str, days: int) -> bool:
    expires_at = None if days == -1 else datetime.now() + timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO activation_keys (key, days, expires_at) VALUES (?, ?, ?)",
                (key, days, expires_at),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def db_activate_key(tg_id: int, username: str, first_name: str, key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, days, used_by FROM activation_keys WHERE key = ?", (key,)
        ) as c:
            row = await c.fetchone()

        if not row:
            return False, "Ключ не найден."
        key_id, days, used_by = row
        if used_by is not None:
            return False, "Этот ключ уже был использован."

        expires_at = None if days == -1 else datetime.now() + timedelta(days=days)

        await db.execute(
            "UPDATE activation_keys SET used_by = ?, used_at = ? WHERE id = ?",
            (tg_id, datetime.now(), key_id),
        )
        await db.execute("""
            INSERT OR REPLACE INTO users
            (telegram_id, username, first_name, activated_key, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (tg_id, username, first_name, key, expires_at))
        await db.commit()

        if days == -1:
            return True, "Ключ активирован. Доступ без ограничений."
        return True, f"Ключ активирован на {days} дн. До {expires_at.strftime('%d.%m.%Y %H:%M')}."


async def db_check_access(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_active, expires_at FROM users WHERE telegram_id = ?", (tg_id,)
        ) as c:
            row = await c.fetchone()

        if not row:
            return False, "У вас нет активированного ключа."
        is_active, expires_at = row
        if not is_active:
            return False, "Доступ деактивирован."
        if expires_at is None:
            return True, "Доступ активен (без ограничений)."

        expires = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(expires_at)
        if expires < datetime.now():
            await db.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (tg_id,))
            await db.commit()
            return False, f"Срок истёк {expires.strftime('%d.%m.%Y')}."
        return True, f"Доступ до {expires.strftime('%d.%m.%Y %H:%M')}."


async def db_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async def q(sql):
            async with db.execute(sql) as c:
                return (await c.fetchone())[0]
        return {
            "total_users": await q("SELECT COUNT(*) FROM users"),
            "active_users": await q("SELECT COUNT(*) FROM users WHERE is_active = 1"),
            "used_keys": await q("SELECT COUNT(*) FROM activation_keys WHERE used_by IS NOT NULL"),
            "free_keys": await q("SELECT COUNT(*) FROM activation_keys WHERE used_by IS NULL"),
        }


# ============================================================
# TELEGRAM-КЛИЕНТ (Telethon)
# ============================================================
tg_client: TelegramClient | None = None


# ============================================================
# СОСТОЯНИЕ РАССЫЛКИ
# ============================================================
class Sender:
    is_running = False
    mode = None            # "simple" | "safe"
    messages: list[str] = []
    interval: int = 60
    targets: list[str] = []
    sent = 0
    failed = 0
    index = 0
    started_at: datetime | None = None
    task: asyncio.Task | None = None

    @classmethod
    def reset(cls):
        cls.is_running = False
        cls.mode = None
        cls.messages = []
        cls.interval = 60
        cls.targets = []
        cls.sent = 0
        cls.failed = 0
        cls.index = 0
        cls.started_at = None
        cls.task = None


async def run_sender():
    try:
        for i, target in enumerate(Sender.targets):
            if not Sender.is_running:
                break

            text = Sender.messages[0] if Sender.mode == "simple" \
                else Sender.messages[Sender.index % 3]

            try:
                await tg_client.send_message(target, text)
                Sender.sent += 1
            except Exception as e:
                Sender.failed += 1
                logger.warning(f"Send error to {target}: {e}")

            Sender.index += 1

            if i < len(Sender.targets) - 1:
                if Sender.mode == "simple":
                    delay = Sender.interval
                else:
                    delay = Sender.interval * (1 + random.uniform(-0.2, 0.2))
                await asyncio.sleep(delay)
    finally:
        Sender.is_running = False


async def start_sender(mode: str, messages: list[str], interval: int, targets: list[str]) -> str:
    if tg_client is None:
        return "Ошибка: Telethon-клиент не готов."
    if Sender.is_running:
        return "Рассылка уже запущена."
    if not targets:
        return "Список получателей пуст."
    if mode == "simple" and len(messages) < 1:
        return "Нужен хотя бы 1 текст."
    if mode == "safe" and len(messages) < 3:
        return "Безопасный режим требует 3 текста."
    if interval < MIN_INTERVAL_SECONDS:
        return f"Интервал меньше минимального ({MIN_INTERVAL_SECONDS}с)."

    msgs_per_hour = 3600 / interval
    if msgs_per_hour > MAX_MESSAGES_PER_HOUR:
        return f"Слишком быстро: {msgs_per_hour:.1f}/час > лимита {MAX_MESSAGES_PER_HOUR}/час."

    Sender.mode = mode
    Sender.messages = messages
    Sender.interval = interval
    Sender.targets = targets
    Sender.sent = 0
    Sender.failed = 0
    Sender.index = 0
    Sender.is_running = True
    Sender.started_at = datetime.now()
    Sender.task = asyncio.create_task(run_sender())

    name = "Обычная" if mode == "simple" else "Безопасная"
    return f"{name} рассылка запущена: {len(targets)} целей, интервал {interval}с."


async def stop_sender() -> str:
    if not Sender.is_running:
        return "Рассылка не запущена."
    Sender.is_running = False
    if Sender.task:
        Sender.task.cancel()
        try:
            await Sender.task
        except asyncio.CancelledError:
            pass
    return f"Остановлено. Отправлено: {Sender.sent}, ошибок: {Sender.failed}."


def sender_status() -> str:
    if not Sender.is_running:
        return "Рассылка не запущена."
    elapsed = (datetime.now() - Sender.started_at).seconds
    name = "Обычная" if Sender.mode == "simple" else "Безопасная"
    return (
        f"<b>{name} рассылка активна</b>\n"
        f"Отправлено: {Sender.sent}\n"
        f"Ошибок: {Sender.failed}\n"
        f"Прогресс: {Sender.index}/{len(Sender.targets)}\n"
        f"Время: {elapsed // 60} мин {elapsed % 60} сек"
    )


# ============================================================
# AIOGRAM: БОТ И FSM
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    key_days = State()
    simple_text = State()
    simple_interval = State()
    simple_targets = State()
    safe_t1 = State()
    safe_t2 = State()
    safe_t3 = State()
    safe_interval = State()
    safe_targets = State()


class UserStates(StatesGroup):
    waiting_key = State()


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="key")],
        [InlineKeyboardButton(text="📢 Обычная рассылка", callback_data="simple")],
        [InlineKeyboardButton(text="🛡️ Безопасная рассылка", callback_data="safe")],
        [InlineKeyboardButton(text="⏹ Стоп", callback_data="stop")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="status")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


def gen_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(3)]
    return "KEY-" + "-".join(parts)


# ============================================================
# ХЕНДЛЕРЫ: ПОЛЬЗОВАТЕЛЬ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    if is_admin(msg.from_user.id):
        await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())
        return

    ok, info = await db_check_access(msg.from_user.id)
    if ok:
        await msg.answer(f"✅ {info}\n\nБот для рассылки в разработке.")
    else:
        await msg.answer(
            "🔑 У вас нет активного доступа.\n\nОтправьте ваш ключ активации:",
            reply_markup=cancel_kb(),
        )
        await state.set_state(UserStates.waiting_key)


@dp.message(UserStates.waiting_key)
async def user_enter_key(msg: Message, state: FSMContext):
    ok, text = await db_activate_key(
        msg.from_user.id,
        msg.from_user.username or "",
        msg.from_user.first_name or "",
        msg.text.strip(),
    )
    await state.clear()
    await msg.answer(("✅ " if ok else "❌ ") + text)


# ============================================================
# ХЕНДЛЕРЫ: АДМИН
# ============================================================
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Доступ запрещён.")
        return
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())


@dp.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Отменено.")
    await call.answer()


@dp.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    s = await db_stats()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего: {s['total_users']}\n"
        f"✅ Активных: {s['active_users']}\n"
        f"🔑 Использовано ключей: {s['used_keys']}\n"
        f"🆓 Свободных ключей: {s['free_keys']}",
        parse_mode="HTML",
        reply_markup=admin_kb(),
    )
    await call.answer()


# ---------- СОЗДАНИЕ КЛЮЧА ----------
@dp.callback_query(F.data == "key")
async def cb_key(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"keydays:{d}")]
        for t, d in [("1 день", 1), ("2 дня", 2), ("3 дня", 3), ("4 дня", 4),
                     ("30 дней", 30), ("360 дней", 360), ("Без ограничений", -1)]
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])
    await call.message.edit_text("Выберите срок действия ключа:", reply_markup=kb)
    await state.set_state(AdminStates.key_days)
    await call.answer()


@dp.callback_query(F.data.startswith("keydays:"))
async def cb_keydays(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    days = int(call.data.split(":")[1])
    if days not in ALLOWED_KEY_DAYS:
        await call.answer("Недопустимый срок")
        return

    key = gen_key()
    ok = await db_create_key(key, days)
    if not ok:
        key = gen_key()
        await db_create_key(key, days)

    days_text = "без ограничений" if days == -1 else f"{days} дн."
    await state.clear()
    await call.message.edit_text(
        f"✅ Ключ создан на {days_text}:\n\n<code>{key}</code>",
        parse_mode="HTML",
        reply_markup=admin_kb(),
    )
    await call.answer()


# ---------- ОБЫЧНАЯ РАССЫЛКА ----------
@dp.callback_query(F.data == "simple")
async def cb_simple(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.simple_text)
    await call.message.edit_text(
        "📢 <b>Обычная рассылка</b>\n\nОтправьте текст сообщения:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )
    await call.answer()


@dp.message(AdminStates.simple_text)
async def admin_simple_text(msg: Message, state: FSMContext):
    await state.update_data(text=msg.text)
    await state.set_state(AdminStates.simple_interval)
    await msg.answer(f"Интервал между сообщениями в секундах (мин. {MIN_INTERVAL_SECONDS}):")


@dp.message(AdminStates.simple_interval)
async def admin_simple_interval(msg: Message, state: FSMContext):
    try:
        interval = int(msg.text)
    except ValueError:
        await msg.answer("Введите целое число.")
        return
    if interval < MIN_INTERVAL_SECONDS:
        await msg.answer(f"Минимум {MIN_INTERVAL_SECONDS}с.")
        return
    await state.update_data(interval=interval)
    await state.set_state(AdminStates.simple_targets)
    await msg.answer(
        "Получатели: username или ID, каждый с новой строки.\n"
        "Пример:\n<code>@user1\n123456789\n@user2</code>",
        parse_mode="HTML",
    )


@dp.message(AdminStates.simple_targets)
async def admin_simple_targets(msg: Message, state: FSMContext):
    data = await state.get_data()
    targets = [t.strip() for t in msg.text.splitlines() if t.strip()]
    await state.clear()
    result = await start_sender("simple", [data["text"]], data["interval"], targets)
    await msg.answer(result, reply_markup=admin_kb())


# ---------- БЕЗОПАСНАЯ РАССЫЛКА ----------
@dp.callback_query(F.data == "safe")
async def cb_safe(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.safe_t1)
    await call.message.edit_text(
        "🛡️ <b>Безопасная рассылка</b>\n\nВведите <b>текст №1</b>:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )
    await call.answer()


@dp.message(AdminStates.safe_t1)
async def admin_safe_t1(msg: Message, state: FSMContext):
    await state.update_data(t1=msg.text)
    await state.set_state(AdminStates.safe_t2)
    await msg.answer("Введите <b>текст №2</b>:", parse_mode="HTML")


@dp.message(AdminStates.safe_t2)
async def admin_safe_t2(msg: Message, state: FSMContext):
    await state.update_data(t2=msg.text)
    await state.set_state(AdminStates.safe_t3)
    await msg.answer("Введите <b>текст №3</b>:", parse_mode="HTML")


@dp.message(AdminStates.safe_t3)
async def admin_safe_t3(msg: Message, state: FSMContext):
    await state.update_data(t3=msg.text)
    await state.set_state(AdminStates.safe_interval)
    await msg.answer(f"Интервал в секундах (мин. {MIN_INTERVAL_SECONDS}, будет ±20%):")


@dp.message(AdminStates.safe_interval)
async def admin_safe_interval(msg: Message, state: FSMContext):
    try:
        interval = int(msg.text)
    except ValueError:
        await msg.answer("Введите целое число.")
        return
    if interval < MIN_INTERVAL_SECONDS:
        await msg.answer(f"Минимум {MIN_INTERVAL_SECONDS}с.")
        return
    await state.update_data(interval=interval)
    await state.set_state(AdminStates.safe_targets)
    await msg.answer("Получатели (каждый с новой строки):")


@dp.message(AdminStates.safe_targets)
async def admin_safe_targets(msg: Message, state: FSMContext):
    data = await state.get_data()
    targets = [t.strip() for t in msg.text.splitlines() if t.strip()]
    await state.clear()
    result = await start_sender(
        "safe",
        [data["t1"], data["t2"], data["t3"]],
        data["interval"],
        targets,
    )
    await msg.answer(result, reply_markup=admin_kb())


# ---------- СТОП / СТАТУС ----------
@dp.callback_query(F.data == "stop")
async def cb_stop(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    res = await stop_sender()
    await call.message.edit_text(res, reply_markup=admin_kb())
    await call.answer()


@dp.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        sender_status(), parse_mode="HTML", reply_markup=admin_kb()
    )
    await call.answer()


# ============================================================
# HTTP HEALTHCHECK ДЛЯ RAILWAY
# ============================================================
async def health(request):
    return web.Response(text="OK")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"HTTP healthcheck on :{port}")


# ============================================================
# ПОДГОТОВКА ФАЙЛА СЕССИИ
# ============================================================
def prepare_session_file():
    """
    Убеждаемся, что файл сессии лежит там, где его ждёт Telethon.
    1. Если файл уже в Volume (/data/sender.session) — используем его.
    2. Иначе — если в корне репозитория есть sender.session (залили через git),
       копируем его в Volume.
    3. Иначе — падаем с понятной ошибкой.
    """
    target = SESSION_PATH + ".session"
    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)

    if os.path.exists(target):
        logger.info(f"Session file found at {target}")
        return

    fallback = "sender.session"  # файл в корне репозитория
    if os.path.exists(fallback):
        shutil.copy(fallback, target)
        logger.info(f"Session copied from repo ({fallback}) to {target}")
        return

    logger.error(
        "Файл сессии %s не найден и нет sender.session в репозитории. "
        "Сгенерируйте его локально и положите либо в Volume, либо в корень репо.",
        target,
    )
    raise SystemExit(1)


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    global tg_client

    prepare_session_file()

    await init_db()
    logger.info(f"DB ready at {DB_PATH}")

    tg_client = TelegramClient(SESSION_PATH, TG_API_ID, TG_API_HASH)
    await tg_client.start()
    me = await tg_client.get_me()
    logger.info(f"Telethon started as @{me.username or me.id}")

    await start_http_server()

    logger.info("Starting aiogram bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
