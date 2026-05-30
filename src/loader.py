from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from maxapi import Bot as MaxBot, Dispatcher as MaxDispatcher
from aiogram import Bot as TgBot, Dispatcher as TgDispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from src.config import MAX_BOT_TOKEN, MESSAGE_MAP_TTL, PROXY_URL, REDIS_URL, TELEGRAM_BOT_TOKEN
from src.storage.message_map import MessageMapService

# Initialize Bots
max_bot = MaxBot(token=MAX_BOT_TOKEN)
max_dp = MaxDispatcher()

session = None
if PROXY_URL:
    session = AiohttpSession(proxy=PROXY_URL)
tg_bot = TgBot(
    token=TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
tg_dp = TgDispatcher(disable_fsm=True)

message_map = MessageMapService(url=REDIS_URL, ttl_seconds=MESSAGE_MAP_TTL)
