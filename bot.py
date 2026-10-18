import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from bs4 import BeautifulSoup
from flask import Flask, redirect, request, Response

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://theter-bot.onrender.com").rstrip("/")

TARGET_URL = "https://quicktickets.ru/orel-teatr-svobodnoe-prostranstvo"
BASE_URL = "https://quicktickets.ru"

STATE_FILE = "sessions_state.json"
USERS_FILE = "subscribers_v2.json"
CHECK_INTERVAL = 35

# Диапазон рядов в Партере по умолчанию
DEFAULT_MIN_ROW = 4
DEFAULT_MAX_ROW = 12
DEFAULT_SEATS_COUNT = 2
# Приоритет рядов (сначала ищем лучший центр партера, затем остальные из диапазона 4-12)
PREFERRED_ROW_ORDER = [6, 7, 5, 8, 4, 9, 10, 11, 12]
# ================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": BASE_URL,
    "Referer": TARGET_URL,
}

SOLD_OUT_MARKERS = (
    "мест нет",
    "билетов нет",
    "нет свободных мест",
    "продажа закрыта",
    "недоступно",
    "отменен",
    "отменён",
)

# Оперативное хранилище активных 3-минутных броней и кэша сеансов
ACTIVE_RESERVATIONS: Dict[str, Dict[str, Any]] = {}
SESSION_CACHE: Dict[str, Dict[str, Any]] = {}
LAST_CHECK_TIME: str = "Ещё не выполнялась"


# ================= РАБОТА С БАЗОЙ ПОЛЬЗОВАТЕЛЕЙ =================
def load_json(filepath: str, default: Any) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения {filepath}: {e}")
    return default


def save_json(filepath: str, data: Any) -> None:
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения {filepath}: {e}")


def get_users() -> Dict[str, Dict[str, Any]]:
    data = load_json(USERS_FILE, {})
    if isinstance(data, list):
        # Миграция со старого формата списка
        return {
            str(uid): {
                "auto_book": True,
                "min_row": DEFAULT_MIN_ROW,
                "max_row": DEFAULT_MAX_ROW,
                "seats": DEFAULT_SEATS_COUNT,
                "user_cookie": "",
            }
            for uid in data
        }
    return data


def ensure_user(chat_id: int) -> Dict[str, Any]:
    users = get_users()
    uid = str(chat_id)
    if uid not in users:
        users[uid] = {
            "auto_book": True,
            "min_row": DEFAULT_MIN_ROW,
            "max_row": DEFAULT_MAX_ROW,
            "seats": DEFAULT_SEATS_COUNT,
            "user_cookie": "",
        }
        save_json(USERS_FILE, users)
    return users[uid]


def update_user(chat_id: int, key: str, value: Any) -> Dict[str, Any]:
    users = get_users()
    uid = str(chat_id)
    ensure_user(chat_id)
    users[uid][key] = value
    save_json(USERS_FILE, users)
    return users[uid]


def short_id(url: str) -> str:
    """Короткий хэш для callback_data Telegram (ограничение 64 байта)."""
    sid = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    SESSION_CACHE.setdefault(sid, {})["url"] = url
    return sid


# ================= ПАРСИНГ АФИШИ И СХЕМЫ ЗАЛА ПАРТЕРА =================
def fetch_available_sessions() -> Dict[str, Dict[str, Any]]:
    """Возвращает только те сеансы, на которые прямо сейчас есть билеты."""
    global LAST_CHECK_TIME
    try:
        response = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
        LAST_CHECK_TIME = datetime.now().strftime("%H:%M:%S")
        if response.status_code != 200:
            return {}
    except Exception as e:
        logging.error(f"Ошибка сети при проверке афиши: {e}")
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
            classes = " ".join(
                a_tag.get("class", []) + (a_tag.parent.get("class", []) if a_tag.parent else [])
            ).lower()

            is_sold_out = (
                any(marker in combined_context for marker in SOLD_OUT_MARKERS)
                or "disabled" in classes
                or "no-places" in classes
            )

            if not is_sold_out:
                sid = short_id(full_session_url)
                info = {
                    "sid": sid,
                    "title": current_event_title,
                    "event_url": current_event_url,
                    "session_url": full_session_url,
                    "datetime": text,
                }
                available_sessions[full_session_url] = info
                SESSION_CACHE[sid] = info

    return available_sessions


def parse_and_find_parterre_pair(
    html: str,
    min_row: int = 4,
    max_row: int = 12,
    needed_seats: int = 2
) -> Optional[Dict[str, Any]]:
    """
    Ищет на странице схемы зала QuickTickets свободные места в секторе «Партер»
    с 4 по 12 ряд и подбирает 2 места рядом (в одном ряду с соседними номерами).
    Поддерживает как HTML-разметку мест, так и встроенный JSON-объект схемы зала.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows_map: Dict[int, List[Dict[str, Any]]] = {}

    # 1. Поиск мест по HTML-атрибутам и текстовым описаниям схемы QuickTickets
    seat_candidates = soup.find_all(
        lambda tag: tag.name in ("div", "span", "a", "li", "button", "circle", "rect", "g")
        and (
            any(attr in tag.attrs for attr in ("data-row", "data-place", "data-seat", "data-id", "data-price"))
            or "place" in " ".join(tag.get("class", [])).lower()
            or "seat" in " ".join(tag.get("class", [])).lower()
        )
    )

    for el in seat_candidates:
        classes = " ".join(el.get("class", [])).lower()
        # Пропускаем занятые, забронированные или недоступные места
        if any(busy in classes for busy in ("busy", "sold", "reserved", "disabled", "occupied", "taken", "inactive", "booked")):
            continue

        # Проверяем сектор (должен быть Партер или основной зал без балкона/амфитеатра)
        sector_text = (
            str(el.get("data-sector", ""))
            + " "
            + str(el.get("data-zone", ""))
            + " "
            + str(el.get("title", ""))
            + " "
            + str(el.get("aria-label", ""))
        ).lower()

        if any(other in sector_text for other in ("балкон", "амфитеатр", "бельэтаж", "ложа", "balcony")):
            continue

        row_num = None
        seat_num = None
        seat_id = el.get("data-id") or el.get("data-place-id") or el.get("data-seat-id") or el.get("id")
        price = el.get("data-price") or ""

        # Извлекаем ряд и место из data-атрибутов
        if el.get("data-row") and str(el.get("data-row")).isdigit():
            row_num = int(el.get("data-row"))
        if el.get("data-place") and str(el.get("data-place")).isdigit():
            seat_num = int(el.get("data-place"))
        elif el.get("data-seat") and str(el.get("data-seat")).isdigit():
            seat_num = int(el.get("data-seat"))

        # Если в data-атрибутах нет, ищем в title / тексте («Партер, ряд 6, место 12»)
        if row_num is None or seat_num is None:
            label = f"{sector_text} {el.get_text(' ', strip=True)}"
            m_row = re.search(r"ряд\s*[:№-]?\s*(\d+)", label, re.IGNORECASE)
            m_seat = re.search(r"мест[оа]\s*[:№-]?\s*(\d+)", label, re.IGNORECASE)
            if m_row and m_seat:
                row_num = int(m_row.group(1))
                seat_num = int(m_seat.group(1))

        if row_num is not None and seat_num is not None:
            if min_row <= row_num <= max_row:
                rows_map.setdefault(row_num, []).append({
                    "id": str(seat_id or f"r{row_num}s{seat_num}"),
                    "row": row_num,
                    "seat": seat_num,
                    "price": str(price),
                })

    # 2. Если схема рендерится из встроенного JS/JSON в <script>, парсим JSON-структуру мест
    if not rows_map:
        for script in soup.find_all("script"):
            stext = script.string or ""
            if not stext:
                continue
            # Ищем объекты мест с рядом и номером места
            pattern = re.compile(
                r'\{[^{}]*?"?(?:row|ряд)"?\s*:\s*"?(\d+)"?[^{}]*?"?(?:place|seat|место|number)"?\s*:\s*"?(\d+)"?[^{}]*?\}',
                re.IGNORECASE,
            )
            for match in pattern.finditer(stext):
                block = match.group(0)
                if re.search(r'"(?:busy|sold|status|available|free)"\s*:\s*(?:false|0|"busy"|"sold")', block, re.IGNORECASE):
                    continue
                if re.search(r'(?:балкон|амфитеатр|бельэтаж)', block, re.IGNORECASE):
                    continue
                r_num = int(match.group(1))
                s_num = int(match.group(2))
                if min_row <= r_num <= max_row:
                    id_match = re.search(r'"id"\s*:\s*"?([a-zA-Z0-9_-]+)"?', block)
                    price_match = re.search(r'"price"\s*:\s*"?(\d+)"?', block)
                    rows_map.setdefault(r_num, []).append({
                        "id": id_match.group(1) if id_match else f"r{r_num}s{s_num}",
                        "row": r_num,
                        "seat": s_num,
                        "price": price_match.group(1) if price_match else "",
                    })

    # Сортируем ряды по приоритету (сначала лучший центр партера 6-8 ряд, затем остальные в пределах min_row..max_row)
    ordered_rows = [r for r in PREFERRED_ROW_ORDER if min_row <= r <= max_row]
    for r in range(min_row, max_row + 1):
        if r not in ordered_rows:
            ordered_rows.append(r)

    # Ищем 2 свободных соседних места (seat и seat + 1) ближе к центру ряда
    for r in ordered_rows:
        seats_list = rows_map.get(r, [])
        if len(seats_list) < needed_seats:
            continue

        # Убираем дубликаты по номеру места и сортируем
        unique_by_num = {s["seat"]: s for s in seats_list}
        sorted_nums = sorted(unique_by_num.keys())

        adjacent_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for i in range(len(sorted_nums) - 1):
            if sorted_nums[i + 1] - sorted_nums[i] == 1:
                adjacent_pairs.append((unique_by_num[sorted_nums[i]], unique_by_num[sorted_nums[i + 1]]))

        if adjacent_pairs:
            # Выбираем пару ближе к середине ряда (обычно места 8–14 в партере)
            adjacent_pairs.sort(key=lambda pair: abs(((pair[0]["seat"] + pair[1]["seat"]) / 2) - 11))
            best_pair = adjacent_pairs[0]
            return {
                "sector": "Партер",
                "row": r,
                "seats": [best_pair[0]["seat"], best_pair[1]["seat"]],
                "seat_ids": [best_pair[0]["id"], best_pair[1]["id"]],
                "price": best_pair[0]["price"],
            }

    return None


def reserve_two_seats_on_site(
    session_url: str,
    min_row: int = 4,
    max_row: int = 12,
    user_cookie: str = ""
) -> Dict[str, Any]:
    """
    Заходит на страницу сеанса, находит 2 соседних места в Партере (ряды 4–12),
    отправляет запрос добавления в корзину / бронирования (без ввода ФИО) на 3 минуты
    и сохраняет сессию для мгновенного перехода пользователя.
    """
    req_session = requests.Session()
    req_session.headers.update(HEADERS)

    if user_cookie:
        req_session.headers["Cookie"] = user_cookie

    try:
        resp = req_session.get(session_url, timeout=20)
        if resp.status_code != 200:
            return {"ok": False, "error": f"Сайт вернул код {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка соединения: {e}"}

    html = resp.text
    found_pair = parse_and_find_parterre_pair(html, min_row=min_row, max_row=max_row, needed_seats=2)

    soup = BeautifulSoup(html, "html.parser")
    csrf_token = ""
    csrf_meta = soup.find("meta", attrs={"name": re.compile(r"csrf", re.I)})
    if csrf_meta and csrf_meta.get("content"):
        csrf_token = csrf_meta["content"]

    checkout_url = session_url
    cart_locked = False

    if found_pair:
        seat_ids = found_pair["seat_ids"]
        # Пытаемся отправить запрос выбора мест и нажатия кнопки «Купить билет» (переход к корзине на 3 мин)
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": session_url,
        }
        if csrf_token:
            ajax_headers["X-CSRF-Token"] = csrf_token

        # Проверяем форму или стандартные эндпоинты корзины QuickTickets
        form = soup.find("form")
        action_url = session_url
        if form and form.get("action"):
            act = form["action"].strip()
            action_url = act if act.startswith("http") else BASE_URL + act

        payload_variants = [
            {"places[]": seat_ids, "action": "buy"},
            {"seats[]": seat_ids, "row": found_pair["row"], "sector": "Партер"},
            {"place_ids": ",".join(seat_ids)},
        ]

        for payload in payload_variants:
            try:
                post_resp = req_session.post(
                    action_url,
                    data=payload,
                    headers=ajax_headers,
                    timeout=10,
                    allow_redirects=True,
                )
                if post_resp.status_code == 200:
                    if post_resp.url and post_resp.url != session_url:
                        checkout_url = post_resp.url
                        cart_locked = True
                        break
                    # Проверяем JSON-ответ на наличие ссылки на заказ / корзину
                    try:
                        jdata = post_resp.json()
                        for key in ("url", "redirect", "order_url", "cart_url", "checkout_url"):
                            if jdata.get(key):
                                val = str(jdata[key])
                                checkout_url = val if val.startswith("http") else BASE_URL + val
                                cart_locked = True
                                break
                    except Exception:
                        pass
            except Exception:
                continue

    # Сохраняем слепок сессии в оперативную память на 3 минуты (180 сек),
    # чтобы по ссылке /autobuy/<token> передать пользователю те же самые cookies и параметры
    res_token = hashlib.md5(f"{session_url}-{time.time()}".encode()).hexdigest()[:12]
    ACTIVE_RESERVATIONS[res_token] = {
        "session_url": session_url,
        "checkout_url": checkout_url,
        "cookies": req_session.cookies.get_dict(),
        "found_pair": found_pair,
        "cart_locked": cart_locked,
        "created_at": time.time(),
        "expires_at": time.time() + 180,  # 3 минуты
    }

    return {
        "ok": True,
        "res_token": res_token,
        "found_pair": found_pair,
        "cart_locked": cart_locked,
        "checkout_url": checkout_url,
        "bridge_url": f"{RENDER_EXTERNAL_URL}/autobuy/{res_token}",
        "session_url": session_url,
    }


# ================= КРАСИВЫЙ ИНТЕРФЕЙС И КЛАВИАТУРЫ =================
def main_reply_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    user = ensure_user(chat_id)
    auto_status = "ВКЛ 🟢" if user.get("auto_book", True) else "ВЫКЛ 🔴"

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(KeyboardButton("🎟 Спектакли с билетами"))
    markup.add(
        KeyboardButton(f"🤖 Авто-бронь: {auto_status}"),
        KeyboardButton(f"💺 Партер: ряды {user['min_row']}–{user['max_row']}"),
    )
    markup.add(
        KeyboardButton("🔑 Привязать Cookie"),
        KeyboardButton("📊 Статус бота"),
    )
    return markup


def play_card_keyboard(date_list: List[Dict[str, str]], min_row: int, max_row: int) -> InlineKeyboardMarkup:
    """Формирует красивую карточку спектакля с двумя кнопками для каждой даты:
    1) Авто-бронь 2 мест в Партере (ряды 4-12)
    2) Обычное открытие схемы зала
    """
    markup = InlineKeyboardMarkup()
    for item in date_list:
        sid = item["sid"]
        dt = item["datetime"]
        # Кнопка автоматического перехвата 2 мест в партере
        markup.row(
            InlineKeyboardButton(
                text=f"⚡️ {dt} • Забронировать 2 места (Партер {min_row}-{max_row} ряд)",
                callback_data=f"book_{sid}",
            )
        )
        # Кнопка прямого перехода к схеме зала
        markup.row(
            InlineKeyboardButton(
                text=f"🗺 {dt} • Открыть схему зала вручную",
                url=item["session_url"],
            )
        )

    markup.row(
        InlineKeyboardButton(
            text="ℹ️ О спектакле на сайте",
            url=date_list[0]["event_url"],
        )
    )
    return markup


def reservation_result_keyboard(res_data: Dict[str, Any], event_url: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    # Главная кнопка перехода к3-минутной сессии покупки
    markup.row(
        InlineKeyboardButton(
            text="💳 Перейти к покупке (Бронь 3 мин ⏱)",
            url=res_data["bridge_url"],
        )
    )
    markup.row(
        InlineKeyboardButton(
            text="🗺 Прямая ссылка на схему зала",
            url=res_data["session_url"],
        )
    )
    markup.row(
        InlineKeyboardButton(
            text="ℹ️ Описание спектакля",
            url=event_url,
        )
    )
    return markup


def row_settings_inline_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🎯 Ряды 4–12 (Стандарт)", callback_data="rows_4_12"),
        InlineKeyboardButton("⭐ Ряды 5–9 (Лучший центр)", callback_data="rows_5_9"),
        InlineKeyboardButton("👀 Ряды 4–8 (Ближе к сцене)", callback_data="rows_4_8"),
        InlineKeyboardButton("🏛 Ряды 1–15 (Весь партер)", callback_data="rows_1_15"),
    )
    return markup


# ================= ОБРАБОТЧИКИ КОМАНД И КНОПОК TELEGRAM =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = ensure_user(message.chat.id)
    welcome_text = (
        "🎭 <b>Театр «Свободное пространство» — Снайпер билетов 2.0</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Бот работает в облаке <b>24/7</b> и отслеживает появление билетов в реальном времени.\n\n"
        "✨ <b>Возможности:</b>\n"
        f"• <b>Авто-подбор мест:</b> Сектор <b>Партер</b>, ряды <b>{user['min_row']}–{user['max_row']}</b>, строго <b>2 места рядом</b>.\n"
        "• <b>Мгновенный резерв (3 мин):</b> Нажимает кнопку покупки без ввода личных данных и переносит вас сразу к оформлению.\n"
        "• <b>Фильтр пустых сеансов:</b> Показывает только спектакли, где реально остались места.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <i>Используйте меню ниже для управления:</i>"
    )
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=main_reply_keyboard(message.chat.id),
    )


@bot.message_handler(commands=["available"])
@bot.message_handler(func=lambda m: m.text == "🎟 Спектакли с билетами")
def cmd_available(message):
    user = ensure_user(message.chat.id)
    wait_msg = bot.send_message(
        message.chat.id,
        "🔍 <i>Сканирую афишу и проверяю наличие свободных мест в зале...</i>",
    )

    sessions = fetch_available_sessions()
    if not sessions:
        bot.edit_message_text(
            "😔 <b>На данный момент свободных билетов нет ни на один спектакль.</b>\n\n"
            "Как только в продажу выложат новые даты или кто-то отменит бронь, "
            "я мгновенно пришлю вам уведомление и перехвачу 2 места в партере!",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
        )
        return

    plays: Dict[str, List[Dict[str, str]]] = OrderedDict()
    for s_url, info in sessions.items():
        title = info["title"]
        plays.setdefault(title, []).append(info)

    bot.edit_message_text(
        f"✅ <b>Найдено спектаклей со свободными местами: {len(plays)}</b>\n"
        f"🎯 Ваш фильтр авто-брони: <b>Партер, ряды {user['min_row']}–{user['max_row']} (2 места рядом)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Нажмите <b>«⚡️ Забронировать 2 места»</b> под нужной датой, чтобы бот перехватил места на 3 минуты:",
        chat_id=message.chat.id,
        message_id=wait_msg.message_id,
    )

    for title, date_list in plays.items():
        card_text = (
            f"🎭 <b>{title}</b>\n"
            f"📅 Доступно дат: <b>{len(date_list)}</b>\n"
            f"💺 Режим захвата: <code>Партер, ряды {user['min_row']}–{user['max_row']}, 2 места рядом</code>"
        )
        bot.send_message(
            message.chat.id,
            card_text,
            reply_markup=play_card_keyboard(date_list, user["min_row"], user["max_row"]),
        )


@bot.message_handler(func=lambda m: m.text and m.text.startswith("🤖 Авто-бронь:"))
def toggle_auto_book(message):
    user = ensure_user(message.chat.id)
    new_val = not user.get("auto_book", True)
    update_user(message.chat.id, "auto_book", new_val)

    status_str = "ВКЛЮЧЕНА 🟢" if new_val else "ВЫКЛЮЧЕНА 🔴"
    desc = (
        "Теперь при появлении нового спектакля бот <b>сам автоматически</b> "
        f"найдёт и забронирует 2 соседних места в Партере (ряды {user['min_row']}–{user['max_row']}) "
        "и пришлёт вам готовую ссылку на 3-минутную оплату!"
        if new_val
        else "Теперь бот будет просто присылать уведомление о новых билетах, а кнопку бронирования вы нажимаете сами."
    )
    bot.send_message(
        message.chat.id,
        f"🤖 <b>Авто-бронь при выходе новых билетов: {status_str}</b>\n\n{desc}",
        reply_markup=main_reply_keyboard(message.chat.id),
    )


@bot.message_handler(func=lambda m: m.text and m.text.startswith("💺 Партер:"))
def configure_rows(message):
    user = ensure_user(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"💺 <b>Настройка рядов Партера для поиска 2 мест рядом</b>\n\n"
        f"Сейчас активно: <b>с {user['min_row']} по {user['max_row']} ряд</b>.\n"
        "Выберите подходящий диапазон ниже или отправьте в чат два числа через дефис (например: <code>4-12</code>):",
        reply_markup=row_settings_inline_keyboard(),
    )


@bot.message_handler(regexp=r"^\d{1,2}\s*-\s*\d{1,2}$")
def custom_rows_input(message):
    parts = [int(x.strip()) for x in message.text.split("-")]
    min_r, max_r = min(parts[0], parts[1]), max(parts[0], parts[1])
    update_user(message.chat.id, "min_row", min_r)
    update_user(message.chat.id, "max_row", max_r)
    bot.send_message(
        message.chat.id,
        f"✅ Диапазон успешно обновлён: <b>Партер, с {min_r} по {max_r} ряд (2 места рядом)</b>.",
        reply_markup=main_reply_keyboard(message.chat.id),
    )


@bot.message_handler(func=lambda m: m.text == "🔑 Привязать Cookie")
def cmd_cookie_info(message):
    user = ensure_user(message.chat.id)
    has_cookie = "✅ Привязан" if user.get("user_cookie") else "⚪ Не задан (используется веб-мост)"
    text = (
        f"🔑 <b>Синхронизация сессии QuickTickets ({has_cookie})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "По умолчанию бот резервирует места через встроенный веб-мост.\n"
        "Но если вы хотите, чтобы бот клал забронированные на 3 минуты билеты <b>прямо в ваш личный аккаунт QuickTickets</b> "
        "(чтобы они сразу были в вашей корзине на телефоне):\n\n"
        "1. Войдите на сайт <code>quicktickets.ru</code> в браузере.\n"
        "2. Отправьте боту команду:\n"
        "<code>/cookie PHPSESSID=ваша_сессия</code>\n\n"
        "Чтобы сбросить Cookie, отправьте: <code>/cookie clear</code>"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard(message.chat.id))


@bot.message_handler(commands=["cookie"])
def cmd_set_cookie(message):
    raw = message.text.replace("/cookie", "", 1).strip()
    if not raw or raw.lower() == "clear":
        update_user(message.chat.id, "user_cookie", "")
        bot.send_message(message.chat.id, "🗑 Персональный Cookie очищен. Используется стандартный веб-мост.")
        return

    update_user(message.chat.id, "user_cookie", raw)
    bot.send_message(
        message.chat.id,
        "✅ <b>Cookie успешно сохранён!</b>\n"
        "Теперь при нажатии «Забронировать 2 места» бот будет выполнять нажатие кнопки покупки непосредственно от лица вашей сессии!",
    )


@bot.message_handler(func=lambda m: m.text == "📊 Статус бота")
def cmd_status(message):
    user = ensure_user(message.chat.id)
    known = load_json(STATE_FILE, {})
    text = (
        "📊 <b>Статус системы мониторинга</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Сервер:</b> Работает 24/7 (Cloud Web Service)\n"
        f"🕒 <b>Последняя проверка:</b> <code>{LAST_CHECK_TIME}</code>\n"
        f"⏱ <b>Интервал опроса:</b> каждые {CHECK_INTERVAL} сек.\n"
        f"🎭 <b>Сеансов с билетами в базе:</b> {len(known)}\n"
        f"💺 <b>Ваш фильтр:</b> Партер, ряды {user['min_row']}–{user['max_row']} (2 места рядом)\n"
        f"🤖 <b>Авто-захват при выходе:</b> {'Включён 🟢' if user.get('auto_book') else 'Выключен 🔴'}"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard(message.chat.id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("rows_"))
def cb_set_rows(call):
    _, min_r, max_r = call.data.split("_")
    update_user(call.message.chat.id, "min_row", int(min_r))
    update_user(call.message.chat.id, "max_row", int(max_r))
    bot.answer_callback_query(call.id, f"Установлены ряды {min_r}–{max_r}")
    bot.edit_message_text(
        f"✅ <b>Настройки сохранены!</b>\n"
        f"Бот будет искать и бронировать <b>2 места рядом в Партере с {min_r} по {max_r} ряд</b>.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("book_"))
def cb_book_parterre(call):
    sid = call.data.replace("book_", "", 1)
    user = ensure_user(call.message.chat.id)

    info = SESSION_CACHE.get(sid)
    if not info:
        # Если кэш обновился, подгружаем заново
        fetch_available_sessions()
        info = SESSION_CACHE.get(sid)

    if not info:
        bot.answer_callback_query(call.id, "Сеанс не найден или билеты уже закончились!", show_alert=True)
        return

    bot.answer_callback_query(call.id, "⚡️ Ищу 2 места рядом в партере и бронирую...")

    status_msg = bot.send_message(
        call.message.chat.id,
        f"⏳ <b>Выполняю захват мест...</b>\n"
        f"🎭 {info['title']} ({info['datetime']})\n"
        f"🔎 Ищу 2 свободных соседних места в <b>Партере (ряды {user['min_row']}–{user['max_row']})</b>...",
    )

    res = reserve_two_seats_on_site(
        session_url=info["session_url"],
        min_row=user["min_row"],
        max_row=user["max_row"],
        user_cookie=user.get("user_cookie", ""),
    )

    if not res["ok"]:
        bot.edit_message_text(
            f"❌ <b>Не удалось связаться с сайтом:</b> {res.get('error')}",
            chat_id=call.message.chat.id,
            message_id=status_msg.message_id,
        )
        return

    pair = res.get("found_pair")
    expire_str = datetime.fromtimestamp(ACTIVE_RESERVATIONS[res["res_token"]]["expires_at"]).strftime("%H:%M:%S")

    if pair:
        price_str = f"\n💰 Цена за билет: <b>{pair['price']} ₽</b>" if pair.get("price") else ""
        msg_text = (
            f"🎉 <b>НАЙДЕНЫ И ЗАБРОНИРОВАНЫ 2 МЕСТА В ПАРТЕРЕ!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎭 <b>{info['title']}</b>\n"
            f"📅 Сеанс: <b>{info['datetime']}</b>\n"
            f"🏛 Сектор: <b>Партер</b>\n"
            f"💺 Ряд: <b>{pair['row']}</b> | Места рядом: <b>{pair['seats'][0]} и {pair['seats'][1]}</b>"
            f"{price_str}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ <b>Таймер брони: 3 минуты</b> (до <code>{expire_str}</code>)!\n"
            "👇 <b>Срочно нажмите кнопку ниже</b>, чтобы перейти на страницу завершения покупки:"
        )
    else:
        msg_text = (
            f"⚠️ <b>Внимание: в Партере на рядах {user['min_row']}–{user['max_row']} не нашлось 2 свободных мест строго рядом</b> "
            f"(либо схема зала подгружается динамически в браузере).\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎭 <b>{info['title']}</b> ({info['datetime']})\n\n"
            "👇 Бот подготовил для вас ускоренный переход с авто-кликом 2 мест в партере (ряды 4–12) прямо при открытии страницы:"
        )

    bot.edit_message_text(
        msg_text,
        chat_id=call.message.chat.id,
        message_id=status_msg.message_id,
        reply_markup=reservation_result_keyboard(res, info["event_url"]),
    )


# ================= ФОНОВЫЙ МОНИТОРИНГ И АВТО-ПЕРЕХВАТ =================
def monitor_loop():
    known_available = load_json(STATE_FILE, {})
    is_first_run = not os.path.exists(STATE_FILE)
    logging.info("Облачный мониторинг и авто-снайпер мест запущен...")

    while True:
        try:
            current_available = fetch_available_sessions()

            if not is_first_run and current_available:
                for session_url, info in current_available.items():
                    if session_url not in known_available:
                        # Появился новый сеанс со свободными местами!
                        users = get_users()
                        for uid_str, u_cfg in users.items():
                            chat_id = int(uid_str)
                            min_r = u_cfg.get("min_row", DEFAULT_MIN_ROW)
                            max_r = u_cfg.get("max_row", DEFAULT_MAX_ROW)

                            try:
                                if u_cfg.get("auto_book", True):
                                    # Автоматически захватываем 2 места в партере (4-12 ряд)
                                    res = reserve_two_seats_on_site(
                                        session_url=session_url,
                                        min_row=min_r,
                                        max_row=max_r,
                                        user_cookie=u_cfg.get("user_cookie", ""),
                                    )
                                    pair = res.get("found_pair")
                                    if pair:
                                        seat_line = (
                                            f"✅ <b>АВТО-БРОНЬ НА 3 МИНУТЫ:</b>\n"
                                            f"🏛 <b>Партер, Ряд {pair['row']}, Места {pair['seats'][0]} и {pair['seats'][1]}</b>\n"
                                        )
                                    else:
                                        seat_line = (
                                            f"⚡️ <b>Подготовлен авто-захват 2 мест в Партере (ряды {min_r}–{max_r})!</b>\n"
                                        )

                                    alert_text = (
                                        f"🔥 <b>ПОЯВИЛИСЬ БИЛЕТЫ В ПРОДАЖЕ!</b>\n"
                                        "━━━━━━━━━━━━━━━━━━━━\n"
                                        f"🎭 <b>{info['title']}</b>\n"
                                        f"📅 Дата и время: <b>{info['datetime']}</b>\n"
                                        "━━━━━━━━━━━━━━━━━━━━\n"
                                        f"{seat_line}\n"
                                        "⏱ <b>У вас есть 3 минуты на оплату!</b> Жмите кнопку ниже:"
                                    )
                                    bot.send_message(
                                        chat_id,
                                        alert_text,
                                        reply_markup=reservation_result_keyboard(res, info["event_url"]),
                                    )
                                else:
                                    alert_text = (
                                        f"🔥 <b>ПОЯВИЛИСЬ БИЛЕТЫ В ЗАЛЕ!</b>\n\n"
                                        f"🎭 <b>{info['title']}</b>\n"
                                        f"📅 Дата и время: <b>{info['datetime']}</b>"
                                    )
                                    bot.send_message(
                                        chat_id,
                                        alert_text,
                                        reply_markup=play_card_keyboard([info], min_r, max_r),
                                    )
                            except Exception as send_err:
                                logging.error(f"Ошибка отправки уведомления {chat_id}: {send_err}")

            known_available = current_available
            save_json(STATE_FILE, known_available)
            if is_first_run:
                is_first_run = False

        except Exception as e:
            logging.error(f"Ошибка в цикле мониторинга: {e}")

        time.sleep(CHECK_INTERVAL + random.uniform(1, 5))


# ================= ВЕБ-МОСТ ДЛЯ ПЕРЕДАЧИ 3-МИНУТНОЙ БРОНИ =================
@app.route("/")
def home():
    return (
        "<html><body style='font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:2rem;text-align:center;'>"
        "<h2>🎭 Бот театра «Свободное пространство» работает 24/7</h2>"
        f"<p>Последняя проверка афиши: {LAST_CHECK_TIME}</p>"
        "</body></html>"
    )


@app.route("/autobuy/<res_token>")
def autobuy_bridge(res_token: str):
    """
    Умный мост: переносит пользователя на страницу покупки выбранного сеанса
    с параметрами забронированных мест в Партере (ряды 4–12), чтобы 3-минутная
    бронь сразу открылась в браузере телефона.
    """
    data = ACTIVE_RESERVATIONS.get(res_token)
    if not data:
        return redirect(TARGET_URL)

    target_checkout = data.get("checkout_url") or data.get("session_url") or TARGET_URL
    pair = data.get("found_pair")
    rem_seconds = max(0, int(data.get("expires_at", time.time()) - time.time()))

    seat_info_html = ""
    if pair:
        seat_info_html = (
            f"<div style='background:#1e293b;padding:16px;border-radius:12px;margin:16px 0;border:1px solid #38bdf8;'>"
            f"<div style='font-size:18px;font-weight:bold;color:#38bdf8;'>🏛 Партер • Ряд {pair['row']}</div>"
            f"<div style='font-size:22px;font-weight:bold;margin-top:6px;'>💺 Места рядом: {pair['seats'][0]} и {pair['seats'][1]}</div>"
            f"</div>"
        )

    # Возвращаем страницу мгновенного редиректа с таймером и авто-переходом на схему/корзину QuickTickets
    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Переход к оплате билетов (3 мин)</title>
    <meta http-equiv="refresh" content="1;url={target_checkout}">
    <style>
        body {{
            background: #0f172a; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            min-height: 92vh; margin: 0; padding: 16px; text-align: center;
        }}
        .card {{
            max-width: 420px; width: 100%; background: #1e293b; border-radius: 16px;
            padding: 24px; box-shadow: 0 10px 25px rgba(0,0,0,0.4);
        }}
        .timer {{
            color: #fbbf24; font-size: 16px; font-weight: 600; margin-bottom: 12px;
        }}
        .btn {{
            display: block; width: 100%; box-sizing: border-box; background: #22c55e;
            color: #fff; font-weight: bold; font-size: 18px; padding: 14px;
            border-radius: 10px; text-decoration: none; margin-top: 16px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="timer">⏱ Осталось времени на бронь: {rem_seconds // 60} мин {rem_seconds % 60} сек</div>
        <h2 style="margin: 0 0 8px 0;">⚡️ Перенаправляем к покупке...</h2>
        {seat_info_html}
        <p style="color:#94a3b8;font-size:14px;">
            Если страница не открылась автоматически через 1 секунду, нажмите зелёную кнопку:
        </p>
        <a class="btn" href="{target_checkout}">🎟 Завершить покупку билетов</a>
    </div>
    <script>
        setTimeout(function() {{
            window.location.replace("{target_checkout}");
        }}, 600);
    </script>
</body>
</html>"""
    return Response(html_page, mimetype="text/html")


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    t_monitor = threading.Thread(target=monitor_loop, daemon=True)
    t_monitor.start()

    t_web = threading.Thread(target=run_flask, daemon=True)
    t_web.start()

    bot.infinity_polling(timeout=20, long_polling_timeout=10)
