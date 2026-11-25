"""Подарочные сертификаты: цены, номера, срок действия и сборка PDF по шаблону.

Шаблон — assets/cert_template.pdf (без размера, количества, номера и срока: их вписывает бот).
Шрифты — assets/fonts/: Raleway (номер и срок) и NauryzRedKeds (размер и количество).
"""
from __future__ import annotations

import random
import string
from datetime import date
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ASSETS = Path(__file__).parent / "assets"
TEMPLATE = ASSETS / "cert_template.pdf"
FONTS = ASSETS / "fonts"

# ---------------- Цены (из прайса групповых МК) ----------------
SIZES = (30, 40, 50)
PRICES = {  # размер → {человек: цена за всех}
    30: {1: 3800, 2: 7300, 3: 10650, 4: 13800},
    40: {1: 4500, 2: 8600, 3: 12600, 4: 16400},
    50: {1: 5200, 2: 9800, 3: 14250, 4: 18400},
}
PER_PERSON_FROM_4 = {30: 3450, 40: 4100, 50: 4600}  # от 4 человек — цена с человека

# ---------------- Срок действия ----------------
VALID_MONTHS = 3
VALID_EXTRA_DAYS = 10

# ---------------- Где и как писать на шаблоне (в пунктах PDF, отсчёт от левого нижнего угла) ----------------
ORANGE = Color(255 / 255, 92 / 255, 56 / 255)
BLACK = Color(0, 0, 0)
SIZE_POS = (40, 470.0, 44)       # x, линия текста, кегль — «50Х50см»
COUNT_POS = (40, 518.0, 20)      # «2 коврика» (только если сертификат на нескольких)
CODE_PILL = (41.5, 122.4)        # серая плашка с номером: левый и правый край
CODE_Y, CODE_SIZE = 91.0, 10
EXPIRY_POS = (132, 91.0, 10)


def price(size: int, qty: int) -> int:
    if qty <= 4:
        return PRICES[size][qty]
    return PER_PERSON_FROM_4[size] * qty


def size_label(size: int) -> str:
    return f"{size}×{size} см"


def rugs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "коврик"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "коврика"
    return "ковриков"


def new_code(taken: set[str]) -> str:
    """4 английские буквы, подчёркивание, 2 цифры: KRWT_27. Не повторяет уже выданные."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4)) + "_" + f"{random.randint(0, 99):02d}"
        if code not in taken:
            taken.add(code)
            return code


def valid_until(bought: date) -> date:
    """Дата покупки + 3 месяца + 10 дней (31 января + 1 месяц = последний день февраля)."""
    m = bought.month - 1 + VALID_MONTHS
    y, m = bought.year + m // 12, m % 12 + 1
    last_day = (date(y + m // 12, m % 12 + 1, 1) - date.resolution).day
    from datetime import timedelta

    return date(y, m, min(bought.day, last_day)) + timedelta(days=VALID_EXTRA_DAYS)


# ---------------- PDF ----------------
_fonts_ready = False
SIZE_FONT = "Nauryz"


def _register_fonts():
    global _fonts_ready, SIZE_FONT
    if _fonts_ready:
        return
    pdfmetrics.registerFont(TTFont("Raleway-Medium", str(FONTS / "Raleway-Medium.ttf")))
    pdfmetrics.registerFont(TTFont("Raleway-Bold", str(FONTS / "Raleway-Bold.ttf")))
    nauryz = next((p for p in FONTS.iterdir()  # имя файла — в любом регистре: NauryzRedKeds.ttf, NAURYZREDKEDS.ttf
                   if p.name.lower().startswith("nauryz") and p.suffix.lower() == ".ttf"), None)
    if nauryz:
        pdfmetrics.registerFont(TTFont("Nauryz", str(nauryz)))
    else:  # пока нет файла шрифта — временно Raleway, чтобы бот не ломался
        SIZE_FONT = "Raleway-Bold"
    _fonts_ready = True


def make_pdf(size: int, code: str, until: date, rugs: int = 1) -> bytes:
    """Сертификат на одного (rugs=1) или на нескольких — тогда сверху строка «2 коврика»."""
    _register_fonts()
    template = PdfReader(str(TEMPLATE))
    page = template.pages[0]
    w, h = float(page.mediabox.width), float(page.mediabox.height)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setFillColor(ORANGE)

    def spaced(pos, text, char_space):  # в макете у этих надписей чуть увеличено расстояние между буквами
        x, y, fs = pos
        t = c.beginText(x, y)
        t.setFont(SIZE_FONT, fs)
        t.setCharSpace(char_space)
        t.textOut(text)
        c.drawText(t)

    spaced(SIZE_POS, f"{size}Х{size}см", 1.37)  # «Х» — русская, как в макете
    if rugs > 1:
        spaced(COUNT_POS, f"{rugs} {rugs_word(rugs)}", 0.39)
    c.setFillColor(BLACK)
    c.setFont("Raleway-Bold", CODE_SIZE)
    c.drawCentredString((CODE_PILL[0] + CODE_PILL[1]) / 2, CODE_Y, f"№{code}")
    x, y, fs = EXPIRY_POS
    t = c.beginText(x, y)
    t.setFont("Raleway-Medium", fs)
    t.setCharSpace(-0.2)  # в макете эта строка набрана чуть плотнее
    t.textOut(f"Срок действия: до {until:%d.%m.%Y}")
    c.drawText(t)
    c.save()

    overlay = PdfReader(BytesIO(buf.getvalue())).pages[0]
    page.merge_page(overlay)
    out = PdfWriter()
    out.add_page(page)
    result = BytesIO()
    out.write(result)
    return result.getvalue()


# ---------------- Номер сертификата, который клиент вводит при записи ----------------
import re  # noqa: E402

_LOOKALIKE = str.maketrans("АВЕКМНОРСТХУавекмнорстху", "ABEKMHOPCTXYABEKMHOPCTXY")


def normalize_code(text: str) -> str | None:
    """Только приводит к виду ABCD_12 («№ krwt-27» → «KRWT_27»). Ничего не проверяет. None — если не похоже."""
    s = str(text or "").strip().translate(_LOOKALIKE).upper()
    s = re.sub(r"[№#\s.]", "", s).replace("-", "_")
    m = re.fullmatch(r"([A-Z]{4})_?(\d{1,2})", s)
    return f"{m.group(1)}_{m.group(2)}" if m else None
