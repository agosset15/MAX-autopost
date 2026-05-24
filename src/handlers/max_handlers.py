import logging
import os
import tempfile

from aiogram.enums import ParseMode as TgParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
from maxapi.enums.attachment import AttachmentType
from maxapi.types import BotStarted, MessageCreated
from maxapi import F

from src.config import MAX_TO_TG
from src.loader import max_dp, max_bot, tg_bot


@max_dp.bot_started()
async def bot_started(event: BotStarted):
    await max_bot.send_message(
        chat_id=event.chat_id,
        text='Привет! Отправь мне /start'
    )


# ====== TEMP UTILS ======

def _cleanup(path: str) -> None:
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


async def _download(url: str) -> str:
    tmp_dir = tempfile.mkdtemp()
    path = await max_bot.download_file(url, tmp_dir)
    return str(path)


# ====== ATTACHMENT URL ======

def _attachment_url(att) -> str | None:
    if att.type == AttachmentType.VIDEO:
        urls = getattr(att, "urls", None)
        if urls:
            for res in (urls.mp4_1080, urls.mp4_720, urls.mp4_480,
                        urls.mp4_360, urls.mp4_240, urls.mp4_144):
                if res:
                    return res
    payload = getattr(att, "payload", None)
    return getattr(payload, "url", None) if payload else None


# ====== SAFE SEND (HTML -> plain fallback) ======

async def _safe(build):
    """build(parse_mode) -> coroutine. Try HTML, fall back to plain text."""
    try:
        await build(TgParseMode.HTML)
    except TelegramBadRequest as e:
        logging.warning("TG send failed (%s); retrying as plain text", e)
        await build(None)


# ====== SENDERS ======

MEDIA_CLS = {
    AttachmentType.IMAGE: InputMediaPhoto,
    AttachmentType.VIDEO: InputMediaVideo,
}

SINGLE_SEND = {
    AttachmentType.IMAGE: ("send_photo", "photo"),
    AttachmentType.VIDEO: ("send_video", "video"),
    AttachmentType.AUDIO: ("send_audio", "audio"),
    AttachmentType.FILE: ("send_document", "document"),
}


async def _send_text(tg_id: int, text: str):
    async def build(pm):
        await tg_bot.send_message(tg_id, text, parse_mode=pm)
    await _safe(build)


async def _send_single_media(tg_id: int, att, path: str, caption: str | None):
    method_name, kw = SINGLE_SEND[att.type]
    send = getattr(tg_bot, method_name)
    filename = getattr(att, "filename", None)

    async def build(pm):
        file = FSInputFile(path, filename=filename) if filename else FSInputFile(path)
        await send(tg_id, **{kw: file}, caption=caption, parse_mode=pm)
    await _safe(build)


async def _send_media_group(tg_id: int, items: list, caption: str | None):
    async def build(pm):
        media = []
        for i, (att, path) in enumerate(items):
            cls = MEDIA_CLS[att.type]
            media.append(cls(
                media=FSInputFile(path),
                caption=caption if i == 0 else None,
                parse_mode=pm,
            ))
        await tg_bot.send_media_group(tg_id, media)
    await _safe(build)


# ====== FORWARDING ======

SUPPORTED = {
    AttachmentType.IMAGE,
    AttachmentType.VIDEO,
    AttachmentType.AUDIO,
    AttachmentType.FILE,
}


async def forward_to_tg(message, tg_id: int):
    body = message.body
    text = None
    attachments = []
    if body:
        text = body.html_text or body.text
        attachments = body.attachments or []

    media_atts = [a for a in attachments
                  if a.type in (AttachmentType.IMAGE, AttachmentType.VIDEO)]
    file_atts = [a for a in attachments
                 if a.type in (AttachmentType.AUDIO, AttachmentType.FILE)]

    for a in attachments:
        if a.type not in SUPPORTED:
            logging.info("Skipping unsupported MAX attachment %s -> TG %d", a.type, tg_id)

    # text only
    if not media_atts and not file_atts:
        if text:
            await _send_text(tg_id, text)
            logging.info("Forwarded MAX text -> TG %d", tg_id)
        return

    caption = text
    paths: list[str] = []
    try:
        downloaded: list[tuple] = []
        for att in media_atts:
            url = _attachment_url(att)
            if not url:
                continue
            try:
                p = await _download(url)
            except Exception as e:
                logging.warning("MAX download failed (%s); skipping", e)
                continue
            paths.append(p)
            downloaded.append((att, p))

        try:
            if len(downloaded) >= 2:
                await _send_media_group(tg_id, downloaded, caption)
                caption = None
            elif len(downloaded) == 1:
                att, p = downloaded[0]
                await _send_single_media(tg_id, att, p, caption)
                caption = None
        except Exception as e:
            logging.warning("Failed sending MAX media group -> TG %d: %s", tg_id, e)

        for att in file_atts:
            url = _attachment_url(att)
            if not url:
                continue
            try:
                p = await _download(url)
            except Exception as e:
                logging.warning("MAX download failed (%s); skipping", e)
                continue
            paths.append(p)
            try:
                await _send_single_media(tg_id, att, p, caption)
                caption = None
            except Exception as e:
                logging.warning("Failed sending MAX file -> TG %d: %s", tg_id, e)

        # text survived (all media failed) — still deliver it
        if caption:
            await _send_text(tg_id, caption)

        logging.info("Forwarded MAX message -> TG %d", tg_id)
    finally:
        for p in paths:
            _cleanup(p)


# ====== MAIN HANDLER ======

@max_dp.message_created(F.message.recipient.chat_id.in_(MAX_TO_TG))
async def on_max_message(event: MessageCreated):
    chat_id = event.message.recipient.chat_id
    if not chat_id:
        return

    sender = event.message.sender
    if sender and max_bot.me and sender.user_id == max_bot.me.user_id:
        return  # ignore the bot's own messages

    cfg = MAX_TO_TG[chat_id]

    if cfg.allowed_user_ids is not None:
        if sender and sender.user_id not in cfg.allowed_user_ids:
            return

    logging.info("New MAX message in chat %s -> TG %d", chat_id, cfg.chat_id)
    await forward_to_tg(event.message, cfg.chat_id)
