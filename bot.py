"""
Бот «Подслушано»: персональная ссылка для анонимных вопросов; по желанию — копия админу.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ReplyKeyboardRemove,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "data.sqlite3"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_USER_ID_RAW = os.environ.get("ADMIN_USER_ID", "").strip()
_support_raw = os.environ.get("SUPPORT_USERNAME", "quesupport").strip().lstrip("@")
SUPPORT_USERNAME = _support_raw or "quesupport"

MAX_CAPTION = 3500
MAX_TEXT = 4000
# Лимит подписи к медиа в Telegram Bot API
TELEGRAM_MEDIA_CAPTION_MAX = 1024

CB_CANCEL_ANON = "cancel_anon"
CB_ANON_SENT_WRITE_MORE = "anon_wm"
CB_ANON_SENT_DELETE_PREFIX = "anon_del:"

TEXT_AFTER_USER_LINK_HTML = (
    "🚀 Здесь можно отправить <b>анонимное сообщение</b> человеку, который опубликовал эту ссылку\n\n"
    "🖊 <b>Напишите сюда всё, что хотите ему передать</b>, и через несколько секунд он получит ваше сообщение, "
    "но не будет знать от кого\n\n"
    "Отправить можно фото, видео, 💬 текст, 🔊 голосовые, 📷 видеосообщения (кружки), а также ✨ стикеры"
)

TEXT_AFTER_GROUP_LINK_HTML = (
    "🚀 Здесь можно отправить <b>анонимное сообщение</b> в чат, ссылку на который вы открыли\n\n"
    "🖊 <b>Напишите сюда всё, что хотите передать</b>, и через несколько секунд участники увидят сообщение "
    "без указания отправителя\n\n"
    "Отправить можно фото, видео, 💬 текст, 🔊 голосовые, 📷 видеосообщения (кружки), а также ✨ стикеры"
)

KEYBOARD_CANCEL_ANON = InlineKeyboardMarkup(
    [[InlineKeyboardButton("✖️ Отменить", callback_data=CB_CANCEL_ANON)]]
)


def keyboard_after_anonymous_sent(user_message_id: int) -> InlineKeyboardMarkup:
    """После успешной анонимной отправки: ещё одно сообщение или удалить след в чате."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Написать ещё ✍️", callback_data=CB_ANON_SENT_WRITE_MORE)],
            [
                InlineKeyboardButton(
                    "Удалить сообщение 🗑️",
                    callback_data=f"{CB_ANON_SENT_DELETE_PREFIX}{user_message_id}",
                )
            ],
        ]
    )

# Москва = UTC+3 (без летнего времени с 2014 г.; без зависимости tzdata на Windows)
MSK_TZ = timezone(timedelta(hours=3), name="MSK")


def _admin_user_id() -> int | None:
    if not ADMIN_USER_ID_RAW:
        return None
    try:
        return int(ADMIN_USER_ID_RAW)
    except ValueError:
        logger.error("ADMIN_USER_ID должен быть целым числом")
        return None


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                content_type TEXT,
                text_content TEXT,
                identifiers_json TEXT NOT NULL,
                raw_message_json TEXT,
                recipient_user_id INTEGER
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "recipient_user_id" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN recipient_user_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_link_clicks_owner ON link_clicks(owner_user_id)"
        )
        if "recipient_chat_id" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN recipient_chat_id INTEGER")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_link_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glclicks_chat ON group_link_clicks(group_chat_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anon_reply_routes (
                dest_chat_id INTEGER NOT NULL,
                bot_message_id INTEGER NOT NULL,
                anon_sender_user_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (dest_chat_id, bot_message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_anon_route_dest ON anon_reply_routes(dest_chat_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_link_tokens (
                token TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ult_owner ON user_link_tokens(owner_user_id)"
        )
        conn.commit()


_LINK_TOKEN_CHARS = string.ascii_letters + string.digits
# Как в эталонных ссылках (например c2k2c31) — 7 символов; весь payload ?start= укладывается в лимит Telegram (64).
_LINK_TOKEN_LEN = 7


def _random_user_link_token() -> str:
    """Непредсказуемый фрагмент для ?start=q… (не только цифры)."""
    while True:
        t = "".join(secrets.choice(_LINK_TOKEN_CHARS) for _ in range(_LINK_TOKEN_LEN))
        if not t.isdigit():
            return t


def _stored_user_link_token_ok(tok: str) -> bool:
    """В БД храним только 7-символьный токен из алфавита; иначе перевыпуск."""
    if not tok or len(tok) != _LINK_TOKEN_LEN or tok.isdigit():
        return False
    return all(c in _LINK_TOKEN_CHARS for c in tok)


def get_or_create_user_link_token(owner_user_id: int) -> str:
    """Стабильный случайный токен владельца для персональной ссылки (без открытого user id)."""
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT token FROM user_link_tokens WHERE owner_user_id = ?",
            (owner_user_id,),
        ).fetchone()
        if row:
            tok = str(row[0])
            if _stored_user_link_token_ok(tok):
                return tok
            conn.execute(
                "DELETE FROM user_link_tokens WHERE owner_user_id = ?",
                (owner_user_id,),
            )
            conn.commit()
            logger.info(
                "Перевыпуск токена ссылки (неверный формат в БД) owner_user_id=%s",
                owner_user_id,
            )
        for _ in range(32):
            tok = _random_user_link_token()
            try:
                conn.execute(
                    """
                    INSERT INTO user_link_tokens (token, owner_user_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (tok, owner_user_id, created),
                )
                conn.commit()
                return tok
            except sqlite3.IntegrityError:
                conn.rollback()
                continue
    raise RuntimeError("Не удалось выделить токен ссылки")


def resolve_user_link_token(token: str) -> int | None:
    """user id владельца по токену или None."""
    if not token or len(token) > 64:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT owner_user_id FROM user_link_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    return int(row[0]) if row else None


def log_user_link_click(owner_user_id: int) -> None:
    """Переход по ссылке пользователя (?start=q…)."""
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO link_clicks (owner_user_id, created_at) VALUES (?, ?)",
            (owner_user_id, created),
        )
        conn.commit()


def log_group_link_click(group_chat_id: int) -> None:
    """Переход по ссылке группы (?start=s…)."""
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO group_link_clicks (group_chat_id, created_at) VALUES (?, ?)",
            (group_chat_id, created),
        )
        conn.commit()


def register_anon_reply_routes(
    dest_chat_id: int,
    bot_message_ids: Sequence[int],
    anon_sender_user_id: int,
    submission_id: int,
) -> None:
    """Связь «сообщение бота у получателя» → id отправителя анонима (для ответа свайпом)."""
    if not bot_message_ids:
        return
    created = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for mid in bot_message_ids:
            conn.execute(
                """
                INSERT OR REPLACE INTO anon_reply_routes
                (dest_chat_id, bot_message_id, anon_sender_user_id, submission_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dest_chat_id, int(mid), anon_sender_user_id, submission_id, created),
            )
        conn.commit()


def lookup_anon_reply_route(
    dest_chat_id: int, reply_to_bot_message_id: int
) -> tuple[int, int] | None:
    """(anon_sender_user_id, submission_id) или None."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT anon_sender_user_id, submission_id FROM anon_reply_routes
            WHERE dest_chat_id = ? AND bot_message_id = ?
            """,
            (dest_chat_id, reply_to_bot_message_id),
        ).fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1])


def fetch_submission_for_reply(
    submission_id: int,
) -> tuple[str | None, int | None, int | None] | None:
    """Текст анонима и получатель для оформления ответа; None если записи нет."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT text_content, recipient_user_id, recipient_chat_id
            FROM submissions WHERE id = ?
            """,
            (submission_id,),
        ).fetchone()
    if not row:
        return None
    tc, ru, rc = row[0], row[1], row[2]
    return (
        str(tc) if tc is not None else None,
        int(ru) if ru is not None else None,
        int(rc) if rc is not None else None,
    )


async def keyboard_write_more_for_sender(
    bot,
    recipient_user_id: int | None,
    recipient_chat_id: int | None,
) -> InlineKeyboardMarkup | None:
    """Ссылка для повторной анонимной отправки тому же получателю."""
    me = await bot.get_me()
    if not me.username:
        return None
    if recipient_user_id is not None:
        tok = get_or_create_user_link_token(recipient_user_id)
        url = f"https://t.me/{me.username}?start=q{tok}"
    elif recipient_chat_id is not None:
        inv = get_or_create_group_invite_row_id(recipient_chat_id)
        url = f"https://t.me/{me.username}?start=s{inv}"
    else:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Написать ещё ✍️", url=url)]]
    )


def format_owner_reply_for_sender_html(
    owner_name: str, original_anon_snippet: str, owner_reply_text: str
) -> str:
    """Цитата с именем + исходный аноним + текст ответа (как в макете)."""
    o = clip(original_anon_snippet.strip() or "📎", 900)
    body = clip(owner_reply_text, MAX_TEXT - 400)
    return (
        f"<b>{html.escape(owner_name)}</b>\n"
        f"<blockquote>{html.escape(o)}</blockquote>\n\n"
        f"{html.escape(body)}"
    )


def get_or_create_group_invite_row_id(chat_id: int) -> int:
    """Короткий id для ссылки s{id} (строка в group_invites)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_invites (chat_id) VALUES (?)",
            (chat_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM group_invites WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def resolve_group_invite_row_id(invite_row_id: int) -> int | None:
    """chat_id супергруппы по числу из ?start=s…"""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT chat_id FROM group_invites WHERE id = ?",
            (invite_row_id,),
        ).fetchone()
        return int(row[0]) if row else None


def _msk_today_start_utc_iso() -> str:
    """Начало «сегодня» по Москве, в UTC для сравнения с created_at в БД."""
    now_msk = datetime.now(MSK_TZ)
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_msk.astimezone(timezone.utc).isoformat()


def _count_messages_to_user(conn: sqlite3.Connection, user_id: int, since_iso: str | None) -> int:
    if since_iso:
        row = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE recipient_user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE recipient_user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _count_link_clicks_for_owner(
    conn: sqlite3.Connection, owner_id: int, since_iso: str | None
) -> int:
    if since_iso:
        row = conn.execute(
            "SELECT COUNT(*) FROM link_clicks WHERE owner_user_id = ? AND created_at >= ?",
            (owner_id, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM link_clicks WHERE owner_user_id = ?",
            (owner_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _popularity_place(conn: sqlite3.Connection, user_id: int) -> str:
    """Место по сумме (сообщения получателю + клики по ссылке); свыше 1000 — как в макете."""
    msg_rows = conn.execute(
        """
        SELECT recipient_user_id, COUNT(*) AS c FROM submissions
        WHERE recipient_user_id IS NOT NULL GROUP BY recipient_user_id
        """
    ).fetchall()
    click_rows = conn.execute(
        """
        SELECT owner_user_id, COUNT(*) AS c FROM link_clicks GROUP BY owner_user_id
        """
    ).fetchall()
    scores: dict[int, int] = {}
    for uid, c in msg_rows:
        scores[uid] = scores.get(uid, 0) + int(c)
    for uid, c in click_rows:
        scores[uid] = scores.get(uid, 0) + int(c)
    my_score = scores.get(user_id, 0)
    ahead = sum(1 for uid, s in scores.items() if s > my_score)
    rank = ahead + 1
    if rank > 1000:
        return "1000+ место"
    return f"{rank} место"


def _count_messages_to_group(
    conn: sqlite3.Connection, group_chat_id: int, since_iso: str | None
) -> int:
    if since_iso:
        row = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE recipient_chat_id = ? AND created_at >= ?",
            (group_chat_id, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE recipient_chat_id = ?",
            (group_chat_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _count_group_link_clicks(
    conn: sqlite3.Connection, group_chat_id: int, since_iso: str | None
) -> int:
    if since_iso:
        row = conn.execute(
            "SELECT COUNT(*) FROM group_link_clicks WHERE group_chat_id = ? AND created_at >= ?",
            (group_chat_id, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM group_link_clicks WHERE group_chat_id = ?",
            (group_chat_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _popularity_place_group(conn: sqlite3.Connection, chat_id: int) -> str:
    msg_rows = conn.execute(
        """
        SELECT recipient_chat_id, COUNT(*) AS c FROM submissions
        WHERE recipient_chat_id IS NOT NULL GROUP BY recipient_chat_id
        """
    ).fetchall()
    click_rows = conn.execute(
        """
        SELECT group_chat_id, COUNT(*) AS c FROM group_link_clicks GROUP BY group_chat_id
        """
    ).fetchall()
    scores: dict[int, int] = {}
    for cid, c in msg_rows:
        scores[cid] = scores.get(cid, 0) + int(c)
    for cid, c in click_rows:
        scores[cid] = scores.get(cid, 0) + int(c)
    my_score = scores.get(chat_id, 0)
    ahead = sum(1 for cid, s in scores.items() if s > my_score)
    rank = ahead + 1
    if rank > 1000:
        return "1000+ место"
    return f"{rank} место"


def _to_dict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return None


def collect_identifiers(update: Update) -> dict[str, Any]:
    """Собирает user, chat и признаки пересылаемых сообщений из объекта Update."""
    msg = update.effective_message
    data: dict[str, Any] = {
        "user": _to_dict(update.effective_user),
        "chat": _to_dict(update.effective_chat),
    }
    if msg:
        if msg.sender_chat:
            data["sender_chat"] = _to_dict(msg.sender_chat)
        # PTB 22+: forward_from / forward_date и др. убраны с Message — только forward_origin
        origin = getattr(msg, "forward_origin", None)
        if origin:
            data["forward_origin"] = _to_dict(origin)
        ff = getattr(msg, "forward_from", None)
        if ff:
            data["forward_from"] = _to_dict(ff)
        ffc = getattr(msg, "forward_from_chat", None)
        if ffc:
            data["forward_from_chat"] = _to_dict(ffc)
        fsn = getattr(msg, "forward_sender_name", None)
        if fsn:
            data["forward_sender_name"] = fsn
        fd = getattr(msg, "forward_date", None)
        if fd is not None:
            data["forward_date"] = fd.isoformat() if hasattr(fd, "isoformat") else fd
        if getattr(msg, "is_automatic_forward", None):
            data["is_automatic_forward"] = bool(msg.is_automatic_forward)
        if msg.contact:
            data["contact"] = _to_dict(msg.contact)
    return data


def _user_display_name(user_dict: dict[str, Any]) -> str:
    first = (user_dict.get("first_name") or "").strip()
    last = (user_dict.get("last_name") or "").strip()
    return " ".join(x for x in (first, last) if x).strip() or "—"


def format_message_body_for_admin(msg, ctype: str) -> str:
    """Текст для поля «Сообщение» в уведомлении админу."""
    if msg.text and not msg.photo:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.contact:
        c = msg.contact
        lines = [f"Контакт, телефон: {c.phone_number}"]
        card_name = " ".join(x for x in (c.first_name, c.last_name) if x)
        if card_name:
            lines.append(f"В карточке: {card_name}")
        if c.user_id is not None:
            lines.append(f"user_id в контакте: {c.user_id}")
        return "\n".join(lines)
    if msg.location:
        return f"Координаты: {msg.location.latitude}, {msg.location.longitude}"
    if msg.poll:
        return msg.poll.question
    if msg.sticker:
        em = (msg.sticker.emoji or "").strip()
        return ("Стикер " + em).strip() if em else "Стикер"
    if msg.document and msg.document.file_name:
        return f"Файл: {msg.document.file_name}"
    if msg.audio:
        if msg.audio.title:
            return f"Аудио: {msg.audio.title}"
        if msg.audio.file_name:
            return f"Аудио: {msg.audio.file_name}"
    labels = {
        "photo": "Фотография (без подписи)",
        "video": "Видео (без подписи)",
        "document": "Документ",
        "voice": "Голосовое сообщение",
        "video_note": "Видеосообщение (кружок)",
        "audio": "Аудио",
        "animation": "GIF / анимация",
        "poll": "Опрос",
        "other": "Вложение",
    }
    return labels.get(ctype, f"Вложение ({ctype})")


def format_person_lines(label: str, u: dict[str, Any]) -> str:
    """Блок «Имя / Username / ID»; если label пустой — только три строки профиля."""
    name = _user_display_name(u)
    uname = u.get("username")
    username_line = f"@{uname}" if uname else "—"
    uid = u.get("id")
    id_line = str(uid) if uid is not None else "—"
    core = (
        f"Имя: {name}\n"
        f"Username: {username_line}\n"
        f"ID: {id_line}\n"
    )
    if label:
        return f"{label}\n{core}"
    return core


def build_admin_notification_text(
    row_id: int,
    ctype: str,
    identifiers: dict[str, Any],
    msg,
) -> str:
    u = identifiers.get("user") or {}
    body = format_message_body_for_admin(msg, ctype)
    return (
        f"📥 Подслушано — запись #{row_id}\n"
        f"Тип: {ctype}\n\n"
        f"{format_person_lines('', u)}"
        f"Сообщение:\n{body}"
    )


def format_group_owner_lines(chat_d: dict[str, Any]) -> str:
    """Блок владельца для группы (аноним в чат)."""
    title = chat_d.get("title") or "—"
    un = chat_d.get("username")
    ul = f"@{un}" if un else "—"
    cid = chat_d.get("id")
    return (
        "Владелец ссылки (группа):\n"
        f"Название: {title}\n"
        f"Username: {ul}\n"
        f"ID: {cid}\n"
    )


def build_anonymous_admin_notification_text(
    row_id: int,
    ctype: str,
    owner_block: str,
    sender: dict[str, Any],
    msg,
) -> str:
    """Уведомление админу: блок владельца (пользователь или группа), отправитель, текст."""
    body = format_message_body_for_admin(msg, ctype)
    return (
        f"📥 Подслушано — аноним по ссылке, запись #{row_id}\n"
        f"Тип: {ctype}\n\n"
        f"{owner_block}\n"
        f"{format_person_lines('Отправитель:', sender)}"
        f"Сообщение:\n{body}"
    )


async def _owner_chat_dict_from_id(bot, chat_id: int) -> dict[str, Any]:
    """Данные чата для карточки админу."""
    try:
        ch = await bot.get_chat(chat_id)
        d = ch.to_dict()
        return {
            "id": d.get("id"),
            "title": d.get("title"),
            "username": d.get("username"),
        }
    except Exception:
        logger.warning("Не удалось get_chat для группы id=%s", chat_id)
        return {"id": chat_id, "title": None, "username": None}


async def _owner_dict_from_chat(bot, owner_user_id: int) -> dict[str, Any]:
    """Профиль владельца ссылки по user_id (нужен хотя бы один /start у бота)."""
    try:
        ch = await bot.get_chat(owner_user_id)
        d = ch.to_dict()
        return {
            "id": d.get("id"),
            "first_name": d.get("first_name"),
            "last_name": d.get("last_name"),
            "username": d.get("username"),
        }
    except Exception:
        logger.warning("Не удалось get_chat для владельца ссылки id=%s", owner_user_id)
        return {
            "id": owner_user_id,
            "first_name": None,
            "last_name": None,
            "username": None,
        }


async def send_admin_message_copy(
    bot,
    admin_id: int,
    source_chat_id: int,
    msg,
    admin_text: str,
) -> None:
    """Копия сообщения админу + подпись с данными."""
    try:
        if msg.text and not msg.photo:
            await bot.send_message(chat_id=admin_id, text=clip(admin_text, MAX_TEXT))
        else:
            copied = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=source_chat_id,
                message_id=msg.message_id,
            )
            await copied.reply_text(clip(admin_text, MAX_CAPTION))
    except Exception:
        logger.exception("Не удалось отправить копию админу основным способом")
        await bot.send_message(chat_id=admin_id, text=clip(admin_text, MAX_TEXT))


def save_submission(
    *,
    user_id: int | None,
    chat_id: int | None,
    message_id: int | None,
    content_type: str,
    text_content: str | None,
    identifiers: dict[str, Any],
    raw_message: dict[str, Any] | None,
    recipient_user_id: int | None = None,
    recipient_chat_id: int | None = None,
) -> int:
    created = datetime.now(timezone.utc).isoformat()
    identifiers_json = json.dumps(identifiers, ensure_ascii=False)
    raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions
            (created_at, user_id, chat_id, message_id, content_type, text_content,
             identifiers_json, raw_message_json, recipient_user_id, recipient_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created,
                user_id,
                chat_id,
                message_id,
                content_type,
                text_content,
                identifiers_json,
                raw_json,
                recipient_user_id,
                recipient_chat_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n… (обрезано)"


def html_personal_link_block(full_link: str, display_link: str) -> str:
    """Визуальная цитата со строкой ссылки + отдельная ссылка (вложенный <a> в blockquote у API часто ломает разметку)."""
    href_esc = html.escape(full_link, quote=True)
    display_esc = html.escape(display_link, quote=False)
    return (
        f"<blockquote>{display_esc}</blockquote>\n"
        f"<a href=\"{href_esc}\">🔗 Открыть ссылку</a>"
    )


def parse_deep_link_payload(arg: str) -> tuple[str, int] | None:
    """qTOKEN → владелец только по токену в БД; s456 → id строки group_invites."""
    p = arg.strip()
    if len(p) >= 2 and p[0] == "q":
        rest = p[1:]
        uid = resolve_user_link_token(rest)
        if uid is not None:
            return ("user", uid)
        return None
    if len(p) >= 2 and p[0] == "s" and p[1:].isdigit():
        return ("group_invite", int(p[1:]))
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return

    args = context.args or []
    if args:
        pl = parse_deep_link_payload(args[0])
        if pl is not None:
            kind, tid = pl
            if kind == "user":
                log_user_link_click(tid)
                context.user_data["anon_target_user_id"] = tid
                context.user_data.pop("anon_target_chat_id", None)
                await msg.reply_text(
                    TEXT_AFTER_USER_LINK_HTML,
                    reply_markup=KEYBOARD_CANCEL_ANON,
                    parse_mode=ParseMode.HTML,
                )
                return
            if kind == "group_invite":
                g_chat_id = resolve_group_invite_row_id(tid)
                if g_chat_id is None:
                    await msg.reply_text(
                        "Ссылка недействительна или устарела. Попросите новую у администраторов чата.",
                        reply_markup=ReplyKeyboardRemove(),
                    )
                    return
                log_group_link_click(g_chat_id)
                context.user_data["anon_target_chat_id"] = g_chat_id
                context.user_data.pop("anon_target_user_id", None)
                await msg.reply_text(
                    TEXT_AFTER_GROUP_LINK_HTML,
                    reply_markup=KEYBOARD_CANCEL_ANON,
                    parse_mode=ParseMode.HTML,
                )
                return

    context.user_data.pop("anon_target_user_id", None)
    context.user_data.pop("anon_target_chat_id", None)

    bot = context.bot
    me = await bot.get_me()
    if not me.username:
        await msg.reply_text(
            "У бота нет username в Telegram — задайте его в @BotFather, иначе ссылку нельзя сделать.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    add_to_chat_href = f"https://t.me/{me.username}?startgroup=true"

    if chat.type in ("group", "supergroup"):
        invite_row = get_or_create_group_invite_row_id(chat.id)
        full_link = f"https://t.me/{me.username}?start=s{invite_row}"
        display_link = f"t.me/{me.username}?start=s{invite_row}"
        share_text = "Напиши анонимно в наш чат 💬"
        share_href = (
            "https://t.me/share/url?"
            f"url={quote(full_link, safe='')}&text={quote(share_text, safe='')}"
        )
        link_block = html_personal_link_block(full_link, display_link)
        text_html = (
            "<b>Начните получать анонимные вопросы прямо в этом чате!</b>\n\n"
            "Ваша ссылка:\n"
            f"{link_block}\n\n"
            "<b>Разместите эту ссылку</b> 👆 в описании своего профиля Telegram, TikTok, Instagram (stories), "
            "чтобы вам могли написать 💬\n\n"
            "❗ <b>Отвечать на сообщения могут все участники чата</b>"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Поделиться ссылкой", url=share_href)],
                [InlineKeyboardButton("👥 Добавить бота в чат", url=add_to_chat_href)],
            ]
        )
    else:
        utok = get_or_create_user_link_token(user.id)
        full_link = f"https://t.me/{me.username}?start=q{utok}"
        display_link = f"t.me/{me.username}?start=q{utok}"
        share_text = "Напиши мне анонимно 💬"
        share_href = (
            "https://t.me/share/url?"
            f"url={quote(full_link, safe='')}&text={quote(share_text, safe='')}"
        )
        link_block = html_personal_link_block(full_link, display_link)
        text_html = (
            "<b>Начните получать анонимные вопросы прямо сейчас!</b>\n\n"
            "Ваша ссылка:\n"
            f"{link_block}\n\n"
            "<b>Разместите эту ссылку</b> 👆 в описании своего профиля Telegram, TikTok, Instagram (stories), "
            "чтобы вам могли написать 💬"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Поделиться ссылкой", url=share_href)],
                [InlineKeyboardButton("👥 Добавить бота в чат", url=add_to_chat_href)],
            ]
        )

    await msg.reply_text(
        text_html,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cancel_anon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена режима отправки анонимного сообщения по кнопке «Отменить»."""
    q = update.callback_query
    if not q or not q.message:
        return
    if q.data != CB_CANCEL_ANON:
        return
    await q.answer()
    context.user_data.pop("anon_target_user_id", None)
    context.user_data.pop("anon_target_chat_id", None)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("Не удалось убрать inline-клавиатуру после отмены")
    await q.message.reply_text("Режим анонимной отправки отключён.")


async def anon_sent_write_more_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Убираем кнопки; отправитель может прислать ещё одно анонимное сообщение."""
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer("Можно отправить ещё одно сообщение.")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("Не удалось убрать кнопки после «Написать ещё»")


async def anon_sent_delete_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Удаляем подтверждение бота и исходное сообщение отправителя (если получится)."""
    q = update.callback_query
    if not q or not q.message or not q.data:
        return
    if not q.data.startswith(CB_ANON_SENT_DELETE_PREFIX):
        return
    tail = q.data[len(CB_ANON_SENT_DELETE_PREFIX) :]
    try:
        user_mid = int(tail)
    except ValueError:
        await q.answer("Некорректные данные.", show_alert=True)
        return
    await q.answer()
    bot = context.bot
    cid = q.message.chat_id
    try:
        await bot.delete_message(chat_id=cid, message_id=q.message.message_id)
    except Exception:
        logger.exception("Не удалось удалить сообщение-подтверждение")
    try:
        await bot.delete_message(chat_id=cid, message_id=user_mid)
    except Exception:
        logger.exception("Не удалось удалить исходное сообщение отправителя")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика: в личке — профиль; в группе — анонимная ссылка чата."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return

    me = await context.bot.get_me()
    if not me.username:
        await msg.reply_text("У бота нет username — статистику ссылки показать нельзя.")
        return

    today_since = _msk_today_start_utc_iso()

    if chat.type in ("group", "supergroup"):
        gid = chat.id
        invite_row = get_or_create_group_invite_row_id(gid)
        with sqlite3.connect(DB_PATH) as conn:
            m_today = _count_messages_to_group(conn, gid, today_since)
            m_all = _count_messages_to_group(conn, gid, None)
            c_today = _count_group_link_clicks(conn, gid, today_since)
            c_all = _count_group_link_clicks(conn, gid, None)
            pop = _popularity_place_group(conn, gid)
        full_link = f"https://t.me/{me.username}?start=s{invite_row}"
        display_link = f"t.me/{me.username}?start=s{invite_row}"
        share_text = "Напиши анонимно в наш чат 💬"
        share_href = (
            "https://t.me/share/url?"
            f"url={quote(full_link, safe='')}&text={quote(share_text, safe='')}"
        )
        pop_esc = html.escape(pop, quote=False)
        text_html = (
            "<b>📌 Статистика группы</b>\n\n"
            "➖ <b>Сегодня:</b>\n"
            "<blockquote>"
            f"💬 Сообщений в чат: {m_today}\n"
            f"👀 Переходов по ссылке: {c_today}\n"
            f"⭐ Популярность: {pop_esc}"
            "</blockquote>\n\n"
            "➖ <b>За всё время:</b>\n"
            "<blockquote>"
            f"💬 Сообщений в чат: {m_all}\n"
            f"👀 Переходов по ссылке: {c_all}\n"
            f"⭐ Популярность: {pop_esc}"
            "</blockquote>\n\n"
            "Чтобы поднять ⭐ уровень популярности, делитесь ссылкой на анонимные сообщения в этот чат:\n"
            f'👉 <a href="{full_link}">{display_link}</a>'
        )
    else:
        uid = user.id
        with sqlite3.connect(DB_PATH) as conn:
            m_today = _count_messages_to_user(conn, uid, today_since)
            m_all = _count_messages_to_user(conn, uid, None)
            c_today = _count_link_clicks_for_owner(conn, uid, today_since)
            c_all = _count_link_clicks_for_owner(conn, uid, None)
            pop = _popularity_place(conn, uid)

        utok = get_or_create_user_link_token(uid)
        full_link = f"https://t.me/{me.username}?start=q{utok}"
        display_link = f"t.me/{me.username}?start=q{utok}"
        share_text = "Напиши мне анонимно 💬"
        share_href = (
            "https://t.me/share/url?"
            f"url={quote(full_link, safe='')}&text={quote(share_text, safe='')}"
        )

        pop_esc = html.escape(pop, quote=False)
        text_html = (
            "<b>📌 Статистика профиля</b>\n\n"
            "➖ <b>Сегодня:</b>\n"
            "<blockquote>"
            f"💬 Сообщений: {m_today}\n"
            f"👀 Переходов по ссылке: {c_today}\n"
            f"⭐ Популярность: {pop_esc}"
            "</blockquote>\n\n"
            "➖ <b>За всё время:</b>\n"
            "<blockquote>"
            f"💬 Сообщений: {m_all}\n"
            f"👀 Переходов по ссылке: {c_all}\n"
            f"⭐ Популярность: {pop_esc}"
            "</blockquote>\n\n"
            "Чтобы поднять ⭐ уровень популярности, распространяйте свою персональную ссылку:\n"
            f'👉 <a href="{full_link}">{display_link}</a>'
        )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Поделиться ссылкой ↗", url=share_href)]]
    )
    await msg.reply_text(
        text_html,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def issue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Идеи по улучшению бота: /issue или /issue текст предложения."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    body = " ".join(context.args or []).strip()
    if not body:
        await msg.reply_text(
            "💡 Здесь вы можете предложить свою идею по улучшению нашего бота\n\n"
            "Напишите <code>/issue Текст...</code>, чтобы отправить нам сообщение.",
            parse_mode=ParseMode.HTML,
        )
        return

    admin_id = _admin_user_id()
    if admin_id is None:
        await msg.reply_text(
            "Сейчас нельзя принять предложение: не настроен администратор бота (ADMIN_USER_ID)."
        )
        return

    u = _to_dict(user) or {}
    admin_text = (
        "💡 Предложение по боту\n\n"
        f"{format_person_lines('', u)}"
        f"Текст:\n{clip(body, MAX_TEXT - 200)}"
    )
    try:
        await context.bot.send_message(chat_id=admin_id, text=admin_text)
    except Exception:
        logger.exception("Не удалось отправить /issue админу")
        await msg.reply_text("Не удалось доставить сообщение. Попробуйте позже.")
        return

    await msg.reply_text("Спасибо! Идея отправлена.")


def message_content_type(msg) -> str:
    if msg.text:
        return "text"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.audio:
        return "audio"
    if msg.sticker:
        return "sticker"
    if msg.animation:
        return "animation"
    if msg.location:
        return "location"
    if msg.contact:
        return "contact"
    if msg.poll:
        return "poll"
    return "other"


def extract_text_content(msg) -> str | None:
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.contact:
        c = msg.contact
        extra = " ".join(x for x in (c.first_name, c.last_name) if x)
        base = f"contact:{c.phone_number}"
        return f"{base} {extra}".strip() if extra else base
    return None


def format_anonymous_recipient_html(body: str | None, *, max_total: int = MAX_TEXT) -> str:
    """Текст для получателя: 💬, blockquote с текстом, «Свайпни» (кавычки рисует клиент)."""
    head = "<b>💬 У тебя новое сообщение!</b>\n\n"
    tail = "</blockquote>\n\n<i>↪️ Свайпни для ответа.</i>"
    open_bq = "<blockquote>"
    raw = (body or "").strip()
    if not raw:
        raw = "📎"
    safe = html.escape(raw, quote=False)
    overhead = len(head) + len(open_bq) + len(tail)
    max_safe = max(0, max_total - overhead)
    if len(safe) > max_safe:
        safe = clip(safe, max_safe)
    return head + open_bq + safe + tail


def format_anonymous_media_caption_html(body: str | None) -> str:
    """Подпись к медиа: в подписях blockquote у части клиентов не рисуется — кавычки + жирный заголовок."""
    head = "<b>💬 У тебя новое сообщение!</b>\n\n"
    tail = "\n\n<i>↪️ Свайпни для ответа.</i>"
    raw = (body or "").strip()
    mid = html.escape(raw, quote=False) if raw else "📎"
    overhead = len(head) + len(tail) + 8
    max_mid = max(0, TELEGRAM_MEDIA_CAPTION_MAX - overhead - 40)
    if len(mid) > max_mid:
        mid = clip(mid, max_mid)
    return head + "❝\n" + mid + "\n❞" + tail


async def _anon_recipient_markup(bot) -> InlineKeyboardMarkup:
    """Кнопки для владельца ссылки: чат с ботом и жалоба в поддержку."""
    me = await bot.get_me()
    rows: list[list[InlineKeyboardButton]] = []
    if me.username:
        rows.append(
            [InlineKeyboardButton("🗨️ Прокомментировать", url=f"https://t.me/{me.username}")]
        )
    sup = SUPPORT_USERNAME.lstrip("@")
    rows.append(
        [InlineKeyboardButton("🚮 Пожаловаться", url=f"https://t.me/{sup}")]
    )
    return InlineKeyboardMarkup(rows)


async def _try_send_anonymous_media_with_caption(
    bot,
    dest: int,
    msg,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> tuple[bool, list[int]]:
    """Одно медиа + подпись + клавиатура; для стикера — стикер и отдельное сообщение с текстом."""
    kw: dict[str, Any] = {
        "chat_id": dest,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": reply_markup,
    }
    try:
        if msg.photo:
            m = await bot.send_photo(photo=msg.photo[-1].file_id, **kw)
            return True, [m.message_id]
        if msg.video:
            m = await bot.send_video(video=msg.video.file_id, **kw)
            return True, [m.message_id]
        if msg.animation:
            m = await bot.send_animation(animation=msg.animation.file_id, **kw)
            return True, [m.message_id]
        if msg.document:
            m = await bot.send_document(document=msg.document.file_id, **kw)
            return True, [m.message_id]
        if msg.voice:
            m = await bot.send_voice(voice=msg.voice.file_id, **kw)
            return True, [m.message_id]
        if msg.audio:
            m = await bot.send_audio(audio=msg.audio.file_id, **kw)
            return True, [m.message_id]
        if msg.video_note:
            sent = await bot.send_video_note(chat_id=dest, video_note=msg.video_note.file_id)
            m2 = await bot.send_message(
                chat_id=dest,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                reply_to_message_id=sent.message_id,
            )
            return True, [sent.message_id, m2.message_id]
        if msg.sticker:
            sent = await bot.send_sticker(chat_id=dest, sticker=msg.sticker.file_id)
            m2 = await bot.send_message(
                chat_id=dest,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                reply_to_message_id=sent.message_id,
            )
            return True, [sent.message_id, m2.message_id]
    except Exception:
        logger.exception(
            "Прямая отправка анонимного медиа с подписью не удалась dest=%s",
            dest,
        )
    return False, []


async def _deliver_anonymous(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    to_user_id: int | None = None,
    to_chat_id: int | None = None,
) -> None:
    """Доставка в личку пользователю или в группу; копия админу с карточкой."""
    if (to_user_id is None) == (to_chat_id is None):
        return

    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    bot = context.bot
    chat = update.effective_chat
    identifiers = collect_identifiers(update)
    ctype = message_content_type(msg)
    text_part = extract_text_content(msg)
    raw_msg = _to_dict(msg)

    row_id = save_submission(
        user_id=update.effective_user.id,
        chat_id=chat.id,
        message_id=msg.message_id,
        content_type=ctype,
        text_content=text_part,
        identifiers=identifiers,
        raw_message=raw_msg,
        recipient_user_id=to_user_id,
        recipient_chat_id=to_chat_id,
    )

    if to_user_id is not None:
        od = await _owner_dict_from_chat(bot, to_user_id)
        owner_block = format_person_lines("Владелец ссылки:", od)
        dest = to_user_id
    else:
        cd = await _owner_chat_dict_from_id(bot, to_chat_id)
        owner_block = format_group_owner_lines(cd)
        dest = to_chat_id

    sender_dict = identifiers.get("user") or {}
    admin_text = build_anonymous_admin_notification_text(
        row_id, ctype, owner_block, sender_dict, msg
    )

    sender_uid = update.effective_user.id
    delivered = False
    try:
        if msg.text and not msg.photo:
            markup = await _anon_recipient_markup(bot)
            try:
                sent = await bot.send_message(
                    chat_id=dest,
                    text=format_anonymous_recipient_html(msg.text, max_total=MAX_TEXT),
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                route_ids = [sent.message_id]
            except Exception:
                logger.exception(
                    "HTML-шаблон анонимного текста не принят API, пробуем без разметки dest=%s",
                    dest,
                )
                plain = clip(
                    "💬 У тебя новое сообщение!\n\n❝\n"
                    + (msg.text or "")
                    + "\n❞\n\n↪️ Свайпни для ответа.",
                    MAX_TEXT,
                )
                sent = await bot.send_message(
                    chat_id=dest, text=plain, reply_markup=markup
                )
                route_ids = [sent.message_id]
            register_anon_reply_routes(dest, route_ids, sender_uid, row_id)
            delivered = True
        else:
            markup = await _anon_recipient_markup(bot)
            cap_html = format_anonymous_media_caption_html(msg.caption)
            sent_ok, media_ids = await _try_send_anonymous_media_with_caption(
                bot, dest, msg, cap_html, markup
            )
            if sent_ok:
                register_anon_reply_routes(dest, media_ids, sender_uid, row_id)
            else:
                copied = await bot.copy_message(
                    chat_id=dest,
                    from_chat_id=chat.id,
                    message_id=msg.message_id,
                )
                route_ids: list[int] = [copied.message_id]
                try:
                    r = await copied.reply_text(
                        cap_html,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                    route_ids.append(r.message_id)
                except Exception:
                    logger.exception(
                        "Запасной ответ с подписью к copy_message не прошёл dest=%s",
                        dest,
                    )
                    plain = clip(
                        "💬 У тебя новое сообщение!\n\n❝\n"
                        + ((msg.caption or "").strip() or "📎")
                        + "\n❞\n\n↪️ Свайпни для ответа.",
                        MAX_CAPTION,
                    )
                    r2 = await copied.reply_text(plain, reply_markup=markup)
                    route_ids.append(r2.message_id)
                register_anon_reply_routes(dest, route_ids, sender_uid, row_id)
            delivered = True
    except Exception:
        logger.exception(
            "Не удалось доставить анонимное сообщение dest=%s user=%s chat=%s",
            dest,
            to_user_id,
            to_chat_id,
        )
        if to_user_id is not None:
            await msg.reply_text(
                "Не удалось доставить. Часто так бывает, если получатель ещё ни разу не нажимал "
                "/start у этого бота — пусть откроет бота и нажмёт «Start»."
            )
        else:
            await msg.reply_text(
                "Не удалось отправить в группу. Убедитесь, что бот в чате и может писать сообщения."
            )

    admin_id = _admin_user_id()
    if admin_id is not None:
        await send_admin_message_copy(bot, admin_id, chat.id, msg, admin_text)

    if delivered:
        await msg.reply_text(
            "Сообщение отправлено, ожидайте ответ!",
            reply_markup=keyboard_after_anonymous_sent(msg.message_id),
        )


async def inline_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """В любом чате: вставить готовое сообщение с персональной ссылкой (?start=qTOKEN)."""
    iq = update.inline_query
    if not iq or not iq.from_user:
        return
    me = await context.bot.get_me()
    if not me.username:
        await iq.answer([], cache_time=0)
        return
    uid = iq.from_user.id
    utok = get_or_create_user_link_token(uid)
    link = f"https://t.me/{me.username}?start=q{utok}"
    text = f"Напиши мне анонимно 💬\n{link}"
    res = InlineQueryResultArticle(
        id="anon_personal",
        title="🎭 Анонимные вопросы (личная ссылка)",
        description="Вставить текст со ссылкой в этот чат",
        input_message_content=InputTextMessageContent(message_text=text),
    )
    await iq.answer([res], cache_time=1, is_personal=True)


async def handle_owner_reply_to_anonymous_sender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    anon_sender_user_id: int,
    submission_id: int,
) -> None:
    """Ответ получателя (свайп на аноним) → отправителю в макете с цитатой и кнопкой «Написать ещё»."""
    msg = update.effective_message
    if not msg:
        return
    bot = context.bot
    owner_chat_id = msg.chat_id
    owner_name = _user_display_name(_to_dict(msg.from_user) or {})

    sub = fetch_submission_for_reply(submission_id)
    if sub is None:
        try:
            await bot.copy_message(
                chat_id=anon_sender_user_id,
                from_chat_id=owner_chat_id,
                message_id=msg.message_id,
            )
        except Exception:
            logger.exception(
                "Не удалось доставить ответ (нет submission) user=%s",
                anon_sender_user_id,
            )
            await msg.reply_text(
                "Не удалось доставить ответ. Возможно, отправитель заблокировал бота."
            )
            return
        await msg.reply_text(
            "<b>✅ Ответ успешно отправлен</b>\n\n"
            "<i>Статистика — /stats</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    text_content, rec_uid, rec_cid = sub
    original = (text_content or "").strip() or "📎"
    kb = await keyboard_write_more_for_sender(bot, rec_uid, rec_cid)

    try:
        if msg.text and not msg.photo:
            body_html = format_owner_reply_for_sender_html(
                owner_name, original, msg.text or ""
            )
            await bot.send_message(
                chat_id=anon_sender_user_id,
                text=body_html,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        else:
            header = (
                f"<b>{html.escape(owner_name)}</b>\n"
                f"<blockquote>{html.escape(clip(original, 900))}</blockquote>"
            )
            sent = await bot.send_message(
                chat_id=anon_sender_user_id,
                text=header,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            await bot.copy_message(
                chat_id=anon_sender_user_id,
                from_chat_id=owner_chat_id,
                message_id=msg.message_id,
                reply_parameters=ReplyParameters(
                    message_id=sent.message_id,
                    chat_id=anon_sender_user_id,
                ),
            )
    except Exception:
        logger.exception(
            "Не удалось доставить ответ владельца анонимному отправителю user=%s",
            anon_sender_user_id,
        )
        await msg.reply_text(
            "Не удалось доставить ответ. Возможно, отправитель заблокировал бота."
        )
        return

    await msg.reply_text(
        "<b>✅ Ответ успешно отправлен</b>\n\n"
        "<i>Статистика — /stats</i>",
        parse_mode=ParseMode.HTML,
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    if msg.reply_to_message:
        route = lookup_anon_reply_route(msg.chat_id, msg.reply_to_message.message_id)
        if route is not None:
            anon_uid, sub_id = route
            if msg.text and msg.text.strip().startswith("/"):
                pass
            else:
                await handle_owner_reply_to_anonymous_sender(
                    update, context, anon_uid, sub_id
                )
                return

    chat_target = context.user_data.get("anon_target_chat_id")
    user_target = context.user_data.get("anon_target_user_id")
    if chat_target is not None:
        await _deliver_anonymous(update, context, to_chat_id=int(chat_target))
        return
    if user_target is not None:
        await _deliver_anonymous(update, context, to_user_id=int(user_target))
        return

    admin_id = _admin_user_id()
    if admin_id is None:
        await msg.reply_text(
            "Нажмите /start — бот покажет вашу ссылку для анонимных вопросов.\n"
            "Чтобы написать кому-то анонимно, откройте именно его ссылку (не просто бота)."
        )
        return

    identifiers = collect_identifiers(update)
    ctype = message_content_type(msg)
    text_part = extract_text_content(msg)

    raw_msg = _to_dict(msg)

    row_id = save_submission(
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        message_id=msg.message_id,
        content_type=ctype,
        text_content=text_part,
        identifiers=identifiers,
        raw_message=raw_msg,
        recipient_user_id=None,
        recipient_chat_id=None,
    )

    admin_text = build_admin_notification_text(row_id, ctype, identifiers, msg)

    bot = context.bot
    chat = update.effective_chat

    await send_admin_message_copy(bot, admin_id, chat.id, msg, admin_text)

    await msg.reply_text("Сообщение получено и передано.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в переменных окружения или в файле .env")

    init_db()
    admin = _admin_user_id()
    if admin is None:
        logger.info(
            "ADMIN_USER_ID не задан — копии сообщений админу не отправляются (только доставка получателям)"
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cancel_anon_callback, pattern=f"^{CB_CANCEL_ANON}$"))
    app.add_handler(
        CallbackQueryHandler(anon_sent_write_more_callback, pattern=f"^{CB_ANON_SENT_WRITE_MORE}$")
    )
    app.add_handler(
        CallbackQueryHandler(
            anon_sent_delete_callback, pattern=f"^{CB_ANON_SENT_DELETE_PREFIX}\\d+$"
        )
    )
    app.add_handler(InlineQueryHandler(inline_share))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("issue", issue_cmd))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message)
    )
    app.add_handler(MessageHandler(filters.PHOTO, handle_user_message))
    app.add_handler(MessageHandler(filters.VIDEO, handle_user_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_user_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_user_message))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_user_message))
    app.add_handler(MessageHandler(filters.AUDIO, handle_user_message))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_user_message))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_user_message))
    app.add_handler(MessageHandler(filters.LOCATION, handle_user_message))
    app.add_handler(MessageHandler(filters.CONTACT, handle_user_message))
    app.add_handler(MessageHandler(filters.POLL, handle_user_message))

    logger.info("Бот «Подслушано» запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
