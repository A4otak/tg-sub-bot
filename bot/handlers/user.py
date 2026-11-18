from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.config import Config, TARIFFS
from bot.database import Database, is_active
from bot.handlers.channel import send_access
from bot.keyboards import button, keyboard, menu, payment_methods, plans


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, db: Database):
    user = message.from_user
    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        value = command.args[4:]
        if value.isascii() and value.isdecimal() and len(value) <= 18:
            referrer_id = int(value)
    await db.register(user.id, user.username, user.full_name, referrer_id)
    await message.answer("Подписка на закрытый канал", reply_markup=menu())


@router.message(Command("paysupport"))
async def payment_support(message: Message, command: CommandObject, bot: Bot, config: Config):
    user = message.from_user
    details = (command.args or "").strip()
    if not details:
        await message.answer("Опишите проблему в сообщении /paysupport <описание проблемы>.")
        return
    sent = False
    for admin in config.admin_ids:
        try:
            await bot.send_message(admin, f"Запрос по оплате от {user.full_name} (ID {user.id}).\n"
                                           f"{details[:3500]}")
            sent = True
        except TelegramAPIError:
            continue
    await message.answer(
        "Запрос передан администратору." if sent else "Администратор сейчас недоступен."
    )


@router.callback_query(F.data == "menu")
async def show_menu(query: CallbackQuery):
    await query.answer()
    await query.message.answer("Подписка на закрытый канал", reply_markup=menu())


@router.callback_query(F.data == "plans")
async def show_plans(query: CallbackQuery):
    await query.answer()
    await query.message.answer("Выберите срок подписки", reply_markup=plans())


@router.callback_query(F.data.startswith("plan:"))
async def show_tariff(query: CallbackQuery, config: Config):
    key = query.data.split(":", 1)[1]
    if key not in TARIFFS:
        await query.answer("Тариф недоступен.", show_alert=True)
        return
    await query.answer()
    await query.message.answer(TARIFFS[key].title, reply_markup=payment_methods(
        key, bool(config.cryptobot_token),
    ))


@router.callback_query(F.data == "profile")
async def profile(query: CallbackQuery, db: Database, config: Config):
    user = await db.user(query.from_user.id)
    rows = [(button("В меню", "menu"),)]
    status = "Нет активной подписки."
    if is_active(user):
        status = "Подписка навсегда." if user["is_lifetime"] else (
            f"Подписка до {datetime.fromisoformat(user['sub_until']):%d.%m.%Y %H:%M} UTC."
        )
        rows.insert(0, (button("Получить ссылку в канал", "access"),))
    if config.cryptobot_token:
        rows.insert(0, (button("Мои счета CryptoBot", "crypto_invoices"),))
    await query.answer()
    await query.message.answer(status, reply_markup=keyboard(*rows))


@router.callback_query(F.data == "access")
async def access(query: CallbackQuery, bot: Bot, config: Config, db: Database):
    await query.answer()
    user_id = query.from_user.id
    async with db.user_lock(user_id):
        try:
            if not await send_access(bot, config, db, user_id):
                await query.message.answer("Нет активной подписки.")
        except TelegramAPIError:
            await query.message.answer("Не удалось выдать ссылку. Попробуйте позже.")


@router.callback_query(F.data == "referrals")
async def referrals(query: CallbackQuery, bot: Bot, db: Database):
    user_id = query.from_user.id
    user = await db.user(user_id)
    if not user:
        await query.answer("Сначала отправьте /start.", show_alert=True)
        return
    await query.answer()
    username = (await bot.me()).username
    count = await db.referral_count(user_id)
    await query.message.answer(
        f"Ваша ссылка: https://t.me/{username}?start=ref_{user_id}\n"
        f"Приглашено: {count}\n"
        f"Баланс: {user['ref_balance']:.6g} XTR и {user['ref_balance_usdt']:.6g} USDT.",
        reply_markup=keyboard(
            (button("Подать заявку на вывод", "withdraw"),), (button("В меню", "menu"),),
        ),
    )


@router.callback_query(F.data == "withdraw")
async def withdraw(query: CallbackQuery, bot: Bot, config: Config, db: Database):
    user = await db.user(query.from_user.id)
    if not user or max(user["ref_balance"], user["ref_balance_usdt"]) <= 0:
        await query.answer("Баланс пуст.", show_alert=True)
        return
    await query.answer()
    label = f"@{user['username']}" if user["username"] else "без username"
    message = (f"Заявка на вывод от {user['full_name']} ({label}, ID {user['user_id']}).\n"
               f"Баланс: {user['ref_balance']:.6g} XTR, {user['ref_balance_usdt']:.6g} USDT.")
    sent = False
    for admin in config.admin_ids:
        try:
            await bot.send_message(admin, message)
            sent = True
        except TelegramAPIError:
            continue
    await query.message.answer(
        "Заявка отправлена." if sent else "Не удалось отправить заявку. Попробуйте позже."
    )
