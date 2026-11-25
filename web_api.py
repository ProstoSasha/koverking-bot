"""Вход для сайта: виджет записи на Тильде обращается сюда по HTTPS.

Запускается вместе с ботом (см. main в bot.py) и слушает 0.0.0.0:PORT (по умолчанию 3000).
Вся логика расписания, таблица и сообщения менеджеру — общие с ботом в Telegram.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import date, timedelta

from aiohttp import web

log = logging.getLogger("web")

FMT_LABELS = {"group": "Групповой", "date": "Свидание", "bday": "День рождения/корпоратив"}
MAX_POSTS_PER_HOUR = 20          # с одного адреса: защита от спама
_posts: dict[str, deque] = defaultdict(deque)


def _ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote or "?")


def _too_many(request: web.Request) -> bool:
    q, now = _posts[_ip(request)], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= MAX_POSTS_PER_HOUR:
        return True
    q.append(now)
    return False


def _ok(**data):
    return web.json_response({"ok": True, **data})


def _err(code: str, status: int = 400, **data):
    return web.json_response({"ok": False, "error": code, **data}, status=status)


@web.middleware
async def cors(request: web.Request, handler):
    """Сайт на другом домене — разрешаем ему обращаться к нам из браузера."""
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        except Exception:
            log.exception("Ошибка обработки %s", request.path)
            resp = _err("server", 500)
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("SITE_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _phone_ok(phone: str) -> bool:
    return 10 <= len(re.sub(r"\D", "", phone)) <= 15


def _return_url(d: dict) -> str:
    """Куда ЮKassa вернёт клиента после оплаты — на ту же страницу сайта."""
    url = str(d.get("return_url", "")).strip()
    return url if url.startswith("https://") and len(url) < 500 else "https://koverking39.ru"


def build_app(bot, ctx) -> web.Application:
    """ctx — модуль bot.py: берём оттуда таблицу, расписание, тексты и отправку менеджеру."""
    logic, texts, certificate, mailer = ctx.logic, ctx.texts, ctx.certificate, ctx.mailer
    yookassa = ctx.yookassa
    routes = web.RouteTableDef()

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------------- справочные данные для виджета ----------------
    @routes.get("/api/ping")
    async def ping(_):
        return _ok()

    @routes.get("/api/config")
    async def config_(_):
        return _ok(
            prepay_per_person=logic.PREPAY_PER_PERSON,
            pay_url=texts.PAY_URL,
            sketches_url=texts.SKETCHES_URL,
            bot_username=ctx.BOT_USERNAME,
            sizes=[{"size": s, "label": certificate.size_label(s), "price": certificate.price(s, 1)}
                   for s in certificate.SIZES],
            bday={"min": logic.BDAY_MIN, "comfort": logic.BDAY_COMFORT, "max": logic.BDAY_MAX},
            prices={code: f"/prices/{name}" for code, name in ctx.PRICE_FILES.items()
                    if (ctx.ASSETS / name).exists()},
            cert_prices={str(s): {str(n): certificate.price(s, n) for n in range(1, 5)} for s in certificate.SIZES},
            cert_per_from_4={str(s): p for s, p in certificate.PER_PERSON_FROM_4.items()},
            how_to_find=texts.HOW_TO_FIND,
            email_enabled=mailer.enabled(),
        )

    @routes.get("/prices/{name}")
    async def price_image(request: web.Request):
        name = request.match_info["name"]
        if name not in ctx.PRICE_FILES.values():
            raise web.HTTPNotFound()
        path = ctx.ASSETS / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})

    @routes.get("/api/group-slots")
    async def group_slots(_):
        studio = await ctx.load_studio()
        hot = logic.nearest_weekend(studio.now.date())
        return _ok(slots=[{
            "day": s.day.isoformat(), "time": f"{s.start:%H:%M}", "free": s.free, "total": s.total,
            "hot": s.day in hot, "label": texts.short_day(s.day),
        } for s in studio.group_options()])

    @routes.get("/api/days")
    async def days(request: web.Request):
        fmt = request.query.get("fmt", "")
        if fmt not in ("date", "bday"):
            return _err("bad_format")
        studio = await ctx.load_studio(fmt=fmt)
        first, last = studio.now.date(), studio.horizon_end
        free = await asyncio.to_thread(studio.individual_days, first, last)
        return _ok(days=[d.isoformat() for d in free], first=first.isoformat(), last=last.isoformat())

    @routes.get("/api/times")
    async def times(request: web.Request):
        fmt = request.query.get("fmt", "")
        try:
            day = date.fromisoformat(request.query.get("day", ""))
        except ValueError:
            return _err("bad_day")
        if fmt not in ("date", "bday"):
            return _err("bad_format")
        studio = await ctx.load_studio(fmt=fmt)
        return _ok(times=[f"{t:%H:%M}" for t in studio.individual_times(day)])

    # ---------------- запись ----------------
    @routes.post("/api/book")
    async def book(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        if d.get("website"):  # невидимое поле-ловушка: его заполняют только спам-боты
            return _ok(id="", prepay=0)
        fmt = d.get("fmt")
        name = str(d.get("name", "")).strip()[:100]
        phone = str(d.get("phone", "")).strip()[:30]
        try:
            day = date.fromisoformat(str(d.get("day", "")))
            start = logic.parse_time(d.get("time"))
            people = int(d.get("count", 0))
        except (ValueError, TypeError):
            return _err("bad_data")
        if fmt not in FMT_LABELS or not start or not name or not _phone_ok(phone):
            return _err("bad_data")
        if fmt == "date":
            people = 2
        if fmt == "bday" and not logic.BDAY_MIN <= people <= logic.BDAY_MAX:
            return _err("bad_count")
        if fmt == "group" and people < 1:
            return _err("bad_count")
        size = ""
        if fmt == "group" and people == 1:
            sz = str(d.get("size", ""))
            size = certificate.size_label(int(sz)) if sz.isdigit() and int(sz) in certificate.SIZES else texts.SIZE_LATER
        comment = str(d.get("comment", "")).strip()[:300]
        occasion = str(d.get("occasion", "")).strip()[:100] if fmt == "bday" else ""
        details = "; ".join(x for x in (f"Повод: {occasion}" if occasion else "",
                                        f"Комментарий: {comment}" if comment else "") if x)

        now = ctx.now_local()
        bid = await asyncio.to_thread(ctx.new_booking_id)
        rec = {
            "Создано": now.strftime("%d.%m.%Y %H:%M"), "ID": bid, "Формат": FMT_LABELS[fmt],
            "Дата": f"{day:%d.%m.%Y}", "Время": f"{start:%H:%M}", "Человек": people,
            "Имя": name, "Контакт": phone, "Telegram": "—", "Telegram ID": "",
            "Статус": logic.STATUS_WAIT, "Размер": size, "Источник": texts.SOURCE_SITE,
            "Детали": details, "Для чека": phone,
        }
        async with ctx.LOCK:
            studio = await ctx.load_studio(fresh=True, fmt=fmt)
            ok = studio.group_can_book(day, start, people) if fmt == "group" else studio.individual_ok(day, start)
            if ok:
                await asyncio.to_thread(ctx.sheets.add_booking, rec)
        if not ok:
            return _err("taken", 409)
        when = texts.when(day, start)
        fmt_label = f"Своё событие в студии — {occasion}" if occasion else texts.FMT_LABEL[fmt]
        await ctx.tell_manager(bot, texts.MGR_FROM_SITE + texts.MGR_NEW.format(
            fmt=fmt_label, when=when, people=people, size_line=ctx.size_line(rec),
            name=name, contact=phone + (f"\nКомментарий: {comment}" if comment else ""), tg="—"))
        pay_url = await ctx.booking_pay_url(rec, _return_url(d))
        return _ok(id=bid, when=when, people=people, prepay=logic.PREPAY_PER_PERSON * people,
                   pay_url=pay_url or texts.PAY_URL, auto=bool(pay_url),
                   tg_link=f"https://t.me/{ctx.BOT_USERNAME}?start=b_{bid}" if ctx.BOT_USERNAME else "")

    @routes.post("/api/outcall")
    async def outcall(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        if d.get("website"):
            return _ok(id="")
        name = str(d.get("name", "")).strip()[:100]
        phone = str(d.get("phone", "")).strip()[:30]
        if not name or not _phone_ok(phone):
            return _err("bad_data")
        parts = [(label, str(d.get(key, "")).strip()[:200]) for label, key in
                 (("Где", "where"), ("Повод", "reason"), ("Человек", "count"), ("Возраст", "age"), ("Когда", "when"), ("Комментарий", "comment"))]
        parts = [(k, v) for k, v in parts if v]
        bid = await asyncio.to_thread(ctx.new_booking_id)
        rec = {
            "Создано": ctx.now_local().strftime("%d.%m.%Y %H:%M"), "ID": bid, "Формат": logic.FMT_OUT,
            "Статус": logic.STATUS_NEW, "Имя": name, "Контакт": phone, "Telegram": "—", "Telegram ID": "",
            "Источник": texts.SOURCE_SITE, "Детали": "; ".join(f"{k}: {v}" for k, v in parts),
        }
        await asyncio.to_thread(ctx.sheets.add_booking, rec)
        await ctx.tell_manager(bot, texts.MGR_FROM_SITE + texts.MGR_OUT.format(
            details="\n".join(f"{k}: {v}" for k, v in parts), name=name, contact=phone, tg="—"))
        return _ok(id=bid, tg_link=f"https://t.me/{ctx.BOT_USERNAME}?start=b_{bid}" if ctx.BOT_USERNAME else "")

    async def active_booking(bid: str):
        r = await ctx.find_booking(str(bid or "").strip())
        if not r:
            return None, _err("not_found", 404)
        if logic.norm(r.get("Статус")) in logic.INACTIVE:
            return None, _err("expired", 410)
        return r, None

    @routes.post("/api/paid")
    async def paid(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        r, error = await active_booking(d.get("id"))
        if error:
            return error
        if logic.norm(r.get("Статус")) == logic.norm(logic.STATUS_PAID):
            return _ok(status="paid", **deadline_info(r))
        if r.get("Платёж") and yookassa.enabled():  # ЮKassa: проверяем сами, менеджер не нужен
            if await ctx.check_booking_payment(bot, r["ID"]):
                return _ok(status="paid", **deadline_info(r))
            return _ok(status="pending")
        await ctx.request_prepay_check(bot, r)
        return _ok(status="checking")

    @routes.post("/api/cert")
    async def cert(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        code = str(d.get("code", "")).strip()
        if not code:
            return _err("bad_data")
        r, error = await active_booking(d.get("id"))
        if error:
            return error
        if logic.norm(r.get("Статус")) == logic.norm(logic.STATUS_PAID):
            return _ok(status="paid")
        if not await ctx.apply_certificate(bot, r["ID"], code):
            return _err("expired", 410)
        return _ok(status="paid", **deadline_info(r))

    def deadline_info(r: dict) -> dict:
        b = logic.parse_booking(r)
        if not b.start_dt:
            return {}
        return {"deadline": texts.deadline(b.start_dt, ctx.now_local())}

    @routes.get("/api/status")
    async def status(request: web.Request):
        r = await ctx.find_booking(request.query.get("id", "").strip())
        if not r:
            return _err("not_found", 404)
        if r.get("Платёж") and yookassa.enabled() and logic.norm(r.get("Статус")) != logic.norm(logic.STATUS_PAID):
            await ctx.check_booking_payment(bot, r["ID"])
            r = await ctx.find_booking(r["ID"]) or r
        st = logic.norm(r.get("Статус"))
        if st == logic.norm(logic.STATUS_PAID):
            return _ok(status="paid", **deadline_info(r))
        if st in logic.INACTIVE:
            return _ok(status="expired")
        if r.get("Статус") == texts.STATUS_CHECKING:
            return _ok(status="checking")
        return _ok(status="waiting")

    # ---------------- подарочные сертификаты ----------------
    @routes.post("/api/cert-order")
    async def cert_order(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        if d.get("website"):
            return _ok(order="")
        try:
            size, qty = int(d.get("size", 0)), int(d.get("qty", 0))
        except (TypeError, ValueError):
            return _err("bad_data")
        kind = d.get("kind")
        name = str(d.get("name", "")).strip()[:100]
        phone = str(d.get("phone", "")).strip()[:30]
        email = str(d.get("email", "")).strip()[:100]
        comment = str(d.get("comment", "")).strip()[:300]
        if size not in certificate.SIZES or not 1 <= qty <= 50 or kind not in ("pdf", "print"):
            return _err("bad_data")
        if not name or not _phone_ok(phone):
            return _err("bad_data")
        if email and not mailer.valid(email):
            return _err("bad_email")
        if kind == "pdf" and mailer.enabled() and not email:
            return _err("bad_email")
        rec = await ctx.add_cert_order(size, qty, bool(d.get("separate")) and qty > 1, kind, name, phone,
                                       "—", "", texts.SOURCE_SITE, email, comment, receipt=email or phone)
        order = rec["order"]
        pay_url = await ctx.cert_pay_url(rec, _return_url(d))
        return _ok(order=order, key=ctx.link_key(order), total=certificate.price(size, qty),
                   what=ctx.cert_what(size, qty), type=texts.CERT_TYPE_LABEL[kind],
                   pay_url=pay_url or texts.PAY_URL, auto=bool(pay_url),
                   tg_link=f"https://t.me/{ctx.BOT_USERNAME}?start=c_{order}" if ctx.BOT_USERNAME else "")

    async def cert_by_key(order: str, key: str):
        order, key = str(order or "").strip(), str(key or "").strip()
        if not order or not hmac.compare_digest(key, ctx.link_key(order)):
            return None, _err("not_found", 404)
        r = await ctx.find_cert(order)
        if not r:
            return None, _err("not_found", 404)
        return r, None

    @routes.post("/api/cert-paid")
    async def cert_paid(request: web.Request):
        if _too_many(request):
            return _err("too_many", 429)
        d = await body(request)
        r, error = await cert_by_key(d.get("order"), d.get("key"))
        if error:
            return error
        if logic.norm(r.get("Статус")) == logic.norm(logic.STATUS_PAID):
            return _ok(status="paid")
        if r.get("Платёж") and yookassa.enabled():
            return _ok(status="paid" if await ctx.check_cert_payment(bot, ctx.cert_codes(r)[0]) else "pending")
        await ctx.request_cert_check(bot, r)
        return _ok(status="checking")

    @routes.get("/api/cert-status")
    async def cert_status(request: web.Request):
        order, key = request.query.get("order", ""), request.query.get("key", "")
        r, error = await cert_by_key(order, key)
        if error:
            return error
        if r.get("Платёж") and yookassa.enabled() and logic.norm(r.get("Статус")) != logic.norm(logic.STATUS_PAID):
            await ctx.check_cert_payment(bot, ctx.cert_codes(r)[0])
            r = await ctx.find_cert(order) or r
        st = logic.norm(r.get("Статус"))
        if st == logic.norm(logic.STATUS_PAID):
            kind = ctx.cert_kind(r)
            files = [{"code": c, "url": f"/api/cert-pdf?order={order}&key={key}&code={c}"}
                     for c in ctx.cert_codes(r)] if kind == "pdf" else []
            return _ok(status="paid", kind=kind, files=files, codes=ctx.cert_codes(r),
                       until=r.get("Действует до", ""), email=r.get("Email", ""),
                       email_sent=bool(str(r.get("Отправлено на почту", "")).strip()),
                       how_to_find=texts.HOW_TO_FIND)
        if r.get("Статус") == texts.STATUS_CHECKING:
            return _ok(status="checking")
        return _ok(status="waiting")

    @routes.get("/api/cert-pdf")
    async def cert_pdf(request: web.Request):
        r, error = await cert_by_key(request.query.get("order"), request.query.get("key"))
        if error:
            return error
        code = request.query.get("code", "")
        if logic.norm(r.get("Статус")) != logic.norm(logic.STATUS_PAID) or code not in ctx.cert_codes(r):
            return _err("not_found", 404)
        files = await asyncio.to_thread(ctx.cert_pdfs, r)
        name, data = next((f for f in files if code in f[0]), files[0])
        return web.Response(body=data, content_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="certificate_{code}.pdf"'})

    app = web.Application(middlewares=[cors], client_max_size=64 * 1024)
    app.add_routes(routes)
    return app


async def start(bot, ctx):
    port = int(os.environ.get("PORT", "3000"))
    runner = web.AppRunner(build_app(bot, ctx))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Вход для сайта запущен на порту %s", port)
    return runner
