import json
import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, Any, List

import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from bs4 import BeautifulSoup
from flask import Flask

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

TARGET_URL = "https://quicktickets.ru/orel-teatr-svobodnoe-prostranstvo"
BASE_URL = "https://quicktickets.ru"
STATE_FILE = "sessions_state.json"
USERS_FILE = "subscribers.json"
CHECK_INTERVAL = 45

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
SOLD_OUT_MARKERS = ("мест нет", "билетов нет", "нет свободных мест", "продажа закрыта", "недоступно", "отменен", "отменён")

def load_json(filepath: str, default: Any) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(filepath: str, data: Any) -> None:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_subscribers() -> List[int]:
    return load_json(USERS_FILE, [])

def add_subscriber(chat_id: int) -> None:
    users = get_subscribers()
    if chat_id not in users:
        users.append(chat_id)
        save_json(USERS_FILE, users)

def fetch_available_sessions() -> Dict[str, Dict[str, Any]]:
    try:
        response = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
        if response.status_code != 200:
            return {}
    except Exception:
        return {}

    soup = BeautifulSoup(response.text, "html.parser")
    available_sessions: Dict[str, Dict[str, Any]] = {}
    current_event_title = "Спектакль"
    current_event_url = TARGET_URL

    event_pattern = re.compile(r"^/orel-teatr-svobodnoe-prostranstvo/e\d+")
    session_pattern = re.compile(r"^/orel-teatr-svobodnoe-prostranstvo/s\d+")

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        text = " ".join(a_tag.get_text().split())

        if event_pattern.match(href):
            if text and "подробнее" not in text.lower():
                current_event_title = text
                current_event_url = BASE_URL + href

        elif session_pattern.match(href):
            full_session_url = BASE_URL + href
            trailing_texts = []
            for el in a_tag.next_elements:
                if getattr(el, "name", None) in ("a", "h1", "h2", "h3", "h4") and el is not a_tag:
                    break
                if isinstance(el, str):
                    trailing_texts.append(el)

            combined_context = " ".join(" ".join(trailing_texts).split()).lower()
            classes = " ".join(a_tag.get("class", []) + (a_tag.parent.get("class", []) if a_tag.parent else [])).lower()
            is_sold_out = any(marker in combined_context for marker in SOLD_OUT_MARKERS) or "disabled" in classes or "no-places" in classes

            if not is_sold_out:
                available_sessions[full_session_url] = {
                    "title": current_event_title,
                    "event_url": current_event_url,
                    "datetime": text,
                }
    return available_sessions

def main_reply_keyboard() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("🎟 Спектакли с билетами"))
    return markup

def single_session_keyboard(session_url: str, event_url: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🎟 Выбрать места в зале", url=session_url))
    markup.row(InlineKeyboardButton("ℹ️ О спектакле", url=event_url))
    return markup

def notify_all(text: str, reply_markup: InlineKeyboardMarkup) -> None:
    for chat_id in get_subscribers():
        try:
            bot.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception:
            pass

def monitor_loop():
    known_available = load_json(STATE_FILE, {})
    is_first_run = not os.path.exists(STATE_FILE)
    logging.info("Облачный мониторинг QuickTickets запущен...")

    while True:
        try:
            current_available = fetch_available_sessions()
            if not is_first_run:
                for session_url, info in current_available.items():
                    if session_url not in known_available:
                        msg = (
                            f"🔥 <b>ЕСТЬ СВОБОДНЫЕ БИЛЕТЫ В ЗАЛЕ!</b>\n\n"
                            f"🎭 <b>{info['title']}</b>\n"
                            f"📅 Дата и время: <b>{info['datetime']}</b>\n\n"
                            f"⚡️ Переходите сразу к выбору мест:"
                        )
                        notify_all(msg, single_session_keyboard(session_url, info["event_url"]))

            known_available = current_available
            save_json(STATE_FILE, known_available)
            if is_first_run:
                is_first_run = False
        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")

        time.sleep(CHECK_INTERVAL + random.uniform(1, 7))

@bot.message_handler(commands=["start"])
def cmd_start(message):
    add_subscriber(message.chat.id)
    bot.send_message(message.chat.id, "✅ Бот запущен в облаке как Web-сервис 24/7!", reply_markup=main_reply_keyboard())

@bot.message_handler(commands=["available"])
@bot.message_handler(func=lambda m: m.text == "🎟 Спектакли с билетами")
def cmd_available(message):
    add_subscriber(message.chat.id)
    sessions = fetch_available_sessions()
    if not sessions:
        bot.send_message(message.chat.id, "😔 Сейчас свободных мест нет.", reply_markup=main_reply_keyboard())
        return

    plays: Dict[str, List[Dict[str, str]]] = OrderedDict()
    for s_url, info in sessions.items():
        title = info["title"]
        if title not in plays:
            plays[title] = []
        plays[title].append({"session_url": s_url, "datetime": info["datetime"], "event_url": info["event_url"]})

    for title, date_list in plays.items():
        markup = InlineKeyboardMarkup()
        for item in date_list:
            markup.row(InlineKeyboardButton(text=f"🎟 {item['datetime']} — Купить", url=item["session_url"]))
        markup.row(InlineKeyboardButton(text="ℹ️ Описание", url=date_list[0]["event_url"]))
        bot.send_message(message.chat.id, f"🎭 <b>{title}</b>", reply_markup=markup)

@app.route("/")
def home():
    return "Bot is running 24/7!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    # Запускаем фоновый мониторинг билетов
    t_monitor = threading.Thread(target=monitor_loop, daemon=True)
    t_monitor.start()

    # Запускаем мини-веб-сервер в отдельном потоке, чтобы Render не усыплял приложение
    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    # Запуск бота Telegram
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
