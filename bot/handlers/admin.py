import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Config
from bot.database import Database
from bot.handlers.channel import remove_access, send_access
from bot.keyboards import admin_menu
from bot.states import AdminStates


router = Router()
logger = logging.getLogger(__name__)


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return bool(event.from_user and event.from_user.id in config.admin_ids)


router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(F.message.chat.type == "private", IsAdmin())


def parse_user_id(value: str) -> int:
    user_id = int(value)
    if not 0 < user_id <= 2**63 - 1:
        raise ValueError("Некорректный user_id")
    return user_id


@router.message(Command("admin"))
async def admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Панель администратора", reply_markup=admin_menu())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=admin_menu())


@router.message(Command("refund"))
async def refund(message: Message, command: CommandObject, bot: Bot):
    try:
        user_id, charge_id = (command.args or "").split()
        user_id = parse_user_id(user_id)
    except ValueError:
        await message.answer("Формат: /refund <user_id> <telegram_payment_charge_id>")
        return
    try:
        refunded = await bot.refund_star_payment(
            user_id=user_id, telegram_payment_charge_id=charge_id,
        )
    except TelegramAPIError:
        logger.exception("Ошибка возврата Stars для %s", user_id)
        refunded = False
    await message.answer("Возврат выполнен." if refunded else "Telegram не подтвердил возврат.")


@router.callback_query(F.data == "admin:stats")
async def stats(query: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    await query.answer()
    total, active, payments = await db.stats()
    await query.message.answer(
        f"Пользователей: {total}\nАктивных подписок: {active}\n"
        f"Сумма оплат: {payments['XTR']:f} Stars, {payments['USDT']:f} USDT."
    )


@router.callback_query(F.data.in_({"admin:grant", "admin:revoke", "admin:broadcast"}))
async def start_action(query: CallbackQuery, state: FSMContext):
    actions = {
        "admin:grant": (AdminStates.grant, "Введите user_id и число дней (1–36500) или forever."),
        "admin:revoke": (AdminStates.revoke, "Введите user_id."),
        "admin:broadcast": (AdminStates.broadcast, "Отправьте сообщение для рассылки."),
    }
    next_state, prompt = actions[query.data]
    await query.answer()
    await state.set_state(next_state)
    await query.message.answer(f"{prompt}\nОтмена /cancel")


@router.message(AdminStates.grant)
async def grant(message: Message, state: FSMContext, bot: Bot, config: Config, db: Database):
    try:
        raw_id, term = (message.text or "").split()
        user_id = parse_user_id(raw_id)
        days = None if term.lower() == "forever" else int(term)
        if days is not None and not 1 <= days <= 36500:
            raise ValueError("Некорректный срок")
    except ValueError:
        await message.answer("Введите user_id и число дней (1–36500) или forever.")
        return
    async with db.user_lock(user_id):
        if not await db.extend(user_id, days):
            await message.answer("Пользователь должен сначала отправить боту /start.")
            return
        try:
            sent = await send_access(bot, config, db, user_id)
        except TelegramAPIError:
            logger.exception("Ошибка выдачи ссылки пользователю %s", user_id)
            sent = False
    await state.clear()
    await message.answer(
        "Подписка обновлена, ссылка отправлена." if sent
        else "Подписка обновлена. Пользователь может запросить ссылку в профиле."
    )


@router.message(AdminStates.revoke)
async def revoke(message: Message, state: FSMContext, bot: Bot, config: Config, db: Database):
    try:
        user_id = parse_user_id(message.text or "")
    except ValueError:
        await message.answer("Введите числовой user_id.")
        return
    async with db.user_lock(user_id):
        if not await db.user(user_id):
            await message.answer("Пользователь не найден.")
            return
        try:
            await remove_access(bot, config, db, user_id)
        except TelegramAPIError:
            logger.exception("Ошибка отзыва доступа пользователя %s", user_id)
            await message.answer("Не удалось отозвать доступ. Проверьте права бота и повторите.")
            return
        await db.clear_subscription(user_id)
    await state.clear()
    await message.answer("Подписка удалена, доступ отозван.")


@router.message(AdminStates.broadcast)
async def broadcast(message: Message, state: FSMContext, bot: Bot, db: Database):
    await state.clear()
    delivered = blocked = errors = 0
    for user_id in await db.user_ids():
        try:
            try:
                await bot.copy_message(user_id, message.chat.id, message.message_id)
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
                await bot.copy_message(user_id, message.chat.id, message.message_id)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            errors += 1
        else:
            delivered += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"Доставлено: {delivered}\nЗаблокировали бота или ЛС недоступны: {blocked}\n"
        f"Другие ошибки: {errors}"
    )
