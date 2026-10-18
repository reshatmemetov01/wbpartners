import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .config import Config, load_config
from .db import Db
from .pdf import build_pdf
from .service import deliver_all, move_to_assembly, refresh_assembling
from .wb import WBClient, WBError

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Заказы"), KeyboardButton(text="На сборке")]],
    resize_keyboard=True,
)


def ikb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]
    )


def order_line(o: dict) -> str:
    return f"#{o['id']} · {o.get('article', '—')}"


def build_router(cfg: Config, wb: WBClient, db: Db) -> Router:
    r = Router()
    info_cache: dict[str, dict] = {}
    seller_cache: dict[str, str] = {}
    r.message.filter(F.from_user.id == cfg.admin_id)
    r.callback_query.filter(F.from_user.id == cfg.admin_id)

    @r.message(CommandStart())
    async def start(m: Message):
        await m.answer("Готов к работе.", reply_markup=MENU)

    @r.message(F.text == "Заказы")
    async def orders(m: Message):
        try:
            items = await wb.new_orders()
        except WBError as e:
            return await m.answer(f"Ошибка WB: {e}")
        if not items:
            return await m.answer("Новых заказов нет.")
        text = "Новые заказы:\n" + "\n".join(order_line(o) for o in items)
        rows = [[(order_line(o)[:60], f"o:{o['id']}")] for o in items[:20]]
        rows.append([("Перенести все на сборку", "move_all")])
        await m.answer(text[:4000], reply_markup=ikb(*rows))

    @r.callback_query(F.data.startswith("o:"))
    async def order_card(cb: CallbackQuery):
        oid = int(cb.data[2:])
        o = next((x for x in await wb.new_orders() if x["id"] == oid), None)
        if not o:
            return await cb.answer("Заказ уже не в новых", show_alert=True)
        text = (
            f"Заказ #{o['id']}\nАртикул: {o.get('article', '—')}\n"
            f"nmId: {o.get('nmId', '—')}\nШтрихкод: {(o.get('skus') or ['—'])[0]}\n"
            f"Создан: {o.get('createdAt', '—')}"
        )
        await cb.message.answer(text, reply_markup=ikb([("Перенести на сборку", f"move:{oid}")]))
        await cb.answer()

    async def _move(cb: CallbackQuery, only: int | None):
        await cb.answer("Переношу…")
        items = await wb.new_orders()
        if only is not None:
            items = [o for o in items if o["id"] == only]
        ok, errors = 0, []
        for o in items:
            try:
                await move_to_assembly(wb, db, o)
                ok += 1
            except WBError as e:
                errors.append(f"#{o['id']}: {e}")
        msg = f"Перенесено на сборку: {ok}"
        if errors:
            msg += "\nОшибки:\n" + "\n".join(errors)[:3000]
        await cb.message.answer(msg)

    @r.callback_query(F.data == "move_all")
    async def move_all(cb: CallbackQuery):
        await _move(cb, None)

    @r.callback_query(F.data.startswith("move:"))
    async def move_one(cb: CallbackQuery):
        await _move(cb, int(cb.data[5:]))

    @r.message(F.text == "На сборке")
    async def assembling(m: Message):
        rows = await refresh_assembling(wb, db)
        if not rows:
            return await m.answer("На сборке ничего нет.")
        text = "На сборке:\n" + "\n".join(
            f"{x['article']} · заказ #{x['order_id']} · {x['supply_id']}" for x in rows
        )
        await m.answer(text[:4000], reply_markup=ikb(
                [("Получить стикеры", "stickers")], [("Передать все в доставку", "deliver_ask")]
            ),
        )

    @r.callback_query(F.data == "deliver_ask")
    async def deliver_ask(cb: CallbackQuery):
        rows = await refresh_assembling(wb, db)
        if not rows:
            await cb.answer()
            return await cb.message.answer("На сборке ничего нет.")
        await cb.message.answer(
            f"Передать в доставку {len(rows)} поставок? После этого изменить их будет нельзя.",
            reply_markup=ikb([(f"Да, передать {len(rows)}", "deliver_yes"), ("Отмена", "deliver_no")]),
        )
        await cb.answer()

    @r.callback_query(F.data == "deliver_no")
    async def deliver_no(cb: CallbackQuery):
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @r.callback_query(F.data == "deliver_yes")
    async def deliver_yes(cb: CallbackQuery):
        await cb.message.edit_text("Передаю в доставку…")
        await cb.answer()
        rows = await refresh_assembling(wb, db)
        shipping = None
        if cfg.shipping_point_id:
            msk = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=cfg.shipping_days)
            shipping = {
                "point_id": cfg.shipping_point_id,
                "type": cfg.shipping_type,
                "dt": msk.strftime("%Y-%m-%d"),
            }
        ok, errors = await deliver_all(wb, db, rows, shipping)
        msg = f"Передано в доставку: {ok}"
        if errors:
            msg += "\nОшибки:\n" + "\n".join(errors)[:3000]
        await cb.message.answer(msg)

    @r.callback_query(F.data == "stickers")
    async def stickers(cb: CallbackQuery):
        await cb.answer("Собираю PDF…")
        rows = await refresh_assembling(wb, db)
        if not rows:
            return await cb.message.answer("На сборке ничего нет.")
        warn = None
        await cb.message.answer(f"Собираю стикеры для {len(rows)} поставок, это может занять пару минут…")
        seller = cfg.seller_name or seller_cache.get("name", "")
        if not seller:
            try:
                seller = seller_cache["name"] = await wb.seller_name()
            except WBError:
                warn = "Не удалось получить имя продавца (лимит WB). Укажите SELLER_NAME в .env."
        try:
            data = []
            for x in rows:
                key = x["sku"] or x["article"]
                if key not in info_cache:
                    try:
                        info_cache[key] = await wb.product_info(x["sku"], x["article"])
                    except WBError as e:
                        info_cache[key] = {}
                        warn = f"Не удалось получить данные карточки (нужен токен с категорией «Контент»): {e}"
                data.append(
                    {
                        "order_png": await wb.order_sticker(x["order_id"]),
                        "sku": x["sku"],
                        "article": x["article"],
                        "info": {**info_cache[key], "seller": seller},
                        "box_png": await wb.box_sticker(x["supply_id"], x["trbx_id"]),
                    }
                )
        except WBError as e:
            return await cb.message.answer(f"Ошибка WB: {e}")
        if warn:
            await cb.message.answer(warn[:500])
        pdf = build_pdf(data)
        await cb.message.answer_document(
            BufferedInputFile(pdf, "stickers.pdf"), caption=f"Поставок: {len(rows)}, стикеров: {len(rows) * 3}"
        )

    return r


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    wb, db = WBClient(cfg.wb_token), Db(cfg.db_path)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(build_router(cfg, wb, db))
    try:
        await dp.start_polling(bot)
    finally:
        await wb.aclose()


if __name__ == "__main__":
    asyncio.run(main())
