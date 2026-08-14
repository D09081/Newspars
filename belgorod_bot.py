
---

## 2️⃣ install.sh

```bash
#!/bin/bash

set -e

echo "========================================"
echo "  Belgorod News Bot — УСТАНОВКА v5.0"
echo "  С AI-редактором (OpenRouter)"
echo "========================================"
echo

if [ "$EUID" -ne 0 ]; then
  echo "Запусти от root: sudo ./install.sh"
  exit 1
fi

# ========== ВАШИ ДАННЫЕ ==========
BOT_TOKEN="8807054442:AAHfRkSj6hI4Slwc_8qc3R48C4q1wOsk5uA"
CHANNEL_ID="-1004314624597"
ADMIN_ID="898467551"
OPENROUTER_KEY="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"
AI_MODEL="deepseek/deepseek-r1:free"
# ==================================

# ================== УДАЛЕНИЕ СТАРОГО ==================
echo "[0/8] Удаление старой версии..."
systemctl stop belgorod-bot 2>/dev/null || true
systemctl disable belgorod-bot 2>/dev/null || true
rm -f /etc/systemd/system/belgorod-bot.service
systemctl daemon-reload
rm -rf /opt/Newspars
pkill -f "python3.*bot.py" 2>/dev/null || true
screen -X -S belbot quit 2>/dev/null || true
echo "       ✅ Старое удалено."

# ================== УСТАНОВКА ==================
echo "[1/8] Обновление системы..."
apt update -y
apt install -y python3 python3-pip python3-venv git curl screen

INSTALL_DIR="/opt/Newspars"
echo "[2/8] Создаём папку $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "[3/8] Виртуальное окружение..."
python3 -m venv venv
source venv/bin/activate

echo "[4/8] Зависимости..."
pip install --upgrade pip
pip install aiogram==3.4.1 httpx apscheduler

echo "[5/8] Создаём код бота..."

cat > "$INSTALL_DIR/bot.py" << 'EOF'
import asyncio
import json
import logging
import re
import traceback
import random
import string
import signal
import os
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Set, Dict, Optional, List
import html

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    FSInputFile, InputMediaPhoto
)
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "REPLACE_BOT_TOKEN"
CHANNEL_ID = REPLACE_CHANNEL_ID
ADMIN_IDS = [REPLACE_ADMIN_ID]

# OpenRouter настройки
OPENROUTER_API_KEY = "REPLACE_OPENROUTER_KEY"
OPENROUTER_MODEL = "REPLACE_OPENROUTER_MODEL"

CONFIG_FILE = Path("config.json")
PENDING_FILE = Path("pending_news.json")
SEEN_FILE = Path("seen_posts.json")
PHOTO_CACHE_DIR = Path("photo_cache")
PHOTO_CACHE_DIR.mkdir(exist_ok=True)

# RSS-Bridge инстансы (fallback цепочка)
RSS_BRIDGE_INSTANCES = [
    "https://rss-bridge.org/bridge01",
    "https://rss-bridge.lewd.tech",
    "https://rss-bridge.se",
    "https://rss.nixnet.services",
]

# ================== КОНФИГ ==================
DEFAULT_CONFIG = {
    "weather_enabled": True,
    "news_enabled": True,
    "ai_enabled": bool(OPENROUTER_API_KEY and OPENROUTER_API_KEY != "REPLACE_OPENROUTER_KEY"),
    "ai_model": OPENROUTER_MODEL if OPENROUTER_MODEL != "REPLACE_OPENROUTER_MODEL" else "deepseek/deepseek-r1:free",
    "ai_temperature": 0.3,
    "ai_max_tokens": 500,
    "weather_time": "08:00",
    "news_interval_minutes": 30,
    "max_news_per_check": 5,
    "tg_sources": [
        {"name": "Губернатор Шуваев", "username": "shuvaev_aleksandr"},
        {"name": "Оперштаб Белгород", "username": "operstab_bel"},
        {"name": "Администрация Белгорода", "username": "beladm31"},
        {"name": "Губернатор Белгородской области", "username": "gubernator_bel"},
        {"name": "Правительство Белгородской области", "username": "belregion_ru"},
        {"name": "МЧС Белгород", "username": "mchs_bel"},
        {"name": "Минздрав Белгородской области", "username": "belzdrav31"},
    ],
    "stats": {
        "weather_sent": 0,
        "news_sent": 0,
        "news_rejected": 0,
        "ai_processed": 0,
        "ai_fallback": 0,
        "last_weather": None,
        "last_news_check": None,
    },
    "error_log": [],
    "lat": 50.6034,
    "lon": 36.5809,
    "city_name": "Белгород",
    "max_error_log": 50,
    "max_pending_days": 7,
    "notify_on_check": True,
    "timezone": "Europe/Moscow",
}

# ================== ЛОГИ ==================
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

# ================== ХРАНИЛИЩА ==================
seen_posts: Dict[str, str] = {}
sent_guids: Set[str] = set()
config: dict = {}
pending_news: Dict[str, dict] = {}

# ================== GRACEFUL SHUTDOWN ==================
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    logger.info(f"Получен сигнал {signum}, завершаю работу...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ================== AI РЕДАКТОР ==================
AI_SYSTEM_PROMPT = """Ты — профессиональный редактор новостей для Telegram-канала. Твоя задача — переписывать новости в нейтрально-информативном стиле.

Правила:
1. Сохраняй все факты: даты, ФИО, адреса, цифры, названия организаций
2. Убирай эмоциональную окраску, оценочные суждения
3. Избавляйся от "воды", канцеляризмов, повторов
4. Сокращай текст до лаконичного формата (300-500 символов)
5. Используй простые, понятные предложения
6. Структурируй: сначала самое важное, затем детали
7. Не добавляй от себя никаких комментариев
8. Сохраняй ссылки и упоминания источников

Ответ должен быть только переработанным текстом новости, без пояснений и вступлений."""

async def ai_rewrite_news(text: str) -> Optional[str]:
    """Переписывает новость через OpenRouter API."""
    if not config.get("ai_enabled", False):
        return None
    
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "REPLACE_OPENROUTER_KEY":
        return None
    
    try:
        model = config.get("ai_model", "deepseek/deepseek-r1:free")
        temperature = config.get("ai_temperature", 0.3)
        max_tokens = config.get("ai_max_tokens", 500)
        
        input_text = text[:3000] if len(text) > 3000 else text
        
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://t.me/belgorod_news_bot",
                    "X-Title": "Belgorod News Bot",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": AI_SYSTEM_PROMPT},
                        {"role": "user", "content": f"Перепиши эту новость:\n\n{input_text}"}
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
            )
            response.raise_for_status()
            data = response.json()
            
            if "choices" in data and len(data["choices"]) > 0:
                rewritten = data["choices"][0]["message"]["content"].strip()
                if rewritten and len(rewritten) > 10:
                    logger.info(f"✅ AI обработал новость: {len(rewritten)} символов")
                    config["stats"]["ai_processed"] = config["stats"].get("ai_processed", 0) + 1
                    save_config()
                    return rewritten
        
        config["stats"]["ai_fallback"] = config["stats"].get("ai_fallback", 0) + 1
        save_config()
        return None
        
    except Exception as e:
        logger.error(f"❌ Ошибка AI: {e}")
        config["stats"]["ai_fallback"] = config["stats"].get("ai_fallback", 0) + 1
        save_config()
        return None

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
    global config
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# ================== SEEN POSTS ==================
def load_seen():
    global seen_posts
    if SEEN_FILE.exists():
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            seen_posts = json.load(f)
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        seen_posts = {k: v for k, v in seen_posts.items() if v > cutoff}
        save_seen()

def save_seen():
    global seen_posts
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_posts, f, ensure_ascii=False, indent=2)

def is_seen(username: str, msg_id: int) -> bool:
    key = f"{username}_{msg_id}"
    return key in seen_posts

def mark_seen(username: str, msg_id: int):
    key = f"{username}_{msg_id}"
    seen_posts[key] = datetime.now().isoformat()
    save_seen()

# ================== PENDING ==================
def load_pending():
    global pending_news
    if PENDING_FILE.exists():
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            cutoff = (datetime.now() - timedelta(days=config.get("max_pending_days", 7))).isoformat()
            pending_news = {
                k: v for k, v in data.items()
                if v.get("time", "") > cutoff
            }
            if len(pending_news) != len(data):
                save_pending()

def save_pending():
    global pending_news
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending_news, f, ensure_ascii=False, indent=2)

# ================== ОШИБКИ ==================
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
    max_log = config.get("max_error_log", 50)
    config["error_log"] = config["error_log"][-max_log:]
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
    menu = State()
    sources = State()
    settings = State()
    weather_menu = State()
    system_menu = State()
    geo_menu = State()
    pending_menu = State()
    del_source = State()
    waiting_source_name = State()
    waiting_source_username = State()
    waiting_weather_time = State()
    waiting_news_interval = State()
    waiting_max_news = State()
    waiting_lat = State()
    waiting_lon = State()
    waiting_city = State()
    waiting_edit_text = State()
    ai_settings = State()
    waiting_ai_model = State()
    waiting_ai_temp = State()

# ================== КЛАВИАТУРЫ ==================
def main_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="📰 Источники"), KeyboardButton(text="⚙️ Настройки"))
    b.row(KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🔄 Проверить новости"))
    b.row(KeyboardButton(text="⏳ Модерация"), KeyboardButton(text="📊 Статистика"))
    b.row(KeyboardButton(text="📋 Логи"), KeyboardButton(text="🔧 Система"))
    b.row(KeyboardButton(text="❌ Закрыть меню"))
    return b.as_markup(resize_keyboard=True)

def cancel_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="❌ Отмена"))
    return b.as_markup(resize_keyboard=True)

def weather_menu_kb() -> ReplyKeyboardMarkup:
    w = "✅ Вкл" if config.get("weather_enabled", True) else "❌ Выкл"
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"Погода: {w}"), KeyboardButton(text="⏰ Время погоды"))
    b.row(KeyboardButton(text="🌍 Геолокация"), KeyboardButton(text="🚀 Отправить сейчас"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def geo_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"Широта: {config.get('lat', 50.6034)}"), KeyboardButton(text=f"Долгота: {config.get('lon', 36.5809)}"))
    b.row(KeyboardButton(text=f"Город: {config.get('city_name', 'Белгород')}"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def system_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="📁 Логи бота"), KeyboardButton(text="🧹 Очистить историю"))
    b.row(KeyboardButton(text="📈 Экспорт статистики"), KeyboardButton(text="🧠 AI-редактор"))
    b.row(KeyboardButton(text="🔄 Перезапустить бота"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def ai_settings_kb() -> ReplyKeyboardMarkup:
    status = "✅ Вкл" if config.get("ai_enabled", False) else "❌ Выкл"
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"AI: {status}"))
    b.row(KeyboardButton(text=f"Модель: {config.get('ai_model', 'deepseek/deepseek-r1:free')}"))
    b.row(KeyboardButton(text=f"Температура: {config.get('ai_temperature', 0.3)}"))
    b.row(KeyboardButton(text="🧪 Тест AI (переписать одну новость)"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def pending_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="✅ Одобрить все"), KeyboardButton(text="❌ Отклонить все"))
    b.row(KeyboardButton(text="🔄 Обновить список"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def settings_menu_kb() -> ReplyKeyboardMarkup:
    n = "✅ Вкл" if config.get("news_enabled", True) else "❌ Выкл"
    notif = "✅ Вкл" if config.get("notify_on_check", True) else "❌ Выкл"
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"Новости: {n}"), KeyboardButton(text=f"🔔 Уведомления: {notif}"))
    b.row(KeyboardButton(text=f"🔄 Интервал: {config.get('news_interval_minutes', 30)} мин"))
    b.row(KeyboardButton(text=f"📰 Лимит за раз: {config.get('max_news_per_check', 5)}"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def sources_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="➕ Добавить источник"))
    b.row(KeyboardButton(text="🗑 Удалить источник"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def del_source_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    sources = config.get("tg_sources", [])
    for i, src in enumerate(sources):
        b.row(KeyboardButton(text=f"🗑 {i+1}. {src['name']} (@{src['username']})"))
    b.row(KeyboardButton(text="❌ Отмена"))
    return b.as_markup(resize_keyboard=True)

def edit_kb(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{short_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{short_id}")
        ],
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{short_id}"),
            InlineKeyboardButton(text="🤖 AI-переписать", callback_data=f"ai_rewrite:{short_id}")
        ]
    ])

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def generate_short_id() -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

# ================== RSS-BRIDGE ПАРСИНГ ==================
def clean_photo_url(url: str) -> str:
    if url.startswith("'") and url.endswith("'"):
        url = url[1:-1]
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r'\?w=\d+(&h=\d+)?', '', url)
    url = re.sub(r'[?&]w=\d+', '', url)
    url = re.sub(r'\?$', '', url)
    return url

def extract_photos_from_html(html_text: str) -> List[str]:
    photos = []
    
    data_src_matches = re.findall(r'<img[^>]*data-src="([^"]+)"', html_text)
    for url in data_src_matches:
        if "telegram.org/img/emoji" in url or "favicon" in url.lower():
            continue
        url = clean_photo_url(url)
        if url and url not in photos:
            photos.append(url)
    
    img_matches = re.findall(r'<img[^>]*src="([^"]+)"', html_text)
    for url in img_matches:
        if "telegram.org/img/emoji" in url or "favicon" in url.lower():
            continue
        url = clean_photo_url(url)
        if url and url not in photos:
            photos.append(url)
    
    poster_matches = re.findall(r'<video[^>]*poster="([^"]+)"', html_text)
    for url in poster_matches:
        url = clean_photo_url(url)
        if url and url not in photos:
            photos.append(url)
    
    a_href_matches = re.findall(r'<a[^>]*href="([^"]+\.(?:jpg|jpeg|png|webp|gif))[^"]*"', html_text, re.IGNORECASE)
    for url in a_href_matches:
        url = clean_photo_url(url)
        if url and url not in photos:
            photos.append(url)
    
    return photos

def extract_text_from_html(html_text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', html_text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    text = re.sub(r'^RSS-Bridge was unable[^\n]*\n*', '', text)
    text = re.sub(r'VIEW IN TELEGRAM', '', text)
    text = re.sub(r'Media is too big', '', text)
    text = text.strip()
    return text

async def fetch_rss_bridge(username: str, instance: str) -> List[dict]:
    url = f"{instance}/?action=display&bridge=Telegram&username={username}&format=Json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    
    posts = []
    for item in data.get("items", []):
        msg_url = item.get("url", "")
        msg_id_match = re.search(r'/(\d{1,20})$', msg_url)
        msg_id = int(msg_id_match.group(1)) if msg_id_match else 0
        
        if msg_id == 0:
            continue
        
        if is_seen(username, msg_id):
            continue
        
        content_html = item.get("content_html", "")
        text = extract_text_from_html(content_html)
        if len(text) < 10:
            mark_seen(username, msg_id)
            continue
        
        if any(w in text.lower() for w in ['погода', 'температура', 'ветер', 'влажность', 'прогноз погоды']):
            mark_seen(username, msg_id)
            continue
        
        photos = extract_photos_from_html(content_html)
        photo = photos[0] if photos else None
        
        posts.append({
            "id": f"{username}_{msg_id}",
            "text": text,
            "photo": photo,
            "photos": photos[:10],
            "source": username,
            "link": msg_url,
            "msg_id": msg_id,
        })
        
        mark_seen(username, msg_id)
    
    return posts

async def fetch_tg_channel(username: str) -> List[dict]:
    last_error = None
    for instance in RSS_BRIDGE_INSTANCES:
        try:
            posts = await fetch_rss_bridge(username, instance)
            if posts:
                logger.info(f"[{instance}] Получено {len(posts)} постов из @{username}")
            return posts
        except Exception as e:
            last_error = e
            logger.warning(f"[{instance}] Не удалось получить @{username}: {e}")
            continue
    
    log_error(f"RSS-Bridge все инстансы для @{username}", last_error or "Все инстансы недоступны", notify_admins=False)
    return []

async def fetch_all_tg_sources() -> List[dict]:
    all_posts = []
    for source in config.get("tg_sources", []):
        posts = await fetch_tg_channel(source["username"])
        for post in posts:
            post["source_name"] = source["name"]
        all_posts.extend(posts)
        await asyncio.sleep(2)
    return all_posts

# ================== ПОГОДА ==================
async def get_weather() -> str:
    try:
        lat = config.get("lat", 50.6034)
        lon = config.get("lon", 36.5809)
        city = config.get("city_name", "Белгород")
        
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,pressure_msl&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe/Moscow&forecast_days=1"
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        
        cur = data["current"]
        day = data["daily"]
        
        weather_codes = {
            0: ("☀️", "Ясно"), 1: ("🌤", "Малооблачно"), 2: ("⛅", "Переменная облачность"),
            3: ("☁️", "Пасмурно"), 45: ("🌫", "Туман"), 48: ("🌫", "Изморозь"),
            51: ("🌦", "Морось"), 53: ("🌦", "Морось"), 55: ("🌧", "Сильная морось"),
            61: ("🌧", "Небольшой дождь"), 63: ("🌧", "Дождь"), 65: ("🌧", "Сильный дождь"),
            71: ("❄️", "Снегопад"), 73: ("❄️", "Снегопад"), 75: ("❄️", "Сильный снегопад"),
            80: ("🌦", "Ливень"), 81: ("🌦", "Ливень"), 82: ("🌧", "Сильный ливень"),
            95: ("⛈", "Гроза"), 96: ("⛈", "Гроза"), 99: ("⛈", "Гроза"),
        }
        
        code = cur.get("weather_code", 0)
        emoji, desc = weather_codes.get(code, ("❓", "Неизвестно"))
        
        temp = cur.get("temperature_2m", 0)
        feels = cur.get("apparent_temperature", 0)
        humidity = cur.get("relative_humidity_2m", 0)
        wind = cur.get("wind_speed_10m", 0)
        pressure = cur.get("pressure_msl", 0)
        max_temp = day.get("temperature_2m_max", [0])[0]
        min_temp = day.get("temperature_2m_min", [0])[0]
        precip = day.get("precipitation_sum", [0])[0]
        
        if temp <= -15: temp_icon = "🥶"
        elif temp <= -5: temp_icon = "❄️"
        elif temp <= 0: temp_icon = "🧊"
        elif temp <= 10: temp_icon = "🌡"
        elif temp <= 20: temp_icon = "☀️"
        elif temp <= 30: temp_icon = "🔥"
        else: temp_icon = "🥵"
        
        text = f"""<b>🌤 Погода в {city}е</b>

{emoji} {desc}

{temp_icon} <b>Температура:</b> {temp:.1f}°C
<b>Ощущается как:</b> {feels:.1f}°C
<b>Макс:</b> {max_temp:.1f}°C  <b>Мин:</b> {min_temp:.1f}°C

<b>💧 Влажность:</b> {humidity:.0f}%
<b>💨 Ветер:</b> {wind:.1f} м/с
<b>🌡 Давление:</b> {pressure:.0f} гПа"""

        if precip and precip > 0:
            text += f"\n<b>🌧 Осадки:</b> {precip:.1f} мм"
        
        return text
        
    except Exception as e:
        log_error("get_weather", e)
        raise

async def send_weather(manual: bool = False):
    if not config.get("weather_enabled", True) and not manual:
        return
    try:
        text = await get_weather()
        await bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        config["stats"]["weather_sent"] = config["stats"].get("weather_sent", 0) + 1
        config["stats"]["last_weather"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        save_config()
        logger.info("Погода отправлена")
    except Exception as e:
        log_error("send_weather", e)

# ================== МОДЕРАЦИЯ ==================
def format_post_text(post: dict, for_channel: bool = False) -> str:
    text = post.get("text", "")
    source_name = post.get("source_name", "")
    source_username = post.get("source", "")
    link = post.get("link", "")
    
    if for_channel:
        source_line = f"\n\n— <a href='{link}'>{source_name}</a>" if source_name else ""
        max_text = 1000 - len(source_line)
        if len(text) > max_text:
            text = text[:max_text] + "..."
        return text + source_line
    else:
        header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
        return header + text

async def download_photo(url: str) -> Optional[str]:
    if not url:
        return None
    
    try:
        filename = f"photo_{abs(hash(url)) % 100000000:09d}.jpg"
        local_path = PHOTO_CACHE_DIR / filename
        
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
        
        return str(local_path)
    except Exception as e:
        logger.warning(f"Не удалось скачать фото {url[:60]}: {e}")
        return None

async def send_to_moderation(post: dict) -> Optional[str]:
    text = format_post_text(post, for_channel=False)
    photo_urls = post.get("photos", [])[:10]
    
    original_text = text
    if config.get("ai_enabled", False):
        ai_text = await ai_rewrite_news(post.get("text", ""))
        if ai_text:
            source_name = post.get("source_name", "")
            source_username = post.get("source", "")
            text = f"📰 <b>{source_name}</b> (@{source_username})\n\n{ai_text}"
            post["ai_processed"] = True
            post["original_text"] = post.get("text", "")
            post["text"] = ai_text
            logger.info(f"✅ AI переписал новость от {source_name}")
        else:
            post["ai_processed"] = False
            text = original_text
    
    photo_paths = []
    for url in photo_urls:
        path = await download_photo(url)
        if path:
            photo_paths.append(path)
    
    short_id = generate_short_id()
    
    for admin_id in ADMIN_IDS:
        try:
            if len(photo_paths) > 1:
                media = []
                for i, path in enumerate(photo_paths):
                    if i == 0:
                        media.append(InputMediaPhoto(
                            media=FSInputFile(path),
                            caption=text[:1024],
                            parse_mode="HTML"
                        ))
                    else:
                        media.append(InputMediaPhoto(media=FSInputFile(path)))
                
                msgs = await bot.send_media_group(admin_id, media=media)
                
                ai_note = " 🤖 AI" if post.get("ai_processed", False) else ""
                kb_msg = await bot.send_message(
                    admin_id,
                    f"👆 <b>Модерация:</b> {post.get('source_name', '?')}{ai_note}\n"
                    f"📷 Фото: {len(photo_paths)} | 📝 Текст выше",
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
                msg_id = kb_msg.message_id
                chat_id = kb_msg.chat.id
                
            elif len(photo_paths) == 1:
                photo_file = FSInputFile(photo_paths[0])
                msg = await bot.send_photo(
                    admin_id,
                    photo=photo_file,
                    caption=text[:1024],
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
                msg_id = msg.message_id
                chat_id = msg.chat.id
            else:
                msg = await bot.send_message(
                    admin_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=edit_kb(short_id)
                )
                msg_id = msg.message_id
                chat_id = msg.chat.id
            
            pending_news[short_id] = {
                "guid": post["id"],
                "text": post.get("text", ""),
                "original_text": post.get("original_text", ""),
                "ai_processed": post.get("ai_processed", False),
                "photos": photo_paths,
                "photo_urls": photo_urls,
                "source": post.get("source_name", ""),
                "source_username": post.get("source", ""),
                "link": post.get("link", ""),
                "admin_id": admin_id,
                "time": datetime.now().isoformat(),
                "edited_text": None,
                "message_id": msg_id,
                "chat_id": chat_id,
                "is_album": len(photo_paths) > 1,
            }
            save_pending()
            
            logger.info(f"На модерацию: {post.get('text', '')[:50]}... [фото: {len(photo_paths)}]")
            return short_id
            
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id}: {e}")
    
    return None

async def approve_news(short_id: str):
    global sent_guids
    if short_id not in pending_news:
        return False
    
    item = pending_news[short_id]
    try:
        text = item.get("edited_text") or item.get("text", "")
        photo_paths = item.get("photos", [])
        source_name = item.get("source", "")
        link = item.get("link", "")
        
        source_line = f"\n\n— <a href='{link}'>{source_name}</a>" if source_name else ""
        caption_text = text[:1000] + "..." if len(text) > 1000 else text
        channel_text = caption_text + source_line
        
        existing_paths = [p for p in photo_paths if os.path.exists(p)]
        
        if len(existing_paths) > 1:
            media = []
            for i, path in enumerate(existing_paths):
                if i == 0:
                    media.append(InputMediaPhoto(
                        media=FSInputFile(path),
                        caption=channel_text,
                        parse_mode="HTML"
                    ))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(path)))
            
            await bot.send_media_group(CHANNEL_ID, media=media)
            logger.info(f"Одобрен альбом: {source_name} [{len(existing_paths)} фото]")
                
        elif len(existing_paths) == 1:
            photo_file = FSInputFile(existing_paths[0])
            await bot.send_photo(CHANNEL_ID, photo=photo_file, caption=channel_text, parse_mode="HTML")
            logger.info(f"Одобрено фото: {source_name}")
        else:
            await bot.send_message(CHANNEL_ID, channel_text, parse_mode="HTML", disable_web_page_preview=True)
            logger.info(f"Одобрен текст: {source_name}")
        
        sent_guids.add(item["guid"])
        config["stats"]["news_sent"] = config["stats"].get("news_sent", 0) + 1
        save_config()
        
        for path in photo_paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass
        
        del pending_news[short_id]
        save_pending()
        return True
        
    except Exception as e:
        log_error("approve_news", e)
        return False

async def reject_news(short_id: str):
    global pending_news
    if short_id not in pending_news:
        return False
    
    item = pending_news.pop(short_id)
    save_pending()
    
    photo_paths = item.get("photos", [])
    for path in photo_paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
    
    config["stats"]["news_rejected"] = config["stats"].get("news_rejected", 0) + 1
    save_config()
    logger.info(f"Отклонено: {item.get('source', '?')} [фото: {len(photo_paths)}]")
    return True

# ================== ПРОВЕРКА НОВОСТЕЙ ==================
async def fetch_and_send_news(force: bool = False):
    global sent_guids
    if not config.get("news_enabled", True) and not force:
        return 0

    new_posts = 0
    posts = await fetch_all_tg_sources()
    
    if not posts:
        config["stats"]["last_news_check"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        save_config()
        return 0

    for post in posts:
        if new_posts >= config.get("max_news_per_check", 5):
            break
        
        if post["id"] in sent_guids:
            continue
        
        short_id = await send_to_moderation(post)
        if short_id:
            new_posts += 1
            await asyncio.sleep(1)

    config["stats"]["last_news_check"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    save_config()

    if new_posts > 0 and config.get("notify_on_check", True):
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"📬 На модерацию: <b>{new_posts}</b>\nВ очереди: <b>{len(pending_news)}</b>",
                    parse_mode="HTML",
                    reply_markup=main_menu_kb()
                )
            except Exception:
                pass
    
    return new_posts

# ================== ПЛАНИРОВЩИК ==================
def reschedule_jobs():
    scheduler.remove_all_jobs()
    if config.get("weather_enabled", True):
        h, m = map(int, config["weather_time"].split(":"))
        scheduler.add_job(send_weather, CronTrigger(hour=h, minute=m), id="weather")
    if config.get("news_enabled", True):
        scheduler.add_job(
            fetch_and_send_news,
            IntervalTrigger(minutes=config.get("news_interval_minutes", 30)),
            id="news"
        )

# ================== INLINE HANDLERS ==================
@admin_router.callback_query(F.data.startswith("approve:"))
async def mod_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    short_id = callback.data.split(":")[-1]
    if await approve_news(short_id):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("✅ ОДОБРЕНО")
        await callback.answer("Опубликовано!")
    else:
        await callback.answer("Ошибка", show_alert=True)

@admin_router.callback_query(F.data.startswith("reject:"))
async def mod_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    short_id = callback.data.split(":")[-1]
    if await reject_news(short_id):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ ОТКЛОНЕНО")
        await callback.answer("Отклонено")
    else:
        await callback.answer("Ошибка", show_alert=True)

@admin_router.callback_query(F.data.startswith("edit:"))
async def mod_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    
    short_id = callback.data.split(":")[-1]
    if short_id not in pending_news:
        await callback.answer("Новость не найдена", show_alert=True)
        return
    
    await state.update_data(edit_short_id=short_id)
    await state.set_state(AdminStates.waiting_edit_text)
    
    item = pending_news[short_id]
    current_text = item.get("edited_text") or item.get("text", "")
    photo_count = len(item.get("photos", []))
    album_note = f"\n📷 Фото в посте: {photo_count}" if photo_count > 0 else ""
    
    await callback.message.answer(
        f"✏️ <b>Редактирование</b>{album_note}\n\n"
        f"📰 Источник: {item.get('source', '?')}\n\n"
        f"Текущий текст:\n<code>{current_text[:800]}</code>\n\n"
        f"Введите новый текст:",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("ai_rewrite:"))
async def mod_ai_rewrite(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    
    short_id = callback.data.split(":")[-1]
    if short_id not in pending_news:
        await callback.answer("Новость не найдена", show_alert=True)
        return
    
    if not config.get("ai_enabled", False):
        await callback.answer("⚠️ AI выключен", show_alert=True)
        return
    
    item = pending_news[short_id]
    original_text = item.get("original_text") or item.get("text", "")
    
    await callback.answer("🤖 Переписываю...")
    
    ai_text = await ai_rewrite_news(original_text)
    if ai_text:
        item["text"] = ai_text
        item["ai_processed"] = True
        item["edited_text"] = None
        save_pending()
        
        try:
            source_name = item.get("source", "")
            source_username = item.get("source_username", "")
            header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
            full_text = header + ai_text
            
            photo_paths = item.get("photos", [])
            
            if len(photo_paths) > 1:
                await bot.delete_message(chat_id=item["chat_id"], message_id=item["message_id"])
                
                media = []
                for i, path in enumerate(photo_paths):
                    if not os.path.exists(path):
                        continue
                    if i == 0:
                        media.append(InputMediaPhoto(
                            media=FSInputFile(path),
                            caption=full_text[:1024],
                            parse_mode="HTML"
                        ))
                    else:
                        media.append(InputMediaPhoto(media=FSInputFile(path)))
                
                if media:
                    msgs = await bot.send_media_group(item["chat_id"], media=media)
                    kb_msg = await bot.send_message(
                        item["chat_id"],
                        f"👆 <b>Модерация:</b> {source_name} 🤖 AI\n"
                        f"📷 Фото: {len(photo_paths)}",
                        parse_mode="HTML",
                        reply_markup=edit_kb(short_id)
                    )
                    item["message_id"] = kb_msg.message_id
                    save_pending()
                    
            elif len(photo_paths) == 1 and os.path.exists(photo_paths[0]):
                photo_file = FSInputFile(photo_paths[0])
                await bot.edit_message_caption(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    caption=full_text[:1024],
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
            else:
                await bot.edit_message_text(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    text=full_text,
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
            
            await callback.message.reply("✅ AI переписал новость!")
            
        except Exception as e:
            logger.error(f"Ошибка обновления AI: {e}")
            await callback.message.reply(f"❌ Ошибка: {e}")
    else:
        await callback.message.reply("⚠️ Не удалось переписать (AI вернул пустой ответ)")

@admin_router.message(AdminStates.waiting_edit_text)
async def process_edit_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    data = await state.get_data()
    short_id = data.get("edit_short_id")
    
    if not short_id or short_id not in pending_news:
        await message.answer("❌ Ошибка: новость не найдена", reply_markup=main_menu_kb())
        await state.clear()
        return
    
    new_text = message.text.strip()
    if not new_text:
        await message.answer("❌ Текст не может быть пустым", reply_markup=cancel_kb())
        return
    
    pending_news[short_id]["edited_text"] = new_text
    pending_news[short_id]["ai_processed"] = False
    save_pending()
    
    item = pending_news[short_id]
    photo_paths = item.get("photos", [])
    
    try:
        source_name = item.get("source", "")
        source_username = item.get("source_username", "")
        header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
        full_text = header + new_text
        
        if len(photo_paths) > 1:
            await bot.delete_message(chat_id=item["chat_id"], message_id=item["message_id"])
            
            media = []
            for i, path in enumerate(photo_paths):
                if not os.path.exists(path):
                    continue
                if i == 0:
                    media.append(InputMediaPhoto(
                        media=FSInputFile(path),
                        caption=full_text[:1024],
                        parse_mode="HTML"
                    ))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(path)))
            
            if media:
                msgs = await bot.send_media_group(item["chat_id"], media=media)
                kb_msg = await bot.send_message(
                    item["chat_id"],
                    f"👆 <b>Модерация:</b> {source_name}\n"
                    f"📷 Фото: {len(photo_paths)} | 📝 Текст обновлён (ручное редактирование)",
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
                item["message_id"] = kb_msg.message_id
                save_pending()
                
        elif len(photo_paths) == 1 and os.path.exists(photo_paths[0]):
            photo_file = FSInputFile(photo_paths[0])
            await bot.edit_message_caption(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                caption=full_text[:1024],
                parse_mode="HTML",
                reply_markup=edit_kb(short_id)
            )
        else:
            await bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                text=full_text,
                parse_mode="HTML",
                reply_markup=edit_kb(short_id)
            )
        
        await message.answer("✅ Текст обновлён", reply_markup=main_menu_kb())
    except Exception as e:
        logger.error(f"Ошибка обновления: {e}")
        await message.answer(f"❌ Ошибка при обновлении: {e}", reply_markup=main_menu_kb())
    
    await state.clear()

# ================== ГЛАВНОЕ МЕНЮ ==================
@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа")
    await state.set_state(AdminStates.menu)
    await message.answer("🛠 Админ-панель", reply_markup=main_menu_kb())

@admin_router.message(F.text == "◀️ Назад")
async def go_back(message: Message, state: FSMContext):
    await state.set_state(AdminStates.menu)
    await message.answer("🛠 Админ-панель", reply_markup=main_menu_kb())

@admin_router.message(F.text == "❌ Закрыть меню", AdminStates.menu)
async def menu_close(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Меню закрыто. Напиши /admin", reply_markup=ReplyKeyboardRemove())

@admin_router.message(F.text == "🔄 Проверить новости")
async def menu_force_news(message: Message):
    await message.answer("🔍 Проверяю новости...", reply_markup=main_menu_kb())
    try:
        count = await fetch_and_send_news(force=True)
        if count > 0:
            await message.answer(f"✅ Найдено {count} новостей", reply_markup=main_menu_kb())
        else:
            await message.answer("✅ Новых новостей нет", reply_markup=main_menu_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:200]}", reply_markup=main_menu_kb())

# === Источники ===
@admin_router.message(F.text == "📰 Источники", AdminStates.menu)
async def menu_sources(message: Message, state: FSMContext):
    await state.set_state(AdminStates.sources)
    text = "📰 Источники\n\n"
    for i, src in enumerate(config.get("tg_sources", []), 1):
        text += f"{i}. {src['name']}\n   @{src['username']}\n\n"
    await message.answer(text, reply_markup=sources_menu_kb())

@admin_router.message(F.text == "➕ Добавить источник", AdminStates.sources)
async def add_source_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_source_name)
    await message.answer("Введите название:", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_source_name)
async def process_source_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым:", reply_markup=cancel_kb())
        return
    await state.update_data(source_name=name)
    await state.set_state(AdminStates.waiting_source_username)
    await message.answer(f"Название: {name}\n\nВведите username (без @):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_source_username)
async def process_source_username(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    username = message.text.strip().replace("@", "")
    if not username:
        await message.answer("Username не может быть пустым:", reply_markup=cancel_kb())
        return
    
    data = await state.get_data()
    name = data.get("source_name", "Без названия")
    
    for src in config.get("tg_sources", []):
        if src["username"].lower() == username.lower():
            await state.set_state(AdminStates.sources)
            await message.answer(f"❌ @{username} уже есть!", reply_markup=sources_menu_kb())
            return
    
    config.setdefault("tg_sources", []).append({"name": name, "username": username})
    save_config()
    await state.set_state(AdminStates.sources)
    await message.answer(f"✅ Добавлен: {name} (@{username})", reply_markup=sources_menu_kb())

@admin_router.message(F.text == "🗑 Удалить источник", AdminStates.sources)
async def del_source_start(message: Message, state: FSMContext):
    sources = config.get("tg_sources", [])
    if not sources:
        await message.answer("❌ Список пуст!", reply_markup=sources_menu_kb())
        return
    await state.set_state(AdminStates.del_source)
    text = "🗑 Удаление\n\n"
    for i, src in enumerate(sources, 1):
        text += f"{i}. {src['name']} (@{src['username']})\n"
    await message.answer(text, reply_markup=del_source_kb())

@admin_router.message(AdminStates.del_source)
async def process_del_source(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    
    if text == "❌ Отмена":
        await state.set_state(AdminStates.sources)
        await message.answer("❌ Отменено.", reply_markup=sources_menu_kb())
        return
    
    match = re.search(r'🗑\s*(\d+)\.', text)
    if not match:
        await message.answer("❌ Выберите кнопкой:", reply_markup=del_source_kb())
        return
    
    idx = int(match.group(1)) - 1
    sources = config.get("tg_sources", [])
    
    if idx < 0 or idx >= len(sources):
        await message.answer("❌ Неверный номер:", reply_markup=del_source_kb())
        return
    
    removed = sources.pop(idx)
    save_config()
    await state.set_state(AdminStates.sources)
    await message.answer(f"✅ Удалён: {removed['name']}", reply_markup=sources_menu_kb())

# === Настройки ===
@admin_router.message(F.text == "⚙️ Настройки", AdminStates.menu)
async def menu_settings(message: Message, state: FSMContext):
    await state.set_state(AdminStates.settings)
    await message.answer("⚙️ Настройки", reply_markup=settings_menu_kb())

@admin_router.message(F.text.startswith("Новости:"), AdminStates.settings)
async def toggle_news(message: Message):
    config["news_enabled"] = not config.get("news_enabled", True)
    save_config()
    reschedule_jobs()
    await message.answer(f"{'✅' if config['news_enabled'] else '❌'} Новости {'вкл' if config['news_enabled'] else 'выкл'}", reply_markup=settings_menu_kb())

@admin_router.message(F.text.startswith("🔔 Уведомления:"), AdminStates.settings)
async def toggle_notify(message: Message):
    config["notify_on_check"] = not config.get("notify_on_check", True)
    save_config()
    await message.answer(f"{'✅' if config['notify_on_check'] else '❌'} Уведомления {'вкл' if config['notify_on_check'] else 'выкл'}", reply_markup=settings_menu_kb())

@admin_router.message(F.text.startswith("🔄 Интервал:"), AdminStates.settings)
async def set_interval(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_news_interval)
    await message.answer("Введите интервал в минутах (5-180):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_news_interval)
async def process_interval(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        minutes = int(message.text.strip())
        if not 5 <= minutes <= 180:
            raise ValueError
        config["news_interval_minutes"] = minutes
        save_config()
        reschedule_jobs()
        await state.set_state(AdminStates.settings)
        await message.answer(f"✅ Интервал: {minutes} мин", reply_markup=settings_menu_kb())
    except Exception:
        await message.answer("❌ Число от 5 до 180:", reply_markup=cancel_kb())

@admin_router.message(F.text.startswith("📰 Лимит за раз:"), AdminStates.settings)
async def set_max_news(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_max_news)
    await message.answer("Введите лимит (1-20):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_max_news)
async def process_max_news(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        n = int(message.text.strip())
        if not 1 <= n <= 20:
            raise ValueError
        config["max_news_per_check"] = n
        save_config()
        await state.set_state(AdminStates.settings)
        await message.answer(f"✅ Лимит: {n}", reply_markup=settings_menu_kb())
    except Exception:
        await message.answer("❌ Число от 1 до 20:", reply_markup=cancel_kb())

# === AI Настройки ===
@admin_router.message(F.text == "🧠 AI-редактор", AdminStates.system_menu)
async def menu_ai_settings(message: Message, state: FSMContext):
    await state.set_state(AdminStates.ai_settings)
    status_text = "✅ Включён" if config.get("ai_enabled", False) else "❌ Выключен"
    stats = config.get("stats", {})
    
    text = f"""🧠 <b>AI-редактор</b>

Статус: {status_text}
Модель: {config.get('ai_model', 'deepseek/deepseek-r1:free')}
Температура: {config.get('ai_temperature', 0.3)}

📊 Статистика:
Обработано AI: {stats.get('ai_processed', 0)}
Fallback на оригинал: {stats.get('ai_fallback', 0)}

💡 AI переписывает новости в нейтрально-информативном стиле
и сокращает их для Telegram"""
    
    await message.answer(text, parse_mode="HTML", reply_markup=ai_settings_kb())

@admin_router.message(F.text.startswith("AI:"), AdminStates.ai_settings)
async def toggle_ai(message: Message):
    config["ai_enabled"] = not config.get("ai_enabled", False)
    save_config()
    status = "✅ Включён" if config["ai_enabled"] else "❌ Выключен"
    await message.answer(f"AI-редактор: {status}", reply_markup=ai_settings_kb())

@admin_router.message(F.text.startswith("Модель:"), AdminStates.ai_settings)
async def set_ai_model(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ai_model)
    await message.answer(
        "Введите модель OpenRouter:\n\n"
        "Популярные бесплатные:\n"
        "• deepseek/deepseek-r1:free (рекомендую)\n"
        "• gpt-4o-mini\n"
        "• claude-3-haiku\n"
        "• llama-3.3-70b\n"
        "• mistral-7b\n\n"
        "Или любую другую из openrouter.ai",
        reply_markup=cancel_kb()
    )

@admin_router.message(AdminStates.waiting_ai_model)
async def process_ai_model(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    model = message.text.strip()
    if not model:
        await message.answer("❌ Модель не может быть пустой", reply_markup=cancel_kb())
        return
    
    config["ai_model"] = model
    save_config()
    await state.set_state(AdminStates.ai_settings)
    await message.answer(f"✅ Модель: {model}", reply_markup=ai_settings_kb())

@admin_router.message(F.text.startswith("Температура:"), AdminStates.ai_settings)
async def set_ai_temp(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ai_temp)
    await message.answer(
        "Введите температуру (0.0 - 1.0):\n\n"
        "• 0.0-0.3 — строго по фактам (рекомендую)\n"
        "• 0.5 — баланс\n"
        "• 0.8-1.0 — креативно",
        reply_markup=cancel_kb()
    )

@admin_router.message(AdminStates.waiting_ai_temp)
async def process_ai_temp(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        temp = float(message.text.strip().replace(',', '.'))
        if not 0 <= temp <= 1:
            raise ValueError
        config["ai_temperature"] = temp
        save_config()
        await state.set_state(AdminStates.ai_settings)
        await message.answer(f"✅ Температура: {temp}", reply_markup=ai_settings_kb())
    except Exception:
        await message.answer("❌ Число от 0.0 до 1.0:", reply_markup=cancel_kb())

@admin_router.message(F.text == "🧪 Тест AI (переписать одну новость)", AdminStates.ai_settings)
async def test_ai_rewrite(message: Message):
    if not config.get("ai_enabled", False):
        await message.answer("⚠️ AI выключен. Включите в настройках.", reply_markup=ai_settings_kb())
        return
    
    if not pending_news:
        await message.answer("❌ Нет новостей в очереди.", reply_markup=ai_settings_kb())
        return
    
    short_id = list(pending_news.keys())[0]
    item = pending_news[short_id]
    original_text = item.get("original_text") or item.get("text", "")
    
    await message.answer("🤖 Переписываю тестовую новость...")
    
    ai_text = await ai_rewrite_news(original_text)
    if ai_text:
        comparison = f"""<b>📝 ОРИГИНАЛ:</b>
<code>{original_text[:500]}</code>

<b>🤖 AI-ВЕРСИЯ:</b>
<code>{ai_text[:500]}</code>

✅ AI успешно обработал новость"""
        await message.answer(comparison, parse_mode="HTML", reply_markup=ai_settings_kb())
    else:
        await message.answer("❌ Не удалось переписать. Проверьте API ключ.", reply_markup=ai_settings_kb())

# === Погода ===
@admin_router.message(F.text == "🌤 Погода", AdminStates.menu)
async def menu_weather(message: Message, state: FSMContext):
    await state.set_state(AdminStates.weather_menu)
    await message.answer("🌤 Управление погодой", reply_markup=weather_menu_kb())

@admin_router.message(F.text.startswith("Погода:"), AdminStates.weather_menu)
async def toggle_weather(message: Message):
    config["weather_enabled"] = not config.get("weather_enabled", True)
    save_config()
    reschedule_jobs()
    await message.answer(f"{'✅' if config['weather_enabled'] else '❌'} Погода {'вкл' if config['weather_enabled'] else 'выкл'}", reply_markup=weather_menu_kb())

@admin_router.message(F.text == "⏰ Время погоды", AdminStates.weather_menu)
async def set_weather_time(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_weather_time)
    await message.answer("Введите время ЧЧ:ММ (например 08:00):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_weather_time)
async def process_weather_time(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        h, m = map(int, message.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        config["weather_time"] = f"{h:02d}:{m:02d}"
        save_config()
        reschedule_jobs()
        await state.set_state(AdminStates.weather_menu)
        await message.answer(f"✅ Время: {config['weather_time']}", reply_markup=weather_menu_kb())
    except Exception:
        await message.answer("❌ Формат ЧЧ:ММ", reply_markup=cancel_kb())

@admin_router.message(F.text == "🚀 Отправить сейчас", AdminStates.weather_menu)
async def send_weather_now(message: Message):
    await message.answer("🌤 Отправляю...", reply_markup=weather_menu_kb())
    await send_weather(manual=True)
    await message.answer("✅ Погода отправлена", reply_markup=weather_menu_kb())

@admin_router.message(F.text == "🌍 Геолокация", AdminStates.weather_menu)
async def geo_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.geo_menu)
    await message.answer("🌍 Геолокация", reply_markup=geo_menu_kb())

@admin_router.message(F.text.startswith("Широта:"), AdminStates.geo_menu)
async def set_lat(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_lat)
    await message.answer("Введите широту (например 50.6034):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_lat)
async def process_lat(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        lat = float(message.text.strip().replace(',', '.'))
        if not -90 <= lat <= 90:
            raise ValueError
        config["lat"] = lat
        save_config()
        await state.set_state(AdminStates.geo_menu)
        await message.answer(f"✅ Широта: {lat}", reply_markup=geo_menu_kb())
    except Exception:
        await message.answer("❌ Широта от -90 до 90:", reply_markup=cancel_kb())

@admin_router.message(F.text.startswith("Долгота:"), AdminStates.geo_menu)
async def set_lon(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_lon)
    await message.answer("Введите долготу (например 36.5809):", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_lon)
async def process_lon(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        lon = float(message.text.strip().replace(',', '.'))
        if not -180 <= lon <= 180:
            raise ValueError
        config["lon"] = lon
        save_config()
        await state.set_state(AdminStates.geo_menu)
        await message.answer(f"✅ Долгота: {lon}", reply_markup=geo_menu_kb())
    except Exception:
        await message.answer("❌ Долгота от -180 до 180:", reply_markup=cancel_kb())

@admin_router.message(F.text.startswith("Город:"), AdminStates.geo_menu)
async def set_city(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_city)
    await message.answer("Введите название города:", reply_markup=cancel_kb())

@admin_router.message(AdminStates.waiting_city)
async def process_city(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    city = message.text.strip()
    if not city:
        await message.answer("❌ Название не может быть пустым:", reply_markup=cancel_kb())
        return
    config["city_name"] = city
    save_config()
    await state.set_state(AdminStates.geo_menu)
    await message.answer(f"✅ Город: {city}", reply_markup=geo_menu_kb())

# === Модерация ===
@admin_router.message(F.text == "⏳ Модерация", AdminStates.menu)
async def menu_pending(message: Message, state: FSMContext):
    await state.set_state(AdminStates.pending_menu)
    count = len(pending_news)
    ai_count = sum(1 for v in pending_news.values() if v.get("ai_processed", False))
    text = f"⏳ Модерация\n\nВ очереди: {count}\n🤖 AI обработано: {ai_count}"
    await message.answer(text, reply_markup=pending_menu_kb())

@admin_router.message(F.text == "✅ Одобрить все", AdminStates.pending_menu)
async def pending_approve_all(message: Message):
    count = len(pending_news)
    if count == 0:
        await message.answer("❌ Очередь пуста.", reply_markup=pending_menu_kb())
        return
    await message.answer(f"⏳ Одобряю {count}...", reply_markup=pending_menu_kb())
    approved = 0
    for short_id in list(pending_news.keys()):
        if await approve_news(short_id):
            approved += 1
            await asyncio.sleep(0.5)
    await message.answer(f"✅ Одобрено: {approved}", reply_markup=pending_menu_kb())

@admin_router.message(F.text == "❌ Отклонить все", AdminStates.pending_menu)
async def pending_reject_all(message: Message):
    count = len(pending_news)
    if count == 0:
        await message.answer("❌ Очередь пуста.", reply_markup=pending_menu_kb())
        return
    await message.answer(f"⏳ Отклоняю {count}...", reply_markup=pending_menu_kb())
    rejected = 0
    for short_id in list(pending_news.keys()):
        if await reject_news(short_id):
            rejected += 1
    await message.answer(f"❌ Отклонено: {rejected}", reply_markup=pending_menu_kb())

@admin_router.message(F.text == "🔄 Обновить список", AdminStates.pending_menu)
async def pending_refresh(message: Message):
    count = len(pending_news)
    ai_count = sum(1 for v in pending_news.values() if v.get("ai_processed", False))
    await message.answer(f"🔄 В очереди: {count}\n🤖 AI: {ai_count}", reply_markup=pending_menu_kb())

# === Статистика ===
@admin_router.message(F.text == "📊 Статистика", AdminStates.menu)
async def menu_stats(message: Message):
    stats = config.get("stats", {})
    seen_count = len(seen_posts)
    text = (
        f"📊 Статистика\n\n"
        f"🌤 Погода: {stats.get('weather_sent', 0)}\n"
        f"📰 Опубликовано: {stats.get('news_sent', 0)}\n"
        f"❌ Отклонено: {stats.get('news_rejected', 0)}\n"
        f"🤖 AI обработано: {stats.get('ai_processed', 0)}\n"
        f"📝 Fallback на оригинал: {stats.get('ai_fallback', 0)}\n"
        f"⏳ В очереди: {len(pending_news)}\n"
        f"👁 Просмотрено (не повторять): {seen_count}\n\n"
        f"📡 Источников: {len(config.get('tg_sources', []))}\n"
        f"🔄 Интервал: {config.get('news_interval_minutes', 30)} мин"
    )
    await message.answer(text, reply_markup=main_menu_kb())

# === Логи ===
@admin_router.message(F.text == "📋 Логи", AdminStates.menu)
async def menu_logs(message: Message):
    errors = config.get("error_log", [])
    if not errors:
        await message.answer("✅ Ошибок нет.", reply_markup=main_menu_kb())
        return
    text = "📋 Последние ошибки\n\n"
    for i, err in enumerate(errors[-5:], 1):
        text += f"{i}. {err.get('time', '?')}\n   {err.get('error', '?')[:100]}\n\n"
    await message.answer(text, reply_markup=main_menu_kb())

# === Система ===
@admin_router.message(F.text == "🔧 Система", AdminStates.menu)
async def menu_system(message: Message, state: FSMContext):
    await state.set_state(AdminStates.system_menu)
    await message.answer("🔧 Система", reply_markup=system_menu_kb())

@admin_router.message(F.text == "📁 Логи бота", AdminStates.system_menu)
async def system_logs(message: Message):
    try:
        log_path = Path("bot.log")
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-20:]
            text = "📁 Логи\n\n" + "".join(lines[-3000:])
        else:
            text = "❌ Логов нет"
    except Exception as e:
        text = f"❌ Ошибка: {e}"
    await message.answer(text, reply_markup=system_menu_kb())

@admin_router.message(F.text == "🧹 Очистить историю", AdminStates.system_menu)
async def system_clear(message: Message):
    global sent_guids, seen_posts
    sent_guids.clear()
    seen_posts.clear()
    config["stats"] = {
        "weather_sent": 0, 
        "news_sent": 0, 
        "news_rejected": 0,
        "ai_processed": 0,
        "ai_fallback": 0,
        "last_weather": None, 
        "last_news_check": None
    }
    config["error_log"] = []
    save_config()
    save_seen()
    await message.answer("🧹 Очищено (включая историю просмотров).", reply_markup=system_menu_kb())

@admin_router.message(F.text == "📈 Экспорт статистики", AdminStates.system_menu)
async def system_export(message: Message):
    try:
        export = {
            "config": config,
            "pending_count": len(pending_news),
            "seen_count": len(seen_posts),
            "export_time": datetime.now().isoformat(),
        }
        export_text = json.dumps(export, ensure_ascii=False, indent=2)
        await message.answer(f"📈 Экспорт\n\n{export_text[:3000]}", reply_markup=system_menu_kb())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=system_menu_kb())

@admin_router.message(F.text == "🔄 Перезапустить бота", AdminStates.system_menu)
async def system_restart(message: Message):
    await message.answer("🔄 Перезапуск...", reply_markup=system_menu_kb())
    shutdown_event.set()

@admin_router.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == AdminStates.sources.state:
        await state.set_state(AdminStates.sources)
        await message.answer("❌ Отменено.", reply_markup=sources_menu_kb())
    elif current == AdminStates.weather_menu.state:
        await state.set_state(AdminStates.weather_menu)
        await message.answer("❌ Отменено.", reply_markup=weather_menu_kb())
    elif current == AdminStates.settings.state:
        await state.set_state(AdminStates.settings)
        await message.answer("❌ Отменено.", reply_markup=settings_menu_kb())
    elif current == AdminStates.system_menu.state:
        await state.set_state(AdminStates.system_menu)
        await message.answer("❌ Отменено.", reply_markup=system_menu_kb())
    elif current == AdminStates.geo_menu.state:
        await state.set_state(AdminStates.geo_menu)
        await message.answer("❌ Отменено.", reply_markup=geo_menu_kb())
    elif current == AdminStates.pending_menu.state:
        await state.set_state(AdminStates.pending_menu)
        await message.answer("❌ Отменено.", reply_markup=pending_menu_kb())
    elif current == AdminStates.ai_settings.state:
        await state.set_state(AdminStates.ai_settings)
        await message.answer("❌ Отменено.", reply_markup=ai_settings_kb())
    else:
        await state.clear()
        await message.answer("❌ Отменено. Напиши /admin", reply_markup=main_menu_kb())

# ================== ЗАПУСК ==================
async def main():
    load_config()
    load_pending()
    load_seen()
    
    dp.include_router(admin_router)
    reschedule_jobs()
    scheduler.start()
    logger.info("🚀 Бот запущен с AI-редактором (DeepSeek)")
    
    polling_task = asyncio.create_task(dp.start_polling(bot))
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    
    done, pending = await asyncio.wait(
        [polling_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    
    for task in pending:
        task.cancel()
    
    scheduler.shutdown()
    await bot.session.close()
    logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())
EOF

# Заменяем токены в bot.py
sed -i "s/REPLACE_BOT_TOKEN/$BOT_TOKEN/g" "$INSTALL_DIR/bot.py"
sed -i "s/REPLACE_CHANNEL_ID/$CHANNEL_ID/g" "$INSTALL_DIR/bot.py"
sed -i "s/REPLACE_ADMIN_ID/$ADMIN_ID/g" "$INSTALL_DIR/bot.py"
sed -i "s/REPLACE_OPENROUTER_KEY/$OPENROUTER_KEY/g" "$INSTALL_DIR/bot.py"
sed -i "s/REPLACE_OPENROUTER_MODEL/$AI_MODEL/g" "$INSTALL_DIR/bot.py"

echo "✅ bot.py создан"

echo "[6/8] Создаём systemd сервис..."

cat > /etc/systemd/system/belgorod-bot.service << EOF
[Unit]
Description=Belgorod News Bot v5.0 with AI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/venv/bin
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=10
KillMode=mixed
TimeoutStopSec=30
WatchdogSec=30
MemoryMax=512M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
EOF

echo "[7/8] Активируем сервис..."
systemctl daemon-reload
systemctl enable belgorod-bot
systemctl start belgorod-bot

echo "[8/8] Настройка мониторинга..."

cat > /usr/local/bin/check_bot.sh << 'EOF'
#!/bin/bash
if ! systemctl is-active --quiet belgorod-bot; then
    systemctl restart belgorod-bot
    echo "$(date): Бот перезапущен" >> /var/log/belbot_monitor.log
fi
EOF
chmod +x /usr/local/bin/check_bot.sh

(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/check_bot.sh") | crontab -

echo
echo "========================================"
echo "  ✅ УСТАНОВКА ЗАВЕРШЕНА! v5.0"
echo "  С AI-редактором на OpenRouter (DeepSeek-R1)"
echo "========================================"
echo
echo "📋 Команды:"
echo "  sudo systemctl status belgorod-bot   — статус"
echo "  sudo journalctl -u belgorod-bot -f   — логи"
echo "  sudo systemctl restart belgorod-bot  — перезапуск"
echo
echo "📱 Напиши боту /admin"
echo
echo "🧠 Что нового в v5.0:"
echo "  • AI-редактор на DeepSeek-R1 (бесплатно!)"
echo "  • Автоматическое переписывание новостей"
echo "  • Настройка модели и температуры"
echo "  • Кнопка '🤖 AI-переписать' для каждой новости"
echo "  • Тестовый режим в настройках AI"
echo "  • Fallback на оригинал при ошибке"
echo "  • Статистика AI-обработки"
echo "========================================"
echo
echo "🔑 Получить API ключ: https://openrouter.ai/"
echo "  • Регистрация 30 секунд"
echo "  • DeepSeek-R1 — бесплатно!"
echo "========================================"
