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
