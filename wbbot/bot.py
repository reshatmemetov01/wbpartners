import asyncio
import logging
from dataclasses import dataclass
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

from .config import AccountConfig, Config, load_config
from .db import Db
from .pdf import build_pdf
from .service import deliver_all, move_to_assembly, refresh_assembling
from .wb import WBClient, WBError

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Заказы"), KeyboardButton(text="На сборке")]],
    resize_keyboard=True,
)
MAX_ORDER_BUTTONS = 15  # на аккаунт: у Telegram ограничено число кнопок в сообщении


@dataclass
class Account:
    cfg: AccountConfig
    wb: WBClient

    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def name(self) -> str:
        return self.cfg.name


def ikb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]
    )


def order_line(o: dict) -> str:
    return f"#{o['id']} · {o.get('article', '—')}"


def build_router(cfg: Config, accounts: dict[str, Account], db: Db) -> Router:
    r = Router()
    multi = len(accounts) > 1
    info_cache: dict[tuple[str, str], dict] = {}
    seller_cache: dict[str, str] = {}
    r.message.filter(F.from_user.id == cfg.admin_id)
    r.callback_query.filter(F.from_user.id == cfg.admin_id)

    def heading(acc: Account) -> str:
        return f"▸ {acc.name}\n" if multi else ""

    def pick(acc_id: str) -> list[Account]:
        """'*' — все аккаунты, иначе один."""
        return list(accounts.values()) if acc_id == "*" else [accounts[acc_id]]

    @r.message(CommandStart())
    async def start(m: Message):
        await m.answer("Готов к работе.", reply_markup=MENU)

    @r.message(F.text == "Заказы")
    async def orders(m: Message):
        parts, rows, errors, total = [], [], [], 0
        for acc in accounts.values():
            try:
                items = await acc.wb.new_orders()
            except WBError as e:
                errors.append(f"{acc.name}: {e}")
                continue
            if not items:
                continue
            total += len(items)
            parts.append(heading(acc) + "\n".join(order_line(o) for o in items))
            tag = f"{acc.name}: " if multi else ""
            rows += [[(f"{tag}{order_line(o)}"[:60], f"o:{acc.id}:{o['id']}")] for o in items[:MAX_ORDER_BUTTONS]]
            label = f"Перенести все — {acc.name}" if multi else "Перенести все на сборку"
            rows.append([(label, f"move_all:{acc.id}")])
        if multi and total:
            rows.append([("Перенести все (все аккаунты)", "move_all:*")])
        if errors:
            parts.append("Ошибки WB:\n" + "\n".join(errors))
        if not total and not errors:
            return await m.answer("Новых заказов нет.")
        await m.answer(("Новые заказы:\n" if total else "") + "\n\n".join(parts)[:3900], reply_markup=ikb(*rows) if rows else None)

    @r.callback_query(F.data.startswith("o:"))
    async def order_card(cb: CallbackQuery):
        _, acc_id, oid = cb.data.split(":")
        acc, oid = accounts[acc_id], int(oid)
        o = next((x for x in await acc.wb.new_orders() if x["id"] == oid), None)
        if not o:
            return await cb.answer("Заказ уже не в новых", show_alert=True)
        text = (
            f"{heading(acc)}Заказ #{o['id']}\nАртикул: {o.get('article', '—')}\n"
            f"nmId: {o.get('nmId', '—')}\nШтрихкод: {(o.get('skus') or ['—'])[0]}\n"
            f"Создан: {o.get('createdAt', '—')}"
        )
        await cb.message.answer(text, reply_markup=ikb([("Перенести на сборку", f"move:{acc_id}:{oid}")]))
        await cb.answer()

    async def _move(cb: CallbackQuery, acc_id: str, only: int | None):
        await cb.answer("Переношу…")
        for acc in pick(acc_id):
            try:
                items = await acc.wb.new_orders()
            except WBError as e:
                await cb.message.answer(f"{heading(acc)}Ошибка WB: {e}")
                continue
            if only is not None:
                items = [o for o in items if o["id"] == only]
            ok, errors = 0, []
            for o in items:
                try:
                    await move_to_assembly(acc.wb, db, o, acc.id)
                    ok += 1
                except WBError as e:
                    errors.append(f"#{o['id']}: {e}")
            msg = f"{heading(acc)}Перенесено на сборку: {ok}"
            if errors:
                msg += "\nОшибки:\n" + "\n".join(errors)[:3000]
            await cb.message.answer(msg)

    @r.callback_query(F.data.startswith("move_all:"))
    async def move_all(cb: CallbackQuery):
        await _move(cb, cb.data.split(":")[1], None)

    @r.callback_query(F.data.startswith("move:"))
    async def move_one(cb: CallbackQuery):
        _, acc_id, oid = cb.data.split(":")
        await _move(cb, acc_id, int(oid))

    @r.message(F.text == "На сборке")
    async def assembling(m: Message):
        parts, buttons = [], []
        for acc in accounts.values():
            rows = await refresh_assembling(acc.wb, db, acc.id)
            if not rows:
                continue
            parts.append(
                heading(acc) + "\n".join(f"{x['article']} · заказ #{x['order_id']} · {x['supply_id']}" for x in rows)
            )
            buttons.append([(f"Получить стикеры — {acc.name}" if multi else "Получить стикеры", f"stickers:{acc.id}")])
            buttons.append(
                [(f"Передать в доставку — {acc.name}" if multi else "Передать все в доставку", f"deliver_ask:{acc.id}")]
            )
        if not parts:
            return await m.answer("На сборке ничего нет.")
        await m.answer(("На сборке:\n" + "\n\n".join(parts))[:4000], reply_markup=ikb(*buttons))

    @r.callback_query(F.data.startswith("deliver_ask:"))
    async def deliver_ask(cb: CallbackQuery):
        acc = accounts[cb.data.split(":")[1]]
        rows = await refresh_assembling(acc.wb, db, acc.id)
        await cb.answer()
        if not rows:
            return await cb.message.answer(f"{heading(acc)}На сборке ничего нет.")
        await cb.message.answer(
            f"{heading(acc)}Передать в доставку {len(rows)} поставок? После этого изменить их будет нельзя.",
            reply_markup=ikb([(f"Да, передать {len(rows)}", f"deliver_yes:{acc.id}"), ("Отмена", "deliver_no")]),
        )

    @r.callback_query(F.data == "deliver_no")
    async def deliver_no(cb: CallbackQuery):
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @r.callback_query(F.data.startswith("deliver_yes:"))
    async def deliver_yes(cb: CallbackQuery):
        acc = accounts[cb.data.split(":")[1]]
        await cb.message.edit_text(f"{heading(acc)}Передаю в доставку…")
        await cb.answer()
        rows = await refresh_assembling(acc.wb, db, acc.id)
        shipping = None
        if acc.cfg.shipping_point_id:
            msk = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=acc.cfg.shipping_days)
            shipping = {
                "point_id": acc.cfg.shipping_point_id,
                "type": acc.cfg.shipping_type,
                "dt": msk.strftime("%Y-%m-%d"),
            }
        ok, errors = await deliver_all(acc.wb, db, rows, shipping)
        msg = f"{heading(acc)}Передано в доставку: {ok}"
        if errors:
            msg += "\nОшибки:\n" + "\n".join(errors)[:3000]
        await cb.message.answer(msg)

    async def _seller(acc: Account) -> tuple[str, str | None]:
        """Имя продавца для стикера: из .env, иначе из API (один раз). Второе значение — предупреждение."""
        name = acc.cfg.seller_name or seller_cache.get(acc.id, "")
        if name:
            return name, None
        try:
            name = seller_cache[acc.id] = await acc.wb.seller_name()
            return name, None
        except WBError:
            return "", "не удалось получить имя продавца (лимит WB). Укажите SELLER_NAME в .env."

    @r.callback_query(F.data.startswith("stickers:"))
    async def stickers(cb: CallbackQuery):
        acc = accounts[cb.data.split(":")[1]]
        await cb.answer("Собираю PDF…")
        rows = await refresh_assembling(acc.wb, db, acc.id)
        if not rows:
            return await cb.message.answer(f"{heading(acc)}На сборке ничего нет.")
        await cb.message.answer(
            f"{heading(acc)}Собираю стикеры для {len(rows)} поставок, это может занять пару минут…"
        )
        warns, data = [], []
        seller, warn = await _seller(acc)
        if warn:
            warns.append(warn)
        try:
            for x in rows:
                key = (acc.id, x["sku"] or x["article"])
                if key not in info_cache:
                    try:
                        info_cache[key] = await acc.wb.product_info(x["sku"], x["article"])
                    except WBError as e:
                        info_cache[key] = {}
                        warns.append(f"не удалось получить данные карточки (нужен токен с категорией «Контент»): {e}")
                data.append(
                    {
                        "order_png": await acc.wb.order_sticker(x["order_id"]),
                        "sku": x["sku"],
                        "article": x["article"],
                        "info": {**info_cache[key], "seller": seller},
                        "box_png": await acc.wb.box_sticker(x["supply_id"], x["trbx_id"]),
                    }
                )
        except WBError as e:
            return await cb.message.answer(f"{heading(acc)}Ошибка WB: {e}")
        if warns:
            await cb.message.answer((heading(acc) + "\n".join(dict.fromkeys(warns)))[:500])
        await cb.message.answer_document(
            BufferedInputFile(build_pdf(data), f"stickers_{acc.id}.pdf"),
            caption=f"{acc.name}: поставок {len(rows)}, стикеров {len(rows) * 3}" if multi else f"Поставок: {len(rows)}, стикеров: {len(rows) * 3}",
        )

    return r


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    accounts = {a.id: Account(a, WBClient(a.wb_token)) for a in cfg.accounts}
    db = Db(cfg.db_path)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(build_router(cfg, accounts, db))
    logging.info("Аккаунты WB: %s", ", ".join(a.name for a in accounts.values()))
    try:
        await dp.start_polling(bot)
    finally:
        for a in accounts.values():
            await a.wb.aclose()


if __name__ == "__main__":
    asyncio.run(main())
