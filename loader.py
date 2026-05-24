from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from maxapi import Bot as MaxBot, Dispatcher as MaxDispatcher
from aiogram import Bot as TgBot, Dispatcher as TgDispatcher
from aiogram.client.session.aiohttp import AiohttpSession
import config

# Initialize Bots
max_bot = MaxBot(token=config.MAX_BOT_TOKEN)
max_dp = MaxDispatcher()

session = None
if config.PROXY_URL:
    session = AiohttpSession(proxy=config.PROXY_URL)
tg_bot = TgBot(
    token=config.TELEGRAM_BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
tg_dp = TgDispatcher(disable_fsm=True)
