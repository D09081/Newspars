import asyncio
import json
import logging
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Set

import feedparser
import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8807054442:AAHfRkSj6hI4Slwc_8qc3R48C4q1wOsk5uA"
CHANNEL_ID = -1004314624597         # ID канала (с минусом)
ADMIN_IDS = [898467551]              # Твой Telegram ID (можно несколько)

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "weather_enabled": True,
    "news_enabled": True,
    "weather_time": "08:00",
    "news_interval_minutes": 30,
    "max_news_per_check": 5,
    "rss_feeds": [
        "https://belgorod.bezformata.com/rss.xml",
        "https://openbelgorod.ru/feed",
    ],
    "stats": {
        "weather_sent": 0,
        "news_sent": 0,
        "last_weather": None,
        "last_news_check": None,
    },
    "error_log": [],
}

LAT, LON = 50.6034, 36.5809
MAX_ERROR_LOG = 50

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
admin_router = Router()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

sent_guids: Set[str] = set()
config = {}

# ================== КОНФИГ ==================
def load_config():
    global config
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in config:
                config[k] = v
    else:
        config = DEFAULT_CONFIG.copy()
        save_config()
    return config

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ================== ЛОГИРОВАНИЕ ОШИБОК ==================
def log_error(place: str, error: Exception | str, notify_admins: bool = True):
    tb = traceback.format_exc() if isinstance(error, Exception) else ""
    error_text = str(error)

    full_msg = f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {place}: {error_text}"
    if tb and "NoneType: None" not in tb:
        full_msg += f"\n{tb}"

    logger.error(full_msg)

    entry = {
        "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "place": place,
        "error": error_text[:300],
    }
    config.setdefault("error_log", [])
    config["error_log"].append(entry)
    config["error_log"] = config["error_log"][-MAX_ERROR_LOG:]
    save_config()

    if notify_admins:
        asyncio.create_task(notify_admins_about_error(place, error_text))

async def notify_admins_about_error(place: str, error_text: str):
    text = (
        f"⚠️ <b>Ошибка в боте</b>\n\n"
        f"<b>Где:</b> {place}\n"
        f"<b>Ошибка:</b> <code>{error_text[:400]}</code>\n\n"
        f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</i>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

# ================== FSM ==================
class AdminStates(StatesGroup):
    waiting_rss = State()
    waiting_weather_time = State()
    waiting_news_interval = State()

# ================== КЛАВИАТУРЫ ==================
def admin_main_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📰 Источники", callback_data="admin:sources"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")
    )
    builder.row(
        InlineKeyboardButton(text="🌤 Отправить погоду", callback_data="admin:send_weather"),
        InlineKeyboardButton(text="🔄 Проверить новости", callback_data="admin:force_news")
    )
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
        InlineKeyboardButton(text="📋 Логи ошибок", callback_data="admin:errors")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")
    )
    return builder.as_markup()

def sources_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, url in enumerate(config["rss_feeds"]):
        short = url.replace("https://", "").replace("http://", "")[:40]
        builder.row(
            InlineKeyboardButton(text=f"🗑 {short}", callback_data=f"admin:del_source:{i}")
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить источник", callback_data="admin:add_source"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back"))
    return builder.as_markup()

def settings_kb() -> InlineKeyboardMarkup:
    w_status = "✅" if config["weather_enabled"] else "❌"
    n_status = "✅" if config["news_enabled"] else "❌"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"Погода: {w_status}", callback_data="admin:toggle_weather")
    )
    builder.row(
        InlineKeyboardButton(text=f"Новости: {n_status}", callback_data="admin:toggle_news")
    )
    builder.row(
        InlineKeyboardButton(
            text=f"⏰ Время погоды: {config['weather_time']}",
            callback_data="admin:set_weather_time"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🔄 Интервал новостей: {config['news_interval_minutes']} мин",
            callback_data="admin:set_news_interval"
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back"))
    return builder.as_markup()

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]
    ])

# ================== ФИЛЬТР АДМИНА ==================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ================== ПОГОДА ==================
async def get_weather() -> str:
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
            f"&timezone=Europe%2FMoscow&forecast_days=1"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()

        current = data["current"]
        daily = data["daily"]

        codes = {
            0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅ Переменная облачность",
            3: "☁️ Пасмурно", 45: "🌫 Туман", 48: "🌫 Изморозь",
            51: "🌦 Морось", 61: "🌧 Небольшой дождь", 63: "🌧 Дождь",
            65: "🌧 Сильный дождь", 71: "❄️ Небольшой снег", 73: "❄️ Снег",
            75: "❄️ Сильный снег", 80: "🌦 Ливень", 95: "⛈ Гроза",
        }
        desc = codes.get(current["weather_code"], "Неизвестно")

        return (
            f"<b>🌤 Погода в Белгороде на сегодня</b>\n"
            f"────────────────\n"
            f"{desc}\n"
            f"🌡 Сейчас: <b>{current['temperature_2m']}°C</b> "
            f"(ощущается как {current['apparent_temperature']}°C)\n"
            f"📈 Макс: {daily['temperature_2m_max'][0]}°C  "
            f"📉 Мин: {daily['temperature_2m_min'][0]}°C\n"
            f"💧 Влажность: {current['relative_humidity_2m']}%\n"
            f"💨 Ветер: {current['wind_speed_10m']} м/с\n"
            f"🌧 Осадки за день: {daily['precipitation_sum'][0]} мм\n"
            f"────────────────\n"
            f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>"
        )
    except Exception as e:
        log_error("get_weather", e)
        raise

async def send_weather(manual: bool = False):
    if not config["weather_enabled"] and not manual:
        return
    try:
        text = await get_weather()
        await bot.send_message(CHANNEL_ID, text)
        config["stats"]["weather_sent"] += 1
        config["stats"]["last_weather"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        save_config()
        logger.info("Погода отправлена")
    except Exception as e:
        log_error("send_weather", e)

# ================== НОВОСТИ ==================
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

async def fetch_and_send_news(force: bool = False):
    if not config["news_enabled"] and not force:
        return

    global sent_guids
    new_posts = 0

    for feed_url in config["rss_feeds"]:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:config["max_news_per_check"]]:
                guid = entry.get("id") or entry.get("link") or entry.get("title")
                if not guid or guid in sent_guids:
                    continue

                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", "") or entry.get("description", ""))

                if not title:
                    continue

                if len(summary) > 800:
                    summary = summary[:800] + "…"

                text = f"<b>{title}</b>\n\n{summary}" if summary else f"<b>{title}</b>"

                try:
                    await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
                    sent_guids.add(guid)
                    new_posts += 1
                    await asyncio.sleep(1.2)
                except Exception as e:
                    log_error(f"send_news ({feed_url[:40]})", e)

        except Exception as e:
            log_error(f"RSS parse ({feed_url[:50]})", e)

    config["stats"]["news_sent"] += new_posts
    config["stats"]["last_news_check"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    save_config()

    if new_posts:
        logger.info(f"Отправлено новостей: {new_posts}")

    if len(sent_guids) > 500:
        sent_guids = set(list(sent_guids)[-300:])

# ================== ПЛАНИРОВЩИК ==================
def reschedule_jobs():
    scheduler.remove_all_jobs()

    if config["weather_enabled"]:
        h, m = map(int, config["weather_time"].split(":"))
        scheduler.add_job(send_weather, CronTrigger(hour=h, minute=m), id="weather")

    if config["news_enabled"]:
        scheduler.add_job(
            fetch_and_send_news,
            IntervalTrigger(minutes=config["news_interval_minutes"]),
            id="news"
        )

# ================== АДМИН ХЕНДЛЕРЫ ==================
@admin_router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа")
    await message.answer(
        "<b>🛠 Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_main_kb()
    )

@admin_router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "<b>🛠 Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_main_kb()
    )
    await callback.answer()

@admin_router.callback_query(F.data == "admin:close")
async def admin_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@admin_router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "<b>🛠 Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_main_kb()
    )
    await callback.answer("Отменено")

@admin_router.callback_query(F.data == "admin:sources")
async def admin_sources(callback: CallbackQuery):
    text = "<b>📰 Источники новостей</b>\n\n"
    if config["rss_feeds"]:
        for i, url in enumerate(config["rss_feeds"], 1):
            text += f"{i}. <code>{url}</code>\n"
    else:
        text += "<i>Источников нет</i>\n"
    await callback.message.edit_text(text, reply_markup=sources_kb())
    await callback.answer()

@admin_router.callback_query(F.data == "admin:add_source")
async def admin_add_source(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_rss)
    await callback.message.edit_text(
        "Отправь ссылку на RSS-ленту:\n\nПример: <code>https://example.com/rss.xml</code>",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_rss)
async def process_add_rss(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    url = message.text.strip()
    if not url.startswith("http"):
        return await message.answer("Некорректная ссылка. Попробуй ещё раз или нажми Отмена.")

    if url in config["rss_feeds"]:
        return await message.answer("Этот источник уже добавлен.")

    config["rss_feeds"].append(url)
    save_config()
    await state.clear()
    await message.answer(f"✅ Источник добавлен:\n<code>{url}</code>")
    await message.answer("<b>📰 Источники новостей</b>", reply_markup=sources_kb())

@admin_router.callback_query(F.data.startswith("admin:del_source:"))
async def admin_del_source(callback: CallbackQuery):
    idx = int(callback.data.split(":")[-1])
    if 0 <= idx < len(config["rss_feeds"]):
        removed = config["rss_feeds"].pop(idx)
        save_config()
        await callback.answer(f"Удалён: {removed[:30]}...")
    await admin_sources(callback)

@admin_router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery):
    await callback.message.edit_text("<b>⚙️ Настройки</b>", reply_markup=settings_kb())
    await callback.answer()

@admin_router.callback_query(F.data == "admin:toggle_weather")
async def toggle_weather(callback: CallbackQuery):
    config["weather_enabled"] = not config["weather_enabled"]
    save_config()
    reschedule_jobs()
    await callback.answer(f"Погода {'включена' if config['weather_enabled'] else 'выключена'}")
    await admin_settings(callback)

@admin_router.callback_query(F.data == "admin:toggle_news")
async def toggle_news(callback: CallbackQuery):
    config["news_enabled"] = not config["news_enabled"]
    save_config()
    reschedule_jobs()
    await callback.answer(f"Новости {'включены' if config['news_enabled'] else 'выключены'}")
    await admin_settings(callback)

@admin_router.callback_query(F.data == "admin:set_weather_time")
async def set_weather_time(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_weather_time)
    await callback.message.edit_text(
        "Введи новое время погоды в формате <b>ЧЧ:ММ</b>\nПример: <code>07:30</code>",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_weather_time)
async def process_weather_time(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    try:
        h, m = map(int, text.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        config["weather_time"] = f"{h:02d}:{m:02d}"
        save_config()
        reschedule_jobs()
        await state.clear()
        await message.answer(f"✅ Время погоды изменено на {config['weather_time']}")
        await message.answer("<b>⚙️ Настройки</b>", reply_markup=settings_kb())
    except Exception:
        await message.answer("Неверный формат. Пример: 08:00")

@admin_router.callback_query(F.data == "admin:set_news_interval")
async def set_news_interval(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_news_interval)
    await callback.message.edit_text(
        "Введи интервал проверки новостей в <b>минутах</b> (от 5 до 180):\nПример: <code>20</code>",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_news_interval)
async def process_news_interval(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        minutes = int(message.text.strip())
        if not 5 <= minutes <= 180:
            raise ValueError
        config["news_interval_minutes"] = minutes
        save_config()
        reschedule_jobs()
        await state.clear()
        await message.answer(f"✅ Интервал новостей: {minutes} мин")
        await message.answer("<b>⚙️ Настройки</b>", reply_markup=settings_kb())
    except Exception:
        await message.answer("Введи число от 5 до 180")

@admin_router.callback_query(F.data == "admin:send_weather")
async def admin_send_weather(callback: CallbackQuery):
    await callback.answer("Отправляю погоду...")
    await send_weather(manual=True)
    await callback.message.answer("✅ Погода отправлена в канал")

@admin_router.callback_query(F.data == "admin:force_news")
async def admin_force_news(callback: CallbackQuery):
    await callback.answer("Проверяю новости...")
    await fetch_and_send_news(force=True)
    await callback.message.answer("✅ Проверка новостей завершена")

@admin_router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    s = config["stats"]
    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"🌤 Погоды отправлено: <b>{s['weather_sent']}</b>\n"
        f"📰 Новостей отправлено: <b>{s['news_sent']}</b>\n\n"
        f"Последняя погода: {s['last_weather'] or '—'}\n"
        f"Последняя проверка новостей: {s['last_news_check'] or '—'}\n\n"
        f"Источников: {len(config['rss_feeds'])}\n"
        f"Погода: {'✅' if config['weather_enabled'] else '❌'}\n"
        f"Новости: {'✅' if config['news_enabled'] else '❌'}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]
        ])
    )
    await callback.answer()

@admin_router.callback_query(F.data == "admin:errors")
async def admin_errors(callback: CallbackQuery):
    errors = config.get("error_log", [])
    if not errors:
        text = "<b>📋 Логи ошибок</b>\n\nОшибок пока нет 🎉"
    else:
        text = "<b>📋 Последние ошибки</b>\n\n"
        for err in reversed(errors[-10:]):
            text += (
                f"<b>{err['time']}</b>\n"
                f"📍 {err['place']}\n"
                f"<code>{err['error']}</code>\n"
                f"────────────\n"
            )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Очистить логи", callback_data="admin:clear_errors")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]
        ])
    )
    await callback.answer()

@admin_router.callback_query(F.data == "admin:clear_errors")
async def admin_clear_errors(callback: CallbackQuery):
    config["error_log"] = []
    save_config()
    await callback.answer("Логи очищены")
    await admin_errors(callback)

# ================== СТАРТ ==================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "Привет, админ!\nИспользуй /admin для панели управления.",
            reply_markup=admin_main_kb()
        )
    else:
        await message.answer("Бот работает только в канале. Админ-команды недоступны.")

# ================== ЗАПУСК ==================
async def main():
    load_config()
    reschedule_jobs()
    scheduler.start()

    dp.include_router(admin_router)

    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
