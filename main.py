import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation

from bot.config import Config
from bot.database import Database
from bot.handlers import admin, channel, payments, user
from bot.services.cryptobot import CryptoBotClient
from bot.services.scheduler import run_scheduler


async def main():
    config = Config.load()
    db = Database(config.db_path)
    await db.init()

    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_routers(payments.router, user.router, admin.router, channel.router)

    async with Bot(config.bot_token) as bot, aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
    ) as session:
        crypto = None
        if config.cryptobot_token:
            crypto = CryptoBotClient(session, config.cryptobot_token, config.cryptobot_testnet)
        worker = asyncio.create_task(run_scheduler(bot, config, db))
        try:
            await dispatcher.start_polling(
                bot, config=config, db=db, crypto=crypto,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
