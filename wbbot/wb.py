"""Клиент WB Marketplace API (FBS).

Пути методов вынесены в константы: сверьте их с актуальной документацией
https://dev.wildberries.ru/docs/openapi/orders-fbs
"""
import asyncio
import base64

import httpx

BASE_URL = "https://marketplace-api.wildberries.ru"

NEW_ORDERS = "/api/v3/orders/new"
SUPPLIES = "/api/v3/supplies"
SUPPLY_ADD_ORDERS = "/api/marketplace/v3/supplies/{supply_id}/orders"
SUPPLY_TRBX = "/api/v3/supplies/{supply_id}/trbx"
SUPPLY_TRBX_STICKERS = "/api/v3/supplies/{supply_id}/trbx/stickers"
ORDER_STICKERS = "/api/v3/orders/stickers"
CARDS_LIST = "https://content-api.wildberries.ru/content/v2/get/cards/list"
SELLER_INFO = "https://common-api.wildberries.ru/api/v1/seller-info"

STICKER_W, STICKER_H = 58, 40
MAX_RETRIES = 5


class WBError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class WBClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._http = client or httpx.AsyncClient(
            base_url=BASE_URL, headers={"Authorization": token}, timeout=30
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(MAX_RETRIES):
            r = await self._http.request(method, url, **kw)
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("X-Ratelimit-Retry", 1)) + attempt)
                continue
            if r.status_code >= 400:
                raise WBError(f"{method} {url} -> {r.status_code}: {r.text[:300]}", r.status_code)
            return r
        raise WBError(f"{method} {url}: превышен лимит запросов")

    async def new_orders(self) -> list[dict]:
        r = await self._request("GET", NEW_ORDERS)
        return r.json().get("orders", [])

    async def create_supply(self, name: str) -> str:
        r = await self._request("POST", SUPPLIES, json={"name": name})
        return r.json()["id"]

    async def add_order(self, supply_id: str, order_id: int) -> None:
        await self._request(
            "PATCH",
            SUPPLY_ADD_ORDERS.format(supply_id=supply_id),
            json={"orders": [order_id]},
        )

    async def create_box(self, supply_id: str) -> str:
        r = await self._request(
            "POST", SUPPLY_TRBX.format(supply_id=supply_id), json={"amount": 1}
        )
        return r.json()["trbxIds"][0]

    async def order_sticker(self, order_id: int) -> bytes:
        r = await self._request(
            "POST",
            ORDER_STICKERS,
            params={"type": "png", "width": STICKER_W, "height": STICKER_H},
            json={"orders": [order_id]},
        )
        return base64.b64decode(r.json()["stickers"][0]["file"])

    async def box_sticker(self, supply_id: str, trbx_id: str) -> bytes:
        r = await self._request(
            "POST",
            SUPPLY_TRBX_STICKERS.format(supply_id=supply_id),
            params={"type": "png"},
            json={"trbxIds": [trbx_id]},
        )
        return base64.b64decode(r.json()["stickers"][0]["file"])

    async def seller_name(self) -> str:
        r = await self._request("GET", SELLER_INFO)
        return r.json().get("name", "")

    async def product_info(self, sku: str, article: str) -> dict:
        """Данные для стикера штрихкода из карточки товара (нужен токен с категорией «Контент»)."""
        body = {
            "settings": {
                "sort": {"ascending": False},
                "cursor": {"limit": 20},
                "filter": {"textSearch": sku or article, "withPhoto": -1},
            }
        }
        r = await self._request("POST", CARDS_LIST, json=body)
        cards = r.json().get("cards", [])
        card = next(
            (c for c in cards if any(sku in (z.get("skus") or []) for z in c.get("sizes", []))),
            next((c for c in cards if c.get("vendorCode") == article), None),
        )
        if not card:
            return {}
        size = next(
            (z.get("techSize", "") for z in card.get("sizes", []) if sku in (z.get("skus") or [])), ""
        )
        color = next(
            (", ".join(map(str, ch.get("value") or [])) for ch in card.get("characteristics", [])
             if ch.get("name") == "Цвет"),
            "",
        )
        return {
            "title": card.get("title", ""),
            "brand": card.get("brand", ""),
            "color": color,
            "size": size,
        }
