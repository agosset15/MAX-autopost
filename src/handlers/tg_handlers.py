import asyncio
import logging
import mimetypes
import os
import re
import tempfile
from typing import TypeAlias, Union

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message as TgMessage, Video, PhotoSize, Document, Audio
from maxapi.enums.message_link_type import MessageLinkType
from maxapi.enums.parse_mode import ParseMode
from maxapi.enums.upload_type import UploadType
from maxapi.exceptions import MaxApiError
from maxapi.methods.types.sended_message import SendedMessage
from maxapi.types import InputMedia, NewMessageLink

from src.config import CHANNEL_MAP, GROUP_MAP
from src.loader import max_bot, message_map, tg_bot, tg_dp
from src.storage.message_map import MaxRef, TgRef


media_groups: dict[str, list[TgMessage]] = {}
_bg_tasks: set[asyncio.Task] = set()

AnyMedia: TypeAlias = Union[Video, PhotoSize, Document, Audio]


async def _safe_max_send(**kwargs) -> SendedMessage | None:
    """Send to MAX as HTML; on API rejection (e.g. unsupported tag) retry as plain text."""
    try:
        return await max_bot.send_message(parse_mode=ParseMode.HTML, **kwargs)
    except MaxApiError as e:
        logging.warning("MAX send failed (%s); retrying as plain text", e)
        return await max_bot.send_message(parse_mode=None, **kwargs)


def _extract_mid(sent: SendedMessage | None) -> str | None:
    if sent is None or sent.message is None or sent.message.body is None:
        return None
    return sent.message.body.mid


async def _resolve_reply_link(message: TgMessage) -> NewMessageLink | None:
    """If TG message is a reply, look up the matching MAX mid and build a REPLY link."""
    if not message.reply_to_message:
        return None
    mx = await message_map.get_max(message.chat.id, message.reply_to_message.message_id)
    if mx is None:
        return None
    return NewMessageLink(type=MessageLinkType.REPLY, mid=mx.mid)


# ====== REGEX ======

TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)
TG_SPOILER_RE = re.compile(r"<tg-spoiler[^>]*>(.*?)</tg-spoiler>", re.DOTALL)
ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.DOTALL | re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{4,31})\b")


# ====== TEXT UTILS ======

def linkify_mentions(html: str) -> str:
    """Wrap @mentions in <a href="https://t.me/..."> while skipping existing anchors."""
    parts = []
    last = 0
    for m in ANCHOR_RE.finditer(html):
        parts.append(MENTION_RE.sub(r'<a href="https://t.me/\1">@tg:\1</a>', html[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(MENTION_RE.sub(r'<a href="https://t.me/\1">@tg:\1</a>', html[last:]))
    return "".join(parts)


def clean_html(html: str) -> str:
    """Удаляет служебные Telegram-теги, сохраняя содержимое."""
    html = TG_EMOJI_RE.sub(r"\1", html)
    html = TG_SPOILER_RE.sub(r"\1", html)
    html = linkify_mentions(html)
    return html


def extract_text(message: TgMessage) -> str:
    return clean_html(message.html_text or "")


# ====== FILE UTILS ======

TG_FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB — Telegram Bot API hard limit


def _resolve_suffix(file: AnyMedia, fallback: str | None) -> str:
    mime = getattr(file, "mime_type", None)
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed
    if fallback and fallback != "*":
        return fallback
    return ".bin"


def _resolve_filename(file: AnyMedia, fallback_suffix: str | None) -> str:
    name = getattr(file, "file_name", None)
    if name:
        name = os.path.basename(name)
        if name:
            return name
    return f"file{_resolve_suffix(file, fallback_suffix)}"


def _cleanup_temp(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    finally:
        parent = os.path.dirname(path)
        if parent and os.path.isdir(parent):
            try:
                os.rmdir(parent)
            except OSError:
                pass


async def download_to_temp(file: AnyMedia, suffix: str | None) -> str:
    file_size = getattr(file, "file_size", None)
    if file_size and file_size > TG_FILE_SIZE_LIMIT:
        raise ValueError(f"File too large for Bot API: {file_size} bytes")

    file_info = await tg_bot.get_file(file.file_id)
    if not file_info.file_path:
        raise ValueError("File not found")

    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, _resolve_filename(file, suffix))

    await tg_bot.download_file(file_info.file_path, destination=path)
    return path


async def send_media(
    *,
    file: AnyMedia,
    suffix: str | None,
    upload_type: UploadType,
    text: str,
    max_id: int,
    link: NewMessageLink | None = None,
) -> SendedMessage | None:
    """Returns SendedMessage on success, None if file too large to download or send failed."""
    try:
        temp_path = await download_to_temp(file, suffix)
    except (TelegramBadRequest, ValueError) as e:
        logging.warning("Cannot download file: %s", e)
        return None

    try:
        media = InputMedia(temp_path, upload_type)
        return await _safe_max_send(
            chat_id=max_id,
            text=text,
            attachments=[media],
            link=link,
        )
    finally:
        _cleanup_temp(temp_path)


# ====== MEDIA GROUP ======

async def send_media_group(media_group_id: str, max_id: int):
    await asyncio.sleep(2)

    messages = media_groups.pop(media_group_id, [])
    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    text = next((extract_text(m) for m in messages if m.caption), None)
    link = await _resolve_reply_link(messages[0])

    temp_files: list[str] = []
    attachments: list[InputMedia] = []

    try:
        for msg in messages:
            if msg.photo:
                file, suffix, upload_type = msg.photo[-1], ".jpg", UploadType.IMAGE
            elif msg.video:
                file, suffix, upload_type = msg.video, ".mp4", UploadType.VIDEO
            elif msg.document:
                file, suffix, upload_type = msg.document, None, UploadType.FILE
            else:
                continue

            try:
                path = await download_to_temp(file, suffix)
            except (TelegramBadRequest, ValueError) as e:
                logging.warning("Media group %s: skipping item — %s", media_group_id, e)
                continue

            temp_files.append(path)
            attachments.append(InputMedia(path, upload_type))

        if attachments:
            sent = await _safe_max_send(
                chat_id=max_id,
                text=text,
                attachments=attachments,
                link=link,
            )
            logging.info(
                "Forwarded media group %s (%d items) to MAX channel %d",
                media_group_id,
                len(attachments),
                max_id,
            )
            mid = _extract_mid(sent)
            if mid:
                tg_refs = [TgRef(chat_id=m.chat.id, message_id=m.message_id) for m in messages]
                await message_map.bind_many(tg_refs, MaxRef(chat_id=max_id, mid=mid))

    except Exception as e:
        logging.exception("Media group error: %s", e)

    finally:
        for path in temp_files:
            _cleanup_temp(path)


# ====== MEDIA HANDLERS ======

MEDIA_HANDLERS = {
    "photo": (".jpg", UploadType.IMAGE),
    "video": (".mp4", UploadType.VIDEO),
    "audio": (".mp3", UploadType.AUDIO),
    "document": (None, UploadType.FILE),
}


# ====== FORWARDING ======

async def forward_to_max(message: TgMessage, max_id: int):
    logging.info(
        "New Telegram post %s in chat %s -> MAX id %d",
        message.message_id,
        message.chat.id,
        max_id,
    )

    # media group
    if message.media_group_id:
        media_groups.setdefault(message.media_group_id, []).append(message)
        if len(media_groups[message.media_group_id]) == 1:
            task = asyncio.create_task(send_media_group(message.media_group_id, max_id))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        return

    text = extract_text(message)
    link = await _resolve_reply_link(message)
    tg_ref = TgRef(chat_id=message.chat.id, message_id=message.message_id)

    # single media
    for attr, (suffix, upload_type) in MEDIA_HANDLERS.items():
        media_obj = getattr(message, attr, None)
        if media_obj:
            file = (
                media_obj[-1]
                if isinstance(media_obj, list)
                else media_obj
            )
            sent = await send_media(
                file=file,
                suffix=suffix,
                upload_type=upload_type,
                text=text,
                max_id=max_id,
                link=link,
            )
            if sent is not None:
                logging.info("Forwarded %s to MAX channel %d", attr, max_id)
                mid = _extract_mid(sent)
                if mid:
                    await message_map.bind(tg_ref, MaxRef(chat_id=max_id, mid=mid))
            else:
                logging.warning(
                    "Skipped %s (post %s) in chat %s — file too large for Bot API",
                    attr, message.message_id, message.chat.id,
                )
            return

    # text only
    if text:
        sent = await _safe_max_send(chat_id=max_id, text=text, link=link)
        logging.info("Forwarded text to MAX id %d", max_id)
        mid = _extract_mid(sent)
        if mid:
            await message_map.bind(tg_ref, MaxRef(chat_id=max_id, mid=mid))


# ====== EDIT FORWARDING ======

async def forward_edit_to_max(message: TgMessage):
    """Propagate TG edit to MAX by looking up the bound MAX mid."""
    mx = await message_map.get_max(message.chat.id, message.message_id)
    if mx is None:
        logging.info(
            "Edit ignored: no MAX mapping for TG (%s, %s)",
            message.chat.id, message.message_id,
        )
        return

    text = extract_text(message)
    if not text:
        return

    try:
        await max_bot.edit_message(message_id=mx.mid, text=text, parse_mode=ParseMode.HTML)
        logging.info("Edited MAX %s after TG edit", mx.mid)
    except MaxApiError as e:
        logging.warning("MAX edit failed as HTML (%s); retrying as plain text", e)
        try:
            await max_bot.edit_message(message_id=mx.mid, text=text, parse_mode=None)
        except MaxApiError as e2:
            logging.warning("MAX edit failed (%s)", e2)


# ====== MAIN HANDLERS ======

@tg_dp.channel_post(F.chat.id.in_(CHANNEL_MAP))
async def on_channel_post(message: TgMessage):
    await forward_to_max(message, CHANNEL_MAP[message.chat.id])


@tg_dp.message(F.chat.id.in_(GROUP_MAP))
async def on_group_message(message: TgMessage):
    cfg = GROUP_MAP[message.chat.id]

    if cfg.allowed_user_ids is not None:
        if not message.from_user or message.from_user.id not in cfg.allowed_user_ids:
            return

    if cfg.allowed_thread_ids is not None:
        if (message.message_thread_id not in cfg.allowed_thread_ids and
                int(bool(message.message_thread_id)) not in cfg.allowed_thread_ids):
            return

    await forward_to_max(message, cfg.chat_id)


@tg_dp.edited_channel_post(F.chat.id.in_(CHANNEL_MAP))
async def on_channel_post_edited(message: TgMessage):
    await forward_edit_to_max(message)


@tg_dp.edited_message(F.chat.id.in_(GROUP_MAP))
async def on_group_message_edited(message: TgMessage):
    cfg = GROUP_MAP[message.chat.id]
    if cfg.allowed_user_ids is not None:
        if not message.from_user or message.from_user.id not in cfg.allowed_user_ids:
            return
    await forward_edit_to_max(message)
