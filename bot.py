"""
Бот «Подслушано»: принимает сообщения пользователей, сохраняет их и дублирует админу
со всеми доступными идентификаторами из Telegram Bot API.
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
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
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
                raw_message_json TEXT
            )
            """
        )
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


def format_phone_line_for_admin(msg) -> str:
    """Телефон бот видит только если пользователь прислал контакт (vCard)."""
    if not msg or not msg.contact:
        return ""
    c = msg.contact
    lines = [f"Телефон (пользователь открыл / прислал контакт): {c.phone_number}"]
    name = " ".join(x for x in (c.first_name, c.last_name) if x)
    if name:
        lines.append(f"Имя в контакте: {name}")
    if c.user_id is not None:
        lines.append(f"user_id в записи контакта: {c.user_id}")
    return "\n".join(lines) + "\n\n"


def pretty_identifiers(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def save_submission(
    *,
    user_id: int | None,
    chat_id: int | None,
    message_id: int | None,
    content_type: str,
    text_content: str | None,
    identifiers: dict[str, Any],
    raw_message: dict[str, Any] | None,
) -> int:
    created = datetime.now(timezone.utc).isoformat()
    identifiers_json = json.dumps(identifiers, ensure_ascii=False)
    raw_json = json.dumps(raw_message, ensure_ascii=False) if raw_message else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions
            (created_at, user_id, chat_id, message_id, content_type, text_content,
             identifiers_json, raw_message_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n… (обрезано)"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("📱 Отправить номер телефона", request_contact=True)],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.effective_message.reply_text(
        "Напишите сюда всё, что нужно передать анонимно или для учёта — "
        "сообщение будет сохранено и передано администратору вместе с вашими "
        "техническими идентификаторами в Telegram (как видит бот).\n\n"
        "Номер телефона бот не видит сам по себе — только если вы нажмёте кнопку ниже "
        "и подтвердите отправку контакта.",
        reply_markup=markup,
    )


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


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_id = _admin_user_id()
    if admin_id is None:
        logger.error("Не задан ADMIN_USER_ID — куда слать сообщения админу")
        if update.effective_message:
            await update.effective_message.reply_text("Бот не настроен. Обратитесь к владельцу.")
        return

    msg = update.effective_message
    if not msg:
        return

    identifiers = collect_identifiers(update)
    ids_block = pretty_identifiers(identifiers)
    ctype = message_content_type(msg)
    text_part = extract_text_content(msg)
    phone_block = format_phone_line_for_admin(msg)

    raw_msg = _to_dict(msg)

    row_id = save_submission(
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        message_id=msg.message_id,
        content_type=ctype,
        text_content=text_part,
        identifiers=identifiers,
        raw_message=raw_msg,
    )

    header = (
        f"📥 Подслушано — запись #{row_id}\n"
        f"Тип: {ctype}\n"
        f"{phone_block}"
        f"Идентификаторы (JSON):\n{clip(ids_block, MAX_TEXT - 400)}"
    )

    bot = context.bot
    chat = update.effective_chat

    try:
        if msg.text and not msg.photo:
            body = f"\n\nТекст:\n{clip(msg.text, MAX_TEXT - len(header))}"
            await bot.send_message(
                chat_id=admin_id,
                text=clip(header + body, MAX_TEXT),
            )
        else:
            copied = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=chat.id,
                message_id=msg.message_id,
            )
            admin_header = (
                f"📥 Подслушано — запись #{row_id}\n"
                f"Тип: {ctype}\n"
                f"{phone_block}"
                f"Идентификаторы:\n{clip(ids_block, MAX_CAPTION)}"
            )
            await copied.reply_text(admin_header)
    except Exception:
        logger.exception("Не удалось отправить админу; пробуем текстом целиком")
        fallback = f"{header}\n\n(медиа не скопировано — откройте запись в БД id={row_id})"
        if text_part:
            fallback += f"\n\nПодпись/текст: {text_part}"
        await bot.send_message(chat_id=admin_id, text=clip(fallback, MAX_TEXT))

    await msg.reply_text("Сообщение получено и передано.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в переменных окружения или в файле .env")

    init_db()
    admin = _admin_user_id()
    if admin is None:
        logger.warning("ADMIN_USER_ID не задан — уведомления админу работать не будут")

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
