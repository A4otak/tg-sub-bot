import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import Config
from bot.database import Database, is_active, now
from bot.handlers.channel import remove_access
from bot.keyboards import renew


logger = logging.getLogger(__name__)


async def check_subscriptions(bot: Bot, config: Config, db: Database):
    await db.prune_invites()
    for candidate in await db.due():
        user_id = candidate["user_id"]
        async with db.user_lock(user_id):
            user = await db.user(user_id)
            if not user or user["is_lifetime"] or not user["sub_until"]:
                continue
            if is_active(user):
                if user["reminded_24h"] or user["sub_until"] > (
                    now() + timedelta(hours=24)
                ).isoformat():
                    continue
                try:
                    await bot.send_message(user_id, "До конца подписки осталось меньше суток.",
                                           reply_markup=renew())
                except TelegramAPIError:
                    logger.warning("Не удалось отправить напоминание пользователю %s", user_id)
                await db.mark_reminded(user_id)
                continue
            try:
                await remove_access(bot, config, db, user_id)
            except TelegramAPIError:
                logger.exception("Не удалось исключить пользователя %s", user_id)
                continue
            await db.clear_subscription(user_id)
            try:
                await bot.send_message(user_id, "Подписка истекла.", reply_markup=renew())
            except TelegramAPIError:
                logger.warning("Не удалось уведомить пользователя %s об истечении", user_id)


async def run_scheduler(bot: Bot, config: Config, db: Database):
    while True:
        try:
            await check_subscriptions(bot, config, db)
        except Exception:
            logger.exception("Ошибка фоновой проверки подписок")
        await asyncio.sleep(60)
