import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite


LIFETIME = "2099-12-31T23:59:59+00:00"


def now() -> datetime:
    return datetime.now(timezone.utc)


def is_active(user: aiosqlite.Row | None) -> bool:
    return bool(user and (user["is_lifetime"] or (
        user["sub_until"] and user["sub_until"] > now().isoformat()
    )))


class Database:
    def __init__(self, path: str):
        self.path = path
        self.locks = [asyncio.Lock() for _ in range(256)]

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self.locks[user_id % len(self.locks)]

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.path, timeout=10) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    async def init(self):
        async with self.connect() as conn:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    referrer_id INTEGER REFERENCES users(user_id),
                    ref_balance REAL NOT NULL DEFAULT 0,
                    ref_balance_usdt REAL NOT NULL DEFAULT 0,
                    sub_until TEXT,
                    is_lifetime INTEGER NOT NULL DEFAULT 0,
                    reminded_24h INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    tariff_id TEXT NOT NULL,
                    provider TEXT NOT NULL CHECK (provider IN ('stars', 'cryptobot')),
                    invoice_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'paid')),
                    created_at TEXT NOT NULL,
                    days INTEGER,
                    UNIQUE (provider, invoice_id)
                );
                CREATE INDEX IF NOT EXISTS users_referrer ON users(referrer_id);
                CREATE INDEX IF NOT EXISTS users_expiry ON users(is_lifetime, sub_until);
                CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id, status);
                CREATE TABLE IF NOT EXISTS invites (
                    link TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS invites_user ON invites(user_id);
            """)

    async def execute(self, sql, params=()):
        async with self.connect() as conn:
            await conn.execute(sql, params)
            await conn.commit()

    async def all(self, sql, params=()):
        async with self.connect() as conn:
            async with conn.execute(sql, params) as cursor:
                return await cursor.fetchall()

    async def register(self, user_id, username, full_name, referrer_id=None):
        await self.execute("""
            INSERT INTO users (user_id, username, full_name, referrer_id, created_at)
            VALUES (?, ?, ?, (SELECT user_id FROM users WHERE user_id = ?), ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username, full_name = excluded.full_name
        """, (user_id, username, full_name, referrer_id if referrer_id != user_id else None,
              now().isoformat()))

    async def user(self, user_id):
        rows = await self.all("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return rows[0] if rows else None

    async def referral_count(self, user_id):
        return (await self.all("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)))[0][0]

    @staticmethod
    async def _extend(conn, user_id, days):
        async with conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
        if user is None:
            return False
        if user["is_lifetime"]:
            return True
        start = now()
        if user["sub_until"]:
            start = max(start, datetime.fromisoformat(user["sub_until"]))
        until = LIFETIME if days is None else (start + timedelta(days=days)).isoformat()
        await conn.execute("""
            UPDATE users SET sub_until = ?, is_lifetime = ?, reminded_24h = 0 WHERE user_id = ?
        """, (until, int(days is None), user_id))
        return True

    async def extend(self, user_id, days):
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            changed = await self._extend(conn, user_id, days)
            await conn.commit()
            return changed

    async def save_crypto_invoice(self, user_id, tariff_id, invoice_id, amount, days):
        await self.execute("""
            INSERT INTO payments (user_id, tariff_id, provider, invoice_id, amount,
                                  currency, status, created_at, days)
            VALUES (?, ?, 'cryptobot', ?, ?, 'USDT', 'pending', ?, ?)
        """, (user_id, tariff_id, invoice_id, str(amount), now().isoformat(), days))

    async def crypto_invoice(self, user_id, invoice_id):
        rows = await self.all("""
            SELECT * FROM payments WHERE user_id = ? AND provider = 'cryptobot' AND invoice_id = ?
        """, (user_id, invoice_id))
        return rows[0] if rows else None

    async def crypto_invoices(self, user_id):
        return await self.all("""
            SELECT invoice_id FROM payments
            WHERE user_id = ? AND provider = 'cryptobot' AND status = 'pending'
            ORDER BY id DESC LIMIT 10
        """, (user_id,))

    async def save_invite(self, user_id, link, expires_at):
        await self.execute(
            "INSERT INTO invites (link, user_id, expires_at) VALUES (?, ?, ?)",
            (link, user_id, expires_at),
        )

    async def invites(self, user_id):
        rows = await self.all("SELECT link FROM invites WHERE user_id = ? AND expires_at > ?",
                              (user_id, now().isoformat()))
        return [row[0] for row in rows]

    async def delete_invite(self, link):
        await self.execute("DELETE FROM invites WHERE link = ?", (link,))

    async def invite_owner(self, link):
        rows = await self.all("SELECT user_id FROM invites WHERE link = ?", (link,))
        return rows[0][0] if rows else None

    async def prune_invites(self):
        await self.execute("DELETE FROM invites WHERE expires_at <= ?", (now().isoformat(),))

    async def settle(self, user_id, tariff_id, provider, invoice_id, amount, currency, days, percent):
        async with self.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            if provider == "stars":
                cursor = await conn.execute("""
                    INSERT INTO payments (user_id, tariff_id, provider, invoice_id, amount,
                                          currency, status, created_at, days)
                    VALUES (?, ?, 'stars', ?, ?, 'XTR', 'paid', ?, ?)
                    ON CONFLICT(provider, invoice_id) DO NOTHING
                """, (user_id, tariff_id, invoice_id, str(amount), now().isoformat(), days))
            else:
                cursor = await conn.execute("""
                    UPDATE payments SET status = 'paid'
                    WHERE provider = 'cryptobot' AND invoice_id = ? AND user_id = ?
                      AND status = 'pending'
                """, (invoice_id, user_id))
            if not cursor.rowcount:
                return False
            await self._extend(conn, user_id, days)
            bonus = Decimal(amount) * percent / 100
            await conn.execute("""
                UPDATE users
                SET ref_balance = ref_balance + ?, ref_balance_usdt = ref_balance_usdt + ?
                WHERE user_id = (SELECT referrer_id FROM users WHERE user_id = ?)
            """, (float(bonus) if currency == "XTR" else 0,
                  float(bonus) if currency == "USDT" else 0, user_id))
            await conn.commit()
            return True

    async def due(self):
        return await self.all("""
            SELECT * FROM users WHERE is_lifetime = 0 AND (
                sub_until <= ? OR (sub_until <= ? AND reminded_24h = 0)
            ) ORDER BY sub_until
        """, (now().isoformat(), (now() + timedelta(hours=24)).isoformat()))

    async def mark_reminded(self, user_id):
        await self.execute("UPDATE users SET reminded_24h = 1 WHERE user_id = ?", (user_id,))

    async def clear_subscription(self, user_id):
        await self.execute("""
            UPDATE users SET sub_until = NULL, is_lifetime = 0, reminded_24h = 0 WHERE user_id = ?
        """, (user_id,))

    async def stats(self):
        counts = (await self.all("""
            SELECT COUNT(*), COUNT(CASE WHEN is_lifetime = 1 OR sub_until > ? THEN 1 END)
            FROM users
        """, (now().isoformat(),)))[0]
        totals = {"XTR": Decimal(0), "USDT": Decimal(0)}
        for row in await self.all("SELECT currency, amount FROM payments WHERE status = 'paid'"):
            totals[row["currency"]] += Decimal(row["amount"])
        return counts[0], counts[1], totals

    async def user_ids(self):
        return [row[0] for row in await self.all("SELECT user_id FROM users ORDER BY user_id")]

