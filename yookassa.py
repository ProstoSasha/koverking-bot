"""Оплата через ЮKassa: бот сам создаёт платёж на нужную сумму и сам проверяет, прошла ли оплата.

Работает, только если в переменных окружения есть YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.
Без них бот принимает оплату по старой общей ссылке и просит менеджера подтвердить вручную.

Чеки (54-ФЗ): к каждому платежу прикладывается чек (в ЮKassa включены чеки). Если чеки когда-нибудь
отключите — добавьте переменную YOOKASSA_RECEIPTS=0. Ставка НДС — YOOKASSA_VAT_CODE
(по умолчанию 1 = «без НДС»).
"""
import base64
import logging
import os
import re
import uuid

import aiohttp

log = logging.getLogger("yookassa")
API = "https://api.yookassa.ru/v3/payments"

SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "").strip()
SECRET = os.environ.get("YOOKASSA_SECRET_KEY", "").strip()
# чеки включены по умолчанию; если в ЮKassa чеков нет — YOOKASSA_RECEIPTS=0
RECEIPTS = os.environ.get("YOOKASSA_RECEIPTS", "1").strip().lower() not in ("0", "no", "false", "нет")
VAT_CODE = int(os.environ.get("YOOKASSA_VAT_CODE", "1") or 1)

PAID = "succeeded"
CANCELED = "canceled"


def enabled() -> bool:
    return bool(SHOP_ID and SECRET)


def _headers(idempotence: str | None = None) -> dict:
    token = base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
    h = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    if idempotence:
        h["Idempotence-Key"] = idempotence
    return h


def customer(contact: str = "", email: str = "") -> dict:
    """Телефон или почта для чека. Пусто — если ни того, ни другого нет."""
    if email and "@" in email and not email.startswith("@"):
        return {"email": email.strip()}
    digits = re.sub(r"\D", "", contact or "")
    if 10 <= len(digits) <= 15:
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits
        return {"phone": digits}
    return {}


async def create(amount: int, description: str, return_url: str, metadata: dict,
                 receipt_item: str = "", payment_mode: str = "full_prepayment",
                 payment_subject: str = "service", buyer: dict | None = None) -> dict:
    """Создаёт платёж. Возвращает {"id": ..., "url": ...}."""
    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": {k: str(v)[:500] for k, v in metadata.items()},
    }
    if RECEIPTS:
        receipt = {"items": [{
            "description": (receipt_item or description)[:128],
            "quantity": "1.00",
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "vat_code": VAT_CODE,
            "payment_mode": payment_mode,
            "payment_subject": payment_subject,
        }]}
        if buyer:
            receipt["customer"] = buyer
        body["receipt"] = receipt
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(API, json=body, headers=_headers(str(uuid.uuid4()))) as r:
            data = await r.json(content_type=None)
            if r.status >= 300:
                raise RuntimeError(f"ЮKassa {r.status}: {data}")
    return {"id": data["id"], "url": data["confirmation"]["confirmation_url"]}


async def status(payment_id: str) -> str:
    """pending / waiting_for_capture / succeeded / canceled."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(f"{API}/{payment_id}", headers=_headers()) as r:
            data = await r.json(content_type=None)
            if r.status >= 300:
                raise RuntimeError(f"ЮKassa {r.status}: {data}")
    return data.get("status", "")
