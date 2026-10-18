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
from typing import Dict, Any, List, Optional

import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from bs4 import BeautifulSoup
from flask import Flask, redirect, Response

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://theter-bot.onrender.com").rstrip("/")

TARGET_URL = "https://quicktickets.ru/orel-teatr-svobodnoe-prostranstvo"
BASE_URL = "https://quicktickets.ru"

STATE_FILE = "sessions_state.json"
USERS_FILE = "subscribers_v4.json"
CHECK_INTERVAL = 35

# Настройки поиска мест в Партере
MIN_ROW = 4
MAX_ROW = 12
DEFAULT_SEATS_COUNT = 2
PREFERRED_ROW_ORDER = [6, 7, 5, 8, 4, 9, 10, 11, 12]
# =============================================

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

ACTIVE_RESERVATIONS: Dict[str, Dict[str, Any]] = {}
SESSION_CACHE: Dict[str, Dict[str, Any]] = {}
POSTER_CACHE: Dict[str, str] = {}
LAST_CHECK_TIME: str = "Запускается..."


# ================= БАЗА ПОЛЬЗОВАТЕЛЕЙ =================
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
    return load_json(USERS_FILE, {})


def ensure_user(chat_id: int) -> Dict[str, Any]:
    users = get_users()
    uid = str(chat_id)
    changed = False
    if uid not in users:
        # Авто-бронь по умолчанию ВЫКЛЮЧЕНА (False), мест подряд по умолчанию: 2
        users[uid] = {
            "auto_book": False,
            "seats_count": DEFAULT_SEATS_COUNT,
        }
        changed = True
    else:
        if "auto_book" not in users[uid]:
            users[uid]["auto_book"] = False
            changed = True
        if "seats_count" not in users[uid]:
            users[uid]["seats_count"] = DEFAULT_SEATS_COUNT
            changed = True

    if changed:
        save_json(USERS_FILE, users)
    return users[uid]


def toggle_user_autobook(chat_id: int) -> bool:
    users = get_users()
    uid = str(chat_id)
    ensure_user(chat_id)
    new_state = not users[uid].get("auto_book", False)
    users[uid]["auto_book"] = new_state
    save_json(USERS_FILE, users)
    return new_state


def set_user_seats_count(chat_id: int, count: int) -> int:
    users = get_users()
    uid = str(chat_id)
    ensure_user(chat_id)
    count = max(1, min(6, int(count)))
    users[uid]["seats_count"] = count
    save_json(USERS_FILE, users)
    return count


def short_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def seats_word(n: int) -> str:
    if n == 1:
        return "1 место"
    elif 2 <= n <= 4:
        return f"{n} места"
    return f"{n} мест"


# ================= ИЗВЛЕЧЕНИЕ ОБЛОЖКИ СПЕКТАКЛЯ =================
def normalize_img_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    raw_url = raw_url.strip()
    if raw_url.startswith("//"):
        return "https:" + raw_url
    if raw_url.startswith("/"):
        return BASE_URL + raw_url
    if raw_url.startswith("http"):
        return raw_url
    return BASE_URL + "/" + raw_url.lstrip("/")


def extract_poster_from_block(tag) -> str:
    """Ищет картинку-обложку внутри карточки спектакля на главной странице."""
    container = tag
    for _ in range(4):
        if container and container.parent:
            container = container.parent
            imgs = container.find_all("img")
            for img in imgs:
                src = img.get("data-src") or img.get("data-lazy") or img.get("src") or ""
                if src and not any(skip in src.lower() for skip in ("logo", "icon", "avatar", "counter", "pixel", ".svg")):
                    return normalize_img_url(src)
    return ""


def get_event_poster(event_url: str, fallback_poster: str = "") -> str:
    """Возвращает URL обложки спектакля (из кэша, из карточки или со страницы описания /e...)."""
    if event_url in POSTER_CACHE and POSTER_CACHE[event_url]:
        return POSTER_CACHE[event_url]

    if fallback_poster:
        POSTER_CACHE[event_url] = fallback_poster
        return fallback_poster

    if not event_url or event_url == TARGET_URL:
        return ""

    try:
        resp = requests.get(event_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # 1. Проверяем OpenGraph обложку (og:image)
            og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
            if og_img and og_img.get("content"):
                url = normalize_img_url(og_img["content"])
                POSTER_CACHE[event_url] = url
                return url

            # 2. Ищем постер в контенте страницы спектакля
            for img in soup.find_all("img"):
                src = img.get("data-src") or img.get("src") or ""
                if src and not any(skip in src.lower() for skip in ("logo", "icon", "avatar", "counter", "pixel", ".svg")):
                    url = normalize_img_url(src)
                    POSTER_CACHE[event_url] = url
                    return url
    except Exception as e:
        logging.warning(f"Не удалось загрузить обложку {event_url}: {e}")

    return ""


# ================= ПАРСИНГ АФИШИ И МЕСТ ПАРТЕРА =================
def fetch_available_sessions() -> Dict[str, Dict[str, Any]]:
    global LAST_CHECK_TIME
    try:
        response = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
        LAST_CHECK_TIME = datetime.now().strftime("%H:%M:%S")
        if response.status_code != 200:
            return {}
    except Exception as e:
        logging.error(f"Ошибка проверки сайта: {e}")
        return {}

    soup = BeautifulSoup(response.text, "html.parser")
    available_sessions: Dict[str, Dict[str, Any]] = {}
    current_event_title = "Спектакль"
    current_event_url = TARGET_URL
    current_poster_url = ""

    event_pattern = re.compile(r"^/orel-teatr-svobodnoe-prostranstvo/e\d+")
    session_pattern = re.compile(r"^/orel-teatr-svobodnoe-prostranstvo/s\d+")

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        text = " ".join(a_tag.get_text().split())

        if event_pattern.match(href):
            full_ev_url = BASE_URL + href
            # Если внутри ссылки лежит картинка-обложка спектакля
            img_in_a = a_tag.find("img")
            if img_in_a:
                src = img_in_a.get("data-src") or img_in_a.get("src") or ""
                if src:
                    current_poster_url = normalize_img_url(src)
                    POSTER_CACHE[full_ev_url] = current_poster_url

            if text and "подробнее" not in text.lower():
                current_event_title = text
                current_event_url = full_ev_url
                if not POSTER_CACHE.get(full_ev_url):
                    found_img = extract_poster_from_block(a_tag)
                    if found_img:
                        current_poster_url = found_img
                        POSTER_CACHE[full_ev_url] = found_img
                    else:
                        current_poster_url = ""
                else:
                    current_poster_url = POSTER_CACHE[full_ev_url]

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
                    "poster_url": current_poster_url,
                    "session_url": full_session_url,
                    "datetime": text,
                }
                available_sessions[full_session_url] = info
                SESSION_CACHE[sid] = info

    return available_sessions


def find_parterre_consecutive_seats(html: str, needed_seats: int = 2) -> Optional[Dict[str, Any]]:
    """
    Ищет заданное количество (needed_seats) свободных мест ПОДРЯД
    в одном ряду сектора «Партер» (ряды 4–12).
    """
    soup = BeautifulSoup(html, "html.parser")
    rows_map: Dict[int, List[Dict[str, Any]]] = {}

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
        if any(busy in classes for busy in ("busy", "sold", "reserved", "disabled", "occupied", "taken", "inactive", "booked")):
            continue

        sector_text = (
            f"{el.get('data-sector', '')} {el.get('data-zone', '')} "
            f"{el.get('title', '')} {el.get('aria-label', '')}"
        ).lower()

        if any(other in sector_text for other in ("балкон", "амфитеатр", "бельэтаж", "ложа", "balcony")):
            continue

        row_num = None
        seat_num = None
        seat_id = el.get("data-id") or el.get("data-place-id") or el.get("data-seat-id") or el.get("id")
        price = el.get("data-price") or ""

        if el.get("data-row") and str(el.get("data-row")).isdigit():
            row_num = int(el.get("data-row"))
        if el.get("data-place") and str(el.get("data-place")).isdigit():
            seat_num = int(el.get("data-place"))
        elif el.get("data-seat") and str(el.get("data-seat")).isdigit():
            seat_num = int(el.get("data-seat"))

        if row_num is None or seat_num is None:
            label = f"{sector_text} {el.get_text(' ', strip=True)}"
            m_row = re.search(r"ряд\s*[:№-]?\s*(\d+)", label, re.IGNORECASE)
            m_seat = re.search(r"мест[оа]\s*[:№-]?\s*(\d+)", label, re.IGNORECASE)
            if m_row and m_seat:
                row_num = int(m_row.group(1))
                seat_num = int(m_seat.group(1))

        if row_num is not None and seat_num is not None and (MIN_ROW <= row_num <= MAX_ROW):
            rows_map.setdefault(row_num, []).append({
                "id": str(seat_id or f"r{row_num}s{seat_num}"),
                "row": row_num,
                "seat": seat_num,
                "price": str(price),
            })

    if not rows_map:
        for script in soup.find_all("script"):
            stext = script.string or ""
            if not stext:
                continue
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
                r_num, s_num = int(match.group(1)), int(match.group(2))
                if MIN_ROW <= r_num <= MAX_ROW:
                    id_match = re.search(r'"id"\s*:\s*"?([a-zA-Z0-9_-]+)"?', block)
                    price_match = re.search(r'"price"\s*:\s*"?(\d+)"?', block)
                    rows_map.setdefault(r_num, []).append({
                        "id": id_match.group(1) if id_match else f"r{r_num}s{s_num}",
                        "row": r_num,
                        "seat": s_num,
                        "price": price_match.group(1) if price_match else "",
                    })

    # Проверяем ряды в порядке приоритета и ищем блок из needed_seats кресел подряд
    for r in PREFERRED_ROW_ORDER:
        seats_list = rows_map.get(r, [])
        if len(seats_list) < needed_seats:
            continue

        unique_by_num = {s["seat"]: s for s in seats_list}
        sorted_nums = sorted(unique_by_num.keys())

        consecutive_groups: List[List[Dict[str, Any]]] = []
        for i in range(len(sorted_nums) - needed_seats + 1):
            window = sorted_nums[i : i + needed_seats]
            # Проверяем, что все номера мест идут строго подряд (разница между крайними == needed_seats - 1)
            if window[-1] - window[0] == needed_seats - 1:
                consecutive_groups.append([unique_by_num[n] for n in window])

        if consecutive_groups:
            # Выбираем группу мест ближе к центру ряда (место ~11)
            consecutive_groups.sort(
                key=lambda grp: abs((sum(s["seat"] for s in grp) / len(grp)) - 11)
            )
            best_group = consecutive_groups[0]
            return {
                "row": r,
                "seats": [s["seat"] for s in best_group],
                "seat_ids": [s["id"] for s in best_group],
                "price": best_group[0]["price"],
            }

    return None


def reserve_seats(session_url: str, needed_seats: int = 2) -> Dict[str, Any]:
    """Бронирует заданное количество соседних мест в Партере (ряды 4–12) на 3 минуты."""
    req_session = requests.Session()
    req_session.headers.update(HEADERS)

    try:
        resp = req_session.get(session_url, timeout=20)
        if resp.status_code != 200:
            return {"ok": False, "error": f"Код ответа сайта: {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка сети: {e}"}

    html = resp.text
    found_group = find_parterre_consecutive_seats(html, needed_seats=needed_seats)
    soup = BeautifulSoup(html, "html.parser")
    checkout_url = session_url

    if found_group:
        seat_ids = found_group["seat_ids"]
        form = soup.find("form")
        action_url = session_url
        if form and form.get("action"):
            act = form["action"].strip()
            action_url = act if act.startswith("http") else BASE_URL + act

        for payload in (
            {"places[]": seat_ids, "action": "buy"},
            {"seats[]": seat_ids, "row": found_group["row"], "sector": "Партер"},
            {"place_ids": ",".join(seat_ids)},
        ):
            try:
                post_resp = req_session.post(
                    action_url,
                    data=payload,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": session_url},
                    timeout=10,
                    allow_redirects=True,
                )
                if post_resp.status_code == 200 and post_resp.url and post_resp.url != session_url:
                    checkout_url = post_resp.url
                    break
            except Exception:
                continue

    res_token = hashlib.md5(f"{session_url}-{time.time()}".encode()).hexdigest()[:12]
    ACTIVE_RESERVATIONS[res_token] = {
        "session_url": session_url,
        "checkout_url": checkout_url,
        "found_group": found_group,
        "needed_seats": needed_seats,
        "expires_at": time.time() + 180,
    }

    return {
        "ok": True,
        "res_token": res_token,
        "found_group": found_group,
        "needed_seats": needed_seats,
        "bridge_url": f"{RENDER_EXTERNAL_URL}/autobuy/{res_token}",
        "session_url": session_url,
    }


# ================= КЛАВИАТУРЫ И ОТПРАВКА КАРТОЧЕК С ОБЛОЖКОЙ =================
def main_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    user = ensure_user(chat_id)
    auto_str = "ВКЛ 🟢" if user.get("auto_book", False) else "ВЫКЛ 🔴"
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(KeyboardButton("🎟 Спектакли с билетами"))
    markup.add(
        KeyboardButton(f"💺 Мест подряд: {seats_cnt}"),
        KeyboardButton(f"🤖 Авто-бронь: {auto_str}"),
    )
    markup.row(KeyboardButton("📊 Статус"))
    return markup


def seats_count_inline_keyboard(current_count: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    buttons = []
    for n in range(1, 7):
        prefix = "✅ " if n == current_count else ""
        buttons.append(
            InlineKeyboardButton(
                text=f"{prefix}{seats_word(n)}",
                callback_data=f"setseats_{n}",
            )
        )
    markup.add(*buttons)
    return markup


def play_inline_keyboard(date_list: List[Dict[str, str]], seats_cnt: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    s_label = seats_word(seats_cnt)
    for item in date_list:
        sid = item["sid"]
        dt = item["datetime"]
        markup.row(
            InlineKeyboardButton(
                text=f"⚡️ {dt} — Бронь ({s_label}, Партер 4–12 ряд)",
                callback_data=f"book_{sid}",
            )
        )
        markup.row(
            InlineKeyboardButton(
                text=f"🗺 {dt} — Открыть схему зала",
                url=item["session_url"],
            )
        )
    markup.row(
        InlineKeyboardButton(
            text="ℹ️ Описание спектакля на сайте",
            url=date_list[0]["event_url"],
        )
    )
    return markup


def checkout_inline_keyboard(res_data: Dict[str, Any]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            text="💳 Перейти к покупке (Таймер 3 мин ⏱)",
            url=res_data["bridge_url"],
        )
    )
    markup.row(
        InlineKeyboardButton(
            text="🗺 Открыть схему зала вручную",
            url=res_data["session_url"],
        )
    )
    return markup


def send_play_card_with_poster(chat_id: int, title: str, date_list: List[Dict[str, Any]], seats_cnt: int) -> None:
    """
    Отправляет красиво оформленную карточку спектакля с его обложкой (постером)
    и визуальным разделителем, чтобы спектакли не сливались друг с другом.
    """
    event_url = date_list[0].get("event_url", TARGET_URL)
    fallback_poster = date_list[0].get("poster_url", "")
    poster_url = get_event_poster(event_url, fallback_poster)

    dates_preview = "\n".join([f"  • <code>{d['datetime']}</code>" for d in date_list])
    caption = (
        f"🎭 <b>{title.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Доступные сеансы ({len(date_list)}):</b>\n"
        f"{dates_preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💺 Авто-подбор: <b>Партер (4–12 ряд), {seats_word(seats_cnt)} подряд</b>"
    )
    kb = play_inline_keyboard(date_list, seats_cnt)

    if poster_url:
        try:
            bot.send_photo(
                chat_id,
                photo=poster_url,
                caption=caption,
                reply_markup=kb,
            )
            return
        except Exception as e:
            logging.warning(f"Не удалось отправить фото {poster_url}, отправляю текстом: {e}")

    bot.send_message(chat_id, caption, reply_markup=kb)


# ================= ОБРАБОТЧИКИ КОМАНД =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = ensure_user(message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    text = (
        "🎭 <b>Билеты в театр «Свободное пространство»</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Бот 24/7 отслеживает спектакли, на которые есть свободные места в зале.\n\n"
        "• <b>🎟 Спектакли с билетами:</b> показывает карточки спектаклей с обложками и кнопками быстрой брони.\n"
        f"• <b>💺 Мест подряд ({seats_word(seats_cnt)}):</b> вы можете выбрать от 1 до 6 соседних мест в Партере (ряды 4–12).\n"
        "• <b>🤖 Авто-бронь:</b> по умолчанию <b>ВЫКЛЮЧЕНА 🔴</b>."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(commands=["available"])
@bot.message_handler(func=lambda m: m.text == "🎟 Спектакли с билетами")
def cmd_available(message):
    user = ensure_user(message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    wait_msg = bot.send_message(message.chat.id, "🔍 <i>Загружаю афишу и обложки спектаклей со свободными местами...</i>")

    sessions = fetch_available_sessions()
    if not sessions:
        bot.edit_message_text(
            "😔 <b>Сейчас свободных билетов нет ни на один спектакль.</b>\n"
            "Как только они появятся в продаже, я пришлю уведомление!",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
        )
        return

    plays: Dict[str, List[Dict[str, Any]]] = OrderedDict()
    for _, info in sessions.items():
        plays.setdefault(info["title"], []).append(info)

    bot.edit_message_text(
        f"✅ <b>Найдено спектаклей с билетами: {len(plays)}</b>\n"
        f"💺 Выбрано для брони: <b>{seats_word(seats_cnt)} подряд (Партер, ряды 4–12)</b>",
        chat_id=message.chat.id,
        message_id=wait_msg.message_id,
    )

    for title, date_list in plays.items():
        send_play_card_with_poster(message.chat.id, title, date_list, seats_cnt)
        time.sleep(0.25)


@bot.message_handler(func=lambda m: m.text and m.text.startswith("💺 Мест подряд:"))
def cmd_choose_seats_count(message):
    user = ensure_user(message.chat.id)
    cur = user.get("seats_count", DEFAULT_SEATS_COUNT)
    bot.send_message(
        message.chat.id,
        f"💺 <b>Сколько мест подряд в одном ряду нужно бронировать?</b>\n\n"
        f"Сейчас выбрано: <b>{seats_word(cur)} подряд</b> (Сектор: <b>Партер, ряды 4–12</b>).\n"
        "Нажмите на нужное количество ниже:",
        reply_markup=seats_count_inline_keyboard(cur),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("setseats_"))
def cb_set_seats_count(call):
    cnt = int(call.data.replace("setseats_", "", 1))
    saved_cnt = set_user_seats_count(call.message.chat.id, cnt)
    bot.answer_callback_query(call.id, f"Выбрано: {seats_word(saved_cnt)} подряд!")

    bot.edit_message_text(
        f"✅ <b>Количество мест обновлено!</b>\n"
        f"Теперь бот будет искать и бронировать <b>{seats_word(saved_cnt)} подряд в одном ряду</b> (Партер, ряды 4–12).",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=seats_count_inline_keyboard(saved_cnt),
    )
    bot.send_message(
        call.message.chat.id,
        f"🔄 Кнопка меню обновлена на <b>«💺 Мест подряд: {saved_cnt}»</b>.",
        reply_markup=main_keyboard(call.message.chat.id),
    )


@bot.message_handler(func=lambda m: m.text and m.text.startswith("🤖 Авто-бронь:"))
def cmd_toggle_auto(message):
    new_state = toggle_user_autobook(message.chat.id)
    user = ensure_user(message.chat.id)
    s_word = seats_word(user.get("seats_count", DEFAULT_SEATS_COUNT))
    status_text = (
        f"🟢 <b>Авто-бронь ВКЛЮЧЕНА!</b>\n"
        f"При выходе нового спектакля бот сам забронирует <b>{s_word} подряд</b> в Партере (4–12 ряд) на 3 минуты."
        if new_state
        else "🔴 <b>Авто-бронь ВЫКЛЮЧЕНА!</b>\n"
        "Бот будет присылать карточки новых спектаклей с обложками, а бронировать места вы сможете нажатием кнопки."
    )
    bot.send_message(message.chat.id, status_text, reply_markup=main_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text and m.text.startswith("📊 Статус"))
def cmd_status(message):
    user = ensure_user(message.chat.id)
    known = load_json(STATE_FILE, {})
    auto_str = "Включена 🟢" if user.get("auto_book", False) else "Выключена 🔴"
    s_word = seats_word(user.get("seats_count", DEFAULT_SEATS_COUNT))
    text = (
        "📊 <b>Статус системы</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Сервер:</b> Активен 24/7 (Защита от сна включена)\n"
        f"🕒 <b>Последняя проверка:</b> <code>{LAST_CHECK_TIME}</code>\n"
        f"🎟 <b>Сеансов с билетами:</b> {len(known)}\n"
        f"💺 <b>Параметры брони:</b> Партер, ряды 4–12, <b>{s_word} подряд</b>\n"
        f"🤖 <b>Авто-бронь при выходе:</b> {auto_str}"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(message.chat.id))


@bot.callback_query_handler(func=lambda call: call.data.startswith("book_"))
def cb_book_seats(call):
    sid = call.data.replace("book_", "", 1)
    user = ensure_user(call.message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    s_word = seats_word(seats_cnt)

    info = SESSION_CACHE.get(sid)
    if not info:
        fetch_available_sessions()
        info = SESSION_CACHE.get(sid)

    if not info:
        bot.answer_callback_query(call.id, "Билеты на этот сеанс уже закончились!", show_alert=True)
        return

    bot.answer_callback_query(call.id, f"⚡️ Ищу {s_word} подряд в партере...")
    msg = bot.send_message(
        call.message.chat.id,
        f"⏳ Ищу <b>{s_word} подряд</b> в Партере (ряды 4–12) на спектакль <b>«{info['title']}»</b> ({info['datetime']})...",
    )

    res = reserve_seats(info["session_url"], needed_seats=seats_cnt)
    if not res["ok"]:
        bot.edit_message_text(
            f"❌ Ошибка соединения с сайтом: {res.get('error')}",
            chat_id=call.message.chat.id,
            message_id=msg.message_id,
        )
        return

    grp = res.get("found_group")
    if grp:
        seats_str = ", ".join(str(s) for s in grp["seats"])
        text = (
            f"✅ <b>БРОНЬ НА 3 МИНУТЫ АКТИВИРОВАНА ({s_word.upper()})!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎭 <b>{info['title']}</b>\n"
            f"📅 Сеанс: <b>{info['datetime']}</b>\n"
            f"🏛 Сектор: <b>Партер, Ряд {grp['row']}</b>\n"
            f"💺 Места подряд: <b>{seats_str}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 <b>Нажмите кнопку ниже</b>, чтобы завершить покупку за 3 минуты:"
        )
    else:
        text = (
            f"⚡️ <b>Переход к покупке подготовлен!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎭 <b>{info['title']}</b> ({info['datetime']})\n"
            f"В рядах 4–12 партера сейчас не нашлось <b>{s_word} строго подряд</b> (или схема загружается в браузере).\n\n"
            "👇 Нажмите кнопку ниже для мгновенного перехода к выбору мест:"
        )

    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=msg.message_id,
        reply_markup=checkout_inline_keyboard(res),
    )


# ================= ФОНОВЫЙ МОНИТОРИНГ И ЗАЩИТА ОТ СНА =================
def monitor_loop():
    known_available = load_json(STATE_FILE, {})
    is_first_run = not os.path.exists(STATE_FILE)
    logging.info("Фоновый мониторинг билетов запущен...")

    while True:
        try:
            current_available = fetch_available_sessions()
            if not is_first_run and current_available:
                for session_url, info in current_available.items():
                    if session_url not in known_available:
                        users = get_users()
                        for uid_str, u_cfg in users.items():
                            chat_id = int(uid_str)
                            seats_cnt = u_cfg.get("seats_count", DEFAULT_SEATS_COUNT)
                            s_word = seats_word(seats_cnt)
                            try:
                                if u_cfg.get("auto_book", False):
                                    res = reserve_seats(session_url, needed_seats=seats_cnt)
                                    grp = res.get("found_group")
                                    seats_str = ", ".join(str(s) for s in grp["seats"]) if grp else ""
                                    seat_line = (
                                        f"🏛 <b>Забронировано ({s_word}): Партер, Ряд {grp['row']}, Места: {seats_str}</b>\n\n"
                                        if grp else ""
                                    )
                                    bot.send_message(
                                        chat_id,
                                        f"🔥 <b>НОВЫЕ БИЛЕТЫ (АВТО-БРОНЬ 3 МИН)!</b>\n\n"
                                        f"🎭 <b>{info['title']}</b>\n"
                                        f"📅 {info['datetime']}\n\n"
                                        f"{seat_line}"
                                        "👇 Срочно перейдите к покупке:",
                                        reply_markup=checkout_inline_keyboard(res),
                                    )
                                else:
                                    send_play_card_with_poster(chat_id, info["title"], [info], seats_cnt)
                            except Exception as e:
                                logging.error(f"Ошибка отправки {chat_id}: {e}")

            known_available = current_available
            save_json(STATE_FILE, known_available)
            if is_first_run:
                is_first_run = False
        except Exception as e:
            logging.error(f"Ошибка цикла мониторинга: {e}")

        time.sleep(CHECK_INTERVAL + random.uniform(1, 5))


def keep_alive_loop():
    """Каждые 10 минут пингует внешний адрес Render, чтобы сервер никогда не засыпал."""
    ping_url = f"{RENDER_EXTERNAL_URL}/ping"
    while True:
        time.sleep(600)
        try:
            requests.get(ping_url, timeout=15)
            logging.info("Keep-alive ping успешно выполнен")
        except Exception as e:
            logging.warning(f"Ошибка keep-alive пинга: {e}")


@app.route("/")
@app.route("/ping")
def home():
    return f"Bot is alive! Last check: {LAST_CHECK_TIME}", 200


@app.route("/autobuy/<res_token>")
def autobuy_bridge(res_token: str):
    data = ACTIVE_RESERVATIONS.get(res_token)
    if not data:
        return redirect(TARGET_URL)

    target_checkout = data.get("checkout_url") or data.get("session_url") or TARGET_URL
    grp = data.get("found_group")
    rem = max(0, int(data.get("expires_at", time.time()) - time.time()))

    seat_box = ""
    if grp:
        seats_str = ", ".join(str(s) for s in grp["seats"])
        seat_box = (
            f"<p style='font-size:20px;color:#38bdf8;font-weight:bold;'>"
            f"🏛 Партер, Ряд {grp['row']} | Места подряд: {seats_str}</p>"
        )

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="1;url={target_checkout}">
    <title>Переход к покупке (3 мин)</title>
</head>
<body style="background:#0f172a;color:#fff;font-family:sans-serif;text-align:center;padding:40px 16px;">
    <h2>⚡️ Перенаправляем на страницу оплаты...</h2>
    <p style="color:#fbbf24;font-weight:bold;">⏱ Осталось времени на бронь: {rem // 60} мин {rem % 60} сек</p>
    {seat_box}
    <a href="{target_checkout}" style="display:inline-block;margin-top:20px;padding:14px 24px;background:#22c55e;color:#fff;text-decoration:none;border-radius:10px;font-weight:bold;">
        🎟 Открыть страницу покупки
    </a>
    <script>setTimeout(function(){{ window.location.replace("{target_checkout}"); }}, 500);</script>
</body>
</html>"""
    return Response(html_page, mimetype="text/html")


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=False)
        except Exception as e:
            logging.error(f"Перезапуск Telegram polling: {e}")
            time.sleep(5)
