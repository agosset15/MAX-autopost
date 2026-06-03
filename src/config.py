from typing import Optional
import json
import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Webhook (Telegram)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://example.com/webhook/tg
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is required in .env")

TELEGRAM_WEBHOOK_PATH: str = os.getenv("TELEGRAM_WEBHOOK_PATH", "/webhook/tg")
MAX_WEBHOOK_PATH: str = os.getenv("MAX_WEBHOOK_PATH", "/webhook/max")

TELEGRAM_WEBHOOK_URL = urlparse(WEBHOOK_URL)._replace(path=TELEGRAM_WEBHOOK_PATH).geturl()
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL") or urlparse(WEBHOOK_URL)._replace(path=MAX_WEBHOOK_PATH).geturl()

WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "9154"))

# Shared secret verifying inbound webhook requests (TG: X-Telegram-Bot-Api-Secret-Token,
# MAX: X-Max-Bot-Api-Secret). If unset, a random one is generated per process — webhooks
# are re-registered with it at startup, so set it explicitly only if you need it stable.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(32)
    logging.warning("WEBHOOK_SECRET not set; generated a random ephemeral secret for this run.")

PROXY_URL = os.getenv("PROXY_URL", None)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MESSAGE_MAP_TTL = int(os.getenv("MESSAGE_MAP_TTL", str(10 * 24 * 60 * 60)))

# Channel mapping
_CHANNELS_FILE = os.getenv("CHANNELS_FILE", "channels.json")

try:
    with open(_CHANNELS_FILE, encoding="utf-8") as _f:
        _entries = json.load(_f)
except FileNotFoundError:
    raise RuntimeError(
        f"Channel mapping file '{_CHANNELS_FILE}' not found. "
        "Copy channels.example.json to channels.json and fill in your IDs."
    )


@dataclass(frozen=True)
class GroupConfig:
    chat_id: int
    allowed_user_ids: frozenset[int] | None
    allowed_thread_ids: frozenset[int] | None
    sign_names: bool = False


def _opt_set(value: Optional[list[str]]) -> frozenset[int] | None:
    return frozenset(int(x) for x in value) if value else None


# tg_id -> chat_id
CHANNEL_MAP: dict[int, int] = {}
# tg_id -> GroupConfig
GROUP_MAP: dict[int, GroupConfig] = {}
# chat_id -> tg_id
MAX_TO_TG: dict[int, GroupConfig] = {}

for _e in _entries:
    _entry_type = _e.get("type", "channel")
    _direction = _e.get("direction", "tg_to_max")
    _tg_id = int(_e["tg_id"])
    _max_id = int(_e["max_id"])

    _sign_names = bool(_e.get("sign_names", False))
    _bidirectional = bool(_e.get("bidirectional", False))

    if _bidirectional:
        if _entry_type != "group":
            raise RuntimeError(
                f"Entry tg_id={_tg_id} max_id={_max_id}: bidirectional only supported for type=group."
            )
        if "direction" in _e:
            logging.warning(
                "Entry tg_id=%d max_id=%d: bidirectional=true, direction=%r ignored.",
                _tg_id, _max_id, _direction,
            )
        _allowed_users = _opt_set(_e.get("allowed_user_ids"))
        GROUP_MAP[_tg_id] = GroupConfig(
            chat_id=_max_id,
            allowed_user_ids=_allowed_users,
            allowed_thread_ids=_opt_set(_e.get("allowed_thread_ids")),
            sign_names=_sign_names,
        )
        MAX_TO_TG[_max_id] = GroupConfig(
            chat_id=_tg_id,
            allowed_user_ids=_allowed_users,
            allowed_thread_ids=None,
            sign_names=_sign_names,
        )
        continue

    if _direction == "max_to_tg":
        if _e.get("allowed_thread_ids"):
            logging.warning(
                "Entry tg_id=%d chat_id=%d is max_to_tg; "
                "allowed_thread_ids are ignored in max_to_tg direction.",
                _tg_id, _max_id,
            )
        MAX_TO_TG[_max_id] = GroupConfig(
            chat_id=_tg_id,
            allowed_user_ids=_opt_set(_e.get("allowed_user_ids")),
            allowed_thread_ids=None,
            sign_names=_sign_names,
        )
        continue

    if _direction != "tg_to_max":
        raise RuntimeError(f"Unknown direction: {_direction!r}")

    if _entry_type == "channel":
        if _sign_names:
            logging.warning(
                "Entry tg_id=%d max_id=%d: sign_names ignored (type=channel, only type=group supported).",
                _tg_id, _max_id,
            )
        CHANNEL_MAP[_tg_id] = _max_id
    elif _entry_type == "group":
        GROUP_MAP[_tg_id] = GroupConfig(
            chat_id=_max_id,
            allowed_user_ids=_opt_set(_e.get("allowed_user_ids")),
            allowed_thread_ids=_opt_set(_e.get("allowed_thread_ids")),
            sign_names=_sign_names,
        )
    else:
        raise RuntimeError(f"Unknown entry type: {_entry_type!r}")

if not CHANNEL_MAP and not GROUP_MAP and not MAX_TO_TG:
    raise RuntimeError(f"'{_CHANNELS_FILE}' contains no mappings.")
