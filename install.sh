#!/bin/bash

# ========================================
#  BELGOROD NEWS BOT — ПОЛНАЯ УСТАНОВКА
#  Версия: 5.0 (DeepSeek-R1 + Фото)
#  GitHub: D09081/Newspars
# ========================================

set -e

echo "========================================"
echo "  Belgorod News Bot — УСТАНОВКА v5.0"
echo "  С AI-редактором (DeepSeek-R1)"
echo "  Поддержка фото и альбомов"
echo "========================================"
echo

if [ "$EUID" -ne 0 ]; then
  echo "❌ Запусти от root: sudo ./install.sh"
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
echo "[1/8] Удаление старой версии..."
systemctl stop belgorod-bot 2>/dev/null || true
systemctl disable belgorod-bot 2>/dev/null || true
rm -f /etc/systemd/system/belgorod-bot.service
systemctl daemon-reload
rm -rf /opt/Newspars
pkill -f "python3.*bot.py" 2>/dev/null || true
screen -X -S belbot quit 2>/dev/null || true
echo "✅ Старое удалено"

# ================== УСТАНОВКА ==================
echo "[2/8] Обновление системы..."
apt update -y
apt install -y python3 python3-pip python3-venv git curl screen

INSTALL_DIR="/opt/Newspars"
echo "[3/8] Создание директории $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "[4/8] Виртуальное окружение..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install aiogram==3.4.1 httpx apscheduler

echo "[5/8] Создание бота..."

cat > "$INSTALL_DIR/bot.py" << 'EOF'
import asyncio
import json
import logging
import re
import random
import string
import signal
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Set, Dict, Optional, List

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
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8807054442:AAHfRkSj6hI4Slwc_8qc3R48C4q1wOsk5uA"
CHANNEL_ID = -1004314624597
ADMIN_IDS = [898467551]
OPENROUTER_API_KEY = "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"
OPENROUTER_MODEL = "deepseek/deepseek-r1:free"

CONFIG_FILE = Path("config.json")
PENDING_FILE = Path("pending_news.json")
SEEN_FILE = Path("seen_posts.json")
PHOTO_CACHE_DIR = Path("photo_cache")
PHOTO_CACHE_DIR.mkdir(exist_ok=True)

# ================== КОНФИГ ==================
DEFAULT_CONFIG = {
    "weather_enabled": True,
    "news_enabled": True,
    "ai_enabled": bool(OPENROUTER_API_KEY and OPENROUTER_API_KEY != ""),
    "ai_model": OPENROUTER_MODEL if OPENROUTER_MODEL else "deepseek/deepseek-r1:free",
    "ai_temperature": 0.3,
    "weather_time": "08:00",
    "news_interval_minutes": 30,
    "max_news_per_check": 3,
    "city_name": "Белгород",
    "lat": 50.6034,
    "lon": 36.5809,
    "tg_sources": [
        {"name": "Губернатор Шуваев", "username": "shuvaev_aleksandr"},
        {"name": "Оперштаб Белгород", "username": "operstab_bel"},
        {"name": "Администрация Белгорода", "username": "beladm31"},
        {"name": "Губернатор Белгородской области", "username": "gubernator_bel"},
        {"name": "МЧС Белгород", "username": "mchs_bel"},
        {"name": "Правительство Белгородской области", "username": "belregion_ru"},
        {"name": "Минздрав Белгородской области", "username": "belzdrav31"},
    ],
    "stats": {"weather_sent": 0, "news_sent": 0, "news_rejected": 0, "ai_processed": 0},
    "max_pending_days": 7,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

seen_posts = {}
sent_guids = set()
config = {}
pending_news = {}
shutdown_event = asyncio.Event()
http_client = None

async def get_http_client():
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
    return http_client

def load_config():
    global config
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in config:
                config[k] = v
    else:
        config = DEFAULT_CONFIG.copy()
        save_config()

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def load_seen():
    global seen_posts
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            seen_posts = json.load(f)
        seen_posts = {k: v for k, v in seen_posts.items() if v > (datetime.now() - timedelta(days=14)).isoformat()}
        save_seen()

def save_seen():
    with open(SEEN_FILE, "w") as f:
        json.dump(seen_posts, f, ensure_ascii=False, indent=2)

def is_seen(username, msg_id):
    return f"{username}_{msg_id}" in seen_posts

def mark_seen(username, msg_id):
    seen_posts[f"{username}_{msg_id}"] = datetime.now().isoformat()
    save_seen()

def load_pending():
    global pending_news
    if PENDING_FILE.exists():
        with open(PENDING_FILE) as f:
            data = json.load(f)
        cutoff = (datetime.now() - timedelta(days=config.get("max_pending_days", 7))).isoformat()
        pending_news = {k: v for k, v in data.items() if v.get("time", "") > cutoff}
        save_pending()

def save_pending():
    with open(PENDING_FILE, "w") as f:
        json.dump(pending_news, f, ensure_ascii=False, indent=2)

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

def main_menu_kb():
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="📰 Источники"), KeyboardButton(text="⚙️ Настройки"))
    b.row(KeyboardButton(text="🌤 Погода"), KeyboardButton(text="🔄 Проверить новости"))
    b.row(KeyboardButton(text="⏳ Модерация"), KeyboardButton(text="📊 Статистика"))
    b.row(KeyboardButton(text="🔧 Система"), KeyboardButton(text="❌ Закрыть меню"))
    return b.as_markup(resize_keyboard=True)

def cancel_kb():
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text="❌ Отмена"))
    return b.as_markup(resize_keyboard=True)

def edit_kb(short_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{short_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{short_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{short_id}"),
         InlineKeyboardButton(text="🤖 AI", callback_data=f"ai_rewrite:{short_id}")]
    ])

def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_short_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

# ================== RSS-BRIDGE ПАРСИНГ ==================
RSS_BRIDGE_INSTANCES = [
    "https://rss-bridge.org/bridge01",
    "https://rss-bridge.lewd.tech",
]

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
    
    return photos[:10]

def extract_text_from_html(html_text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', html_text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    text = re.sub(r'^RSS-Bridge was unable[^\n]*\n*', '', text)
    text = re.sub(r'VIEW IN TELEGRAM', '', text)
    text = re.sub(r'Media is too big', '', text)
    return text.strip()

async def fetch_rss_bridge(username, instance):
    try:
        client = await get_http_client()
        url = f"{instance}/?action=display&bridge=Telegram&username={username}&format=Json"
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        
        posts = []
        for item in data.get("items", []):
            msg_url = item.get("url", "")
            msg_id_match = re.search(r'/(\d{1,20})$', msg_url)
            msg_id = int(msg_id_match.group(1)) if msg_id_match else 0
            
            if not msg_id or is_seen(username, msg_id):
                continue
            
            content_html = item.get("content_html", "")
            text = extract_text_from_html(content_html)
            if len(text) < 10:
                mark_seen(username, msg_id)
                continue
            
            if any(w in text.lower() for w in ['погода', 'температура', 'прогноз погоды']):
                mark_seen(username, msg_id)
                continue
            
            photos = extract_photos_from_html(content_html)
            
            posts.append({
                "id": f"{username}_{msg_id}",
                "text": text,
                "photos": photos[:10],
                "source": username,
                "link": msg_url,
                "msg_id": msg_id,
            })
            mark_seen(username, msg_id)
            if len(posts) >= 3:
                break
        return posts
    except Exception as e:
        logger.warning(f"RSS ошибка {instance}: {e}")
        return []

async def fetch_all_tg_sources():
    all_posts = []
    for source in config.get("tg_sources", []):
        for instance in RSS_BRIDGE_INSTANCES:
            posts = await fetch_rss_bridge(source["username"], instance)
            if posts:
                for post in posts:
                    post["source_name"] = source["name"]
                all_posts.extend(posts)
                break
        await asyncio.sleep(0.5)
    return all_posts

# ================== ПОГОДА ==================
async def get_weather():
    try:
        client = await get_http_client()
        lat = config.get('lat', 50.6034)
        lon = config.get('lon', 36.5809)
        city = config.get('city_name', 'Белгород')
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,pressure_msl&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe/Moscow&forecast_days=1"
        data = (await client.get(url)).json()
        
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
    except:
        return "⚠️ Погода недоступна"

async def send_weather(manual=False):
    if not config.get("weather_enabled", True) and not manual:
        return
    await bot.send_message(CHANNEL_ID, await get_weather(), parse_mode="HTML")
    config["stats"]["weather_sent"] = config["stats"].get("weather_sent", 0) + 1
    save_config()

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
    if not config.get("ai_enabled", False) or not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "":
        return None
    
    try:
        client = await get_http_client()
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/belgorod_news_bot",
                "X-Title": "Belgorod News Bot",
            },
            json={
                "model": config.get("ai_model", "deepseek/deepseek-r1:free"),
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Перепиши эту новость:\n\n{text[:2000]}"}
                ],
                "temperature": config.get("ai_temperature", 0.3),
                "max_tokens": 400,
            }
        )
        response.raise_for_status()
        data = response.json()
        
        if data.get("choices"):
            rewritten = data["choices"][0]["message"]["content"].strip()
            if rewritten and len(rewritten) > 10:
                config["stats"]["ai_processed"] = config["stats"].get("ai_processed", 0) + 1
                save_config()
                return rewritten
        return None
    except Exception as e:
        logger.error(f"AI ошибка: {e}")
        return None

# ================== МОДЕРАЦИЯ ==================
async def download_photo(url):
    if not url:
        return None
    try:
        filename = f"photo_{abs(hash(url)) % 100000000:09d}.jpg"
        path = PHOTO_CACHE_DIR / filename
        if path.exists():
            return str(path)
        client = await get_http_client()
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        with open(path, "wb") as f:
            f.write(resp.content)
        return str(path)
    except Exception as e:
        logger.warning(f"Фото ошибка: {e}")
        return None

async def send_to_moderation(post):
    text = post.get("text", "")
    source_name = post.get("source_name", "")
    source_username = post.get("source", "")
    short_id = generate_short_id()
    
    # AI обработка
    if config.get("ai_enabled", False):
        ai_text = await ai_rewrite_news(text)
        if ai_text:
            text = ai_text
            post["ai_processed"] = True
    
    # Скачиваем фото
    photo_paths = []
    for url in post.get("photos", [])[:5]:
        path = await download_photo(url)
        if path:
            photo_paths.append(path)
    
    header = f"📰 <b>{source_name}</b> (@{source_username})\n\n" if source_name else ""
    full_text = header + text[:3500]
    
    for admin_id in ADMIN_IDS:
        try:
            if len(photo_paths) > 1:
                # АЛЬБОМ
                media = []
                for i, path in enumerate(photo_paths):
                    if i == 0:
                        media.append(InputMediaPhoto(
                            media=FSInputFile(path),
                            caption=full_text[:4000],
                            parse_mode="HTML"
                        ))
                    else:
                        media.append(InputMediaPhoto(media=FSInputFile(path)))
                await bot.send_media_group(admin_id, media=media)
                await bot.send_message(
                    admin_id,
                    f"👆 {source_name} 📷 {len(photo_paths)} фото",
                    reply_markup=edit_kb(short_id)
                )
            elif len(photo_paths) == 1:
                await bot.send_photo(
                    admin_id,
                    photo=FSInputFile(photo_paths[0]),
                    caption=full_text[:4000],
                    parse_mode="HTML",
                    reply_markup=edit_kb(short_id)
                )
            else:
                await bot.send_message(
                    admin_id,
                    full_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=edit_kb(short_id)
                )
            
            pending_news[short_id] = {
                "guid": post["id"],
                "text": text,
                "photos": photo_paths,
                "source": source_name,
                "link": post.get("link", ""),
                "time": datetime.now().isoformat(),
                "ai_processed": post.get("ai_processed", False),
            }
            save_pending()
            return short_id
        except Exception as e:
            logger.error(f"Модерация ошибка: {e}")
            continue
    return None

async def approve_news(short_id):
    if short_id not in pending_news:
        return False
    item = pending_news[short_id]
    try:
        text = item.get("text", "")[:3500]
        source = item.get("source", "")
        link = item.get("link", "")
        
        channel_text = text
        if source:
            channel_text += f"\n\n— <a href='{link}'>{source}</a>"
        
        photo_paths = [p for p in item.get("photos", []) if os.path.exists(p)]
        
        if len(photo_paths) > 1:
            media = []
            for i, path in enumerate(photo_paths):
                if i == 0:
                    media.append(InputMediaPhoto(
                        media=FSInputFile(path),
                        caption=channel_text[:4000],
                        parse_mode="HTML"
                    ))
                else:
                    media.append(InputMediaPhoto(media=FSInputFile(path)))
            await bot.send_media_group(CHANNEL_ID, media=media)
        elif len(photo_paths) == 1:
            await bot.send_photo(
                CHANNEL_ID,
                photo=FSInputFile(photo_paths[0]),
                caption=channel_text[:4000],
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                CHANNEL_ID,
                channel_text[:4000],
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        
        sent_guids.add(item["guid"])
        config["stats"]["news_sent"] = config["stats"].get("news_sent", 0) + 1
        save_config()
        
        for path in item.get("photos", []):
            if path and os.path.exists(path):
                os.remove(path)
        
        del pending_news[short_id]
        save_pending()
        return True
    except Exception as e:
        logger.error(f"Approve ошибка: {e}")
        return False

async def reject_news(short_id):
    if short_id not in pending_news:
        return False
    item = pending_news.pop(short_id)
    save_pending()
    for path in item.get("photos", []):
        if path and os.path.exists(path):
            os.remove(path)
    config["stats"]["news_rejected"] = config["stats"].get("news_rejected", 0) + 1
    save_config()
    return True

async def fetch_and_send_news(force=False):
    if not config.get("news_enabled", True) and not force:
        return 0
    posts = await fetch_all_tg_sources()
    count = 0
    for post in posts[:config.get("max_news_per_check", 3)]:
        if post["id"] not in sent_guids and await send_to_moderation(post):
            count += 1
            await asyncio.sleep(0.5)
    return count

def reschedule_jobs():
    scheduler.remove_all_jobs()
    if config.get("weather_enabled", True):
        h, m = map(int, config["weather_time"].split(":"))
        scheduler.add_job(send_weather, CronTrigger(hour=h, minute=m), id="weather")
    if config.get("news_enabled", True):
        scheduler.add_job(fetch_and_send_news, IntervalTrigger(minutes=config.get("news_interval_minutes", 30)), id="news")

# ================== МАРШРУТЫ ==================
@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.menu)
    await message.answer("🛠 Админ-панель", reply_markup=main_menu_kb())

@dp.callback_query(F.data.startswith("approve:"))
async def mod_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа")
    if await approve_news(callback.data.split(":")[-1]):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("✅ Одобрено")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject:"))
async def mod_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("⛔ Нет доступа")
    if await reject_news(callback.data.split(":")[-1]):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ Отклонено")
    await callback.answer()

@dp.message(F.text == "🔄 Проверить новости")
async def menu_force_news(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🔍 Проверяю...")
    count = await fetch_and_send_news(force=True)
    await message.answer(f"✅ Найдено {count} новостей" if count else "✅ Новых нет", reply_markup=main_menu_kb())

@dp.message(F.text == "📊 Статистика")
async def menu_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    stats = config.get("stats", {})
    await message.answer(
        f"📊 Статистика\n\n"
        f"🌤 Погода: {stats.get('weather_sent', 0)}\n"
        f"📰 Опубликовано: {stats.get('news_sent', 0)}\n"
        f"❌ Отклонено: {stats.get('news_rejected', 0)}\n"
        f"🤖 AI: {stats.get('ai_processed', 0)}\n"
        f"⏳ В очереди: {len(pending_news)}",
        reply_markup=main_menu_kb()
    )

@dp.message(F.text == "❌ Закрыть меню")
async def menu_close(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Меню закрыто", reply_markup=ReplyKeyboardRemove())

@dp.message(F.text == "◀️ Назад")
async def go_back(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.menu)
    await message.answer("🛠 Админ-панель", reply_markup=main_menu_kb())

@dp.message(F.text == "🔧 Система")
async def system_menu(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.system_menu)
    await message.answer("🔧 Система", reply_markup=ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧠 AI-редактор")],
            [KeyboardButton(text="🧹 Очистить историю")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    ))

@dp.message(F.text == "🧠 AI-редактор")
async def ai_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.ai_settings)
    status = "✅ Включён" if config.get("ai_enabled", False) else "❌ Выключен"
    await message.answer(
        f"🧠 AI-редактор\n\nСтатус: {status}\nМодель: {config.get('ai_model', 'deepseek/deepseek-r1:free')}\nТемпература: {config.get('ai_temperature', 0.3)}",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=f"AI: {status}")],
                [KeyboardButton(text="◀️ Назад")]
            ],
            resize_keyboard=True
        )
    )

@dp.message(F.text.startswith("AI:"))
async def toggle_ai(message: Message):
    if not is_admin(message.from_user.id):
        return
    config["ai_enabled"] = not config.get("ai_enabled", False)
    save_config()
    await message.answer(f"{'✅' if config['ai_enabled'] else '❌'} AI", reply_markup=main_menu_kb())

@dp.message(F.text == "🧹 Очистить историю")
async def clear_history(message: Message):
    if not is_admin(message.from_user.id):
        return
    global sent_guids, seen_posts
    sent_guids.clear()
    seen_posts.clear()
    config["stats"] = {"weather_sent": 0, "news_sent": 0, "news_rejected": 0, "ai_processed": 0}
    save_config()
    save_seen()
    await message.answer("🧹 Очищено", reply_markup=main_menu_kb())

# ================== ЗАПУСК ==================
async def main():
    load_config()
    load_pending()
    load_seen()
    reschedule_jobs()
    scheduler.start()
    logger.info("🚀 Бот запущен с AI-редактором (DeepSeek-R1)")
    try:
        await dp.start_polling(bot)
    finally:
        if http_client:
            await http_client.aclose()
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
EOF

echo "✅ bot.py создан"

# ================== SYSTEMD ==================
echo "[6/8] Настройка systemd..."

cat > /etc/systemd/system/belgorod-bot.service << EOF
[Unit]
Description=Belgorod News Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/venv/bin
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=10
MemoryMax=256M
CPUQuota=30%

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable belgorod-bot
systemctl start belgorod-bot

echo "[7/8] Настройка мониторинга..."

cat > /usr/local/bin/check_bot.sh << 'EOF'
#!/bin/bash
if ! systemctl is-active --quiet belgorod-bot; then
    systemctl restart belgorod-bot
    echo "$(date): Бот перезапущен" >> /var/log/belbot_monitor.log
fi
EOF
chmod +x /usr/local/bin/check_bot.sh

(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/check_bot.sh") | crontab -

echo "[8/8] Завершение..."

echo ""
echo "========================================"
echo "  ✅ УСТАНОВКА ЗАВЕРШЕНА!"
echo "========================================"
echo ""
echo "📋 Команды:"
echo "  systemctl status belgorod-bot   — статус"
echo "  journalctl -u belgorod-bot -f   — логи"
echo "  systemctl restart belgorod-bot  — перезапуск"
echo ""
echo "📱 Напиши боту /admin"
echo ""
echo "📂 Установлено: $INSTALL_DIR"
echo "🤖 Модель: $AI_MODEL"
echo "📸 Фото: включены (до 10 фото)"
echo "========================================"
