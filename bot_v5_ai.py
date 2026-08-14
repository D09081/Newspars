import asyncio
import json
import re
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

CONFIG_FILE = Path("config.json")
PENDING_FILE = Path("pending_news.json")
SEEN_FILE = Path("seen_posts.json")
PHOTO_CACHE_DIR = Path("photo_cache")
PHOTO_CACHE_DIR.mkdir(exist_ok=True)

OPENROUTER_API_KEY = "REPLACE_OPENROUTER_KEY"  # Заменить при установке

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
    "ai_editor_enabled": False,
    "ai_model": "openai/gpt-4o-mini",
    "ai_temperature": 0.3,
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
    "lat": 50.6034,
    "lon": 36.5809,
    "city_name": "Белгород",
    "timezone": "Europe/Moscow",
}

# ================== AI ПРОМПТ ==================
AI_PROMPT = """Ты — профессиональный редактор новостей для Telegram-канала города Белгород.

ЗАДАЧА: Перепиши следующую новость в нейтрально-информативном стиле, сохраняя все факты.

ПРАВИЛА:
1. Убери канцеляризмы, лишние эпитеты, "воду", официоз
2. Сохрани все даты, ФИО, адреса, цифры, суммы — они важны
3. Сделай текст лаконичным, читаемым в ленте Telegram (2-4 абзаца)
4. Начинай с главного: что, где, когда
5. Не добавляй своих оценок, мнений, эмоций
6. Не используй маркдаун, только обычный текст
7. Если текст уже хороший — оставь как есть, только убери лишнее

ИСХОДНЫЙ ТЕКСТ:
{text}

ПЕРЕПИСАННЫЙ ТЕКСТ:"""

# ================== ХРАНИЛИЩА ==================
seen_posts: Dict[str, str] = {}
sent_guids: Set[str] = set()
config: dict = {}
pending_news: Dict[str, dict] = {}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
admin_router = Router()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
shutdown_event = asyncio.Event()

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
    return f"{username}_{msg_id}" in seen_posts

def mark_seen(username: str, msg_id: int):
    seen_posts[f"{username}_{msg_id}"] = datetime.now().isoformat()
    save_seen()

# ================== PENDING ==================
def load_pending():
    global pending_news
    if PENDING_FILE.exists():
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            pending_news = {k: v for k, v in data.items() if v.get("time", "") > cutoff}
            if len(pending_news) != len(data):
                save_pending()

def save_pending():
    global pending_news
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending_news, f, ensure_ascii=False, indent=2)

# ================== AI РЕДАКТОР ==================
async def ai_rewrite(text: str) -> str:
    if not config.get("ai_editor_enabled", False):
        return text

    api_key = OPENROUTER_API_KEY
    if not api_key or api_key == "REPLACE_OPENROUTER_KEY":
        return text

    model = config.get("ai_model", "openai/gpt-4o-mini")
    temperature = config.get("ai_temperature", 0.3)

    prompt = AI_PROMPT.format(text=text[:4000])

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://newspars.local",
                    "X-Title": "Belgorod News Bot",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 1500,
                }
            )
            resp.raise_for_status()
            data = resp.json()
            result = data["choices"][0]["message"]["content"].strip()
            # Убираем кавычки вокруг, если модель их добавила
            result = result.strip('"').strip("'")
            if len(result) < 20:
                return text  # Слишком короткий ответ — fallback
            return result
    except Exception:
        return text  # Fallback на оригинал при любой ошибке

# ================== FSM ==================
class AdminStates(StatesGroup):
    menu = State()
    sources = State()
    settings = State()
    weather_menu = State()
    geo_menu = State()
    pending_menu = State()
    ai_menu = State()
    waiting_source_name = State()
    waiting_source_username = State()
    waiting_weather_time = State()
    waiting_news_interval = State()
    waiting_max_news = State()
    waiting_lat = State()
    waiting_lon = State()
    waiting_city = State()
    waiting_ai_model = State()
    waiting_ai_temp = State()
    waiting_edit_text = State()

# ================== КЛАВИАТУРЫ ==================
def main_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="📰 Источники"), KeyboardButton(text="⚙️ Настройки"))
    b.row(KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🤖 AI Редактор"))
    b.row(KeyboardButton(text="🔄 Проверить новости"), KeyboardButton(text="⏳ Модерация"))
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

def pending_menu_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="✅ Одобрить все"), KeyboardButton(text="❌ Отклонить все"))
    b.row(KeyboardButton(text="🔄 Обновить список"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def settings_menu_kb() -> ReplyKeyboardMarkup:
    n = "✅ Вкл" if config.get("news_enabled", True) else "❌ Выкл"
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"Новости: {n}"))
    b.row(KeyboardButton(text=f"🔄 Интервал: {config.get('news_interval_minutes', 30)} мин"))
    b.row(KeyboardButton(text=f"📰 Лимит за раз: {config.get('max_news_per_check', 5)}"))
    b.row(KeyboardButton(text="◀️ Назад"))
    return b.as_markup(resize_keyboard=True)

def ai_menu_kb() -> ReplyKeyboardMarkup:
    a = "✅ Вкл" if config.get("ai_editor_enabled", False) else "❌ Выкл"
    model = config.get("ai_model", "openai/gpt-4o-mini").split("/")[-1]
    temp = config.get("ai_temperature", 0.3)
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=f"AI Редактор: {a}"))
    b.row(KeyboardButton(text=f"🧠 Модель: {model}"))
    b.row(KeyboardButton(text=f"🌡 Температура: {temp}"))
    b.row(KeyboardButton(text="🤖 Переписать одну новость"))
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
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{short_id}")
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
            return posts
        except Exception as e:
            last_error = e
            continue
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

async def send_weather(manual: bool = False):
    if not config.get("weather_enabled", True) and not manual:
        return
    try:
        text = await get_weather()
        await bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
    except Exception:
        pass

# ================== МОДЕРАЦИЯ ==================
def format_post_text(post: dict, for_channel: bool = False) -> str:
    text = post.get("text", "")
    source_name = post.get("source_name", "")
    link = post.get("link", "")
    if for_channel:
        source_line = f"\n\n— <a href='{link}'>{source_name}</a>" if source_name else ""
        max_text = 1000 - len(source_line)
        if len(text) > max_text:
            text = text[:max_text] + "..."
        return text + source_line
    else:
        header = f"📰 <b>{source_name}</b> (@{post.get('source', '')})\n\n" if source_name else ""
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
    except Exception:
        return None

async def send_to_moderation(post: dict) -> Optional[str]:
    # AI-редактор: переписываем текст перед модерацией
    original_text = post.get("text", "")
    rewritten_text = await ai_rewrite(original_text)
    post["text"] = rewritten_text
    post["original_text"] = original_text  # Сохраняем оригинал

    text = format_post_text(post, for_channel=False)
    photo_urls = post.get("photos", [])[:10]
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
                kb_msg = await bot.send_message(
                    admin_id,
                    f"👆 <b>Модерация:</b> {post.get('source_name', '?')}\n"
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
            return short_id
        except Exception:
            pass
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
        elif len(existing_paths) == 1:
            photo_file = FSInputFile(existing_paths[0])
            await bot.send_photo(CHANNEL_ID, photo=photo_file, caption=channel_text, parse_mode="HTML")
        else:
            await bot.send_message(CHANNEL_ID, channel_text, parse_mode="HTML", disable_web_page_preview=True)
        sent_guids.add(item["guid"])
        for path in photo_paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass
        del pending_news[short_id]
        save_pending()
        return True
    except Exception:
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
    return True

# ================== ПРОВЕРКА НОВОСТЕЙ ==================
async def fetch_and_send_news(force: bool = False):
    global sent_guids
    if not config.get("news_enabled", True) and not force:
        return 0
    new_posts = 0
    posts = await fetch_all_tg_sources()
    if not posts:
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
    save_pending()
    item = pending_news[short_id]
    photo_paths = item.get("photos", [])
    is_album = item.get("is_album", False)
    try:
        if is_album and len(photo_paths) > 1:
            await bot.delete_message(chat_id=item["chat_id"], message_id=item["message_id"])
            source_name = item.get("source", "")
            source_username = item.get("source_username", "")
            header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
            full_text = header + new_text
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
                    f"📷 Фото: {len(photo_paths)} | 📝 Текст обновлён",
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
                item["message_id"] = kb_msg.message_id
                save_pending()
        elif len(photo_paths) == 1 and os.path.exists(photo_paths[0]):
            photo_file = FSInputFile(photo_paths[0])
            source_name = item.get("source", "")
            source_username = item.get("source_username", "")
            header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
            full_text = header + new_text
            caption_text = full_text[:1020] + "..." if len(full_text) > 1024 else full_text
            await bot.edit_message_caption(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=edit_kb(short_id)
            )
        else:
            source_name = item.get("source", "")
            source_username = item.get("source_username", "")
            header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
            full_text = header + new_text
            await bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                text=full_text,
                parse_mode="HTML",
                reply_markup=edit_kb(short_id)
            )
        await message.answer("✅ Текст обновлён", reply_markup=main_menu_kb())
    except Exception:
        await message.answer("❌ Ошибка при обновлении", reply_markup=main_menu_kb())
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

# === AI Редактор ===
@admin_router.message(F.text == "🤖 AI Редактор", AdminStates.menu)
async def menu_ai(message: Message, state: FSMContext):
    await state.set_state(AdminStates.ai_menu)
    await message.answer("🤖 AI Редактор\n\n"
                        "Переписывает новости в лаконичном стиле перед модерацией.\n"
                        "При ошибке API — оригинальный текст.", reply_markup=ai_menu_kb())

@admin_router.message(F.text.startswith("AI Редактор:"), AdminStates.ai_menu)
async def toggle_ai(message: Message):
    config["ai_editor_enabled"] = not config.get("ai_editor_enabled", False)
    save_config()
    await message.answer(f"{'✅' if config['ai_editor_enabled'] else '❌'} AI Редактор {'вкл' if config['ai_editor_enabled'] else 'выкл'}", reply_markup=ai_menu_kb())

@admin_router.message(F.text.startswith("🧠 Модель:"), AdminStates.ai_menu)
async def set_ai_model(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ai_model)
    await message.answer(
        "Введите модель OpenRouter:\n\n"
        "<code>openai/gpt-4o-mini</code> — быстрая, дешёвая\n"
        "<code>anthropic/claude-sonnet-4-20250514</code> — качественная\n"
        "<code>meta-llama/llama-3.3-70b-instruct</code> — бесплатная\n\n"
        "Полный список: https://openrouter.ai/models",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )

@admin_router.message(AdminStates.waiting_ai_model)
async def process_ai_model(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    model = message.text.strip()
    if not model or "/" not in model:
        await message.answer("❌ Укажите полное название модели (provider/model):", reply_markup=cancel_kb())
        return
    config["ai_model"] = model
    save_config()
    await state.set_state(AdminStates.ai_menu)
    await message.answer(f"✅ Модель: {model}", reply_markup=ai_menu_kb())

@admin_router.message(F.text.startswith("🌡 Температура:"), AdminStates.ai_menu)
async def set_ai_temp(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ai_temp)
    await message.answer(
        "Введите температуру (0.0 — 1.0):\n\n"
        "<b>0.0</b> — строгое следование правилам, минимум креатива\n"
        "<b>0.3</b> — рекомендуется, баланс\n"
        "<b>0.7</b> — больше разнообразия\n"
        "<b>1.0</b> — максимум креатива (не рекомендуется для новостей)",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )

@admin_router.message(AdminStates.waiting_ai_temp)
async def process_ai_temp(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        temp = float(message.text.strip().replace(",", "."))
        if not 0.0 <= temp <= 1.0:
            raise ValueError
        config["ai_temperature"] = temp
        save_config()
        await state.set_state(AdminStates.ai_menu)
        await message.answer(f"✅ Температура: {temp}", reply_markup=ai_menu_kb())
    except Exception:
        await message.answer("❌ Число от 0.0 до 1.0:", reply_markup=cancel_kb())

@admin_router.message(F.text == "🤖 Переписать одну новость", AdminStates.ai_menu)
async def ai_test_rewrite(message: Message):
    await message.answer("🔍 Ищу новую новость для теста...", reply_markup=ai_menu_kb())
    try:
        posts = await fetch_all_tg_sources()
        if not posts:
            await message.answer("❌ Новых новостей нет. Попробуйте позже.", reply_markup=ai_menu_kb())
            return
        post = posts[0]
        original = post.get("text", "")
        await message.answer(
            f"📰 <b>Оригинал:</b>\n<code>{original[:800]}</code>",
            parse_mode="HTML"
        )
        await message.answer("🤖 Переписываю...", reply_markup=ai_menu_kb())
        rewritten = await ai_rewrite(original)
        if rewritten == original:
            await message.answer(
                "⚠️ <b>AI вернул оригинал</b> (возможно, AI выключен или ошибка API).",
                parse_mode="HTML",
                reply_markup=ai_menu_kb()
            )
        else:
            await message.answer(
                f"✅ <b>Результат AI:</b>\n<code>{rewritten[:1000]}</code>",
                parse_mode="HTML",
                reply_markup=ai_menu_kb()
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:200]}", reply_markup=ai_menu_kb())

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
    await message.answer(f"⏳ Модерация\n\nВ очереди: {count}", reply_markup=pending_menu_kb())

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
    await message.answer(f"🔄 В очереди: {count}", reply_markup=pending_menu_kb())

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
    elif current == AdminStates.geo_menu.state:
        await state.set_state(AdminStates.geo_menu)
        await message.answer("❌ Отменено.", reply_markup=geo_menu_kb())
    elif current == AdminStates.pending_menu.state:
        await state.set_state(AdminStates.pending_menu)
        await message.answer("❌ Отменено.", reply_markup=pending_menu_kb())
    elif current == AdminStates.ai_menu.state:
        await state.set_state(AdminStates.ai_menu)
        await message.answer("❌ Отменено.", reply_markup=ai_menu_kb())
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

if __name__ == "__main__":
    asyncio.run(main())
