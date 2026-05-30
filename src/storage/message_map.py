from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from redis.asyncio import Redis, from_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TgRef:
    chat_id: int
    message_id: int


@dataclass(frozen=True)
class MaxRef:
    chat_id: int
    mid: str


class MessageMapService:
    """
    Async Redis-backed mapping between Telegram and MAX message identifiers.

    Keys:
        mm:tg:{tg_chat_id}:{tg_msg_id}  -> "{max_chat_id}:{max_mid}"
        mm:max:{max_chat_id}:{max_mid}  -> "{tg_chat_id}:{tg_msg_id}"

    Both directions written atomically with the same TTL so lookups stay
    symmetric. A single MAX message may be referenced by several TG message
    ids (media groups) — call :meth:`bind` once per TG message and the most
    recent TG ref wins for the reverse lookup.
    """

    _TG_PREFIX = "mm:tg"
    _MAX_PREFIX = "mm:max"

    def __init__(self, url: str, ttl_seconds: int) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._redis: Optional[Redis] = None

    async def connect(self) -> None:
        if self._redis is not None:
            return
        self._redis = from_url(self._url, decode_responses=True)
        await self._redis.ping()
        logger.info("MessageMap connected to Redis (%s), TTL=%ds", self._url, self._ttl)

    async def close(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.aclose()
        finally:
            self._redis = None

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("MessageMapService is not connected. Call connect() first.")
        return self._redis

    @classmethod
    def _tg_key(cls, chat_id: int, msg_id: int) -> str:
        return f"{cls._TG_PREFIX}:{chat_id}:{msg_id}"

    @classmethod
    def _max_key(cls, chat_id: int, mid: str) -> str:
        return f"{cls._MAX_PREFIX}:{chat_id}:{mid}"

    async def bind(self, tg: TgRef, mx: MaxRef) -> None:
        """Store both forward (tg→max) and reverse (max→tg) mappings."""
        pipe = self.redis.pipeline(transaction=False)
        await pipe.set(self._tg_key(tg.chat_id, tg.message_id), f"{mx.chat_id}:{mx.mid}", ex=self._ttl)
        await pipe.set(self._max_key(mx.chat_id, mx.mid), f"{tg.chat_id}:{tg.message_id}", ex=self._ttl)
        await pipe.execute()
        logger.debug("MessageMap bind tg=%s max=%s", tg, mx)

    async def bind_many(self, tg_refs: list[TgRef], mx: MaxRef) -> None:
        """Bind several TG messages (e.g. media group) to one MAX message."""
        if not tg_refs:
            return
        pipe = self.redis.pipeline(transaction=False)
        for tg in tg_refs:
            await pipe.set(self._tg_key(tg.chat_id, tg.message_id), f"{mx.chat_id}:{mx.mid}", ex=self._ttl)
        # Reverse points to the first (primary) TG ref; replies in TG can use any of them.
        primary = tg_refs[0]
        await pipe.set(
            self._max_key(mx.chat_id, mx.mid),
            f"{primary.chat_id}:{primary.message_id}",
            ex=self._ttl,
        )
        await pipe.execute()
        logger.debug("MessageMap bind_many tg=%s max=%s", tg_refs, mx)

    async def get_max(self, tg_chat_id: int, tg_msg_id: int) -> Optional[MaxRef]:
        raw = await self.redis.get(self._tg_key(tg_chat_id, tg_msg_id))
        if not raw:
            return None
        chat_str, _, mid = raw.partition(":")  # ty:ignore[invalid-argument-type]
        if not mid:
            return None
        try:
            return MaxRef(chat_id=int(chat_str), mid=str(mid))
        except ValueError:
            logger.warning("Corrupt MessageMap value for tg=(%s,%s): %r", tg_chat_id, tg_msg_id, raw)
            return None

    async def get_tg(self, max_chat_id: int, max_mid: str) -> Optional[TgRef]:
        raw = await self.redis.get(self._max_key(max_chat_id, max_mid))
        if not raw:
            return None
        chat_str, _, msg_str = raw.partition(":")  # ty:ignore[invalid-argument-type]
        if not msg_str:
            return None
        try:
            return TgRef(chat_id=int(chat_str), message_id=int(msg_str))
        except ValueError:
            logger.warning("Corrupt MessageMap value for max=(%s,%s): %r", max_chat_id, max_mid, raw)
            return None

    async def forget_tg(self, tg_chat_id: int, tg_msg_id: int) -> None:
        mx = await self.get_max(tg_chat_id, tg_msg_id)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._tg_key(tg_chat_id, tg_msg_id))
        if mx is not None:
            await pipe.delete(self._max_key(mx.chat_id, mx.mid))
        await pipe.execute()

    async def forget_max(self, max_chat_id: int, max_mid: str) -> None:
        tg = await self.get_tg(max_chat_id, max_mid)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._max_key(max_chat_id, max_mid))
        if tg is not None:
            await pipe.delete(self._tg_key(tg.chat_id, tg.message_id))
        await pipe.execute()
