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
    def __init__(self, fail_box_once=False):
        self.calls, self.fail_box_once = [], fail_box_once

    async def create_supply(self, name):
        self.calls.append(("supply", name))
        return "WB-GI-1"

    async def add_order(self, s, o):
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
