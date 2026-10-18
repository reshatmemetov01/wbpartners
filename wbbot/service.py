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


LEFT_ASSEMBLY = {"complete", "cancel"}


async def refresh_assembling(wb: WBClient, db: Db) -> list:
    """Список «На сборке» без заказов, которые уже переданы в доставку или отменены.

    При сбое запроса к WB возвращает список из базы без изменений.
    """
    rows = db.assembling()
    if not rows:
        return rows
    try:
        statuses = await wb.orders_status([r["order_id"] for r in rows])
    except WBError:
        return rows
    gone = [r["order_id"] for r in rows if statuses.get(r["order_id"]) in LEFT_ASSEMBLY]
    if gone:
        db.remove(gone)
    return [r for r in rows if r["order_id"] not in gone]


async def deliver_all(
    wb: WBClient, db: Db, rows: list, shipping: dict | None = None
) -> tuple[int, list[str]]:
    """Устанавливает параметры отгрузки (если заданы), передаёт поставки в доставку
    и убирает успешно переданные из списка «На сборке».

    shipping: {"point_id": int, "type": str, "dt": "YYYY-MM-DD"} или None.
    """
    done, errors = [], []
    blocked: dict[str, str] = {}
    if shipping:
        try:
            res = await wb.set_shipping(
                [r["supply_id"] for r in rows], shipping["point_id"], shipping["type"], shipping["dt"]
            )
        except WBError as e:
            return 0, [f"Параметры отгрузки: {e}"]
        # SupplyAlreadyScanned — параметры уже зафиксированы, передаём как есть
        blocked = {k: v for k, v in res.items() if v and v != "SupplyAlreadyScanned"}
    for r in rows:
        if r["supply_id"] in blocked:
            errors.append(f"#{r['order_id']} ({r['supply_id']}): параметры отгрузки: {blocked[r['supply_id']]}")
            continue
        try:
            await wb.deliver(r["supply_id"])
            done.append(r["order_id"])
        except WBError as e:
            errors.append(f"#{r['order_id']} ({r['supply_id']}): {e}")
    if done:
        db.remove(done)
    return len(done), errors
