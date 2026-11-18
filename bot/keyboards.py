from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import TARIFFS


def button(label, action):
    return InlineKeyboardButton(text=label, callback_data=action)


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def menu():
    return keyboard(
        (button("Тарифы", "plans"), button("Профиль", "profile")),
        (button("Реферальная программа", "referrals"),),
    )


def renew():
    return keyboard((button("Оформить или продлить подписку", "plans"),))


def plans():
    return keyboard(
        *((button(tariff.title, f"plan:{key}"),) for key, tariff in TARIFFS.items()),
        (button("В меню", "menu"),),
    )


def payment_methods(key, crypto_enabled):
    tariff = TARIFFS[key]
    rows = [(button(f"Оплатить {tariff.stars} Stars", f"stars:{key}"),)]
    if crypto_enabled:
        rows.append((button(f"Оплатить {tariff.usdt} USDT", f"crypto:{key}"),))
    return keyboard(*rows, (button("К тарифам", "plans"),))


def crypto_payment(url, invoice_id):
    return keyboard(
        (InlineKeyboardButton(text="Открыть счёт CryptoBot", url=url),),
        (button("Проверить оплату", f"check:{invoice_id}"),),
    )


def admin_menu():
    return keyboard(
        (button("Статистика", "admin:stats"),),
        (button("Выдать / продлить", "admin:grant"), button("Забрать", "admin:revoke")),
        (button("Рассылка", "admin:broadcast"),),
    )
