from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Tariff:
    title: str
    days: int | None
    stars: int
    usdt: Decimal


TARIFFS = {
    "7": Tariff("7 дней", 7, 50, Decimal("1.50")),
    "30": Tariff("30 дней", 30, 150, Decimal("4.00")),
    "90": Tariff("90 дней", 90, 400, Decimal("10.00")),
    "life": Tariff("Навсегда", None, 1000, Decimal("25.00")),
}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: list[int]
    channel_id: int
    cryptobot_token: str
    cryptobot_testnet: bool
    referral_percent: Decimal
    db_path: str

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        admins = list(dict.fromkeys(
            int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()
        ))
        channel = int(os.getenv("CHANNEL_ID", "0"))
        testnet = os.getenv("CRYPTOBOT_TESTNET", "false").strip().lower()
        percent = Decimal(os.getenv("REFERRAL_PERCENT", "15"))
        if not token or not admins or min(admins) <= 0 or channel >= 0:
            raise ValueError("Укажите BOT_TOKEN, ADMIN_IDS и отрицательный CHANNEL_ID")
        if not percent.is_finite() or not 0 <= percent <= 100:
            raise ValueError("REFERRAL_PERCENT должен быть в диапазоне 0–100")
        if testnet not in {"true", "false"}:
            raise ValueError("CRYPTOBOT_TESTNET должен быть true или false")
        return cls(
            token, admins, channel, os.getenv("CRYPTOBOT_TOKEN", "").strip(),
            testnet == "true", percent, os.getenv("DB_PATH", "bot.db"),
        )

