from .db import Db
from .wb import WBClient, WBError


def order_article(order: dict) -> str:
    return str(order.get("article") or order.get("nmId") or order["id"])


def order_sku(order: dict) -> str:
    skus = order.get("skus") or []
    return str(skus[0]) if skus else ""


async def move_to_assembly(wb: WBClient, db: Db, order: dict) -> str:
    """Создаёт поставку (имя = артикул), кладёт в неё заказ и создаёт 1 грузоместо.

    Каждый шаг фиксируется в БД, поэтому повторный вызов после сбоя
    продолжает с места остановки и не плодит дубли поставок.
    """
    oid = order["id"]
    db.ensure(oid, order_article(order), order_sku(order))
    row = db.get(oid)
    for attempt in (1, 2):
        if not row["supply_id"]:
            db.set_supply(oid, await wb.create_supply(row["article"]))
            row = db.get(oid)
        if not row["added"]:
            try:
                await wb.add_order(row["supply_id"], oid)
            except WBError as e:
                # WB удаляет пустые поставки: сохранённая могла исчезнуть — создаём заново
                if e.status == 404 and attempt == 1:
                    db.reset_supply(oid)
                    row = db.get(oid)
                    continue
                raise
            db.set_added(oid)
            row = db.get(oid)
        break
    if not row["trbx_id"]:
        db.set_trbx(oid, await wb.create_box(row["supply_id"]))
    return db.get(oid)["supply_id"]
