import io
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128, eanbc
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

W, H = 58 * mm, 40 * mm
FONTS = Path(__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("Sans", str(FONTS / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))


def _png_page(c: canvas.Canvas, png: bytes) -> None:
    c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, W, H, preserveAspectRatio=True, anchor="c")
    c.showPage()


def _draw_barcode(c: canvas.Canvas, sku: str, top: float, height: float) -> None:
    """Штрихкод с цифрами под ним: EAN-13, иначе Code128. Верх штрихкода — на высоте top."""
    if len(sku) == 13 and sku.isdigit():
        widget = eanbc.Ean13BarcodeWidget(sku, barHeight=height - 3 * mm, humanReadable=True, fontSize=6)
        x0, y0, x1, y1 = widget.getBounds()
        scale = min(1.0, (W - 4 * mm) / (x1 - x0))
        d = Drawing(x1 - x0, y1 - y0)
        d.add(widget)
        d.scale(scale, scale)
        d.width, d.height = (x1 - x0) * scale, (y1 - y0) * scale
        renderPDF.draw(d, c, (W - d.width) / 2, top - d.height)
    else:
        bc = code128.Code128(sku, barHeight=height - 3 * mm, barWidth=0.3 * mm, humanReadable=True)
        bc.drawOn(c, max((W - bc.width) / 2, 1 * mm), top - height)


def _barcode_page(c: canvas.Canvas, sku: str, article: str, info: dict) -> None:
    top = H - 1.5 * mm
    if sku:
        _draw_barcode(c, sku, top, 13 * mm)
    y = top - 14 * mm
    lines = [
        (info.get("seller", ""), "Sans", 5),
        (info.get("title", ""), "Sans-Bold", 5.5),
        (f"Бренд: {info['brand']}" if info.get("brand") else "", "Sans", 5),
        (f"Цвет: {info['color']}" if info.get("color") else "Цвет:", "Sans", 5),
        (f"Размер: {info['size']}" if info.get("size") != "" and "size" in info else "Размер:", "Sans", 5),
        ("Артикул:", "Sans", 5),
        (article, "Sans-Bold", 5.5),
    ]
    for text, font, size in lines:
        if not text:
            continue
        for part in simpleSplit(text, font, size, W - 4 * mm)[:3]:
            if y < 1 * mm:
                break
            c.setFont(font, size)
            c.drawCentredString(W / 2, y, part)
            y -= size * 1.15
    c.showPage()


def build_pdf(supplies: list[dict]) -> bytes:
    """supplies: [{order_png, sku, article, info, box_png}] -> по 3 страницы на поставку."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    for s in supplies:
        _png_page(c, s["order_png"])
        _barcode_page(c, s["sku"], s["article"], s.get("info", {}))
        _png_page(c, s["box_png"])
    c.save()
    return buf.getvalue()
