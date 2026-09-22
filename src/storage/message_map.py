from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

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

    Keys (both are Redis sets):
        mm:tg:{tg_chat_id}:{tg_msg_id}  -> {"{max_chat_id}:{max_mid}", ...}
        mm:max:{max_chat_id}:{max_mid}  -> {"{tg_chat_id}:{tg_msg_id}", ...}

    Sets, not plain values: one source message may be mirrored into several
    destination chats (one tg_id fanning out to many max_id and vice versa), so
    a single message id can hold several counterparts — at most one per
    destination chat. Lookups are therefore scoped by destination chat
    (:meth:`get_max_in` / :meth:`get_tg_in`); the ``*_all`` variants return
    every counterpart, for fanning an edit out to all of them.

    Both directions are written with the same TTL, refreshed on every bind.
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

    # ====== WRITE ======

    async def bind(self, tg: TgRef, mx: MaxRef) -> None:
        """Store both forward (tg->max) and reverse (max->tg) mappings."""
        await self.bind_many([tg], mx)

    async def bind_many(self, tg_refs: list[TgRef], mx: MaxRef) -> None:
        """Bind several TG messages (e.g. media group) to one MAX message.

        Every TG message points at the MAX one; the reverse points back at the
        first (primary) TG ref only, so a reply to the MAX message lands on the
        head of the album rather than an arbitrary item.
        """
        if not tg_refs:
            return
        pipe = self.redis.pipeline(transaction=False)
        for tg in tg_refs:
            key = self._tg_key(tg.chat_id, tg.message_id)
            await pipe.sadd(key, f"{mx.chat_id}:{mx.mid}")  # ty:ignore[invalid-argument-type]
            await pipe.expire(key, self._ttl)
        primary = tg_refs[0]
        max_key = self._max_key(mx.chat_id, mx.mid)
        await pipe.sadd(max_key, f"{primary.chat_id}:{primary.message_id}")  # ty:ignore[invalid-argument-type]
        await pipe.expire(max_key, self._ttl)
        await pipe.execute()
        logger.debug("MessageMap bind tg=%s max=%s", tg_refs, mx)

    # ====== READ ======

    @staticmethod
    def _split(raw: str) -> tuple[str, str] | None:
        head, _, tail = raw.partition(":")
        if not head or not tail:
            return None
        return head, tail

    def _parse_max(self, members: Iterable[str]) -> list[MaxRef]:
        out: list[MaxRef] = []
        for raw in members:
            parts = self._split(raw)
            if parts is None:
                logger.warning("Corrupt MessageMap member: %r", raw)
                continue
            chat_str, mid = parts
            try:
                out.append(MaxRef(chat_id=int(chat_str), mid=mid))
            except ValueError:
                logger.warning("Corrupt MessageMap member: %r", raw)
        return out

    def _parse_tg(self, members: Iterable[str]) -> list[TgRef]:
        out: list[TgRef] = []
        for raw in members:
            parts = self._split(raw)
            if parts is None:
                logger.warning("Corrupt MessageMap member: %r", raw)
                continue
            chat_str, msg_str = parts
            try:
                out.append(TgRef(chat_id=int(chat_str), message_id=int(msg_str)))
            except ValueError:
                logger.warning("Corrupt MessageMap member: %r", raw)
        return out

    async def get_max_all(self, tg_chat_id: int, tg_msg_id: int) -> list[MaxRef]:
        """Every MAX counterpart of a TG message (one per destination MAX chat)."""
        members = await self.redis.smembers(self._tg_key(tg_chat_id, tg_msg_id))  # ty:ignore[invalid-argument-type]
        return self._parse_max(members)

    async def get_max_in(self, tg_chat_id: int, tg_msg_id: int, max_chat_id: int) -> Optional[MaxRef]:
        """The MAX counterpart of a TG message inside one specific MAX chat."""
        for mx in await self.get_max_all(tg_chat_id, tg_msg_id):
            if mx.chat_id == max_chat_id:
                return mx
        return None

    async def get_tg_all(self, max_chat_id: int, max_mid: str) -> list[TgRef]:
        """Every TG counterpart of a MAX message (one per destination TG chat)."""
        members = await self.redis.smembers(self._max_key(max_chat_id, max_mid))  # ty:ignore[invalid-argument-type]
        return self._parse_tg(members)

    async def get_tg_in(self, max_chat_id: int, max_mid: str, tg_chat_id: int) -> Optional[TgRef]:
        """The TG counterpart of a MAX message inside one specific TG chat."""
        for tg in await self.get_tg_all(max_chat_id, max_mid):
            if tg.chat_id == tg_chat_id:
                return tg
        return None

    # ====== DELETE ======

    async def forget_tg(self, tg_chat_id: int, tg_msg_id: int) -> None:
        """Drop a TG message and unlink it from every MAX counterpart."""
        refs = await self.get_max_all(tg_chat_id, tg_msg_id)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._tg_key(tg_chat_id, tg_msg_id))
        for mx in refs:
            await pipe.srem(self._max_key(mx.chat_id, mx.mid), f"{tg_chat_id}:{tg_msg_id}")  # ty:ignore[invalid-argument-type]
        await pipe.execute()

    async def forget_max(self, max_chat_id: int, max_mid: str) -> None:
        """Drop a MAX message and unlink it from every TG counterpart."""
        refs = await self.get_tg_all(max_chat_id, max_mid)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._max_key(max_chat_id, max_mid))
        for tg in refs:
            await pipe.srem(self._tg_key(tg.chat_id, tg.message_id), f"{max_chat_id}:{max_mid}")  # ty:ignore[invalid-argument-type]
        await pipe.execute()
