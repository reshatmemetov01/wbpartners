import io

import pytest
from PIL import Image

from wbbot.db import Db
from wbbot.pdf import build_pdf
from wbbot.service import move_to_assembly
from wbbot.wb import WBError


def png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (58, 40), "white").save(b, "PNG")
    return b.getvalue()


class FakeWB:
    def __init__(self, fail_box_once=False, stale_supply=False):
        self.calls, self.fail_box_once, self.stale = [], fail_box_once, stale_supply
        self.n = 0

    async def create_supply(self, name):
        self.n += 1
        self.calls.append(("supply", name))
        return f"WB-GI-{self.n}"

    async def add_order(self, s, o):
        if self.stale and s == "WB-GI-STALE":
            raise WBError("404", 404)
        self.calls.append(("add", s, o))

    async def create_box(self, s):
        if self.fail_box_once:
            self.fail_box_once = False
            raise WBError("boom")
        self.calls.append(("box", s))
        return "TRBX-1"


ORDER = {"id": 5, "article": "ART-1", "skus": ["2000000000001"]}


@pytest.mark.asyncio
async def test_move_creates_supply_named_by_article():
    wb, db = FakeWB(), Db(":memory:")
    await move_to_assembly(wb, db, ORDER)
    assert wb.calls == [("supply", "ART-1"), ("add", "WB-GI-1", 5), ("box", "WB-GI-1")]
    assert len(db.assembling()) == 1


@pytest.mark.asyncio
async def test_retry_after_failure_does_not_duplicate_supply():
    wb, db = FakeWB(fail_box_once=True), Db(":memory:")
    with pytest.raises(WBError):
        await move_to_assembly(wb, db, ORDER)
    await move_to_assembly(wb, db, ORDER)
    assert [c[0] for c in wb.calls] == ["supply", "add", "box"]


def test_pdf_has_three_pages_per_supply():
    item = {"order_png": png(), "sku": "2000000000001", "article": "ART-1", "box_png": png()}
    pdf = build_pdf([item, item])
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 6


@pytest.mark.asyncio
async def test_stale_supply_is_recreated_on_404():
    wb, db = FakeWB(stale_supply=True), Db(":memory:")
    db.ensure(5, "ART-1", "2000000000001")
    db.set_supply(5, "WB-GI-STALE")
    await move_to_assembly(wb, db, ORDER)
    assert db.get(5)["supply_id"] == "WB-GI-1" and db.get(5)["added"] == 1


class StatusWB:
    def __init__(self, statuses=None, fail=False):
        self.statuses, self.fail = statuses or {}, fail

    async def orders_status(self, ids):
        if self.fail:
            raise WBError("boom", 500)
        return {i: self.statuses[i] for i in ids if i in self.statuses}


def _db_with(*ids):
    db = Db(":memory:")
    for i in ids:
        db.ensure(i, "A", "1")
        db.set_supply(i, f"S{i}")
        db.set_added(i)
        db.set_trbx(i, f"T{i}")
    return db


@pytest.mark.asyncio
async def test_refresh_removes_shipped_and_cancelled():
    from wbbot.service import refresh_assembling

    db = _db_with(1, 2, 3)
    rows = await refresh_assembling(StatusWB({1: "confirm", 2: "complete", 3: "cancel"}), db)
    assert [r["order_id"] for r in rows] == [1]
    assert [r["order_id"] for r in db.assembling()] == [1]


@pytest.mark.asyncio
async def test_refresh_keeps_everything_when_wb_fails():
    from wbbot.service import refresh_assembling

    db = _db_with(1, 2)
    rows = await refresh_assembling(StatusWB(fail=True), db)
    assert len(rows) == 2 and len(db.assembling()) == 2


class DeliverWB:
    def __init__(self, bad=(), ship_errors=None):
        self.bad, self.sent, self.ship_errors, self.ship_args = set(bad), [], ship_errors or {}, None

    async def set_shipping(self, ids, point, typ, dt):
        self.ship_args = (ids, point, typ, dt)
        return {i: self.ship_errors.get(i) for i in ids}

    async def deliver(self, supply_id):
        if supply_id in self.bad:
            raise WBError("nope", 400)
        self.sent.append(supply_id)


@pytest.mark.asyncio
async def test_deliver_all_removes_only_delivered():
    from wbbot.service import deliver_all

    db = _db_with(1, 2, 3)
    wb = DeliverWB(bad={"S2"})
    ok, errors = await deliver_all(wb, db, db.assembling())
    assert ok == 2 and len(errors) == 1 and "#2" in errors[0]
    assert [r["order_id"] for r in db.assembling()] == [2]


@pytest.mark.asyncio
async def test_shipping_params_set_before_deliver_and_errors_block():
    from wbbot.service import deliver_all

    db = _db_with(1, 2, 3)
    wb = DeliverWB(ship_errors={"S2": "InvalidShippingDt", "S3": "SupplyAlreadyScanned"})
    ship = {"point_id": 100, "type": "selfShipping", "dt": "2026-10-18"}
    ok, errors = await deliver_all(wb, db, db.assembling(), ship)
    assert wb.ship_args == (["S1", "S2", "S3"], 100, "selfShipping", "2026-10-18")
    assert wb.sent == ["S1", "S3"] and ok == 2
    assert len(errors) == 1 and "InvalidShippingDt" in errors[0]
    assert [r["order_id"] for r in db.assembling()] == [2]


def test_config_reads_second_account_from_suffixed_env():
    from wbbot.config import load_config

    env = {
        "BOT_TOKEN": "b", "ADMIN_ID": "1",
        "WB_TOKEN": "t1", "SELLER_NAME": "ИП Один", "SHIPPING_POINT_ID": "100",
        "WB_TOKEN_2": "t2", "WB_NAME_2": "Магазин 2", "SELLER_NAME_2": "ИП Два",
        "SHIPPING_POINT_ID_2": "200", "SHIPPING_DAYS_2": "1",
    }
    a1, a2 = load_config(env).accounts
    assert (a1.id, a1.name, a1.wb_token, a1.shipping_point_id) == ("1", "Аккаунт 1", "t1", 100)
    assert (a2.id, a2.name, a2.wb_token, a2.seller_name) == ("2", "Магазин 2", "t2", "ИП Два")
    assert (a2.shipping_point_id, a2.shipping_days, a2.shipping_type) == (200, 1, "selfShipping")


def test_single_account_config_still_works():
    from wbbot.config import load_config

    cfg = load_config({"BOT_TOKEN": "b", "ADMIN_ID": "1", "WB_TOKEN": "t"})
    assert [a.id for a in cfg.accounts] == ["1"]


def test_assembling_is_filtered_by_account():
    db = Db(":memory:")
    for oid, acc in ((1, "1"), (2, "2"), (3, "2")):
        db.ensure(oid, "A", "1", acc)
        db.set_supply(oid, f"S{oid}")
        db.set_added(oid)
        db.set_trbx(oid, f"T{oid}")
    assert [r["order_id"] for r in db.assembling("2")] == [2, 3]
    assert [r["order_id"] for r in db.assembling("1")] == [1]
    assert len(db.assembling()) == 3


def test_old_database_gets_account_column(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE items (order_id INTEGER PRIMARY KEY, article TEXT NOT NULL, sku TEXT NOT NULL DEFAULT '', supply_id TEXT, added INTEGER NOT NULL DEFAULT 0, trbx_id TEXT)")
    c.execute("INSERT INTO items VALUES (7, 'A', '1', 'S7', 1, 'T7')")
    c.commit()
    c.close()
    db = Db(path)
    assert db.get(7)["account"] == "1" and len(db.assembling("1")) == 1


@pytest.mark.asyncio
async def test_deliver_drops_empty_supplies_from_list():
    from wbbot.service import deliver_all

    class EmptyWB(DeliverWB):
        async def deliver(self, supply_id):
            if supply_id == "S2":
                raise WBError('409: {"code":"SupplyHasZeroOrders"}', 409)
            await super().deliver(supply_id)

    db = _db_with(1, 2)
    ok, errors = await deliver_all(EmptyWB(), db, db.assembling())
    assert ok == 1 and len(errors) == 1 and "пуста" in errors[0]
    assert db.assembling() == []  # пустая поставка больше не висит в списке


@pytest.mark.asyncio
async def test_orders_status_treats_buyer_cancel_as_gone():
    import httpx
    from wbbot.wb import WBClient

    def handler(request):
        return httpx.Response(200, json={"orders": [
            {"id": 1, "supplierStatus": "confirm", "wbStatus": "waiting"},
            {"id": 2, "supplierStatus": "confirm", "wbStatus": "canceled_by_client"},
        ]})

    wb = WBClient("t", httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
    assert await wb.orders_status([1, 2]) == {1: "confirm", 2: "cancel"}


class ImportWB:
    def __init__(self, orders, statuses, boxes):
        self.orders, self.statuses, self.boxes, self.created = orders, statuses, boxes, []

    async def orders_since(self, days):
        return self.orders

    async def orders_status(self, ids):
        return {i: self.statuses[i] for i in ids}

    async def supply_boxes(self, sid):
        return self.boxes.get(sid, [])

    async def create_box(self, sid):
        self.created.append(sid)
        return f"NEW-{sid}"


@pytest.mark.asyncio
async def test_import_assembling_picks_confirmed_orders_in_supplies():
    from wbbot.service import import_assembling

    orders = [
        {"id": 1, "supplyId": "S1", "article": "A1", "skus": ["111"]},
        {"id": 2, "supplyId": "S1", "article": "A2", "skus": ["222"]},  # та же поставка
        {"id": 3, "supplyId": "S3", "article": "A3", "skus": []},  # без грузоместа
        {"id": 4, "supplyId": "S4", "article": "A4"},  # уже в доставке
        {"id": 5, "article": "A5"},  # не в поставке
        {"id": 6, "supplyId": "S6", "article": "A6"},  # уже есть в базе
    ]
    wb = ImportWB(orders, {1: "confirm", 2: "confirm", 3: "confirm", 4: "complete", 6: "confirm"}, {"S1": ["TRBX-1"]})
    db = Db(":memory:")
    db.ensure(6, "A6", "", "7")
    added, errors = await import_assembling(wb, db, "7")
    assert (added, errors) == (3, [])
    rows = {r["order_id"]: r for r in db.assembling("7")}
    assert set(rows) == {1, 2, 3}
    assert rows[1]["trbx_id"] == rows[2]["trbx_id"] == "TRBX-1" and rows[1]["sku"] == "111"
    assert rows[3]["trbx_id"] == "NEW-S3" and wb.created == ["S3"]  # грузоместо создано один раз
    assert (await import_assembling(wb, db, "7"))[0] == 0  # повторный скан не дублирует


@pytest.mark.asyncio
async def test_deliver_each_supply_once_even_with_several_orders():
    from wbbot.service import deliver_all

    db = Db(":memory:")
    for oid, sid in ((1, "S1"), (2, "S1"), (3, "S3")):
        db.ensure(oid, "A", "1")
        db.set_supply(oid, sid)
        db.set_added(oid)
        db.set_trbx(oid, "T")
    wb = DeliverWB()
    ok, errors = await deliver_all(wb, db, db.assembling(), {"point_id": 1, "type": "selfShipping", "dt": "x"})
    assert wb.sent == ["S1", "S3"] and wb.ship_args[0] == ["S1", "S3"]
    assert ok == 2 and errors == [] and db.assembling() == []


def _client(handler):
    import httpx
    from wbbot.wb import WBClient

    return WBClient("t", httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expected", [
    ({"trbxes": [{"id": "WB-TRBX-1"}, {"id": "WB-TRBX-2"}]}, ["WB-TRBX-1", "WB-TRBX-2"]),
    ({"trbxIds": ["WB-TRBX-9"]}, ["WB-TRBX-9"]),
    ({"trbxes": []}, []),
    ([{"trbxId": "WB-TRBX-5"}], ["WB-TRBX-5"]),
])
async def test_supply_boxes_parses_response_variants(payload, expected):
    import httpx

    wb = _client(lambda req: httpx.Response(200, json=payload))
    assert await wb.supply_boxes("S") == expected


@pytest.mark.asyncio
async def test_orders_since_paginates():
    import httpx

    calls = []

    def handler(req):
        nxt = int(req.url.params["next"])
        calls.append(nxt)
        if nxt == 0:
            return httpx.Response(200, json={"next": 77, "orders": [{"id": i} for i in range(1000)]})
        return httpx.Response(200, json={"next": 78, "orders": [{"id": 5000}]})

    wb = _client(handler)
    got = await wb.orders_since(30)
    assert len(got) == 1001 and calls == [0, 77]


def test_pdf_skips_missing_box_sticker():
    item = {"order_png": png(), "sku": "2000000000001", "article": "ART-1", "box_png": png()}
    one = build_pdf([item])
    two = build_pdf([item, {**item, "box_png": None}])
    count = lambda b: b.count(b"/Type /Page\n") + b.count(b"/Type /Page ")
    assert count(two) - count(one) == 2  # второй заказ той же поставки: без стикера грузоместа
