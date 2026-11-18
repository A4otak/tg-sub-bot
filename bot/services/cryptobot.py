from decimal import Decimal

import aiohttp


class CryptoPayError(RuntimeError):
    pass


class CryptoBotClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, testnet: bool):
        self.session = session
        self.token = token
        self.base_url = (
            "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"
        )

    async def _request(self, method: str, **params):
        try:
            async with self.session.post(
                f"{self.base_url}/{method}",
                json=params,
                headers={"Crypto-Pay-API-Token": self.token},
            ) as response:
                response.raise_for_status()
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise CryptoPayError("Сбой запроса к Crypto Pay") from error
        if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
            raise CryptoPayError("Crypto Pay отклонил запрос")
        return data["result"]

    async def create_invoice(self, amount: Decimal, description: str):
        return await self._request(
            "createInvoice", currency_type="crypto", asset="USDT", amount=str(amount),
            description=description, expires_in=3600, allow_comments=False, allow_anonymous=False,
        )

    async def get_invoice(self, invoice_id: str):
        result = await self._request("getInvoices", invoice_ids=invoice_id)
        return next((item for item in result["items"]
                     if str(item["invoice_id"]) == invoice_id), None)
