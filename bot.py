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
from flask import Flask, redirect, Response

# ================= НАСТРОЙКИ СИСТЕМЫ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "123456789:AAPlaceholderTokenForQuickTicketsBot")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://theter-bot.onrender.com").rstrip("/")

TARGET_URL = "https://quicktickets.ru/orel-teatr-svobodnoe-prostranstvo"
BASE_URL = "https://quicktickets.ru"
ORGANISATION_ALIAS = "orel-teatr-svobodnoe-prostranstvo"

STATE_FILE = "sessions_state.json"
USERS_FILE = "subscribers_v5.json"
CHECK_INTERVAL = 35

MIN_ROW = 4
MAX_ROW = 12
DEFAULT_SEATS_COUNT = 2
DEFAULT_PREFERRED_ROW = "any"  # "any" (любой 4..12) или число 4..12
PREFERRED_ROW_ORDER = [6, 7, 5, 8, 4, 9, 10, 11, 12]
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
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

ACTIVE_RESERVATIONS: Dict[str, Dict[str, Any]] = {}
SESSION_CACHE: Dict[str, Dict[str, Any]] = {}
HALL_STATIC_CACHE: Dict[str, Dict[str, Any]] = {}
POSTER_CACHE: Dict[str, str] = {}
LAST_CHECK_TIME: str = "Запускается..."


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
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
        users[uid] = {
            "auto_book": False,
            "seats_count": DEFAULT_SEATS_COUNT,
            "preferred_row": DEFAULT_PREFERRED_ROW,
        }
        changed = True
    else:
        if "auto_book" not in users[uid]:
            users[uid]["auto_book"] = False
            changed = True
        if "seats_count" not in users[uid]:
            users[uid]["seats_count"] = DEFAULT_SEATS_COUNT
            changed = True
        if "preferred_row" not in users[uid]:
            users[uid]["preferred_row"] = DEFAULT_PREFERRED_ROW
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


def set_user_preferred_row(chat_id: int, row_val: Any) -> Any:
    users = get_users()
    uid = str(chat_id)
    ensure_user(chat_id)
    if str(row_val).lower() == "any":
        users[uid]["preferred_row"] = "any"
    else:
        try:
            r = int(row_val)
            if MIN_ROW <= r <= MAX_ROW:
                users[uid]["preferred_row"] = r
        except Exception:
            users[uid]["preferred_row"] = "any"
    save_json(USERS_FILE, users)
    return users[uid]["preferred_row"]


def short_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:10]


def seats_word(n: int) -> str:
    if n == 1:
        return "1 место"
    elif 2 <= n <= 4:
        return f"{n} места"
    return f"{n} мест"


def row_label(pref: Any) -> str:
    if str(pref).lower() == "any" or pref is None:
        return "Любой (4–12)"
    return f"Ряд {pref}"


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


def get_event_poster(event_url: str, fallback_poster: str = "") -> str:
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
            og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
            if og_img and og_img.get("content"):
                url = normalize_img_url(og_img["content"])
                POSTER_CACHE[event_url] = url
                return url
            for img in soup.find_all("img"):
                src = img.get("data-src") or img.get("src") or ""
                if src and not any(skip in src.lower() for skip in ("logo", "icon", "avatar", "counter", "pixel", ".svg")):
                    url = normalize_img_url(src)
                    POSTER_CACHE[event_url] = url
                    return url
    except Exception:
        pass
    return ""


# ================= ВЗАИМОДЕЙСТВИЕ С QUICKTICKETS API =================
def fetch_session_meta(session_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Возвращает (token, elem_id, raw_html) сеанса QuickTickets.
    """
    try:
        resp = requests.get(session_url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None, None, None
        html = resp.text
        token_match = re.search(r"token:\s*['\"]([^'\"]+)['\"]", html)
        elem_match = re.search(r"elem:\s*\{\s*type:\s*['\"]session['\"],\s*id:\s*(\d+)", html)
        token = token_match.group(1) if token_match else None
        elem_id = elem_match.group(1) if elem_match else None
        if not elem_id:
            m_s = re.search(r"/s(\d+)", session_url)
            if m_s:
                elem_id = m_s.group(1)
        return token, elem_id, html
    except Exception as e:
        logging.error(f"Ошибка получения метаданных сеанса {session_url}: {e}")
        return None, None, None


def get_hall_places_and_live_status(session_url: str) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str], Optional[str]]:
    """
    Получает актуальные данные зала (геометрию и занятые места) через QuickTickets API.
    Возвращает (hall_places, busy_places, token, elem_id).
    """
    token, elem_id, _ = fetch_session_meta(session_url)
    if not token or not elem_id:
        return {}, {}, None, None

    api_headers = {
        "Authorization": "Basic " + token,
        "Api-Id": "quick-tickets",
        "Cache-Control": "no-cache",
        "User-Agent": HEADERS["User-Agent"],
        "Origin": "https://hall.quicktickets.ru",
        "Referer": "https://hall.quicktickets.ru/",
    }
    params = {
        "scope": "qt",
        "panel": "site",
        "user_id": "0",
        "organisation_alias": ORGANISATION_ALIAS,
        "elem_type": "session",
        "elem_id": elem_id,
    }

    # Статический зал кэшируем для экономии трафика и ускорения
    hall_places = HALL_STATIC_CACHE.get(elem_id)
    if not hall_places:
        try:
            r_hall = requests.get("https://api.quicktickets.ru/v1/hall/hall", headers=api_headers, params=params, timeout=10)
            if r_hall.status_code == 200:
                h_data = r_hall.json()
                hall_places = h_data.get("response", {}).get("places", {})
                if hall_places:
                    HALL_STATIC_CACHE[elem_id] = hall_places
        except Exception as e:
            logging.error(f"Ошибка загрузки геометрии зала {elem_id}: {e}")

    if not hall_places:
        hall_places = {}

    # Занятые / проданные места всегда запрашиваем в реальном времени
    busy_places = {}
    try:
        r_any = requests.get("https://api.quicktickets.ru/v1/anyticket/anyticket", headers=api_headers, params=params, timeout=10)
        if r_any.status_code == 200:
            busy_places = r_any.json().get("response", {}).get("places", {})
    except Exception as e:
        logging.error(f"Ошибка загрузки статусов билетов {elem_id}: {e}")

    return hall_places, busy_places, token, elem_id


def get_parterre_rows_stats(session_url: str) -> Dict[int, Dict[str, Any]]:
    """
    Анализирует наличие свободных мест в партере для каждого ряда с 4 по 12.
    Возвращает:
    {
       row_num: {
          "free_seats": [ {place_object}, ... ],
          "count": int,
          "min_price": int,
          "max_price": int,
       }
    }
    """
    hall_places, busy_places, _, _ = get_hall_places_and_live_status(session_url)
    stats: Dict[int, Dict[str, Any]] = {r: {"free_seats": [], "count": 0, "min_price": 0, "max_price": 0} for r in range(MIN_ROW, MAX_ROW + 1)}

    for pid, pl in hall_places.items():
        if pl.get("block") == "Партер":
            r_str = pl.get("series")
            if r_str and r_str.isdigit() and MIN_ROW <= int(r_str) <= MAX_ROW:
                r_num = int(r_str)
                # Место свободно, если его нет в anyticket или статус 'free'
                is_free = (pid not in busy_places) or (busy_places.get(pid, {}).get("status") == "free")
                if is_free:
                    stats[r_num]["free_seats"].append(pl)

    for r_num, data in stats.items():
        data["free_seats"].sort(key=lambda x: int(x.get("place", 0)))
        data["count"] = len(data["free_seats"])
        if data["count"] > 0:
            prices = [int(p.get("price", 0)) for p in data["free_seats"] if p.get("price")]
            if prices:
                data["min_price"] = min(prices)
                data["max_price"] = max(prices)

    return stats


def find_best_consecutive_group(seats_list: List[Dict[str, Any]], needed_seats: int) -> Optional[List[Dict[str, Any]]]:
    """
    Ищет группу из needed_seats подряд идущих мест, предпочитая места ближе к центру (место ~11).
    """
    if len(seats_list) < needed_seats:
        return None

    unique_by_num: Dict[int, Dict[str, Any]] = {}
    for s in seats_list:
        try:
            p_num = int(s.get("place", 0))
            unique_by_num[p_num] = s
        except Exception:
            pass

    sorted_nums = sorted(unique_by_num.keys())
    consecutive_groups: List[List[Dict[str, Any]]] = []

    for i in range(len(sorted_nums) - needed_seats + 1):
        window = sorted_nums[i : i + needed_seats]
        if window[-1] - window[0] == needed_seats - 1:
            consecutive_groups.append([unique_by_num[n] for n in window])

    if consecutive_groups:
        consecutive_groups.sort(key=lambda grp: abs((sum(int(s["place"]) for s in grp) / len(grp)) - 11))
        return consecutive_groups[0]

    return None


def select_seats_for_purchase(
    session_url: str,
    needed_seats: int = 2,
    target_row: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Выбирает наилучшие места в рядах 4..12 партера (или в конкретном ряду target_row).
    """
    rows_stats = get_parterre_rows_stats(session_url)

    # Если указан конкретный ряд
    if target_row is not None and MIN_ROW <= target_row <= MAX_ROW:
        row_data = rows_stats.get(target_row)
        if row_data and row_data["count"] >= needed_seats:
            grp = find_best_consecutive_group(row_data["free_seats"], needed_seats)
            if grp:
                total_price = sum(int(s.get("price", 0)) for s in grp)
                return {
                    "row": target_row,
                    "seats": [int(s["place"]) for s in grp],
                    "seat_ids": [int(s["id"]) for s in grp],
                    "price_per_ticket": int(grp[0].get("price", 0)),
                    "total_price": total_price,
                    "is_consecutive": True,
                }
            # Если подряд нет, берем ближайшие доступные в этом ряду
            closest = row_data["free_seats"][:needed_seats]
            total_price = sum(int(s.get("price", 0)) for s in closest)
            return {
                "row": target_row,
                "seats": [int(s["place"]) for s in closest],
                "seat_ids": [int(s["id"]) for s in closest],
                "price_per_ticket": int(closest[0].get("price", 0)),
                "total_price": total_price,
                "is_consecutive": False,
            }
        return None

    # Если "Любой лучший ряд" (по PREFERRED_ROW_ORDER)
    for r in PREFERRED_ROW_ORDER:
        row_data = rows_stats.get(r)
        if row_data and row_data["count"] >= needed_seats:
            grp = find_best_consecutive_group(row_data["free_seats"], needed_seats)
            if grp:
                total_price = sum(int(s.get("price", 0)) for s in grp)
                return {
                    "row": r,
                    "seats": [int(s["place"]) for s in grp],
                    "seat_ids": [int(s["id"]) for s in grp],
                    "price_per_ticket": int(grp[0].get("price", 0)),
                    "total_price": total_price,
                    "is_consecutive": True,
                }

    # Если подряд ни в одном ряду не нашлось, ищем любой ряд с достаточным кол-вом мест
    for r in PREFERRED_ROW_ORDER:
        row_data = rows_stats.get(r)
        if row_data and row_data["count"] >= needed_seats:
            closest = row_data["free_seats"][:needed_seats]
            total_price = sum(int(s.get("price", 0)) for s in closest)
            return {
                "row": r,
                "seats": [int(s["place"]) for s in closest],
                "seat_ids": [int(s["id"]) for s in closest],
                "price_per_ticket": int(closest[0].get("price", 0)),
                "total_price": total_price,
                "is_consecutive": False,
            }

    return None


def execute_one_click_booking(
    session_url: str,
    needed_seats: int = 2,
    target_row: Optional[int] = None,
    play_title: str = "Спектакль",
    play_datetime: str = ""
) -> Dict[str, Any]:
    """
    МГНОВЕННАЯ ПОКУПКА В 1 КЛИК:
    1. Находит свободные места в партере (ряды 4..12 или target_row).
    2. Отправляет запрос /ordering/initAnytickets на QuickTickets для бронирования мест.
    3. Получает anyticketsCodes и готовит POST-форму передачи сессии пользователю.
    4. Генерирует одноразовый токен и мост-ссылку для передачи сессии с таймером 3 минуты.
    """
    s_group = select_seats_for_purchase(session_url, needed_seats=needed_seats, target_row=target_row)
    if not s_group:
        row_msg = f"в ряду {target_row}" if target_row else "в рядах 4–12"
        return {
            "ok": False,
            "error": f"К сожалению, {row_msg} сейчас нет {seats_word(needed_seats)}.",
            "session_url": session_url,
        }

    token, elem_id, _ = fetch_session_meta(session_url)
    if not elem_id:
        return {"ok": False, "error": "Не удалось определить ID сеанса QuickTickets.", "session_url": session_url}

    req_session = requests.Session()
    req_session.headers.update(HEADERS)
    req_session.headers["Referer"] = session_url

    # Выполняем GET сеанса, чтобы получить валидный PHPSESSID
    try:
        req_session.get(session_url, timeout=12)
    except Exception:
        pass

    ajax_headers = dict(HEADERS)
    ajax_headers["X-Requested-With"] = "XMLHttpRequest"
    ajax_headers["Referer"] = session_url

    payload = {
        "organisationAlias": ORGANISATION_ALIAS,
        "elemType": "session",
        "elemId": elem_id,
        "collectiveSell": 0,
        "sessionAnyplaces[hallplaces][]": s_group["seat_ids"],
        "sessionAnyplaces[count]": len(s_group["seat_ids"]),
        "sessionAnyplaces[amount]": s_group["total_price"],
    }

    try:
        r_init = req_session.post("https://quicktickets.ru/ordering/initAnytickets", data=payload, headers=ajax_headers, timeout=12)
        if r_init.status_code != 200:
            return {"ok": False, "error": f"Ошибка ответа QuickTickets: статус {r_init.status_code}", "session_url": session_url}

        j_init = r_init.json()
        if j_init.get("result") != "success":
            err_msg = j_init.get("data", "Места уже были заняты кем-то другим.")
            return {"ok": False, "error": str(err_msg), "session_url": session_url}

        data_resp = j_init.get("data", {})
        anytickets_codes = data_resp.get("anyticketsCodes", [])
        select_count = data_resp.get("selectAnyplacesCount", len(s_group["seat_ids"]))

        if not anytickets_codes:
            return {"ok": False, "error": "Сервер QuickTickets не вернул коды билетов.", "session_url": session_url}

    except Exception as e:
        logging.error(f"Исключение при initAnytickets: {e}")
        return {"ok": False, "error": f"Сетевая ошибка при связи с QuickTickets: {e}", "session_url": session_url}

    # Создаем защищенную сессию для моста оплаты
    res_token = hashlib.md5(f"{session_url}-{time.time()}-{random.random()}".encode()).hexdigest()[:12]
    now_ts = time.time()

    ACTIVE_RESERVATIONS[res_token] = {
        "token": res_token,
        "session_url": session_url,
        "elem_id": elem_id,
        "organisationAlias": ORGANISATION_ALIAS,
        "title": play_title,
        "datetime": play_datetime,
        "row": s_group["row"],
        "seats": s_group["seats"],
        "seat_ids": s_group["seat_ids"],
        "total_price": s_group["total_price"],
        "price_per_ticket": s_group["price_per_ticket"],
        "is_consecutive": s_group["is_consecutive"],
        "anyticketsCodes": anytickets_codes,
        "selectAnyplacesCount": select_count,
        "cookies": req_session.cookies.get_dict(),
        "created_at": now_ts,
        "expires_at": now_ts + 180,  # 3 минуты таймер QuickTickets
    }

    bridge_url = f"{RENDER_EXTERNAL_URL}/autobuy/{res_token}"

    return {
        "ok": True,
        "res_token": res_token,
        "bridge_url": bridge_url,
        "row": s_group["row"],
        "seats": s_group["seats"],
        "total_price": s_group["total_price"],
        "is_consecutive": s_group["is_consecutive"],
        "session_url": session_url,
        "expires_in": 180,
    }


# ================= ПАРСИНГ АФИШИ =================
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
                    found_img = get_event_poster(full_ev_url)
                    if found_img:
                        current_poster_url = found_img

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


# ================= КЛАВИАТУРЫ TELEGRAM =================
def main_reply_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    user = ensure_user(chat_id)
    auto_str = "ВКЛ 🟢" if user.get("auto_book", False) else "ВЫКЛ 🔴"
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    r_pref = user.get("preferred_row", DEFAULT_PREFERRED_ROW)

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(KeyboardButton("🎟 Афиша и билеты"))
    markup.add(
        KeyboardButton(f"💺 Мест: {seats_cnt}"),
        KeyboardButton(f"🎯 Ряд: {row_label(r_pref)}"),
    )
    markup.add(
        KeyboardButton(f"🤖 Авто-бронь: {auto_str}"),
        KeyboardButton("📊 Статус"),
    )
    markup.row(KeyboardButton("ℹ️ Помощь"))
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
    markup.row(InlineKeyboardButton("🔙 Назад в меню", callback_data="close_menu"))
    return markup


def preferred_row_inline_keyboard(current_row: Any) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    any_prefix = "✅ " if str(current_row).lower() == "any" else ""
    markup.row(
        InlineKeyboardButton(
            text=f"{any_prefix}⚡️ Любой лучший ряд (4–12)",
            callback_data="setrow_any",
        )
    )
    buttons = []
    for r in range(MIN_ROW, MAX_ROW + 1):
        prefix = "✅ " if str(current_row) == str(r) else ""
        buttons.append(
            InlineKeyboardButton(
                text=f"{prefix}Ряд {r}",
                callback_data=f"setrow_{r}",
            )
        )
    markup.add(*buttons)
    markup.row(InlineKeyboardButton("🔙 Назад в меню", callback_data="close_menu"))
    return markup


def play_card_inline_keyboard(
    sid: str,
    session_url: str,
    event_url: str,
    seats_cnt: int,
    pref_row: Any
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    r_str = row_label(pref_row)
    s_str = seats_word(seats_cnt)

    # Главная кнопка быстрой покупки в 1 клик
    markup.row(
        InlineKeyboardButton(
            text=f"⚡️ Купить в 1 клик ({r_str}, {s_str})",
            callback_data=f"quickbuy_{sid}",
        )
    )
    # Кнопка перехода к выбору конкретного ряда 4..12
    markup.row(
        InlineKeyboardButton(
            text="🎯 Выбрать место с 4 по 12 ряд в 1 клик ▾",
            callback_data=f"rowsmenu_{sid}",
        )
    )
    # Схема зала и обновление
    markup.row(
        InlineKeyboardButton(text="🗺 Схема зала на сайте", url=session_url),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{sid}"),
    )
    return markup


def rows_selection_inline_keyboard(
    sid: str,
    rows_stats: Dict[int, Dict[str, Any]],
    needed_seats: int
) -> InlineKeyboardMarkup:
    """
    Клавиатура с кнопками для КАЖДОГО ряда с 4 по 12!
    Один клик по ряду сразу бронирует места в этом ряду!
    """
    markup = InlineKeyboardMarkup(row_width=2)
    s_label = seats_word(needed_seats)

    buttons = []
    for r in range(MIN_ROW, MAX_ROW + 1):
        r_info = rows_stats.get(r, {"count": 0, "min_price": 0})
        cnt = r_info["count"]
        price = r_info.get("min_price", 0)

        if cnt >= needed_seats:
            text = f"🟢 Ряд {r} ({cnt} мест, {price} ₽)"
            cb_data = f"buyrow_{sid}_{r}"
        elif cnt > 0:
            text = f"🟡 Ряд {r} (всего {cnt} м.)"
            cb_data = f"buyrow_{sid}_{r}"
        else:
            text = f"⚪️ Ряд {r} (мест нет)"
            cb_data = f"rowempty_{sid}_{r}"

        buttons.append(InlineKeyboardButton(text=text, callback_data=cb_data))

    markup.add(*buttons)
    # Кнопка авто-выбора любого лучшего ряда
    markup.row(
        InlineKeyboardButton(
            text=f"⚡️ Купить в лучшем ряду ({s_label})",
            callback_data=f"quickbuy_{sid}",
        )
    )
    markup.row(
        InlineKeyboardButton(text="🔙 Назад к карточке спектакля", callback_data=f"backcard_{sid}")
    )
    return markup


def checkout_bridge_keyboard(res_data: Dict[str, Any]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            text="💳 ОПЛАТИТЬ НА QUICKTICKETS (3 мин ⏱)",
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


# ================= ОТПРАВКА КАРТОЧКИ СПЕКТАКЛЯ =================
def build_play_card_text(
    title: str,
    dt_str: str,
    seats_cnt: int,
    pref_row: Any,
    rows_stats: Dict[int, Dict[str, Any]]
) -> str:
    s_label = seats_word(seats_cnt)
    r_label = row_label(pref_row)

    total_parterre = sum(d["count"] for d in rows_stats.values())

    # Формируем краткий визуальный список свободных рядов
    free_rows_preview = []
    for r in range(MIN_ROW, MAX_ROW + 1):
        c = rows_stats[r]["count"]
        if c > 0:
            p = rows_stats[r]["min_price"]
            price_tag = f" — {p} ₽" if p else ""
            free_rows_preview.append(f"  • <b>Ряд {r}:</b> {c} мест{price_tag}")

    if free_rows_preview:
        rows_text = "\n".join(free_rows_preview)
    else:
        rows_text = "  <i>В рядах 4–12 сейчас нет доступных мест</i>"

    text = (
        f"🎭 <b>{title.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Сеанс: <b>{dt_str}</b>\n"
        f"🏛 Сектор: <b>Партер (ряды 4–12)</b>\n"
        f"🎟 Свободно в рядах 4–12: <b>{total_parterre} мест</b>\n\n"
        f"<b>Наличие мест по рядам:</b>\n"
        f"{rows_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💺 Выбрано для заказа: <b>{s_label}</b> | <b>{r_label}</b>\n"
        f"<i>💡 Нажмите кнопку ниже, чтобы купить в 1 клик или выбрать ряд:</i>"
    )
    return text


def send_play_card(chat_id: int, info: Dict[str, Any], user_cfg: Dict[str, Any]) -> None:
    sid = info["sid"]
    title = info["title"]
    dt = info["datetime"]
    session_url = info["session_url"]
    event_url = info.get("event_url", TARGET_URL)
    fallback_poster = info.get("poster_url", "")

    seats_cnt = user_cfg.get("seats_count", DEFAULT_SEATS_COUNT)
    pref_row = user_cfg.get("preferred_row", DEFAULT_PREFERRED_ROW)

    rows_stats = get_parterre_rows_stats(session_url)
    caption = build_play_card_text(title, dt, seats_cnt, pref_row, rows_stats)
    kb = play_card_inline_keyboard(sid, session_url, event_url, seats_cnt, pref_row)

    poster_url = get_event_poster(event_url, fallback_poster)
    if poster_url:
        try:
            bot.send_photo(chat_id, photo=poster_url, caption=caption, reply_markup=kb)
            return
        except Exception:
            pass

    bot.send_message(chat_id, caption, reply_markup=kb)


# ================= ХЭНДЛЕРЫ TELEGRAM =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = ensure_user(message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    pref_row = user.get("preferred_row", DEFAULT_PREFERRED_ROW)

    text = (
        "🎭 <b>Билеты в театр «Свободное пространство» (QuickTickets)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 Бот для мгновенного мониторинга и <b>покупки билетов в 1 клик</b>!\n\n"
        "⚡️ <b>Ключевые возможности:</b>\n"
        "• <b>Выбор мест с 4 по 12 ряд в 1 клик</b> — бот сам блокирует места через QuickTickets API.\n"
        "• <b>Мгновенная передача сессии</b> — переход по ссылке сразу открывает страницу оплаты с вашими местами в корзине!\n"
        "• <b>24/7 Авто-бронь</b> — при появлении билетов бот сам бронирует их и присылает готовую ссылку с таймером 3 минуты.\n\n"
        f"⚙️ <b>Текущие настройки:</b>\n"
        f"• Количество мест: <b>{seats_word(seats_cnt)} подряд</b>\n"
        f"• Выбранный ряд: <b>{row_label(pref_row)}</b>\n"
        f"• Авто-бронь: <b>{'ВКЛЮЧЕНА 🟢' if user.get('auto_book') else 'ВЫКЛЮЧЕНА 🔴'}</b>\n\n"
        "👇 <i>Используйте кнопки меню внизу для управления:</i>"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard(message.chat.id))


@bot.message_handler(commands=["afisha", "available"])
@bot.message_handler(func=lambda m: m.text in ("🎟 Афиша и билеты", "🎟 Спектакли с билетами"))
def cmd_afisha(message):
    user = ensure_user(message.chat.id)
    wait_msg = bot.send_message(message.chat.id, "🔍 <i>Сканирую афишу QuickTickets и свободные места в партере...</i>")

    sessions = fetch_available_sessions()
    if not sessions:
        bot.edit_message_text(
            "😔 <b>Сейчас в театре нет доступных сеансов с открытой продажей билетов.</b>\n"
            "Бот продолжает круглосуточный мониторинг и сразу уведомит вас при появлении мест!",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
        )
        return

    bot.edit_message_text(
        f"✅ <b>Найдено сеансов с билетами: {len(sessions)}</b>\n"
        f"💺 Загружаю интерактивные карточки с выбором мест (ряды 4–12)...",
        chat_id=message.chat.id,
        message_id=wait_msg.message_id,
    )

    for _, info in sessions.items():
        send_play_card(message.chat.id, info, user)
        time.sleep(0.3)


@bot.message_handler(func=lambda m: m.text and m.text.startswith("💺 Мест:"))
def cmd_seats_settings(message):
    user = ensure_user(message.chat.id)
    cur = user.get("seats_count", DEFAULT_SEATS_COUNT)
    text = (
        f"💺 <b>Сколько мест подряд в партере нужно бронировать?</b>\n\n"
        f"Сейчас выбрано: <b>{seats_word(cur)} подряд</b> (ряды 4–12).\n"
        f"Выберите нужное количество кнопками ниже:"
    )
    bot.send_message(message.chat.id, text, reply_markup=seats_count_inline_keyboard(cur))


@bot.message_handler(func=lambda m: m.text and m.text.startswith("🎯 Ряд:"))
def cmd_row_settings(message):
    user = ensure_user(message.chat.id)
    cur_row = user.get("preferred_row", DEFAULT_PREFERRED_ROW)
    text = (
        f"🎯 <b>Настройка ряда по умолчанию (Партер 4–12 ряд)</b>\n\n"
        f"Сейчас установлено: <b>{row_label(cur_row)}</b>.\n\n"
        f"• При выборе <i>«Любой лучший ряд»</i> бот выберет лучший ряд из доступных (6, 7, 5, 8, 4...).\n"
        f"• Либо зафиксируйте конкретный ряд:"
    )
    bot.send_message(message.chat.id, text, reply_markup=preferred_row_inline_keyboard(cur_row))


@bot.message_handler(func=lambda m: m.text and m.text.startswith("🤖 Авто-бронь:"))
def cmd_toggle_auto(message):
    new_state = toggle_user_autobook(message.chat.id)
    if new_state:
        msg = (
            "🟢 <b>АВТО-БРОНЬ ВКЛЮЧЕНА!</b>\n\n"
            "Как только появятся билеты в партере (ряды 4–12), бот <b>автоматически забронирует</b> лучшие места "
            "и пришлет готовую ссылку для оплаты. Таймер удержания мест — 3 минуты!"
        )
    else:
        msg = (
            "🔴 <b>АВТО-БРОНЬ ВЫКЛЮЧЕНА.</b>\n\n"
            "При появлении билетов бот просто пришлет карточку со свободными местами и кнопками быстрой покупки."
        )
    bot.send_message(message.chat.id, msg, reply_markup=main_reply_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text and m.text.startswith("📊 Статус"))
def cmd_status(message):
    user = ensure_user(message.chat.id)
    known = load_json(STATE_FILE, {})
    auto_str = "Включена 🟢" if user.get("auto_book", False) else "Выключена 🔴"
    s_word = seats_word(user.get("seats_count", DEFAULT_SEATS_COUNT))
    r_str = row_label(user.get("preferred_row", DEFAULT_PREFERRED_ROW))

    text = (
        "📊 <b>Панель мониторинга QuickTickets</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Сервер бота:</b> Активен 24/7\n"
        f"🕒 <b>Последняя проверка:</b> <code>{LAST_CHECK_TIME}</code>\n"
        f"🎭 <b>Сеансов в базе:</b> {len(known)}\n"
        f"💺 <b>Настройки брони:</b> {s_word} | {r_str}\n"
        f"🤖 <b>Режим авто-покупки:</b> {auto_str}\n"
        f"🌐 <b>Web-мост сессий:</b> <code>{RENDER_EXTERNAL_URL}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Бот сканирует театр каждые 35 секунд и держит соединение с QuickTickets.</i>"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard(message.chat.id))


@bot.message_handler(func=lambda m: m.text and m.text.startswith("ℹ️ Помощь"))
@bot.message_handler(commands=["help"])
def cmd_help(message):
    text = (
        "ℹ️ <b>Как работает покупка в 1 клик и передача сессии:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "1. <b>Быстрая покупка в 1 клик:</b>\n"
        "Нажмите ⚡️ <i>«Купить в 1 клик»</i> или выберите конкретный ряд через кнопку 🎯 <i>«Выбрать место с 4 по 12 ряд»</i>.\n\n"
        "2. <b>Бронирование на QuickTickets:</b>\n"
        "Бот напрямую обращается к API платформы QuickTickets, мгновенно бронирует выбранные места в зале и блокирует их для вас.\n\n"
        "3. <b>Передача сессии в ваш браузер:</b>\n"
        "Бот генерирует персональную ссылку. При нажатии на кнопку оплаты ваш браузер открывает QuickTickets с уже выбранными местами в корзине!\n\n"
        "⏱ <b>Важно:</b> По правилам театра бронь удерживается <b>3 минуты</b>. За это время нужно подтвердить заказ."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_reply_keyboard(message.chat.id))


# ================= CALLBACK QUERY ХЭНДЛЕРЫ =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("setseats_"))
def cb_set_seats(call):
    cnt = int(call.data.replace("setseats_", "", 1))
    saved_cnt = set_user_seats_count(call.message.chat.id, cnt)
    bot.answer_callback_query(call.id, f"Выбрано: {seats_word(saved_cnt)} подряд!")
    try:
        bot.edit_message_text(
            f"✅ <b>Количество мест обновлено: {seats_word(saved_cnt)} подряд</b> (Партер, ряды 4–12).",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=seats_count_inline_keyboard(saved_cnt),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("setrow_"))
def cb_set_row(call):
    val = call.data.replace("setrow_", "", 1)
    saved_row = set_user_preferred_row(call.message.chat.id, val)
    bot.answer_callback_query(call.id, f"Установлен ряд: {row_label(saved_row)}!")
    try:
        bot.edit_message_text(
            f"✅ <b>Ряд по умолчанию обновлен: {row_label(saved_row)}</b> (Партер).",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=preferred_row_inline_keyboard(saved_row),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "close_menu")
def cb_close_menu(call):
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("rowsmenu_"))
def cb_show_rows_menu(call):
    sid = call.data.replace("rowsmenu_", "", 1)
    user = ensure_user(call.message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)

    info = SESSION_CACHE.get(sid)
    if not info:
        fetch_available_sessions()
        info = SESSION_CACHE.get(sid)

    if not info:
        bot.answer_callback_query(call.id, "Сеанс не найден или билеты закончились!", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Загружаю схему рядов 4–12...")
    rows_stats = get_parterre_rows_stats(info["session_url"])

    text = (
        f"🎯 <b>ВЫБОР РЯДА В 1 КЛИК: ПАРТЕР (РЯДЫ 4–12)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎭 <b>{info['title']}</b>\n"
        f"📅 {info['datetime']}\n"
        f"💺 Нужно мест подряд: <b>{seats_word(seats_cnt)}</b>\n\n"
        f"👇 <b>Нажмите на нужный ряд</b> — бот сразу забронирует места в этом ряду и отдаст ссылку на оплату:"
    )
    kb = rows_selection_inline_keyboard(sid, rows_stats, seats_cnt)

    try:
        if call.message.caption:
            bot.edit_message_caption(caption=text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
        else:
            bot.edit_message_text(text=text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
    except Exception as e:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("backcard_"))
def cb_back_to_card(call):
    sid = call.data.replace("backcard_", "", 1)
    user = ensure_user(call.message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    pref_row = user.get("preferred_row", DEFAULT_PREFERRED_ROW)

    info = SESSION_CACHE.get(sid)
    if not info:
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)
    rows_stats = get_parterre_rows_stats(info["session_url"])
    caption = build_play_card_text(info["title"], info["datetime"], seats_cnt, pref_row, rows_stats)
    kb = play_card_inline_keyboard(sid, info["session_url"], info.get("event_url", TARGET_URL), seats_cnt, pref_row)

    try:
        if call.message.caption:
            bot.edit_message_caption(caption=caption, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
        else:
            bot.edit_message_text(text=caption, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("refresh_"))
def cb_refresh_card(call):
    sid = call.data.replace("refresh_", "", 1)
    user = ensure_user(call.message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    pref_row = user.get("preferred_row", DEFAULT_PREFERRED_ROW)

    info = SESSION_CACHE.get(sid)
    if not info:
        bot.answer_callback_query(call.id, "Обновляю данные...")
        fetch_available_sessions()
        info = SESSION_CACHE.get(sid)

    if not info:
        bot.answer_callback_query(call.id, "Билеты на этот сеанс закончились!", show_alert=True)
        return

    bot.answer_callback_query(call.id, "🔄 Данные обновлены!")
    rows_stats = get_parterre_rows_stats(info["session_url"])
    caption = build_play_card_text(info["title"], info["datetime"], seats_cnt, pref_row, rows_stats)
    kb = play_card_inline_keyboard(sid, info["session_url"], info.get("event_url", TARGET_URL), seats_cnt, pref_row)

    try:
        if call.message.caption:
            bot.edit_message_caption(caption=caption, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
        else:
            bot.edit_message_text(text=caption, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("rowempty_"))
def cb_row_empty(call):
    parts = call.data.split("_")
    r_num = parts[2] if len(parts) > 2 else "выбранном"
    bot.answer_callback_query(call.id, f"В ряду {r_num} сейчас нет свободных мест. Выберите другой ряд!", show_alert=True)


# МГНОВЕННАЯ ПОКУПКА В 1 КЛИК: ПО ЛЮБОМУ РЯДУ ИЛИ КОНКРЕТНОМУ РЯДУ
@bot.callback_query_handler(func=lambda call: call.data.startswith("quickbuy_") or call.data.startswith("buyrow_"))
def cb_execute_booking(call):
    user = ensure_user(call.message.chat.id)
    seats_cnt = user.get("seats_count", DEFAULT_SEATS_COUNT)
    s_label = seats_word(seats_cnt)

    target_row = None
    if call.data.startswith("buyrow_"):
        parts = call.data.split("_")
        sid = parts[1]
        target_row = int(parts[2])
    else:
        sid = call.data.replace("quickbuy_", "", 1)
        user_row = user.get("preferred_row", DEFAULT_PREFERRED_ROW)
        if str(user_row).isdigit():
            target_row = int(user_row)

    info = SESSION_CACHE.get(sid)
    if not info:
        fetch_available_sessions()
        info = SESSION_CACHE.get(sid)

    if not info:
        bot.answer_callback_query(call.id, "Билеты на этот сеанс уже закончились!", show_alert=True)
        return

    r_text = f"в ряду {target_row}" if target_row else "в партере (ряды 4–12)"
    bot.answer_callback_query(call.id, f"⚡️ Бронирую {s_label} {r_text}...")

    wait_msg = bot.send_message(
        call.message.chat.id,
        f"⏳ <b>Бронирую {s_label} {r_text}...</b>\n"
        f"Спектакль: <b>«{info['title']}»</b> ({info['datetime']})\n"
        f"<i>Связываюсь с сервером QuickTickets...</i>",
    )

    res = execute_one_click_booking(
        session_url=info["session_url"],
        needed_seats=seats_cnt,
        target_row=target_row,
        play_title=info["title"],
        play_datetime=info["datetime"],
    )

    if not res.get("ok"):
        err_msg = res.get("error", "Не удалось забронировать места.")
        bot.edit_message_text(
            f"❌ <b>Не удалось забронировать места</b>\n\n"
            f"{err_msg}\n\n"
            f"👉 Попробуйте выбрать другой ряд или открыть схему зала вручную:",
            chat_id=call.message.chat.id,
            message_id=wait_msg.message_id,
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton("🗺 Открыть схему зала", url=info["session_url"]),
                InlineKeyboardButton("🎯 Выбрать другой ряд", callback_data=f"rowsmenu_{sid}"),
            )
        )
        return

    seats_str = ", ".join(str(s) for s in res["seats"])
    price_str = f"{res['total_price']:,}".replace(",", " ")

    success_text = (
        f"🎉 <b>МЕСТА УСПЕШНО ЗАБРОНИРОВАНЫ В 1 КЛИК!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎭 <b>{info['title']}</b>\n"
        f"📅 Сеанс: <b>{info['datetime']}</b>\n"
        f"🏛 <b>Партер, Ряд {res['row']} | Места: {seats_str}</b>\n"
        f"💰 К оплате: <b>{price_str} ₽</b> ({seats_word(len(res['seats']))})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ <b>Внимание: бронь удерживается ровно 3 минуты!</b>\n\n"
        f"👇 <b>Нажмите кнопку ниже</b> — откроется страница оформления заказа QuickTickets с уже выбранными местами:"
    )

    bot.edit_message_text(
        success_text,
        chat_id=call.message.chat.id,
        message_id=wait_msg.message_id,
        reply_markup=checkout_bridge_keyboard(res),
    )


# ================= 24/7 ФОНОВЫЙ МОНИТОРИНГ =================
def monitor_loop():
    known_available = load_json(STATE_FILE, {})
    is_first_run = not os.path.exists(STATE_FILE)
    logging.info("Фоновый мониторинг билетов QuickTickets запущен...")

    while True:
        try:
            current_available = fetch_available_sessions()
            if not is_first_run and current_available:
                for session_url, info in current_available.items():
                    if session_url not in known_available:
                        # Новый сеанс с билетами!
                        users = get_users()
                        for uid_str, u_cfg in users.items():
                            chat_id = int(uid_str)
                            seats_cnt = u_cfg.get("seats_count", DEFAULT_SEATS_COUNT)
                            pref_row = u_cfg.get("preferred_row", DEFAULT_PREFERRED_ROW)
                            target_r = int(pref_row) if str(pref_row).isdigit() else None

                            try:
                                if u_cfg.get("auto_book", False):
                                    # АВТО-БРОНИРОВАНИЕ
                                    res = execute_one_click_booking(
                                        session_url=session_url,
                                        needed_seats=seats_cnt,
                                        target_row=target_r,
                                        play_title=info["title"],
                                        play_datetime=info["datetime"],
                                    )
                                    if res.get("ok"):
                                        seats_str = ", ".join(str(s) for s in res["seats"])
                                        price_str = f"{res['total_price']:,}".replace(",", " ")
                                        bot.send_message(
                                            chat_id,
                                            f"🔥 <b>АВТО-БРОНЬ СРАБОТАЛА! БИЛЕТЫ В КОРЗИНЕ!</b>\n\n"
                                            f"🎭 <b>{info['title']}</b>\n"
                                            f"📅 {info['datetime']}\n"
                                            f"🏛 <b>Партер, Ряд {res['row']} | Места: {seats_str}</b>\n"
                                            f"💰 Сумма: <b>{price_str} ₽</b>\n"
                                            f"⏱ <b>У вас есть 3 минуты на оплату:</b>",
                                            reply_markup=checkout_bridge_keyboard(res),
                                        )
                                    else:
                                        send_play_card(chat_id, info, u_cfg)
                                else:
                                    # Обычное уведомление
                                    send_play_card(chat_id, info, u_cfg)
                            except Exception as e:
                                logging.error(f"Ошибка отправки уведомления {chat_id}: {e}")

            known_available = current_available
            save_json(STATE_FILE, known_available)
            if is_first_run:
                is_first_run = False
        except Exception as e:
            logging.error(f"Ошибка цикла мониторинга: {e}")

        time.sleep(CHECK_INTERVAL + random.uniform(1, 4))


def keep_alive_loop():
    ping_url = f"{RENDER_EXTERNAL_URL}/ping"
    while True:
        time.sleep(600)
        try:
            requests.get(ping_url, timeout=15)
        except Exception:
            pass


# ================= FLASK WEB-МОСТ ПЕРЕДАЧИ СЕССИИ =================
@app.route("/")
@app.route("/ping")
def home():
    return f"QuickTickets Bot is running! Last scan: {LAST_CHECK_TIME}", 200


@app.route("/autobuy/<res_token>")
@app.route("/checkout/<res_token>")
@app.route("/pay/<res_token>")
def checkout_bridge(res_token: str):
    data = ACTIVE_RESERVATIONS.get(res_token)
    if not data:
        # Сессия устарела или не найдена
        html_expired = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Сессия бронирования истекла</title>
    <style>
        body {{
            background: #0b0f19; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 16px; box-sizing: border-box;
        }}
        .card {{
            background: #1e293b; border: 1px solid #334155; border-radius: 20px; padding: 32px 24px; max-width: 480px; width: 100%; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.5);
        }}
        h2 {{ color: #f87171; margin-top: 0; }}
        p {{ color: #94a3b8; font-size: 15px; line-height: 1.5; }}
        .btn {{
            display: inline-block; margin-top: 20px; padding: 14px 28px; background: #3b82f6; color: #fff; text-decoration: none; border-radius: 12px; font-weight: bold; transition: 0.2s;
        }}
        .btn:hover {{ background: #2563eb; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>⏱ Время брони истекло</h2>
        <p>3-минутный интервал удержания мест на платформе QuickTickets завершился. Места могли вернуться в общую продажу.</p>
        <a href="{TARGET_URL}" class="btn">Вернуться к афише театра</a>
    </div>
</body>
</html>"""
        return Response(html_expired, mimetype="text/html")

    now = time.time()
    rem = max(0, int(data.get("expires_at", now) - now))
    if rem <= 0:
        ACTIVE_RESERVATIONS.pop(res_token, None)
        return redirect(f"/autobuy/{res_token}")

    title = data.get("title", "Спектакль")
    dt = data.get("datetime", "")
    row = data.get("row", "")
    seats_str = ", ".join(str(s) for s in data.get("seats", []))
    price_str = f"{data.get('total_price', 0):,}".replace(",", " ")

    elem_id = data.get("elem_id", "")
    org_alias = data.get("organisationAlias", ORGANISATION_ALIAS)
    anytickets_codes_str = ",".join(data.get("anyticketsCodes", []))
    select_count = data.get("selectAnyplacesCount", 1)

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Оплата билетов: {title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: linear-gradient(135deg, #0b0f19 0%, #111827 50%, #0f172a 100%);
            color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{
            background: rgba(30, 41, 59, 0.85);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 24px;
            max-width: 520px;
            width: 100%;
            padding: 36px 28px;
            text-align: center;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6), 0 0 40px rgba(59, 130, 246, 0.15);
        }}
        .badge {{
            display: inline-block;
            background: rgba(34, 197, 94, 0.15);
            color: #4ade80;
            border: 1px solid rgba(34, 197, 94, 0.3);
            font-size: 13px;
            font-weight: 700;
            padding: 6px 14px;
            border-radius: 100px;
            margin-bottom: 16px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}
        h1 {{
            font-size: 22px;
            font-weight: 800;
            margin-bottom: 6px;
            color: #ffffff;
            line-height: 1.3;
        }}
        .datetime {{
            font-size: 15px;
            color: #94a3b8;
            margin-bottom: 24px;
        }}
        .seat-card {{
            background: rgba(15, 23, 42, 0.7);
            border: 1px solid rgba(56, 189, 248, 0.25);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 24px;
        }}
        .seat-row {{
            font-size: 20px;
            font-weight: 800;
            color: #38bdf8;
            margin-bottom: 6px;
        }}
        .seat-places {{
            font-size: 16px;
            color: #e2e8f0;
            margin-bottom: 12px;
        }}
        .price {{
            font-size: 24px;
            font-weight: 900;
            color: #22c55e;
        }}
        .timer-box {{
            background: rgba(245, 158, 11, 0.1);
            border: 1px dashed rgba(245, 158, 11, 0.4);
            border-radius: 12px;
            padding: 12px;
            margin-bottom: 26px;
        }}
        .timer-label {{
            font-size: 13px;
            color: #f59e0b;
            font-weight: 600;
            margin-bottom: 4px;
        }}
        .timer-val {{
            font-size: 24px;
            font-weight: 800;
            color: #fbbf24;
            font-family: monospace;
        }}
        .submit-btn {{
            display: block;
            width: 100%;
            background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
            color: #ffffff;
            border: none;
            border-radius: 16px;
            padding: 18px 24px;
            font-size: 18px;
            font-weight: 800;
            cursor: pointer;
            text-decoration: none;
            box-shadow: 0 10px 25px rgba(34, 197, 94, 0.4);
            transition: all 0.2s ease;
        }}
        .submit-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 15px 30px rgba(34, 197, 94, 0.5);
        }}
        .loading-text {{
            font-size: 13px;
            color: #64748b;
            margin-top: 16px;
        }}
        .spinner {{
            display: inline-block;
            width: 18px;
            height: 18px;
            border: 2px solid rgba(255,255,255,0.2);
            border-top-color: #38bdf8;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            vertical-align: middle;
            margin-right: 6px;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="badge">⚡️ Места закреплены</div>
        <h1>{title}</h1>
        <div class="datetime">📅 {dt}</div>

        <div class="seat-card">
            <div class="seat-row">🏛 Партер, Ряд {row}</div>
            <div class="seat-places">💺 Выбранные места: <b>{seats_str}</b></div>
            <div class="price">{price_str} ₽</div>
        </div>

        <div class="timer-box">
            <div class="timer-label">⏱ ВРЕМЯ НА ОПЛАТУ КОРЗИНЫ:</div>
            <div class="timer-val" id="countdown">--:--</div>
        </div>

        <!-- Форма мгновенной передачи сессии с билетами в QuickTickets -->
        <form id="qtOrderForm" action="https://quicktickets.ru/ordering/anytickets" method="POST">
            <input type="hidden" name="organisationAlias" value="{org_alias}" />
            <input type="hidden" name="elemType" value="session" />
            <input type="hidden" name="elemId" value="{elem_id}" />
            <input type="hidden" name="anyticketsCodes" value="{anytickets_codes_str}" />
            <input type="hidden" name="selectAnyplacesCount" value="{select_count}" />

            <button type="submit" class="submit-btn" id="payBtn">
                💳 Перейти к оплате билетов
            </button>
        </form>

        <div class="loading-text">
            <span class="spinner"></span> Перенаправляем на защищенный шлюз QuickTickets...
        </div>
    </div>

    <script>
        // Таймер обратного отсчета
        let remainingSeconds = {rem};
        const timerEl = document.getElementById("countdown");

        function updateTimer() {{
            if (remainingSeconds <= 0) {{
                timerEl.textContent = "00:00 (Истекло)";
                timerEl.style.color = "#f87171";
                return;
            }}
            const m = Math.floor(remainingSeconds / 60);
            const s = remainingSeconds % 60;
            timerEl.textContent = (m < 10 ? "0" + m : m) + ":" + (s < 10 ? "0" + s : s);
            remainingSeconds--;
        }}
        updateTimer();
        setInterval(updateTimer, 1000);

        // Автоматическая отправка формы для мгновенного входа в корзину пользователя
        setTimeout(function() {{
            try {{
                document.getElementById("qtOrderForm").submit();
            }} catch(e) {{}}
        }}, 400);
    </script>
</body>
</html>"""
    return Response(html_page, mimetype="text/html")


def run_flask():
    app.run(host="0.0.0.0", port=PORT)


# ================= ТОЧКА ВХОДА =================
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    logging.info("Telegram-бот успешно запущен и готов к работе...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=False)
        except Exception as e:
            logging.error(f"Перезапуск Telegram polling: {e}")
            time.sleep(5)
