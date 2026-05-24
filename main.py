import asyncio
import logging

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from maxapi.webhook.aiohttp import AiohttpMaxWebhook

import config
from loader import max_dp, max_bot, tg_dp, tg_bot
import handlers.max_handlers  # noqa: F401  — registers MAX handlers on import
import handlers.tg_handlers  # noqa: F401  — registers TG handlers on import

logging.basicConfig(level=logging.INFO)


async def main():
    logging.info("Starting bots...")

    max_bot.me = await max_bot.get_me()

    await tg_bot.set_webhook(
        url=config.TELEGRAM_WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=tg_dp.resolve_used_update_types(),
        secret_token=config.WEBHOOK_SECRET,
    )
    logging.info("Telegram webhook set to %s", config.TELEGRAM_WEBHOOK_URL)

    await max_bot.subscribe_webhook(url=config.MAX_WEBHOOK_URL, secret=config.WEBHOOK_SECRET)
    logging.info("MAX webhook set to %s", config.MAX_WEBHOOK_URL)

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=tg_dp, bot=tg_bot, secret_token=config.WEBHOOK_SECRET
    ).register(app, path=config.TELEGRAM_WEBHOOK_PATH)
    AiohttpMaxWebhook(dp=max_dp, bot=max_bot, secret=config.WEBHOOK_SECRET).setup(
        app, path=config.MAX_WEBHOOK_PATH
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    logging.info(
        "Webhook server listening on %s:%d (paths: %s; %s)",
        config.WEBHOOK_HOST,
        config.WEBHOOK_PORT,
        config.TELEGRAM_WEBHOOK_PATH,
        config.MAX_WEBHOOK_PATH
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await tg_bot.delete_webhook()
        await max_bot.delete_webhook()
        logging.info("Shutdown complete")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Stopped by user")
