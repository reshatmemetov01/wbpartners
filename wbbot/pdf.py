import io

from reportlab.graphics.barcode import code128
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

W, H = 58 * mm, 40 * mm


def _png_page(c: canvas.Canvas, png: bytes) -> None:
    c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, W, H, preserveAspectRatio=True, anchor="c")
    c.showPage()


def _barcode_page(c: canvas.Canvas, sku: str, article: str) -> None:
    if sku:
        bc = code128.Code128(sku, barHeight=18 * mm, barWidth=0.33 * mm, humanReadable=True)
        bc.drawOn(c, max((W - bc.width) / 2, 1 * mm), 14 * mm)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W / 2, 6 * mm, article[:30])
    c.showPage()


def build_pdf(supplies: list[dict]) -> bytes:
    """supplies: [{order_png, sku, article, box_png}] -> по 3 страницы на поставку."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    for s in supplies:
        _png_page(c, s["order_png"])
        _barcode_page(c, s["sku"], s["article"])
        _png_page(c, s["box_png"])
    c.save()
    return buf.getvalue()
