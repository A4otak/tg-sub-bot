import hashlib
import hmac
import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from bot.config import Config, TARIFFS
from bot.database import Database
from bot.handlers.channel import send_access
from bot.keyboards import button, crypto_payment, keyboard
from bot.services.cryptobot import CryptoBotClient, CryptoPayError


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)


def stars_payload(config: Config, user_id: int, key: str, amount: int, days: int | None):
    order = f"sub:{user_id}:{key}:{amount}:{days or 0}"
    signature = hmac.new(config.bot_token.encode(), order.encode(), hashlib.sha256).hexdigest()
    return f"{order}:{signature[:32]}"


def stars_order(config: Config, payload: str, user_id: int, currency: str, amount: int):
    try:
        prefix, buyer, key, price, duration, _ = payload.split(":")
        days = int(duration)
        if prefix != "sub" or int(buyer) != user_id or currency != "XTR" or int(price) != amount:
            return None
        expected = stars_payload(config, user_id, key, amount, days)
        if hmac.compare_digest(payload.encode(), expected.encode()):
            return key, days or None
    except ValueError:
        pass
    return None


async def deliver(bot: Bot, config: Config, db: Database, user_id: int):
    try:
        await send_access(bot, config, db, user_id)
    except TelegramAPIError:
        logger.exception("Не удалось выдать ссылку пользователю %s", user_id)
        await bot.send_message(
            user_id, "Оплата зачислена. Ссылку можно запросить в разделе «Профиль».",
            reply_markup=keyboard((button("Профиль", "profile"),)),
        )


@router.callback_query(F.data.startswith("stars:"))
async def create_stars(query: CallbackQuery, bot: Bot, config: Config, db: Database):
    key = query.data.split(":", 1)[1]
    tariff = TARIFFS.get(key)
    if tariff is None:
        await query.answer("Тариф недоступен.", show_alert=True)
        return
    await query.answer()
    user = query.from_user
    await db.register(user.id, user.username, user.full_name)
    await bot.send_invoice(
        chat_id=user.id, title=f"Подписка · {tariff.title}",
        description="Доступ к закрытому каналу", currency="XTR", provider_token="",
        payload=stars_payload(config, user.id, key, tariff.stars, tariff.days),
        prices=[LabeledPrice(label=tariff.title, amount=tariff.stars)],
        start_parameter="subscribe",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, config: Config):
    if stars_order(config, query.invoice_payload, query.from_user.id,
                   query.currency, query.total_amount) is None:
        await query.answer(ok=False, error_message="Счёт недействителен. Создайте новый.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_stars(message: Message, bot: Bot, config: Config, db: Database):
    payment = message.successful_payment
    user = message.from_user
    order = stars_order(config, payment.invoice_payload, user.id,
                        payment.currency, payment.total_amount)
    if order is None:
        logger.error("Получен платёж Stars с недействительным заказом для %s", user.id)
        await message.answer("Не удалось распознать оплату. Обратитесь через /paysupport.")
        return
    key, days = order
    async with db.user_lock(user.id):
        await db.register(user.id, user.username, user.full_name)
        if await db.settle(
            user.id, key, "stars", payment.telegram_payment_charge_id, payment.total_amount,
            "XTR", days, config.referral_percent,
        ):
            await deliver(bot, config, db, user.id)


@router.callback_query(F.data.startswith("crypto:"))
async def create_crypto(query: CallbackQuery, db: Database, crypto: CryptoBotClient | None):
    key = query.data.split(":", 1)[1]
    tariff = TARIFFS.get(key)
    if not crypto or not tariff:
        await query.answer("Оплата этим способом недоступна.", show_alert=True)
        return
    await query.answer()
    user = query.from_user
    await db.register(user.id, user.username, user.full_name)
    try:
        invoice = await crypto.create_invoice(tariff.usdt, f"Подписка · {tariff.title}")
    except CryptoPayError:
        logger.exception("Ошибка создания счёта Crypto Pay")
        await query.message.answer("Не удалось создать счёт. Попробуйте позже.")
        return
    invoice_id = str(invoice["invoice_id"])
    await db.save_crypto_invoice(user.id, key, invoice_id, tariff.usdt, tariff.days)
    await query.message.answer(
        f"К оплате {tariff.usdt} USDT. После перевода нажмите «Проверить оплату».",
        reply_markup=crypto_payment(invoice["bot_invoice_url"], invoice_id),
    )


@router.callback_query(F.data == "crypto_invoices")
async def list_crypto(query: CallbackQuery, db: Database, crypto: CryptoBotClient | None):
    if not crypto:
        await query.answer("Оплата этим способом отключена.", show_alert=True)
        return
    await query.answer()
    invoices = await db.crypto_invoices(query.from_user.id)
    if not invoices:
        await query.message.answer("Счетов, ожидающих проверки, нет.")
        return
    for invoice in invoices:
        invoice_id = invoice["invoice_id"]
        await query.message.answer(
            f"Счёт CryptoBot №{invoice_id}",
            reply_markup=keyboard((button("Проверить оплату", f"check:{invoice_id}"),)),
        )


@router.callback_query(F.data.startswith("check:"))
async def check_crypto(
    query: CallbackQuery, bot: Bot, config: Config, db: Database, crypto: CryptoBotClient | None,
):
    if not crypto:
        await query.answer("Оплата этим способом отключена.", show_alert=True)
        return
    user_id = query.from_user.id
    invoice_id = query.data.split(":", 1)[1]
    stored = await db.crypto_invoice(user_id, invoice_id)
    if not stored:
        await query.answer("Счёт не найден.", show_alert=True)
        return
    await query.answer()
    if stored["status"] == "paid":
        await query.message.answer("Оплата уже зачислена. Ссылка доступна в профиле.")
        return
    try:
        invoice = await crypto.get_invoice(invoice_id)
    except CryptoPayError:
        logger.exception("Ошибка проверки счёта Crypto Pay")
        await query.message.answer("Не удалось проверить счёт. Попробуйте позже.")
        return
    if not invoice or invoice["status"] != "paid":
        await query.message.answer("Срок счёта истёк." if invoice and invoice["status"] == "expired"
                                   else "Оплата ещё не подтверждена.")
        return
    amount = Decimal(stored["amount"])
    if invoice.get("asset") != "USDT" or Decimal(invoice["amount"]) != amount:
        logger.error("Сумма или валюта счёта Crypto Pay %s не совпадает", invoice_id)
        await query.message.answer("Данные оплаты не совпадают. Обратитесь через /paysupport.")
        return
    async with db.user_lock(user_id):
        if await db.settle(
            user_id, stored["tariff_id"], "cryptobot", invoice_id, amount, "USDT",
            stored["days"], config.referral_percent,
        ):
            await deliver(bot, config, db, user_id)
        else:
            await query.message.answer("Оплата уже зачислена. Ссылка доступна в профиле.")
