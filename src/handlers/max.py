import html
import logging
import os
import tempfile

from aiogram.enums import ParseMode as TgParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message as TgMessage, ReplyParameters
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.message_link_type import MessageLinkType
from maxapi.types import BotStarted, MessageCreated
from maxapi.types.updates.message_edited import MessageEdited
from maxapi import F, Router

from src.config import MAX_ROUTES, Route
from src.loader import max_bot, message_map, tg_bot
from src.storage.message_map import MaxRef, TgRef

max_router = Router(router_id="main")


@max_router.bot_started()
async def bot_started(event: BotStarted):
    await max_bot.send_message(
        chat_id=event.chat_id,
        text='Привет! Отправь мне /start'
    )


# ====== SIGN UTILS ======

def build_sign_prefix(sender) -> str:
    """Build HTML signature prefix `<b>Name</b>:\\n` for MAX -> TG sign_names mode."""
    if not sender:
        return ""
    name = getattr(sender, "full_name", None) or getattr(sender, "first_name", None)
    if not name:
        username = getattr(sender, "username", None)
        name = f"@{username}" if username else ""
    if not name:
        return ""
    return f"<b>{html.escape(name)}</b>:\n"


def _apply_prefix(prefix: str, text: str | None) -> str | None:
    if not prefix:
        return text
    if not text:
        return prefix.rstrip(":\n")
    return prefix + text


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


async def _download_cached(url: str, cache: dict[str, str]) -> str:
    """Download once per url; a MAX message fanned out to several TG chats reuses the file."""
    path = cache.get(url)
    if path is None:
        path = await _download(url)
        cache[url] = path
    return path


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
    """build(parse_mode) -> coroutine returning a Message (or list).

    Tries HTML, falls back to plain text. Returns whatever the build returns
    so the caller can capture message ids.
    """
    try:
        return await build(TgParseMode.HTML)
    except TelegramBadRequest as e:
        logging.warning("TG send failed (%s); retrying as plain text", e)
        return await build(None)


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


async def _send_text(tg_id: int, text: str, reply_to: int | None = None) -> TgMessage | None:
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        return await tg_bot.send_message(tg_id, text, parse_mode=pm, reply_parameters=rp)

    return await _safe(build)


async def _send_single_media(
    tg_id: int, att, path: str, caption: str | None, reply_to: int | None = None,
) -> TgMessage | None:
    method_name, kw = SINGLE_SEND[att.type]
    send = getattr(tg_bot, method_name)
    filename = getattr(att, "filename", None)
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        file = FSInputFile(path, filename=filename) if filename else FSInputFile(path)
        return await send(tg_id, **{kw: file}, caption=caption, parse_mode=pm, reply_parameters=rp)

    return await _safe(build)


async def _send_media_group(
    tg_id: int, items: list, caption: str | None, reply_to: int | None = None,
) -> list[TgMessage] | None:
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        media = []
        for i, (att, path) in enumerate(items):
            cls = MEDIA_CLS[att.type]
            media.append(cls(
                media=FSInputFile(path),
                caption=caption if i == 0 else None,
                parse_mode=pm,
            ))
        return await tg_bot.send_media_group(tg_id, media, reply_parameters=rp)

    return await _safe(build)


# ====== FORWARDING ======

SUPPORTED = {
    AttachmentType.IMAGE,
    AttachmentType.VIDEO,
    AttachmentType.AUDIO,
    AttachmentType.FILE,
}


async def _resolve_reply_to(message, tg_id: int) -> int | None:
    """If MAX message replies to another, look up its copy inside `tg_id`."""
    link = getattr(message, "link", None)
    if not link or link.type != MessageLinkType.REPLY:
        return None
    src_mid = link.message.mid if link.message else None
    if not src_mid:
        return None
    max_chat_id = message.recipient.chat_id
    if max_chat_id is None:
        return None
    tg_ref = await message_map.get_tg_in(max_chat_id, src_mid, tg_id)
    if tg_ref is None:
        return None
    return tg_ref.message_id


async def forward_to_tg(
    message, tg_id: int, sender_prefix: str = "", cache: dict[str, str] | None = None,
) -> int | None:
    """Forward MAX message to TG. Returns the primary TG message_id on success.

    `cache` maps attachment url -> local path and is owned by the caller when
    given (so one message fanned out to several TG chats downloads once); the
    caller then cleans those files up via :func:`cleanup_cache`.
    """
    own_cache = cache is None
    if cache is None:
        cache = {}
    body = message.body
    text = None
    attachments = []
    if body:
        text = body.html_text or body.text
        attachments = body.attachments or []
    text = _apply_prefix(sender_prefix, text)

    media_atts = [a for a in attachments
                  if a.type in (AttachmentType.IMAGE, AttachmentType.VIDEO)]
    file_atts = [a for a in attachments
                 if a.type in (AttachmentType.AUDIO, AttachmentType.FILE)]

    for a in attachments:
        if a.type not in SUPPORTED:
            logging.info("Skipping unsupported MAX attachment %s -> TG %d", a.type, tg_id)

    reply_to = await _resolve_reply_to(message, tg_id)
    primary_tg_id: int | None = None

    # text only
    if not media_atts and not file_atts:
        if text:
            sent = await _send_text(tg_id, text, reply_to=reply_to)
            if sent is not None:
                primary_tg_id = sent.message_id
            logging.info("Forwarded MAX text -> TG %d", tg_id)
        return primary_tg_id

    caption = text
    try:
        downloaded: list[tuple] = []
        for att in media_atts:
            url = _attachment_url(att)
            if not url:
                continue
            try:
                p = await _download_cached(url, cache)
            except Exception as e:
                logging.warning("MAX download failed (%s); skipping", e)
                continue
            downloaded.append((att, p))

        try:
            if len(downloaded) >= 2:
                sent = await _send_media_group(tg_id, downloaded, caption, reply_to=reply_to)
                if sent:
                    primary_tg_id = sent[0].message_id
                caption = None
            elif len(downloaded) == 1:
                att, p = downloaded[0]
                sent = await _send_single_media(tg_id, att, p, caption, reply_to=reply_to)
                if sent is not None and primary_tg_id is None:
                    primary_tg_id = sent.message_id
                caption = None
        except Exception as e:
            logging.warning("Failed sending MAX media group -> TG %d: %s", tg_id, e)

        for att in file_atts:
            url = _attachment_url(att)
            if not url:
                continue
            try:
                p = await _download_cached(url, cache)
            except Exception as e:
                logging.warning("MAX download failed (%s); skipping", e)
                continue
            try:
                sent = await _send_single_media(tg_id, att, p, caption, reply_to=reply_to)
                if sent is not None and primary_tg_id is None:
                    primary_tg_id = sent.message_id
                caption = None
            except Exception as e:
                logging.warning("Failed sending MAX file -> TG %d: %s", tg_id, e)

        # text survived (all media failed) — still deliver it
        if caption:
            sent = await _send_text(tg_id, caption, reply_to=reply_to)
            if sent is not None and primary_tg_id is None:
                primary_tg_id = sent.message_id

        logging.info("Forwarded MAX message -> TG %d", tg_id)
    finally:
        if own_cache:
            cleanup_cache(cache)

    return primary_tg_id


def cleanup_cache(cache: dict[str, str]) -> None:
    for path in cache.values():
        _cleanup(path)


# ====== EDIT FORWARDING ======

async def forward_edit_to_tg(message, tg_id: int, sender_prefix: str = ""):
    """Propagate a MAX edit to the copy of that message inside `tg_id`."""
    max_chat_id = message.recipient.chat_id
    body = message.body
    if max_chat_id is None or body is None:
        return

    tg_ref = await message_map.get_tg_in(max_chat_id, body.mid, tg_id)
    if tg_ref is None:
        logging.info(
            "Edit ignored: no TG mapping for MAX (%s, %s) in TG %d",
            max_chat_id, body.mid, tg_id,
        )
        return

    text = _apply_prefix(sender_prefix, body.html_text or body.text)
    if not text:
        return

    async def try_edit(method, **kwargs):
        try:
            await method(chat_id=tg_ref.chat_id, message_id=tg_ref.message_id, parse_mode=TgParseMode.HTML, **kwargs)
            return True
        except TelegramBadRequest as e:
            logging.warning("TG edit (HTML) failed (%s); retrying as plain text", e)
            try:
                await method(chat_id=tg_ref.chat_id, message_id=tg_ref.message_id, parse_mode=None, **kwargs)
                return True
            except TelegramBadRequest as e2:
                logging.warning("TG edit failed (%s)", e2)
                return False

    has_media = bool(body.attachments)
    if has_media:
        if not await try_edit(tg_bot.edit_message_caption, caption=text):
            return
    else:
        if not await try_edit(tg_bot.edit_message_text, text=text):
            return
    logging.info("Edited TG (%s, %s) after MAX edit", tg_ref.chat_id, tg_ref.message_id)


# ====== MAIN HANDLERS ======

def _targets(chat_id: int, sender) -> list[tuple[Route, str]]:
    """Routes leaving this MAX chat that accept `sender`, each with its prefix."""
    out: list[tuple[Route, str]] = []
    for route in MAX_ROUTES.get(chat_id, ()):
        if route.allowed_user_ids is not None:
            if sender and sender.user_id not in route.allowed_user_ids:
                continue
        out.append((route, build_sign_prefix(sender) if route.sign_names else ""))
    return out


@max_router.message_created(F.message.recipient.chat_id.in_(MAX_ROUTES))
async def on_max_message(event: MessageCreated):
    chat_id = event.message.recipient.chat_id
    if not chat_id:
        return

    sender = event.message.sender
    if sender and max_bot.me and sender.user_id == max_bot.me.user_id:
        return  # ignore the bot's own messages

    targets = _targets(chat_id, sender)
    if not targets:
        return

    logging.info(
        "New MAX message in chat %s -> TG ids %s",
        chat_id, [route.tg_id for route, _ in targets],
    )

    body = event.message.body
    cache: dict[str, str] = {}
    try:
        for route, sender_prefix in targets:
            primary_tg_id = await forward_to_tg(
                event.message, route.tg_id, sender_prefix=sender_prefix, cache=cache,
            )
            if primary_tg_id is not None and body is not None:
                await message_map.bind(
                    TgRef(chat_id=route.tg_id, message_id=primary_tg_id),
                    MaxRef(chat_id=chat_id, mid=body.mid),
                )
    finally:
        cleanup_cache(cache)


@max_router.message_edited(F.message.recipient.chat_id.in_(MAX_ROUTES))
async def on_max_message_edited(event: MessageEdited):
    chat_id = event.message.recipient.chat_id
    if not chat_id:
        return

    sender = event.message.sender
    if sender and max_bot.me and sender.user_id == max_bot.me.user_id:
        return

    for route, sender_prefix in _targets(chat_id, sender):
        logging.info("MAX message edited in chat %s -> TG %d", chat_id, route.tg_id)
        await forward_edit_to_tg(event.message, route.tg_id, sender_prefix=sender_prefix)
