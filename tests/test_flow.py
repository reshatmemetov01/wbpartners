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
