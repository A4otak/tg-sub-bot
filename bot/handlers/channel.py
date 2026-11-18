from datetime import datetime, timedelta
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from bot.config import Config
from bot.database import Database, is_active, now
from bot.keyboards import renew


router = Router()
logger = logging.getLogger(__name__)


async def send_access(bot: Bot, config: Config, db: Database, user_id: int):
    user = await db.user(user_id)
    if not is_active(user):
        return False
    expires = now() + timedelta(minutes=15)
    if not user["is_lifetime"]:
        expires = min(expires, datetime.fromisoformat(user["sub_until"]))
    invite = await bot.create_chat_invite_link(
        chat_id=config.channel_id, member_limit=1, expire_date=expires,
    )
    await db.save_invite(user_id, invite.invite_link, expires.isoformat())
    await bot.send_message(user_id, f"Ссылка для входа в канал:\n{invite.invite_link}")
    return True


async def remove_access(bot: Bot, config: Config, db: Database, user_id: int):
    for invite in await db.invites(user_id):
        await bot.revoke_chat_invite_link(config.channel_id, invite)
        await db.delete_invite(invite)
    await bot.ban_chat_member(config.channel_id, user_id)
    await bot.unban_chat_member(config.channel_id, user_id, only_if_banned=True)


@router.chat_member()
async def consume_invite(update: ChatMemberUpdated, config: Config, db: Database, bot: Bot):
    if update.chat.id != config.channel_id or not update.invite_link:
        return
    if update.new_chat_member.status != "member" or update.old_chat_member.status == "member":
        return
    link = update.invite_link.invite_link
    owner = await db.invite_owner(link)
    if owner is None:
        return
    try:
        async with db.user_lock(owner):
            if await db.invite_owner(link) is not None:
                await bot.revoke_chat_invite_link(config.channel_id, link)
                await db.delete_invite(link)
        member = update.new_chat_member.user.id
        async with db.user_lock(member):
            if not is_active(await db.user(member)):
                await remove_access(bot, config, db, member)
    except TelegramAPIError:
        logger.exception("Ошибка обработки входа по ссылке пользователя %s", owner)


@router.chat_join_request()
async def join_request(request: ChatJoinRequest, config: Config, db: Database, bot: Bot):
    if request.chat.id != config.channel_id:
        return
    user_id = request.from_user.id
    try:
        async with db.user_lock(user_id):
            if is_active(await db.user(user_id)):
                await request.approve()
                return
            await request.decline()
    except TelegramAPIError:
        logger.exception("Ошибка обработки заявки пользователя %s", user_id)
        return
    try:
        await bot.send_message(request.user_chat_id, "Для вступления оформите подписку.",
                               reply_markup=renew())
    except TelegramAPIError:
        logger.info("Не удалось уведомить о заявке пользователя %s", user_id)
