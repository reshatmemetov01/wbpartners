import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
from .crypto import Crypto, load_crypto, token_hash
from .db import Db
from .pdf import build_pdf
from .service import deliver_all, move_to_assembly, refresh_assembling, seed_legacy_stores
from .wb import WBClient, WBError

BTN_STORE, BTN_ASSEMBLY = "Мой магазин", "На сборке"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STORE), KeyboardButton(text=BTN_ASSEMBLY)]],
    resize_keyboard=True,
)
MAX_ORDER_BUTTONS = 15  # на магазин: у Telegram ограничено число кнопок в сообщении

WELCOME = (
    "Бот помогает управлять заказами Wildberries FBS: переносить их на сборку, "
    "печатать стикеры и передавать поставки в доставку.\n\n"
    "Нажмите «Мой магазин» → «Привязать магазин», чтобы начать."
)
BIND_HELP = (
    "Отправьте API-токен Wildberries одним сообщением.\n\n"
    "Как создать: кабинет WB → Профиль → Интеграции → Доступ к API → «Создать токен». "
    "Категории: «Маркетплейс» (обязательно) и «Контент» (для данных на стикере штрихкода).\n\n"
    "Токен хранится в зашифрованном виде, сообщение с ним бот сразу удалит из чата. "
    "Отвязать магазин и стереть токен можно в любой момент."
)
NO_STORES = "Сначала привяжите магазин: «Мой магазин» → «Привязать магазин»."


class Bind(StatesGroup):
    token = State()


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


def looks_like_token(text: str) -> bool:
    """Токены WB — JWT: три части через точку, без пробелов."""
    parts = text.split(".")
    return len(parts) == 3 and all(parts) and " " not in text and len(text) > 60


def build_router(
    cfg: Config,
    db: Db,
    crypto: Crypto,
    client_factory: Callable[[str], WBClient] = WBClient,
) -> tuple[Router, Callable[[], list[WBClient]]]:
    r = Router()
    r.message.filter(F.chat.type == "private")
    clients: dict[str, WBClient] = {}
    info_cache: dict[tuple[str, str], dict] = {}
    seller_cache: dict[str, str] = {}

    # ---------- магазины пользователя ----------

    def user_accounts(tg_id: int) -> dict[str, Account]:
        out: dict[str, Account] = {}
        for row in db.stores(tg_id):
            sid = str(row["id"])
            if sid not in clients:
                try:
                    clients[sid] = client_factory(crypto.decrypt(row["token"]))
                except Exception:  # потерян ключ шифрования — магазин придётся привязать заново
                    logging.exception("Не удалось расшифровать токен магазина %s", sid)
                    continue
            acfg = AccountConfig(
                id=sid, name=row["name"], wb_token="", seller_name=row["seller_name"],
                shipping_point_id=row["shipping_point_id"], shipping_type=row["shipping_type"],
                shipping_days=row["shipping_days"],
            )
            out[sid] = Account(acfg, clients[sid])
        return out

    def heading(acc: Account, multi: bool) -> str:
        return f"▸ {acc.name}\n" if multi else ""

    def pick(accounts: dict[str, Account], acc_id: str) -> list[Account]:
        """'*' — все магазины пользователя, иначе один (чужой или удалённый → пусто)."""
        if acc_id == "*":
            return list(accounts.values())
        return [accounts[acc_id]] if acc_id in accounts else []

    async def close_client(store_id: str) -> None:
        wbc = clients.pop(store_id, None)
        if wbc:
            await wbc.aclose()

    # ---------- старт и меню ----------

    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(WELCOME, reply_markup=MENU)

    @r.message(Command("help"))
    async def help_(m: Message):
        await m.answer(WELCOME + "\n\n" + BIND_HELP)

    @r.message(F.text == BTN_STORE)
    async def my_store(m: Message, state: FSMContext):
        await state.clear()
        await show_store_menu(m, m.from_user.id)

    async def show_store_menu(target: Message, tg_id: int):
        accounts = user_accounts(tg_id)
        if not accounts:
            return await target.answer(
                "У вас пока нет привязанных магазинов.", reply_markup=ikb([("Привязать магазин", "bind")])
            )
        names = "\n".join(f"• {a.name}" for a in accounts.values())
        buttons = [[("Заказы (FBS)", "orders")], [("Привязать магазин", "bind")], [("Отвязать магазин", "del_menu")]]
        await target.answer(f"Ваши магазины:\n{names}", reply_markup=ikb(*buttons))

    # ---------- привязка магазина ----------

    @r.callback_query(F.data == "bind")
    async def bind(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        if db.store_count(cb.from_user.id) >= cfg.max_stores:
            return await cb.message.answer(f"Можно привязать не больше {cfg.max_stores} магазинов.")
        await state.set_state(Bind.token)
        await cb.message.answer(BIND_HELP, reply_markup=ikb([("Отмена", "bind_cancel")]))

    @r.callback_query(F.data == "bind_cancel")
    async def bind_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text("Привязка отменена.")
        await cb.answer()

    @r.message(Bind.token, F.text, ~F.text.in_({BTN_STORE, BTN_ASSEMBLY}))
    async def bind_token(m: Message, state: FSMContext):
        token = m.text.strip()
        try:  # токен не должен оставаться в истории чата
            await m.delete()
        except Exception:
            pass
        if not looks_like_token(token):
            return await m.answer("Это не похоже на токен WB. Отправьте токен целиком или нажмите «Отмена».")
        tg_id = m.from_user.id
        if db.store_count(tg_id) >= cfg.max_stores:
            await state.clear()
            return await m.answer(f"Можно привязать не больше {cfg.max_stores} магазинов.")
        if db.has_store_token(tg_id, token_hash(token)):
            await state.clear()
            return await m.answer("Этот магазин уже привязан.")
        wbc = client_factory(token)
        try:
            await wbc.ping()
        except WBError as e:
            await wbc.aclose()
            if e.status == 401:
                msg = "WB не принял токен (неверный или просрочен). Отправьте другой токен."
            elif e.status == 403:
                msg = "У токена нет доступа к категории «Маркетплейс». Создайте токен с этой категорией."
            else:
                msg = f"Не удалось проверить токен: {e}"
            return await m.answer(msg)
        content = await wbc.has_content_access()
        try:
            seller = await wbc.seller_name()
        except WBError:
            seller = ""
        name = seller or f"Магазин {db.store_count(tg_id) + 1}"
        sid = db.add_store(tg_id, name, crypto.encrypt(token), token_hash(token), seller)
        clients[str(sid)] = wbc
        await state.clear()
        text = f"Магазин привязан: {name}"
        if content is False:
            text += "\n\nУ токена нет категории «Контент»: на стикере штрихкода не будет названия, бренда и размера."
        await m.answer(text, reply_markup=ikb([("Заказы (FBS)", "orders")]))

    @r.callback_query(F.data == "del_menu")
    async def del_menu(cb: CallbackQuery):
        accounts = user_accounts(cb.from_user.id)
        await cb.answer()
        if not accounts:
            return await cb.message.answer(NO_STORES)
        await cb.message.answer(
            "Какой магазин отвязать?",
            reply_markup=ikb(*[[(a.name, f"del:{a.id}")] for a in accounts.values()], [("Отмена", "del_no")]),
        )

    @r.callback_query(F.data.startswith("del:"))
    async def del_ask(cb: CallbackQuery):
        acc = user_accounts(cb.from_user.id).get(cb.data.split(":")[1])
        await cb.answer()
        if not acc:
            return await cb.message.answer("Магазин не найден.")
        await cb.message.edit_text(
            f"Отвязать «{acc.name}»? Токен будет удалён, заказы магазина пропадут из списка «На сборке» "
            "(в самом WB ничего не меняется).",
            reply_markup=ikb([("Да, отвязать", f"del_yes:{acc.id}"), ("Отмена", "del_no")]),
        )

    @r.callback_query(F.data == "del_no")
    async def del_no(cb: CallbackQuery):
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @r.callback_query(F.data.startswith("del_yes:"))
    async def del_yes(cb: CallbackQuery):
        sid = cb.data.split(":")[1]
        await cb.answer()
        if sid.isdigit() and db.delete_store(int(sid), cb.from_user.id):
            await close_client(sid)
            return await cb.message.edit_text("Магазин отвязан, токен удалён.")
        await cb.message.edit_text("Магазин не найден.")

    # ---------- заказы FBS ----------

    async def show_orders(target: Message, tg_id: int):
        accounts = user_accounts(tg_id)
        if not accounts:
            return await target.answer(NO_STORES, reply_markup=ikb([("Привязать магазин", "bind")]))
        multi = len(accounts) > 1
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
            parts.append(heading(acc, multi) + "\n".join(order_line(o) for o in items))
            tag = f"{acc.name}: " if multi else ""
            rows += [[(f"{tag}{order_line(o)}"[:60], f"o:{acc.id}:{o['id']}")] for o in items[:MAX_ORDER_BUTTONS]]
            label = f"Перенести все — {acc.name}" if multi else "Перенести все на сборку"
            rows.append([(label, f"move_all:{acc.id}")])
        if multi and total:
            rows.append([("Перенести все (все магазины)", "move_all:*")])
        if errors:
            parts.append("Ошибки WB:\n" + "\n".join(errors))
        if not total and not errors:
            return await target.answer("Новых заказов нет.")
        await target.answer(
            ("Новые заказы:\n" if total else "") + "\n\n".join(parts)[:3900],
            reply_markup=ikb(*rows) if rows else None,
        )

    @r.callback_query(F.data == "orders")
    async def orders(cb: CallbackQuery):
        await cb.answer()
        await show_orders(cb.message, cb.from_user.id)

    @r.callback_query(F.data.startswith("o:"))
    async def order_card(cb: CallbackQuery):
        accounts = user_accounts(cb.from_user.id)
        _, acc_id, oid = cb.data.split(":")
        if acc_id not in accounts:
            return await cb.answer("Магазин не найден", show_alert=True)
        acc, oid = accounts[acc_id], int(oid)
        o = next((x for x in await acc.wb.new_orders() if x["id"] == oid), None)
        if not o:
            return await cb.answer("Заказ уже не в новых", show_alert=True)
        text = (
            f"{heading(acc, len(accounts) > 1)}Заказ #{o['id']}\nАртикул: {o.get('article', '—')}\n"
            f"nmId: {o.get('nmId', '—')}\nШтрихкод: {(o.get('skus') or ['—'])[0]}\n"
            f"Создан: {o.get('createdAt', '—')}"
        )
        await cb.message.answer(text, reply_markup=ikb([("Перенести на сборку", f"move:{acc_id}:{oid}")]))
        await cb.answer()

    async def _move(cb: CallbackQuery, acc_id: str, only: int | None):
        accounts = user_accounts(cb.from_user.id)
        targets = pick(accounts, acc_id)
        if not targets:
            return await cb.answer("Магазин не найден", show_alert=True)
        await cb.answer("Переношу…")
        multi = len(accounts) > 1
        for acc in targets:
            try:
                items = await acc.wb.new_orders()
            except WBError as e:
                await cb.message.answer(f"{heading(acc, multi)}Ошибка WB: {e}")
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
            msg = f"{heading(acc, multi)}Перенесено на сборку: {ok}"
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

    # ---------- на сборке ----------

    @r.message(F.text == BTN_ASSEMBLY)
    async def assembling(m: Message, state: FSMContext):
        await state.clear()
        accounts = user_accounts(m.from_user.id)
        if not accounts:
            return await m.answer(NO_STORES, reply_markup=ikb([("Привязать магазин", "bind")]))
        multi = len(accounts) > 1
        parts, buttons = [], []
        for acc in accounts.values():
            rows = await refresh_assembling(acc.wb, db, acc.id)
            if not rows:
                continue
            parts.append(
                heading(acc, multi)
                + "\n".join(f"{x['article']} · заказ #{x['order_id']} · {x['supply_id']}" for x in rows)
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
        accounts = user_accounts(cb.from_user.id)
        acc = accounts.get(cb.data.split(":")[1])
        await cb.answer()
        if not acc:
            return await cb.message.answer("Магазин не найден.")
        multi = len(accounts) > 1
        rows = await refresh_assembling(acc.wb, db, acc.id)
        if not rows:
            return await cb.message.answer(f"{heading(acc, multi)}На сборке ничего нет.")
        await cb.message.answer(
            f"{heading(acc, multi)}Передать в доставку {len(rows)} поставок? После этого изменить их будет нельзя.",
            reply_markup=ikb([(f"Да, передать {len(rows)}", f"deliver_yes:{acc.id}"), ("Отмена", "deliver_no")]),
        )

    @r.callback_query(F.data == "deliver_no")
    async def deliver_no(cb: CallbackQuery):
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @r.callback_query(F.data.startswith("deliver_yes:"))
    async def deliver_yes(cb: CallbackQuery):
        accounts = user_accounts(cb.from_user.id)
        acc = accounts.get(cb.data.split(":")[1])
        await cb.answer()
        if not acc:
            return await cb.message.edit_text("Магазин не найден.")
        multi = len(accounts) > 1
        await cb.message.edit_text(f"{heading(acc, multi)}Передаю в доставку…")
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
        msg = f"{heading(acc, multi)}Передано в доставку: {ok}"
        if errors:
            msg += "\nОшибки:\n" + "\n".join(errors)[:3000]
        await cb.message.answer(msg)

    async def _seller(acc: Account) -> tuple[str, str | None]:
        """Имя продавца для стикера: из базы, иначе из API (один раз). Второе значение — предупреждение."""
        name = acc.cfg.seller_name or seller_cache.get(acc.id, "")
        if name:
            return name, None
        try:
            name = seller_cache[acc.id] = await acc.wb.seller_name()
            db.set_seller_name(int(acc.id), name)
            return name, None
        except WBError:
            return "", "не удалось получить имя продавца (лимит WB), на стикере его не будет."

    @r.callback_query(F.data.startswith("stickers:"))
    async def stickers(cb: CallbackQuery):
        accounts = user_accounts(cb.from_user.id)
        acc = accounts.get(cb.data.split(":")[1])
        if not acc:
            return await cb.answer("Магазин не найден", show_alert=True)
        multi = len(accounts) > 1
        await cb.answer("Собираю PDF…")
        rows = await refresh_assembling(acc.wb, db, acc.id)
        if not rows:
            return await cb.message.answer(f"{heading(acc, multi)}На сборке ничего нет.")
        await cb.message.answer(
            f"{heading(acc, multi)}Собираю стикеры для {len(rows)} поставок, это может занять пару минут…"
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
            return await cb.message.answer(f"{heading(acc, multi)}Ошибка WB: {e}")
        if warns:
            await cb.message.answer((heading(acc, multi) + "\n".join(dict.fromkeys(warns)))[:500])
        caption = f"Поставок: {len(rows)}, стикеров: {len(rows) * 3}"
        await cb.message.answer_document(
            BufferedInputFile(build_pdf(data), f"stickers_{acc.id}.pdf"),
            caption=f"{acc.name}: {caption.lower()}" if multi else caption,
        )

    return r, lambda: list(clients.values())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    db = Db(cfg.db_path)
    crypto = load_crypto(os.environ, cfg.db_path)
    moved = seed_legacy_stores(db, crypto, cfg.admin_id, cfg.accounts)
    if moved:
        logging.info("Аккаунты из .env перенесены в базу: %s", moved)
    router, all_clients = build_router(cfg, db, crypto)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        for c in all_clients():
            await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
