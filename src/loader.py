from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from maxapi import Bot as MaxBot, Dispatcher as MaxDispatcher
from aiogram import Bot as TgBot, Dispatcher as TgDispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from src.config import MAX_BOT_TOKEN, PROXY_URL, TELEGRAM_BOT_TOKEN

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
