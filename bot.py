"""
Бот «Подслушано»: персональная ссылка для анонимных вопросов; по желанию — копия админу.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "podslushano.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_USER_ID_RAW = os.environ.get("ADMIN_USER_ID", "").strip()

MAX_CAPTION = 3500
MAX_TEXT = 4000


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
        conn.commit()


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


def build_admin_notification_text(
    row_id: int,
    ctype: str,
    identifiers: dict[str, Any],
    msg,
) -> str:
    u = identifiers.get("user") or {}
    name = _user_display_name(u)
    uname = u.get("username")
    username_line = f"@{uname}" if uname else "—"
    uid = u.get("id")
    id_line = str(uid) if uid is not None else "—"
    body = format_message_body_for_admin(msg, ctype)
    return (
        f"📥 Подслушано — запись #{row_id}\n"
        f"Тип: {ctype}\n\n"
        f"Имя: {name}\n"
        f"Username: {username_line}\n"
        f"ID: {id_line}\n\n"
        f"Сообщение:\n{body}"
    )


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
) -> int:
    created = datetime.now(timezone.utc).isoformat()
    identifiers_json = json.dumps(identifiers, ensure_ascii=False)
    raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions
            (created_at, user_id, chat_id, message_id, content_type, text_content,
             identifiers_json, raw_message_json, recipient_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n… (обрезано)"


def _parse_anon_target_start(args: list[str]) -> int | None:
    """Deep link: /start q<telegram_user_id> (латиница q + цифры)."""
    if not args:
        return None
    payload = args[0].strip()
    if len(payload) >= 2 and payload[0] == "q" and payload[1:].isdigit():
        return int(payload[1:])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    target = _parse_anon_target_start(context.args or [])
    if target is not None:
        context.user_data["anon_target_id"] = target
        await msg.reply_text(
            "Напишите сообщение — оно уйдёт анонимно человеку, который дал вам ссылку.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    context.user_data.pop("anon_target_id", None)

    bot = context.bot
    me = await bot.get_me()
    if not me.username:
        await msg.reply_text(
            "У бота нет username в Telegram — задайте его в @BotFather, иначе ссылку нельзя сделать.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    link = f"https://t.me/{me.username}?start=q{user.id}"
    text = (
        "Начните получать анонимные вопросы прямо сейчас!\n\n"
        f'👉 "{link}"\n\n'
        "Разместите эту ссылку ☝️ в описании своего профиля Telegram, TikTok, Instagram (stories), "
        "чтобы вам могли написать 💬"
    )
    await msg.reply_text(text, reply_markup=ReplyKeyboardRemove(), disable_web_page_preview=True)


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


ANON_HEADER = "💬 Анонимное сообщение\n\n"


async def _deliver_anonymous_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    recipient_id: int,
) -> None:
    """Доставка получателю без раскрытия отправителя в тексте."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    bot = context.bot
    chat = update.effective_chat
    identifiers = collect_identifiers(update)
    ctype = message_content_type(msg)
    text_part = extract_text_content(msg)
    raw_msg = _to_dict(msg)

    save_submission(
        user_id=update.effective_user.id,
        chat_id=chat.id,
        message_id=msg.message_id,
        content_type=ctype,
        text_content=text_part,
        identifiers=identifiers,
        raw_message=raw_msg,
        recipient_user_id=recipient_id,
    )

    try:
        if msg.text and not msg.photo:
            await bot.send_message(
                chat_id=recipient_id,
                text=clip(ANON_HEADER + msg.text, MAX_TEXT),
            )
        else:
            copied = await bot.copy_message(
                chat_id=recipient_id,
                from_chat_id=chat.id,
                message_id=msg.message_id,
            )
            tail = ANON_HEADER.strip()
            if msg.caption:
                tail += "\n\n" + msg.caption
            await copied.reply_text(clip(tail, MAX_CAPTION))
    except Exception:
        logger.exception("Не удалось доставить анонимное сообщение user_id=%s", recipient_id)
        await msg.reply_text(
            "Не удалось доставить. Часто так бывает, если получатель ещё ни разу не нажимал "
            "/start у этого бота — пусть откроет бота и нажмёт «Start»."
        )
        return

    await msg.reply_text("Отправлено.")


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    recipient_id = context.user_data.get("anon_target_id")
    if recipient_id is not None:
        await _deliver_anonymous_message(update, context, int(recipient_id))
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
    )

    admin_text = build_admin_notification_text(row_id, ctype, identifiers, msg)

    bot = context.bot
    chat = update.effective_chat

    try:
        if msg.text and not msg.photo:
            await bot.send_message(
                chat_id=admin_id,
                text=clip(admin_text, MAX_TEXT),
            )
        else:
            copied = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=chat.id,
                message_id=msg.message_id,
            )
            await copied.reply_text(clip(admin_text, MAX_CAPTION))
    except Exception:
        logger.exception("Не удалось отправить админу; дублируем одним текстом")
        await bot.send_message(chat_id=admin_id, text=clip(admin_text, MAX_TEXT))

    await msg.reply_text("Сообщение получено и передано.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в переменных окружения или в файле .env")

    init_db()
    admin = _admin_user_id()
    if admin is None:
        logger.info("ADMIN_USER_ID не задан — режим только личных ссылок и анонимных сообщений")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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
