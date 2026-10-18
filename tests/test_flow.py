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
