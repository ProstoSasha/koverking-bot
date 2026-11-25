import asyncio
import hashlib
import hmac
import logging
import random
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

import certificate
import config
import logic
import mailer
import texts
import web_api
import yookassa
from sheets import Sheets

log = logging.getLogger("bot")
ASSETS = Path(__file__).parent / "assets"
PRICE_FILES = {
    "group": "price_group.png",
    "date": "price_date.png",
    "out": "price_out.png",
    "bday": "price_group.png",  # для дня рождения цены те же, что для группового
}
FORMATS = ("group", "date", "out", "bday")
FMT_SHEET = {"group": logic.FMT_GROUP, "date": logic.FMT_DATE, "bday": logic.FMT_BDAY, "out": logic.FMT_OUT}
SHEET_FMT = {v: k for k, v in FMT_SHEET.items()}

CERT_WAIT = "Ждёт оплаты"
CERT_SOURCE_TG = "Telegram"
ID_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

sheets = Sheets(config.CREDS_FILE, config.SHEET_ID)
LOCK = asyncio.Lock()  # чтобы двое не заняли одно место одновременно
router = Router()
BOT_USERNAME = ""      # узнаём при запуске
_welcome_photo_id = None


class Book(StatesGroup):
    count = State()
    name = State()
    contact = State()
    out_where = State()
    out_reason = State()
    out_count = State()
    out_age = State()
    cert_code = State()
    receipt = State()


class Cert(StatesGroup):
    qty = State()
    contact = State()
    receipt = State()


class Review(StatesGroup):
    text = State()


# ---------------------------------------------------------------- helpers
def now_local() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None)


async def load_studio(fresh: bool = False, fmt: str | None = None) -> logic.Studio:
    bookings = await asyncio.to_thread(sheets.bookings, fresh)
    groups = await asyncio.to_thread(sheets.groups, fresh)
    closed = await asyncio.to_thread(sheets.closed, fresh)
    now = now_local()
    horizon = logic.bday_horizon_end(now) if fmt == "bday" else None
    return logic.Studio(bookings, groups, closed, now, horizon_end=horizon)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


MENU_ROW = [btn(texts.BTN_MENU, "menu")]
MANAGER_ROW = [url_btn(texts.BTN_MANAGER, f"https://t.me/{config.MANAGER_USERNAME}")]


def menu_kb():
    rows = [[btn(texts.BTN_BOOK, "book")]]
    new_year = logic.is_new_year_season(now_local().date())
    if new_year:
        rows.append([btn(texts.BTN_CERT_NEW_YEAR, "cert")])
    rows += [[btn(texts.BTN_SKETCHES, "sketches")], [btn(texts.BTN_PRICE, "price")]]
    if not new_year:
        rows.append([btn(texts.BTN_CERT, "cert")])
    rows.append(MANAGER_ROW)
    return kb(rows)


async def send_welcome(bot: Bot, chat_id: int):
    """Приветствие с картинкой assets/welcome.jpg (если её нет — просто текст)."""
    global _welcome_photo_id
    text = texts.welcome_text(logic.is_new_year_season(now_local().date()))
    path = ASSETS / "welcome.jpg"
    if _welcome_photo_id or path.exists():
        try:
            msg = await bot.send_photo(chat_id, _welcome_photo_id or FSInputFile(path), caption=text,
                                       reply_markup=menu_kb())
            _welcome_photo_id = msg.photo[-1].file_id
            return
        except Exception:
            log.exception("Не получилось отправить картинку приветствия")
            _welcome_photo_id = None
    await bot.send_message(chat_id, text, reply_markup=menu_kb())


async def tell_manager(bot: Bot, text: str, markup=None):
    try:
        await bot.send_message(config.MANAGER_ID, text, reply_markup=markup)
    except Exception:
        log.exception("Не получилось написать менеджеру. Менеджер нажимал /start у бота?")


async def tell_client(bot: Bot, tg_id: str, text: str, markup=None) -> bool:
    if not str(tg_id or "").isdigit():
        return False
    try:
        await bot.send_message(int(tg_id), text, reply_markup=markup)
        return True
    except Exception:
        log.warning("Не получилось написать клиенту %s", tg_id)
        return False


async def restart(msg: Message):
    await msg.answer(texts.RESTART, reply_markup=menu_kb())


def tg_of(user) -> str:
    return f"@{user.username}" if user.username else "—"


def note_interest(user, what: str):
    """Отмечаем в листе «Интерес», что человек что-то смотрел (для напоминания тем, кто не записался)."""
    if user.id == config.MANAGER_ID:
        return

    async def run():
        try:
            await asyncio.to_thread(sheets.upsert_lead, str(user.id), user.full_name or "", tg_of(user), what,
                                    now_local().strftime("%d.%m.%Y %H:%M"))
        except Exception:
            log.warning("Не получилось записать интерес %s", user.id)

    asyncio.create_task(run())


def new_booking_id() -> str:
    taken = {str(r.get("ID", "")).strip() for r in sheets.bookings(False)}
    while True:
        bid = "".join(random.choices(ID_CHARS, k=6))
        if bid not in taken:
            return bid


async def find_booking(bid: str) -> dict | None:
    if not bid:
        return None
    rows = await asyncio.to_thread(sheets.bookings, True)
    return next((r for r in rows if str(r.get("ID", "")).strip() == bid), None)


def size_line(rec: dict) -> str:
    size = str(rec.get("Размер", "")).strip()
    return texts.SIZE_LINE.format(size=size) if size else ""


def prepay(people: int) -> int:
    return logic.PREPAY_PER_PERSON * max(int(people or 1), 1)


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def rec_when(rec: dict) -> str:
    b = logic.parse_booking(rec)
    return texts.when(b.day, b.start) if b.day and b.start else "—"


def rec_fmt_label(rec: dict) -> str:
    return texts.FMT_LABEL.get(SHEET_FMT.get(str(rec.get("Формат", "")).strip(), ""), rec.get("Формат", ""))


def pay_choice_kb(bid: str, url: str = ""):
    return kb([
        [url_btn(texts.BTN_PREPAY, url or texts.PAY_URL)],
        [btn(texts.BTN_PREPAID, f"bpaid:{bid}")],
        [btn(texts.BTN_HAVE_CERT, f"bcert:{bid}")],
    ])


def link_key(value: str) -> str:
    """Подпись для ссылок с сайта (скачать сертификат и т.п.), чтобы чужие номера нельзя было подобрать."""
    return hmac.new(config.BOT_TOKEN.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- commands
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot, command: CommandObject):
    await state.clear()
    arg = (command.args or "").strip()
    if arg == "book":
        await ask_format(message)
    elif arg.startswith("b_"):
        await link_site_booking(bot, message, arg[2:])
    elif arg.startswith("c_"):
        await link_site_cert(bot, message, arg[2:])
    else:
        await send_welcome(bot, message.chat.id)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await send_welcome(bot, message.chat.id)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=menu_kb())


@router.message(Command("list"))
async def cmd_list(message: Message):
    if message.from_user.id != config.MANAGER_ID:
        return
    studio = await load_studio(fresh=True)
    now = studio.now
    items = sorted(
        (b for b in studio.bookings
         if b.active and b.fmt in logic.STUDIO_FORMATS and b.start_dt and b.start_dt >= now - timedelta(hours=4)),
        key=lambda b: b.start_dt,
    )
    lines = ["Ближайшие записи:"]
    lines += [
        f"{texts.when(b.day, b.start)} · {b.fmt} · {b.name} · {b.people} чел. · {b.status}" for b in items
    ] or ["пока пусто"]
    slots = [s for s in sorted(studio.group_slots.values(), key=lambda s: s.start_dt)
             if s.start_dt >= now and s.taken > 0]
    if slots:
        lines += ["", "Групповые:"]
        lines += [
            f"{texts.when(s.day, s.start)} · занято {s.taken} из {s.total}"
            + (" · набираем" if s.taken < logic.GROUP_MIN else "")
            for s in slots
        ]
    await message.answer("\n".join(lines)[:4000])


# ---------------------------------------------------------------- menu buttons
@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await send_welcome(bot, cb.message.chat.id)
    await cb.answer()


@router.callback_query(F.data == "sketches")
async def cb_sketches(cb: CallbackQuery):
    note_interest(cb.from_user, "эскизы")
    await cb.message.answer(texts.SKETCH_INSTRUCTION)
    await cb.message.answer(texts.SKETCH_OWN, reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))
    await cb.answer()


@router.callback_query(F.data == "price")
async def cb_price(cb: CallbackQuery):
    rows = [[btn(texts.BTN_FMT[c], f"pr:{c}")] for c in FORMATS] + [MENU_ROW]
    await cb.message.answer(texts.PRICE_ASK, reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("pr:"))
async def cb_price_show(cb: CallbackQuery):
    code = cb.data[3:]
    note_interest(cb.from_user, f"цены: {texts.FMT_LABEL.get(code, code)}")
    after = kb([[btn(texts.BTN_BOOK, f"fmt:{code}" if code in FORMATS else "book")],
                [btn(texts.BTN_DATE_PRICES, "price")], MENU_ROW])
    path = ASSETS / PRICE_FILES.get(code, "missing")
    if code in PRICE_FILES and path.exists():
        await cb.message.answer_photo(FSInputFile(path), caption=texts.PRICE_CAPTION[code], reply_markup=after)
    else:
        await cb.message.answer(texts.PRICE_MISSING, reply_markup=after)
    await cb.answer()


# ---------------------------------------------------------------- booking: format
async def ask_format(msg: Message):
    rows = [[btn(texts.BTN_FMT[c], f"fmt:{c}")] for c in FORMATS] + [MENU_ROW]
    await msg.answer(texts.ASK_FORMAT, reply_markup=kb(rows))


@router.callback_query(F.data == "book")
async def cb_book(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ask_format(cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("fmt:"))
async def cb_format(cb: CallbackQuery, state: FSMContext):
    code = cb.data[4:]
    await state.clear()
    await state.update_data(fmt=code)
    note_interest(cb.from_user, f"запись: {texts.FMT_LABEL.get(code, code)}")
    if code == "out":
        await ask_out_where(cb.message, state)
    elif code == "group":
        await show_group_slots(cb.message)
    elif code == "bday":
        await ask_bday_count(cb.message, state)  # для дня рождения сначала узнаём, сколько гостей
    else:
        await show_dates(cb.message, 0)
    await cb.answer()


# ---------------------------------------------------------------- booking: group
async def show_group_slots(msg: Message):
    studio = await load_studio()
    slots = studio.group_options()
    if not slots:
        await msg.answer(texts.NO_SLOTS, reply_markup=kb([MANAGER_ROW, MENU_ROW]))
        return
    hot = logic.nearest_weekend(studio.now.date())
    rows = [
        [btn(texts.SLOT_LABEL.format(hot=texts.SLOT_HOT if s.day in hot else "", day=texts.short_day(s.day),
                                     time=f"{s.start:%H:%M}", free=s.free, total=s.total),
             f"g:{s.day.isoformat()}|{s.start:%H:%M}")]
        for s in slots[:90]
    ]
    rows.append([btn(texts.BTN_DATE_INSTEAD, "grp2date")])
    await msg.answer(texts.GROUP_DATES, reply_markup=kb(rows + [MENU_ROW]))


@router.callback_query(F.data == "grp2date")
async def cb_group_to_date(cb: CallbackQuery):
    """С группового: хотят один коврик на двоих — показываем цены МК-свидания и предлагаем записаться."""
    markup = kb([[btn(texts.BTN_BOOK_DATE, "fmt:date")], [btn(texts.BTN_BACK_GROUP, "fmt:group")], MENU_ROW])
    path = ASSETS / PRICE_FILES["date"]
    if path.exists():
        await cb.message.answer_photo(FSInputFile(path), caption=texts.DATE_OFFER_CAPTION, reply_markup=markup)
    else:
        await cb.message.answer(texts.PRICE_MISSING, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("g:"))
async def cb_group_slot(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("fmt") != "group":
        await restart(cb.message)
        await cb.answer()
        return
    iso, tm = cb.data[2:].split("|")
    day, start = date.fromisoformat(iso), logic.parse_time(tm)
    slot = (await load_studio()).group_slot(day, start)
    if not slot:
        await cb.message.answer(texts.SLOT_TAKEN)
        await show_group_slots(cb.message)
        await cb.answer()
        return
    await state.update_data(day=iso, time=tm, free=slot.free)
    nums = [btn(str(n), f"gn:{n}") for n in range(1, slot.free + 1)]
    await cb.message.answer(texts.ASK_COUNT_GROUP.format(free=slot.free), reply_markup=kb(chunk(nums, 6) + [MENU_ROW]))
    await cb.answer()


@router.callback_query(F.data.startswith("gn:"))
async def cb_group_count(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "day" not in data:
        await restart(cb.message)
        await cb.answer()
        return
    n = int(cb.data[3:])
    await state.update_data(count=n)
    if n == 1 and data.get("free", 0) >= 2:
        rows = [[btn(texts.BTN_FRIEND_YES, "gf:yes")], [btn(texts.BTN_FRIEND_NO, "gf:no")]]
        await cb.message.answer(texts.FRIEND, reply_markup=kb(rows))
    elif n == 1:
        await ask_size(cb.message)
    else:
        await ask_name(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("gf:"))
async def cb_friend(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "day" not in data:
        await restart(cb.message)
        await cb.answer()
        return
    if cb.data == "gf:yes":
        await state.update_data(count=2)
        await ask_name(cb.message, state)
    else:
        await ask_size(cb.message)
    await cb.answer()


async def ask_size(msg: Message):
    """Размер спрашиваем, только если человек приходит один: менеджеру так проще подготовиться."""
    rows = [[btn(texts.BTN_SIZE.format(label=certificate.size_label(s), price=rub(certificate.price(s, 1))),
                 f"sz:{s}")] for s in certificate.SIZES]
    rows += [[btn(texts.SIZE_LATER, "sz:later")], MENU_ROW]
    await msg.answer(texts.ASK_SIZE, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("sz:"))
async def cb_size(cb: CallbackQuery, state: FSMContext):
    if "day" not in await state.get_data():
        await restart(cb.message)
        await cb.answer()
        return
    v = cb.data[3:]
    size = certificate.size_label(int(v)) if v.isdigit() else texts.SIZE_LATER
    await state.update_data(size=size)
    await ask_name(cb.message, state)
    await cb.answer()


# ---------------------------------------------------------------- booking: date (6 недель, по 2 недели)
async def show_dates(msg: Message, page: int):
    studio = await load_studio()
    first_all, last_all = studio.now.date(), studio.horizon_end
    if not studio.individual_days(first_all, last_all):
        await msg.answer(texts.NO_SLOTS, reply_markup=kb([MANAGER_ROW, MENU_ROW]))
        return
    first = first_all + timedelta(days=14 * page)
    last = min(first + timedelta(days=13), last_all)
    days = studio.individual_days(first, last)
    rows = chunk([btn(texts.short_day(d), f"d:{d.isoformat()}") for d in days], 4)
    nav = []
    if page > 0:
        nav.append(btn("◀️ Раньше", f"dp:{page - 1}"))
    if first + timedelta(days=14) <= last_all:
        nav.append(btn("Позже ▶️", f"dp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(MENU_ROW)
    await msg.answer(texts.PICK_DATE if days else texts.NO_DATES_PAGE, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("dp:"))
async def cb_dates_page(cb: CallbackQuery, state: FSMContext):
    if (await state.get_data()).get("fmt") != "date":
        await restart(cb.message)
    else:
        await show_dates(cb.message, int(cb.data[3:]))
    await cb.answer()


# ---------------------------------------------------------------- booking: birthday (месяц → неделя → день)
async def show_bday_months(msg: Message):
    studio = await load_studio(fmt="bday")
    months = logic.bday_months(studio)
    if not months:
        await msg.answer(texts.NO_SLOTS, reply_markup=kb([MANAGER_ROW, MENU_ROW]))
        return
    rows = chunk([btn(texts.MONTHS[m - 1], f"bm:{y}-{m}") for y, m in months], 2) + [MENU_ROW]
    await msg.answer(texts.PICK_MONTH, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("bm:"))
async def cb_bday_month(cb: CallbackQuery, state: FSMContext):
    if (await state.get_data()).get("fmt") != "bday":
        await restart(cb.message)
        await cb.answer()
        return
    if cb.data == "bm:back":
        await show_bday_months(cb.message)
        await cb.answer()
        return
    y, m = (int(x) for x in cb.data[3:].split("-"))
    await state.update_data(month=f"{y}-{m}")
    weeks = logic.bday_weeks(await load_studio(fmt="bday"), y, m)
    if not weeks:
        await cb.message.answer(texts.SLOT_TAKEN)
        await show_bday_months(cb.message)
        await cb.answer()
        return
    rows = [[btn(texts.WEEK_LABEL.format(a=a, b=b), f"bw:{a.isoformat()}|{b.isoformat()}")] for a, b in weeks]
    rows += [[btn(texts.BTN_OTHER_MONTH, "bm:back")], MENU_ROW]
    await cb.message.answer(texts.PICK_WEEK.format(month=texts.MONTHS[m - 1]), reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("bw:"))
async def cb_bday_week(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("fmt") != "bday":
        await restart(cb.message)
        await cb.answer()
        return
    a, b = (date.fromisoformat(x) for x in cb.data[3:].split("|"))
    days = (await load_studio(fmt="bday")).individual_days(a, b)
    if not days:
        await cb.message.answer(texts.SLOT_TAKEN)
        await show_bday_months(cb.message)
        await cb.answer()
        return
    rows = chunk([btn(texts.short_day(d), f"d:{d.isoformat()}") for d in days], 4)
    rows += [[btn(texts.BTN_OTHER_WEEK, f"bm:{data.get('month', f'{a.year}-{a.month}')}")], MENU_ROW]
    await cb.message.answer(texts.PICK_DAY, reply_markup=kb(rows))
    await cb.answer()


# ---------------------------------------------------------------- booking: day and time (свидание и день рождения)
@router.callback_query(F.data.startswith("d:"))
async def cb_day(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fmt = data.get("fmt")
    if fmt not in ("date", "bday"):
        await restart(cb.message)
        await cb.answer()
        return
    day = date.fromisoformat(cb.data[2:])
    times = (await load_studio(fmt=fmt)).individual_times(day)
    if not times:
        await cb.message.answer(texts.SLOT_TAKEN)
        if fmt == "bday":
            await show_bday_months(cb.message)
        else:
            await show_dates(cb.message, 0)
        await cb.answer()
        return
    await state.update_data(day=day.isoformat())
    rows = chunk([btn(f"{t:%H:%M}", f"t:{t:%H:%M}") for t in times], 4)
    back = btn(texts.BTN_OTHER_MONTH, f"bm:{data['month']}") if fmt == "bday" and data.get("month") \
        else btn("◀️ Другая дата", "dp:0")
    rows += [[back], MENU_ROW]
    await cb.message.answer(texts.PICK_TIME.format(day=texts.short_day(day)), reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_time(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fmt = data.get("fmt")
    if fmt not in ("date", "bday") or "day" not in data:
        await restart(cb.message)
        await cb.answer()
        return
    tm = cb.data[2:]
    day, start = date.fromisoformat(data["day"]), logic.parse_time(tm)
    if not (await load_studio(fmt=fmt)).individual_ok(day, start):
        await cb.message.answer(texts.SLOT_TAKEN)
        if fmt == "bday":
            await show_bday_months(cb.message)
        else:
            await show_dates(cb.message, 0)
        await cb.answer()
        return
    await state.update_data(time=tm)
    if fmt == "date":
        await state.update_data(count=2)
    await ask_name(cb.message, state)  # для дня рождения число гостей уже знаем
    await cb.answer()


# ---------------------------------------------------------------- booking: birthday guests count
async def ask_bday_count(msg: Message, state: FSMContext):
    await state.set_state(Book.count)
    nums = [btn(str(n), f"bn:{n}") for n in range(logic.BDAY_MIN, logic.BDAY_MAX + 1)]
    rows = chunk(nums, 3) + [[btn(texts.BTN_BDAY_MORE, "bn:more")], MENU_ROW]
    await msg.answer(texts.ASK_COUNT_BDAY, reply_markup=kb(rows))


async def got_bday_count(msg: Message, state: FSMContext, n: int | None):
    """n = None — «больше 8» без точного числа."""
    await state.set_state(None)
    if n is None or n > logic.BDAY_MAX:
        await state.update_data(bday_guests=n)
        rows = [
            [btn(texts.BTN_BDAY_OUT, "bdout")],
            [btn(texts.BTN_OUT_PRICE, "pr:out")],
            [btn(texts.BTN_BDAY_LESS, "fmt:bday")],
            MANAGER_ROW,
            MENU_ROW,
        ]
        await msg.answer(texts.BDAY_TOO_MANY, reply_markup=kb(rows))
        return
    await state.update_data(count=n)
    if n > logic.BDAY_COMFORT:
        await msg.answer(texts.BDAY_CROWDED.format(n=n))
    await show_bday_months(msg)


@router.callback_query(Book.count, F.data.startswith("bn:"))
async def cb_bday_count(cb: CallbackQuery, state: FSMContext):
    value = cb.data[3:]
    await got_bday_count(cb.message, state, None if value == "more" else int(value))
    await cb.answer()


@router.message(Book.count, F.text)
async def msg_bday_count(message: Message, state: FSMContext):
    t = message.text.strip()
    if not t.isdigit() or int(t) < logic.BDAY_MIN:
        await message.answer(texts.BAD_COUNT_BDAY)
        return
    await got_bday_count(message, state, int(t))


@router.callback_query(F.data == "bdout")
async def cb_bday_to_outcall(cb: CallbackQuery, state: FSMContext):
    """Гостей больше, чем помещается: переводим на заявку на выездной МК, число гостей уже знаем."""
    guests = (await state.get_data()).get("bday_guests")
    await state.clear()
    await state.update_data(fmt="out")
    if guests:
        await state.update_data(out_count=str(guests))
    await ask_out_where(cb.message, state)
    await cb.answer()


# ---------------------------------------------------------------- booking: outcall
async def ask_out_where(msg: Message, state: FSMContext):
    await state.set_state(Book.out_where)
    rows = chunk([btn(v, f"ow:{k}") for k, v in texts.OUT_PLACES.items()], 2)
    rows += [[btn(texts.OUT_PLACE_OTHER, "ow:other")], MENU_ROW]
    await msg.answer(texts.OUT_START, reply_markup=kb(rows))


@router.callback_query(Book.out_where, F.data.startswith("ow:"))
async def cb_out_where(cb: CallbackQuery, state: FSMContext):
    key = cb.data[3:]
    if key == "other":
        await cb.message.answer(texts.OUT_PLACE_TYPE)
    else:
        await got_out_where(cb.message, state, texts.OUT_PLACES.get(key, key))
    await cb.answer()


@router.message(Book.out_where, F.text)
async def msg_out_where(message: Message, state: FSMContext):
    await got_out_where(message, state, message.text.strip()[:200])


async def got_out_where(msg: Message, state: FSMContext, where: str):
    await state.update_data(out_where=where)
    await state.set_state(Book.out_reason)
    await msg.answer(texts.OUT_REASON)


@router.message(Book.out_reason, F.text)
async def msg_out_reason(message: Message, state: FSMContext):
    await state.update_data(out_reason=message.text.strip()[:200])
    if (await state.get_data()).get("out_count"):  # уже узнали, когда считали гостей на день рождения
        await state.set_state(Book.out_age)
        await message.answer(texts.OUT_AGE)
        return
    await state.set_state(Book.out_count)
    await message.answer(texts.OUT_COUNT)


@router.message(Book.out_count, F.text)
async def msg_out_count(message: Message, state: FSMContext):
    await state.update_data(out_count=message.text.strip()[:100])
    await state.set_state(Book.out_age)
    await message.answer(texts.OUT_AGE)


@router.message(Book.out_age, F.text)
async def msg_out_age(message: Message, state: FSMContext):
    await state.update_data(out_age=message.text.strip()[:100])
    await ask_name(message, state)


# ---------------------------------------------------------------- booking: name, contact, finish
async def ask_name(msg: Message, state: FSMContext):
    await state.set_state(Book.name)
    await msg.answer(texts.ASK_NAME)


@router.message(Book.name, F.text)
async def msg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(Book.contact)
    markup = None
    if message.from_user.username:
        markup = kb([[btn(texts.BTN_USE_USERNAME.format(username="@" + message.from_user.username), "useun")]])
    await message.answer(texts.ASK_CONTACT, reply_markup=markup)


@router.callback_query(Book.contact, F.data == "useun")
async def cb_use_username(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.update_data(contact="@" + (cb.from_user.username or ""))
    await cb.answer()
    await finish(bot, cb.from_user, cb.message.chat.id, state)


def contact_ok(text: str) -> bool:
    digits = len(re.sub(r"\D", "", text))
    return text.startswith("@") or "t.me/" in text or digits >= 5


@router.message(Book.contact, F.text)
async def msg_contact(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    if not contact_ok(text):
        await message.answer(texts.BAD_CONTACT)
        return
    await state.update_data(contact=text[:100])
    await finish(bot, message.from_user, message.chat.id, state)


async def finish(bot: Bot, user, chat_id: int, state: FSMContext):
    data = await state.get_data()
    fmt = data.get("fmt")
    if not fmt or "name" not in data or "contact" not in data:
        await state.clear()
        await bot.send_message(chat_id, texts.RESTART, reply_markup=menu_kb())
        return
    if fmt != "out" and not data.get("receipt") and need_receipt_contact(data["contact"]):
        await state.set_state(Book.receipt)  # для чека ЮKassa нужен телефон или почта
        await bot.send_message(chat_id, texts.ASK_RECEIPT)
        return
    now = now_local()
    tg = tg_of(user)
    base = {
        "Создано": now.strftime("%d.%m.%Y %H:%M"),
        "Имя": data["name"],
        "Контакт": data["contact"],
        "Telegram": tg,
        "Telegram ID": str(user.id),
        "Источник": "Telegram",
    }

    if fmt == "out":
        parts = [
            ("Где", data.get("out_where", "")),
            ("Повод", data.get("out_reason", "")),
            ("Человек", data.get("out_count", "")),
            ("Возраст", data.get("out_age", "")),
        ]
        rec = {**base, "ID": await asyncio.to_thread(new_booking_id), "Формат": logic.FMT_OUT,
               "Статус": logic.STATUS_NEW, "Детали": "; ".join(f"{k}: {v}" for k, v in parts)}
        await asyncio.to_thread(sheets.add_booking, rec)
        await state.clear()
        await bot.send_message(chat_id, texts.OUT_DONE, reply_markup=kb([MENU_ROW]))
        await tell_manager(bot, texts.MGR_OUT.format(
            details="\n".join(f"{k}: {v}" for k, v in parts), name=data["name"], contact=data["contact"], tg=tg))
        return

    day, start = date.fromisoformat(data["day"]), logic.parse_time(data["time"])
    people = int(data["count"])
    rec = {**base, "Формат": FMT_SHEET[fmt], "Дата": f"{day:%d.%m.%Y}", "Время": f"{start:%H:%M}",
           "Человек": people, "Статус": logic.STATUS_WAIT, "Для чека": data.get("receipt", ""),
           "Размер": data.get("size", "") if fmt == "group" and people == 1 else ""}
    async with LOCK:
        studio = await load_studio(fresh=True, fmt=fmt)
        ok = studio.group_can_book(day, start, people) if fmt == "group" else studio.individual_ok(day, start)
        if ok:
            rec["ID"] = await asyncio.to_thread(new_booking_id)
            await asyncio.to_thread(sheets.add_booking, rec)
    await state.clear()
    if not ok:
        await bot.send_message(chat_id, texts.SLOT_TAKEN, reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))
        return
    await send_pay_offer(bot, chat_id, rec)
    await tell_manager(bot, texts.MGR_NEW.format(
        fmt=texts.FMT_LABEL[fmt], when=texts.when(day, start), people=people, size_line=size_line(rec),
        name=data["name"], contact=data["contact"], tg=tg))


@router.message(Book.receipt, F.text)
async def msg_receipt(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()[:100]
    if not yookassa.customer(text, text):
        await message.answer(texts.BAD_RECEIPT)
        return
    await state.update_data(receipt=text)
    await finish(bot, message.from_user, message.chat.id, state)


async def send_pay_offer(bot: Bot, chat_id: int, rec: dict):
    people = logic.to_int(rec.get("Человек"), 1)
    await bot.send_message(chat_id, texts.CONFIRM.format(
        fmt=rec_fmt_label(rec), when=rec_when(rec), people=people, prepay=rub(prepay(people))),
        reply_markup=await pay_kb(rec))


# ---------------------------------------------------------------- ЮKassa: платёж на каждую запись и сертификат
PAY_LOCK = asyncio.Lock()  # чтобы одну оплату не подтвердили дважды (фоновая проверка + кнопка + сайт)


def default_return_url() -> str:
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://koverking39.ru"


def need_receipt_contact(*contacts: str) -> bool:
    """Чеки включены, а телефона или почты для чека нет — надо спросить."""
    return yookassa.enabled() and yookassa.RECEIPTS and not any(yookassa.customer(c, c) for c in contacts if c)


async def _payment_url(rec: dict, sheet: str, amount: int, description: str, item: str, mode: str,
                       subject: str, metadata: dict, return_url: str = "") -> str:
    """Ссылка на оплату именно этой записи/покупки. Пусто — ЮKassa не подключена или не ответила
    (тогда используется общая ссылка, а оплату подтверждает менеджер)."""
    if not yookassa.enabled() or amount <= 0:
        return ""
    if rec.get("Платёж") and rec.get("Ссылка оплаты"):
        return rec["Ссылка оплаты"]
    buyer = {}
    for c in (rec.get("Для чека", ""), rec.get("Email", ""), rec.get("Контакт", "")):
        buyer = buyer or yookassa.customer(c, c)
    try:
        p = await yookassa.create(amount, description, return_url or default_return_url(), metadata, item, mode, subject,
                                  buyer)
    except Exception:
        log.exception("ЮKassa: не получилось создать платёж")
        return ""
    row = rec.get("_row")
    if not row:
        found = await (find_booking(rec.get("ID", "")) if sheet == "Записи" else find_cert(cert_codes(rec)[0]))
        row = found and found["_row"]
    if row:
        await asyncio.to_thread(sheets.set_cell, row, "Платёж", p["id"], sheet)
        await asyncio.to_thread(sheets.set_cell, row, "Ссылка оплаты", p["url"], sheet)
    rec.update({"Платёж": p["id"], "Ссылка оплаты": p["url"]})
    return p["url"]


async def booking_pay_url(rec: dict, return_url: str = "") -> str:
    people = logic.to_int(rec.get("Человек"), 1)
    return await _payment_url(
        rec, "Записи", prepay(people), f"Предоплата: {rec_fmt_label(rec)}, {rec_when(rec)}, {people} чел.",
        texts.RECEIPT_PREPAY, "full_prepayment", "service", {"kind": "booking", "id": rec.get("ID", "")}, return_url)


async def cert_pay_url(rec: dict, return_url: str = "") -> str:
    qty = logic.to_int(rec.get("Ковриков"), 1)
    return await _payment_url(
        rec, "Сертификаты", logic.to_int(rec.get("Сумма")), f"Подарочный сертификат {cert_what(cert_size(rec), qty)}",
        texts.RECEIPT_CERT, "full_prepayment", "service", {"kind": "cert", "order": cert_codes(rec)[0]}, return_url)


async def pay_kb(rec: dict):
    return pay_choice_kb(rec["ID"], await booking_pay_url(rec))


async def _payment_state(rec: dict, sheet: str) -> str:
    """Спрашиваем ЮKassa. Отменённый/просроченный платёж забываем — в следующий раз создастся новый."""
    pid = str(rec.get("Платёж", "")).strip()
    if not pid or not yookassa.enabled():
        return ""
    try:
        state = await yookassa.status(pid)
    except Exception:
        log.warning("ЮKassa: не получилось проверить платёж %s", pid)
        return ""
    if state == yookassa.CANCELED:
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Платёж", "", sheet)
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Ссылка оплаты", "", sheet)
    return state


async def check_booking_payment(bot: Bot, bid: str) -> bool:
    """True — запись оплачена (сейчас или раньше)."""
    async with PAY_LOCK:
        rec = await find_booking(bid)
        if not rec:
            return False
        status = logic.norm(rec.get("Статус"))
        if status == logic.norm(logic.STATUS_PAID):
            return True
        if status in logic.INACTIVE and status != logic.norm(logic.STATUS_EXPIRED):
            return False
        if await _payment_state(rec, "Записи") != yookassa.PAID:
            return False
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_PAID)
    people = logic.to_int(rec.get("Человек"), 1)
    head = texts.MGR_FROM_SITE if rec.get("Источник") == texts.SOURCE_SITE else ""
    note = head + texts.MGR_AUTO_PAID.format(
        fmt=rec_fmt_label(rec), when=rec_when(rec), people=people, prepay=rub(prepay(people)),
        name=rec.get("Имя", ""), contact=rec.get("Контакт", ""), tg=rec.get("Telegram", "—") or "—")
    if status == logic.norm(logic.STATUS_EXPIRED):
        note += "\n\n⚠️ Бронь успела сгореть до оплаты — проверь, что место ещё свободно."
    if not await send_confirmed(bot, rec):
        note += texts.MGR_SITE_NO_TG.format(contact=rec.get("Контакт", ""))
    await tell_manager(bot, note)
    return True


async def check_cert_payment(bot: Bot, order: str) -> bool:
    async with PAY_LOCK:
        rec = await find_cert(order)
        if not rec:
            return False
        if logic.norm(rec.get("Статус")) == logic.norm(logic.STATUS_PAID):
            return True
        if await _payment_state(rec, "Сертификаты") != yookassa.PAID:
            return False
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_PAID, "Сертификаты")
    qty = logic.to_int(rec.get("Ковриков"), 1)
    await tell_manager(bot, texts.MGR_AUTO_CERT.format(codes=", ".join(cert_codes(rec)), what=cert_what(cert_size(rec), qty),
                                                      total=rub(logic.to_int(rec.get("Сумма")))))
    await deliver_cert(bot, rec)
    return True


async def check_payments(bot: Bot):
    """Фоновая проверка: все неоплаченные записи и сертификаты, у которых есть платёж в ЮKassa."""
    if not yookassa.enabled():
        return
    now = now_local()
    for r in await asyncio.to_thread(sheets.bookings, True):
        created = logic.parse_created(r.get("Создано"))
        if r.get("Платёж") and logic.norm(r.get("Статус")) != logic.norm(logic.STATUS_PAID) \
                and created and now - created < timedelta(days=3):
            await check_booking_payment(bot, str(r.get("ID", "")))
    for r in await asyncio.to_thread(sheets.certs, True):
        created = logic.parse_created(r.get("Создано"))
        if r.get("Платёж") and logic.norm(r.get("Статус")) != logic.norm(logic.STATUS_PAID) \
                and created and now - created < timedelta(days=3) and cert_codes(r):
            await check_cert_payment(bot, cert_codes(r)[0])


async def payments_loop(bot: Bot):
    while True:
        try:
            await check_payments(bot)
        except Exception:
            log.exception("Ошибка проверки оплат ЮKassa")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- prepayment and certificate at booking
async def request_prepay_check(bot: Bot, rec: dict):
    """Клиент нажал «Я внёс(ла) предоплату» (в боте или на сайте): просим менеджера проверить."""
    bid = rec["ID"]
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", texts.STATUS_CHECKING)
    people = logic.to_int(rec.get("Человек"), 1)
    head = texts.MGR_FROM_SITE if rec.get("Источник") == texts.SOURCE_SITE else ""
    await tell_manager(bot, head + texts.MGR_PREPAY.format(
        fmt=rec_fmt_label(rec), when=rec_when(rec), people=people, prepay=rub(prepay(people)),
        name=rec.get("Имя", ""), contact=rec.get("Контакт", ""), tg=rec.get("Telegram", "—") or "—"),
        markup=kb([[btn(texts.BTN_MGR_PREPAY_OK, f"bok:{bid}")], [btn(texts.BTN_MGR_PREPAY_NO, f"bno:{bid}")]]))


async def send_confirmed(bot: Bot, rec: dict) -> bool:
    """Подтверждение, эскизы и срок выбора эскиза. False — если у клиента нет Telegram (записался на сайте)."""
    tg_id = str(rec.get("Telegram ID", "")).strip()
    if not tg_id.isdigit():
        return False
    b = logic.parse_booking(rec)
    try:
        await bot.send_message(int(tg_id), texts.BOOKING_CONFIRMED.format(when=rec_when(rec)))
        await bot.send_message(int(tg_id), texts.SKETCH_INSTRUCTION)
        if b.start_dt:
            await bot.send_message(int(tg_id), texts.deadline(b.start_dt, now_local()))
        share = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text={texts.SHARE_TEXT}" if BOT_USERNAME else ""
        rows = ([[url_btn(texts.BTN_SHARE, share)]] if share else []) + [[btn(texts.BTN_CERT, "cert")], MENU_ROW]
        await bot.send_message(int(tg_id), texts.UPSELL_FRIEND + "\n\n" + texts.UPSELL_CERT, reply_markup=kb(rows))
        return True
    except Exception:
        log.warning("Не получилось отправить подтверждение %s", tg_id)
        return False


async def apply_certificate(bot: Bot, bid: str, code_text: str) -> bool:
    """Запись по сертификату: номер не проверяем, только записываем в таблицу и ставим «Оплачено»."""
    rec = await find_booking(bid)
    if not rec or logic.norm(rec.get("Статус")) in logic.INACTIVE:
        return False
    code = certificate.normalize_code(code_text) or str(code_text).strip()[:30]
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Сертификат", code)
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_PAID)
    people = logic.to_int(rec.get("Человек"), 1)
    head = texts.MGR_FROM_SITE if rec.get("Источник") == texts.SOURCE_SITE else ""
    note = texts.MGR_BY_CERT.format(fmt=rec_fmt_label(rec), when=rec_when(rec), people=people, code=code,
                                    name=rec.get("Имя", ""), contact=rec.get("Контакт", ""),
                                    tg=rec.get("Telegram", "—") or "—")
    sent = await send_confirmed(bot, rec)
    if not sent:
        note += texts.MGR_SITE_NO_TG.format(contact=rec.get("Контакт", ""))
    await tell_manager(bot, head + note)
    return True


@router.callback_query(F.data.startswith("bpaid:"))
async def cb_prepaid(cb: CallbackQuery):
    rec = await find_booking(cb.data[6:])
    if not rec or logic.norm(rec.get("Статус")) in logic.INACTIVE:
        await cb.message.answer(texts.BOOKING_GONE, reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))
    elif logic.norm(rec.get("Статус")) == logic.norm(logic.STATUS_PAID):
        await cb.message.answer(texts.BOOKING_ALREADY)
    elif rec.get("Платёж") and yookassa.enabled():  # оплата через ЮKassa — проверяем сами, без менеджера
        if not await check_booking_payment(cb.bot, rec["ID"]):
            await cb.message.answer(texts.PAY_NOT_YET)
    else:
        await request_prepay_check(cb.bot, rec)
        await cb.message.answer(texts.PREPAY_WAIT)
    await cb.answer()


@router.callback_query(F.data.startswith("bcert:"))
async def cb_have_cert(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Book.cert_code)
    await state.update_data(cert_bid=cb.data[6:])
    await cb.message.answer(texts.ASK_CERT_CODE)
    await cb.answer()


@router.message(Book.cert_code, F.text)
async def msg_cert_code(message: Message, state: FSMContext, bot: Bot):
    bid = (await state.get_data()).get("cert_bid", "")
    await state.clear()
    rec = await find_booking(bid)
    if rec and logic.norm(rec.get("Статус")) == logic.norm(logic.STATUS_PAID):
        await message.answer(texts.BOOKING_ALREADY)
        return
    if not await apply_certificate(bot, bid, message.text):
        await message.answer(texts.BOOKING_GONE, reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))


@router.callback_query(F.data.startswith("bok:") | F.data.startswith("bno:"))
async def cb_manager_prepay(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != config.MANAGER_ID:
        await cb.answer()
        return
    ok = cb.data.startswith("bok:")
    rec = await find_booking(cb.data[4:])
    if not rec:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    if ok:
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_PAID)
        if await send_confirmed(bot, rec):
            await cb.message.answer(texts.MGR_PREPAY_OK)
        else:
            await cb.message.answer(texts.MGR_PREPAY_OK_SITE + texts.MGR_SITE_NO_TG.format(contact=rec.get("Контакт", "")))
    else:
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_WAIT)
        await tell_client(bot, rec.get("Telegram ID", ""), texts.PREPAY_NOT_PAID, await pay_kb(rec))
        await cb.message.answer(texts.MGR_PREPAY_NO)
    await cb.answer()


# ---------------------------------------------------------------- links from the site (/start b_ID, /start c_ORDER)
async def link_site_booking(bot: Bot, message: Message, bid: str):
    rec = await find_booking(bid)
    if not rec or logic.norm(rec.get("Статус")) in logic.INACTIVE:
        await message.answer(texts.BOOKING_GONE, reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))
        return
    user = message.from_user
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Telegram ID", str(user.id))
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Telegram", tg_of(user))
    rec = {**rec, "Telegram ID": str(user.id), "Telegram": tg_of(user)}
    when = rec_when(rec)
    status = logic.norm(rec.get("Статус"))
    if rec.get("Формат") == logic.FMT_OUT:
        await message.answer(texts.SITE_LINKED_OUT, reply_markup=kb([MENU_ROW]))
    elif status == logic.norm(logic.STATUS_PAID):
        await message.answer(texts.SITE_LINKED)
        await send_confirmed(bot, rec)
    elif rec.get("Статус") == texts.STATUS_CHECKING:
        await message.answer(texts.SITE_LINKED + "\n\n" + texts.PREPAY_WAIT)
    else:
        people = logic.to_int(rec.get("Человек"), 1)
        await message.answer(texts.SITE_LINKED)
        await message.answer(texts.SITE_LINKED_WAIT.format(when=when, prepay=rub(prepay(people))),
                             reply_markup=await pay_kb(rec))
    await tell_manager(bot, texts.MGR_SITE_LINKED.format(name=rec.get("Имя", ""), when=when, tg=tg_of(user)))


async def link_site_cert(bot: Bot, message: Message, order: str):
    rec = await find_cert(order)
    if not rec:
        await send_welcome(bot, message.chat.id)
        return
    user = message.from_user
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Telegram ID", str(user.id), "Сертификаты")
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Telegram", tg_of(user), "Сертификаты")
    await message.answer(texts.SITE_CERT_LINKED)
    if logic.norm(rec.get("Статус")) == logic.norm(logic.STATUS_PAID):
        await deliver_cert(bot, {**rec, "Telegram ID": str(user.id)}, notify_manager=False)
    await tell_manager(bot, texts.MGR_SITE_CERT_LINKED.format(name=rec.get("Имя", ""), codes=rec.get("Номера", ""),
                                                              tg=tg_of(user)))


# ---------------------------------------------------------------- gift certificates
def cert_codes(rec: dict) -> list[str]:
    return [c.strip() for c in str(rec.get("Номера", "")).split(",") if c.strip()]


def cert_what(size: int, qty: int) -> str:
    return f"{certificate.size_label(size)}, {qty} {certificate.rugs_word(qty)}"


async def find_cert(order: str) -> dict | None:
    if not order:
        return None
    rows = await asyncio.to_thread(sheets.certs, True)
    return next((r for r in rows if cert_codes(r)[:1] == [order]), None)


def new_cert_codes(n: int) -> list[str]:
    taken = set()
    for r in sheets.certs(True):
        taken.update(cert_codes(r))
    return [certificate.new_code(taken) for _ in range(n)]


async def add_cert_order(size: int, qty: int, separate: bool, kind: str, name: str, contact: str,
                         tg: str, tg_id: str, source: str, email: str = "", comment: str = "",
                         receipt: str = "") -> dict:
    """Записывает покупку в лист «Сертификаты». Номер заказа = первый номер сертификата."""
    files = qty if separate else 1
    codes = await asyncio.to_thread(new_cert_codes, files)
    now = now_local()
    rec = {
        "Создано": now.strftime("%d.%m.%Y %H:%M"), "Номера": ", ".join(codes), "Размер": certificate.size_label(size),
        "Ковриков": qty, "Файлов": files, "Тип": texts.CERT_TYPE_LABEL[kind],
        "Сумма": certificate.price(size, qty), "Статус": CERT_WAIT,
        "Действует до": f"{certificate.valid_until(now.date()):%d.%m.%Y}",
        "Имя": name, "Контакт": contact, "Telegram": tg, "Telegram ID": tg_id, "Источник": source, "Email": email,
        "Комментарий": comment, "Для чека": receipt,
    }
    await asyncio.to_thread(sheets.add_cert, rec)
    return {**rec, "order": codes[0]}


def cert_kind(rec: dict) -> str:
    return "print" if rec.get("Тип") == texts.CERT_TYPE_LABEL["print"] else "pdf"


def cert_size(rec: dict) -> int:
    return logic.to_int(rec.get("Размер"), 30) // 100 or 30  # «30×30 см» → 3030 → 30


def cert_pdfs(rec: dict) -> list[tuple[str, bytes]]:
    codes = cert_codes(rec)
    qty = logic.to_int(rec.get("Ковриков"), 1)
    until = logic.parse_date(rec.get("Действует до")) or certificate.valid_until(now_local().date())
    size = cert_size(rec)
    rugs = qty if len(codes) == 1 else 1
    return [(f"Сертификат_{code}.pdf", certificate.make_pdf(size, code, until, rugs)) for code in codes]


async def email_cert(rec: dict, files) -> bool | None:
    """True — отправили на почту, False — не получилось, None — почты нет или отправка выключена."""
    email = str(rec.get("Email", "")).strip()
    if not email or not mailer.enabled():
        return None
    qty = logic.to_int(rec.get("Ковриков"), 1)
    bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
    try:
        await asyncio.to_thread(mailer.send_certificates, email, rec.get("Имя", ""), files,
                                cert_what(cert_size(rec), qty), rec.get("Действует до", ""), cert_codes(rec), bot_link)
        if rec.get("_row"):
            await asyncio.to_thread(sheets.set_cell, rec["_row"], "Отправлено на почту",
                                    now_local().strftime("%d.%m.%Y %H:%M"), "Сертификаты")
        return True
    except Exception:
        log.exception("Не получилось отправить сертификат на %s", email)
        return False


async def deliver_cert(bot: Bot, rec: dict, notify_manager: bool = True):
    """После подтверждения оплаты: файлы клиенту (Telegram и/или почта), сообщение менеджеру."""
    kind, tg_id, contact = cert_kind(rec), str(rec.get("Telegram ID", "")).strip(), rec.get("Контакт", "")
    codes = ", ".join(cert_codes(rec))
    if kind == "print":
        sent = await tell_client(bot, tg_id, texts.CERT_DONE_PRINT, kb([MENU_ROW]))
        if notify_manager:
            tpl = texts.MGR_CERT_OK_PRINT if sent else texts.MGR_CERT_SITE_PRINT
            await tell_manager(bot, tpl.format(codes=codes, until=rec.get("Действует до", ""), contact=contact))
        return
    try:
        files = await asyncio.to_thread(cert_pdfs, rec)
    except Exception:
        log.exception("Не получилось собрать PDF сертификата %s", codes)
        if notify_manager:
            await tell_manager(bot, texts.MGR_CERT_PDF_FAIL.format(codes=codes, contact=contact))
        return
    sent = False
    if tg_id.isdigit():
        try:
            for filename, data in files:
                await bot.send_document(int(tg_id), BufferedInputFile(data, filename))
            await bot.send_message(int(tg_id), texts.CERT_DONE_PDF.format(s="ы" if len(files) > 1 else "",
                                                                      it="Их" if len(files) > 1 else "Его"),
                                   reply_markup=kb([[btn(texts.BTN_BOOK, "book")], MENU_ROW]))
            sent = True
        except Exception:
            log.warning("Не получилось отправить сертификат клиенту %s", tg_id)
    if not notify_manager:
        return
    emailed = await email_cert(rec, files)
    email = rec.get("Email", "")
    mail_note = "" if emailed is None else (mailer.MGR_EMAIL_OK if emailed else mailer.MGR_EMAIL_FAIL).format(email=email)
    if sent:
        await tell_manager(bot, texts.MGR_CERT_OK_PDF.format(n=len(files)) + mail_note)
    elif emailed:
        await tell_manager(bot, mailer.MGR_SITE_PDF_EMAILED + mail_note)
    else:
        if tg_id.isdigit():
            await tell_manager(bot, texts.MGR_CERT_CLIENT_FAIL.format(contact=contact))
        await tell_manager(bot, texts.MGR_CERT_SITE_PDF.format(contact=contact) + mail_note)
        for filename, data in files:
            try:
                await bot.send_document(config.MANAGER_ID, BufferedInputFile(data, filename))
            except Exception:
                log.exception("Не получилось отправить файл менеджеру")


async def request_cert_check(bot: Bot, rec: dict):
    order = cert_codes(rec)[0]
    await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", texts.STATUS_CHECKING, "Сертификаты")
    head = texts.MGR_FROM_SITE if rec.get("Источник") == texts.SOURCE_SITE else ""
    qty = logic.to_int(rec.get("Ковриков"), 1)
    contact = rec.get("Контакт", "") + (f"\nEmail: {rec['Email']}" if rec.get("Email") else "") \
        + (f"\nКомментарий: {rec['Комментарий']}" if rec.get("Комментарий") else "")
    await tell_manager(bot, head + texts.MGR_CERT.format(
        what=cert_what(cert_size(rec), qty), type=rec.get("Тип", ""), total=rub(logic.to_int(rec.get("Сумма"))),
        codes=rec.get("Номера", ""), name=rec.get("Имя", ""), contact=contact, tg=rec.get("Telegram", "—") or "—"),
        markup=kb([[btn(texts.BTN_MGR_OK, f"cok:{order}")], [btn(texts.BTN_MGR_NO, f"cno:{order}")]]))


@router.callback_query(F.data == "cert")
async def cb_cert(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    note_interest(cb.from_user, "сертификат")
    rows = [[btn(texts.BTN_CERT_SIZE.format(size=s, price=rub(certificate.price(s, 1))), f"cs:{s}")]
            for s in certificate.SIZES] + [MENU_ROW]
    await cb.message.answer(texts.CERT_START, reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("cs:"))
async def cb_cert_size(cb: CallbackQuery, state: FSMContext):
    size = int(cb.data[3:])
    await state.clear()
    await state.update_data(c_size=size)
    lines = [texts.CERT_PRICE_LINE.format(n=n, total=rub(certificate.price(size, n)),
                                          each=rub(certificate.price(size, n) // n)) for n in range(1, 5)]
    lines.append(texts.CERT_PRICE_LINE.format(n="5 и больше", total="…", each=rub(certificate.PER_PERSON_FROM_4[size])))
    rows = [[btn(str(n), f"cq:{n}") for n in range(1, 5)], [btn(texts.BTN_CERT_MORE, "cq:more")], MENU_ROW]
    await state.set_state(Cert.qty)
    await cb.message.answer(texts.CERT_ASK_QTY.format(size=size, prices="\n".join(lines)), reply_markup=kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("cq:"))
async def cb_cert_qty(cb: CallbackQuery, state: FSMContext):
    if "c_size" not in await state.get_data():
        await restart(cb.message)
    elif cb.data == "cq:more":
        await state.set_state(Cert.qty)
        await cb.message.answer(texts.CERT_TYPE_MORE)
    else:
        await got_cert_qty(cb.message, state, int(cb.data[3:]))
    await cb.answer()


@router.message(Cert.qty, F.text)
async def msg_cert_qty(message: Message, state: FSMContext):
    t = message.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 50:
        await message.answer(texts.CERT_BAD_QTY)
        return
    await got_cert_qty(message, state, int(t))


async def got_cert_qty(msg: Message, state: FSMContext, qty: int):
    await state.set_state(None)
    await state.update_data(c_qty=qty)
    if qty > 1:
        rows = [[btn(texts.BTN_CERT_ONE, "cf:one")], [btn(texts.BTN_CERT_SEP, "cf:sep")], MENU_ROW]
        await msg.answer(texts.CERT_ASK_FILES.format(n=qty, rugs=certificate.rugs_word(qty)), reply_markup=kb(rows))
    else:
        await ask_cert_type(msg)


@router.callback_query(F.data.startswith("cf:"))
async def cb_cert_files(cb: CallbackQuery, state: FSMContext):
    if "c_qty" not in await state.get_data():
        await restart(cb.message)
    else:
        await state.update_data(c_sep=cb.data == "cf:sep")
        await ask_cert_type(cb.message)
    await cb.answer()


async def ask_cert_type(msg: Message):
    rows = [[btn(texts.BTN_CERT_PDF, "ct:pdf")], [btn(texts.BTN_CERT_PRINT, "ct:print")], MENU_ROW]
    await msg.answer(texts.CERT_ASK_TYPE, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("ct:"))
async def cb_cert_type(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if "c_qty" not in await state.get_data():
        await restart(cb.message)
        await cb.answer()
        return
    kind = cb.data[3:]
    await state.update_data(c_kind=kind)
    if kind == "print":  # печатный забирают в студии — нужен контакт, чтобы договориться
        await state.set_state(Cert.contact)
        markup = None
        if cb.from_user.username:
            markup = kb([[btn(texts.BTN_USE_USERNAME.format(username="@" + cb.from_user.username), "cun")]])
        await cb.message.answer(texts.CERT_ASK_CONTACT, reply_markup=markup)
    else:
        await create_cert_order(bot, cb.from_user, cb.message.chat.id, state, tg_of(cb.from_user))
    await cb.answer()


@router.callback_query(Cert.contact, F.data == "cun")
async def cb_cert_username(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    await create_cert_order(bot, cb.from_user, cb.message.chat.id, state, "@" + (cb.from_user.username or ""))


@router.message(Cert.contact, F.text)
async def msg_cert_contact(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    if not contact_ok(text):
        await message.answer(texts.BAD_CONTACT)
        return
    await create_cert_order(bot, message.from_user, message.chat.id, state, text[:100])


async def create_cert_order(bot: Bot, user, chat_id: int, state: FSMContext, contact: str):
    d = await state.get_data()
    if {"c_size", "c_qty", "c_kind"} <= d.keys() and not d.get("c_receipt") and need_receipt_contact(contact):
        await state.update_data(c_contact=contact)  # для чека ЮKassa нужен телефон или почта
        await state.set_state(Cert.receipt)
        await bot.send_message(chat_id, texts.ASK_RECEIPT)
        return
    await state.clear()
    if not {"c_size", "c_qty", "c_kind"} <= d.keys():
        await bot.send_message(chat_id, texts.RESTART, reply_markup=menu_kb())
        return
    size, qty, kind = d["c_size"], d["c_qty"], d["c_kind"]
    rec = await add_cert_order(size, qty, bool(d.get("c_sep")), kind, user.full_name or "", contact,
                               tg_of(user), str(user.id), CERT_SOURCE_TG, receipt=d.get("c_receipt", ""))
    url = await cert_pay_url(rec)
    rows = [[url_btn(texts.BTN_PAY, url or texts.PAY_URL)], [btn(texts.BTN_PAID, f"cpaid:{rec['order']}")], MENU_ROW]
    await bot.send_message(chat_id, texts.CERT_PAY.format(
        what=cert_what(size, qty), type=texts.CERT_TYPE_LABEL[kind], total=rub(certificate.price(size, qty))),
        reply_markup=kb(rows))


@router.message(Cert.receipt, F.text)
async def msg_cert_receipt(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()[:100]
    if not yookassa.customer(text, text):
        await message.answer(texts.BAD_RECEIPT)
        return
    await state.update_data(c_receipt=text)
    contact = (await state.get_data()).get("c_contact", "")
    await create_cert_order(bot, message.from_user, message.chat.id, state, contact)


@router.callback_query(F.data.startswith("cpaid:"))
async def cb_cert_paid(cb: CallbackQuery, bot: Bot):
    rec = await find_cert(cb.data[6:])
    if not rec:
        await restart(cb.message)
    elif logic.norm(rec.get("Статус")) == logic.norm(logic.STATUS_PAID):
        await cb.message.answer(texts.CERT_ALREADY)
    elif rec.get("Платёж") and yookassa.enabled():
        if not await check_cert_payment(bot, cert_codes(rec)[0]):
            await cb.message.answer(texts.PAY_NOT_YET)
    else:
        await request_cert_check(bot, rec)
        await cb.message.answer(texts.CERT_WAIT_CHECK)
    await cb.answer()


@router.callback_query(F.data.startswith("cok:") | F.data.startswith("cno:"))
async def cb_manager_cert(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != config.MANAGER_ID:
        await cb.answer()
        return
    rec = await find_cert(cb.data[4:])
    if not rec:
        await cb.answer("Покупка не найдена", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    if cb.data.startswith("cok:"):
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", logic.STATUS_PAID, "Сертификаты")
        await deliver_cert(bot, rec)
    else:
        await asyncio.to_thread(sheets.set_cell, rec["_row"], "Статус", CERT_WAIT, "Сертификаты")
        rows = [[url_btn(texts.BTN_PAY, await cert_pay_url(rec) or texts.PAY_URL)],
                [btn(texts.BTN_PAID, f"cpaid:{cert_codes(rec)[0]}")]]
        await tell_client(bot, rec.get("Telegram ID", ""), texts.CERT_NOT_PAID, kb(rows))
        await cb.message.answer(texts.MGR_CERT_NO)
    await cb.answer()


# ---------------------------------------------------------------- rebook after the hold expired
@router.callback_query(F.data.startswith("rebook:"))
async def cb_rebook(cb: CallbackQuery, bot: Bot):
    old = await find_booking(cb.data[7:])
    fmt = SHEET_FMT.get(str((old or {}).get("Формат", "")).strip())
    if not old or fmt not in ("group", "date", "bday"):
        await ask_format(cb.message)
        await cb.answer()
        return
    b = logic.parse_booking(old)
    rec = {k: v for k, v in old.items() if not k.startswith("_")}
    rec.update({"Создано": now_local().strftime("%d.%m.%Y %H:%M"), "Статус": logic.STATUS_WAIT, "Напомнили": "",
                "Бронь напомнили": "", "Отзыв запрошен": "", "Сертификат": "",
                "Платёж": "", "Ссылка оплаты": ""})
    ok = False
    if b.day and b.start:
        async with LOCK:
            studio = await load_studio(fresh=True, fmt=fmt)
            ok = studio.group_can_book(b.day, b.start, b.people) if fmt == "group" else studio.individual_ok(b.day, b.start)
            if ok:
                rec["ID"] = await asyncio.to_thread(new_booking_id)
                await asyncio.to_thread(sheets.add_booking, rec)
    if not ok:
        await cb.message.answer(texts.REBOOK_TAKEN)
        await ask_format(cb.message)
        await cb.answer()
        return
    await cb.message.answer(texts.REBOOK_OK)
    await send_pay_offer(bot, cb.message.chat.id, rec)
    await tell_manager(bot, texts.MGR_NEW.format(
        fmt=texts.FMT_LABEL[fmt], when=rec_when(rec), people=b.people, size_line=size_line(rec),
        name=rec.get("Имя", ""), contact=rec.get("Контакт", ""), tg=rec.get("Telegram", "—")))
    await cb.answer()


# ---------------------------------------------------------------- review after the workshop
@router.callback_query(F.data.startswith("rv:"))
async def cb_review(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Review.text)
    await cb.message.answer(texts.REVIEW_TYPE)
    await cb.answer()


@router.message(Review.text)
async def msg_review(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    text = message.text or message.caption or "(без текста)"
    await tell_manager(bot, texts.MGR_REVIEW.format(name=message.from_user.full_name, tg=tg_of(message.from_user),
                                                    text=text[:3500]))
    if message.photo or message.document:
        try:
            await message.forward(config.MANAGER_ID)
        except Exception:
            log.warning("Не получилось переслать фото отзыва")
    await message.answer(texts.REVIEW_THANKS, reply_markup=kb([MENU_ROW]))


# ---------------------------------------------------------------- fallbacks (keep last)
@router.message(StateFilter(None))
async def fallback_menu(message: Message):
    await message.answer(texts.UNKNOWN, reply_markup=menu_kb())


@router.message()
async def fallback_need_text(message: Message):
    await message.answer(texts.NEED_TEXT)


# ---------------------------------------------------------------- background jobs
async def expire_holds(bot: Bot):
    async with LOCK:
        rows = await asyncio.to_thread(sheets.bookings, True)
        bookings = [logic.parse_booking(r) for r in rows]
        expired = logic.expired_waiting(bookings, now_local())
        for b in expired:
            await asyncio.to_thread(sheets.set_cell, b.row, "Статус", logic.STATUS_EXPIRED)
    for b in expired:
        when = texts.when(b.day, b.start) if b.day and b.start else "—"
        await tell_manager(bot, texts.MGR_EXPIRED.format(name=b.name, when=when, people=b.people, contact=b.contact))
        rows = ([[btn(texts.BTN_REBOOK, f"rebook:{b.bid}")]] if b.bid else []) + [MANAGER_ROW]
        await tell_client(bot, b.tg_id, texts.CLIENT_EXPIRED.format(when=when), kb(rows))


async def hold_reminders(bot: Bot):
    now = now_local()
    rows = await asyncio.to_thread(sheets.bookings, True)
    for b in (logic.parse_booking(r) for r in rows):
        if b.hold_reminded or not b.bid or not logic.hold_reminder_due(b, now):
            continue
        when = texts.when(b.day, b.start) if b.day and b.start else "—"
        sent = await tell_client(bot, b.tg_id, texts.HOLD_REMINDER.format(when=when),
                                 await pay_kb(next(r for r in rows if r["_row"] == b.row)))
        await asyncio.to_thread(sheets.set_cell, b.row, "Бронь напомнили", "да" if sent else "нет Telegram")


def how_to_find_photos() -> list[Path]:
    """Фото пути до студии из assets: how_to_find_1.jpg, how_to_find_2.jpg … — по порядку номеров."""
    files = [p for p in ASSETS.glob("how_to_find*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]

    def number(p: Path) -> int:
        digits = re.sub(r"\D", "", p.stem)
        return int(digits) if digits else 0

    return sorted(files, key=number)[:10]  # в одном альбоме Telegram — не больше 10 фото


async def send_reminders(bot: Bot):
    rows = await asyncio.to_thread(sheets.bookings, True)
    now = now_local()
    for b in logic.due_reminders([logic.parse_booking(r) for r in rows], now):
        if not b.tg_id.isdigit():
            continue
        day_word = "сегодня" if b.day == now.date() else "завтра"
        try:
            await bot.send_message(int(b.tg_id), texts.REMINDER.format(
                name=b.name or "друг", day=day_word, date=texts.short_day(b.day), time=f"{b.start:%H:%M}"))
            photos = how_to_find_photos()
            if len(photos) == 1:
                await bot.send_photo(int(b.tg_id), FSInputFile(photos[0]), caption=texts.REMINDER_PHOTO)
            elif photos:
                album = [
                    InputMediaPhoto(media=FSInputFile(p), caption=texts.REMINDER_PHOTO if i == 0 else None)
                    for i, p in enumerate(photos)
                ]
                await bot.send_media_group(int(b.tg_id), album)
            mark = "да"
        except Exception:
            log.warning("Не получилось отправить напоминание %s", b.tg_id)
            mark = "ошибка"
        await asyncio.to_thread(sheets.set_cell, b.row, "Напомнили", mark)


async def followups(bot: Bot):
    """Тем, кто смотрел цены/даты/эскизы и не записался за сутки, — одно мягкое сообщение (не чаще раза в 30 дней)."""
    now = now_local()
    if not logic.is_quiet_ok(now):
        return
    leads = await asyncio.to_thread(sheets.leads, True)
    bookings = await asyncio.to_thread(sheets.bookings, False)
    certs = await asyncio.to_thread(sheets.certs, False)
    for r in leads:
        tg_id = str(r.get("Telegram ID", "")).strip()
        last = logic.parse_created(r.get("Последний интерес"))
        if not tg_id.isdigit() or not last or not (logic.FOLLOWUP_AFTER <= now - last <= logic.FOLLOWUP_MAX_AGE):
            continue
        done = logic.parse_created(r.get("Напомнили"))
        if done and (now - done).days < logic.FOLLOWUP_REPEAT_DAYS:
            continue
        acted = any(str(x.get("Telegram ID", "")).strip() == tg_id
                    and (logic.parse_created(x.get("Создано")) or now) >= last - timedelta(days=1)
                    for x in bookings + certs)
        if acted:
            continue
        await tell_client(bot, tg_id, texts.FOLLOWUP, kb([[btn(texts.BTN_BOOK, "book")], MANAGER_ROW]))
        await asyncio.to_thread(sheets.set_cell, r["_row"], "Напомнили", now.strftime("%d.%m.%Y %H:%M"), "Интерес")


async def review_requests(bot: Bot):
    now = now_local()
    if not logic.is_quiet_ok(now):
        return
    rows = await asyncio.to_thread(sheets.bookings, True)
    for b in (logic.parse_booking(r) for r in rows):
        if b.review_asked or not b.tg_id.isdigit() or not logic.review_due(b, now):
            continue
        sent = await tell_client(bot, b.tg_id, texts.REVIEW_ASK.format(name=b.name or "друг"),
                                 kb([[btn(texts.BTN_REVIEW, f"rv:{b.bid or b.row}")]]))
        await asyncio.to_thread(sheets.set_cell, b.row, "Отзыв запрошен", "да" if sent else "ошибка")


async def background(bot: Bot):
    while True:
        for job in (expire_holds, hold_reminders, send_reminders, followups, review_requests):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка фоновой задачи %s", job.__name__)
        await asyncio.sleep(300)


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await asyncio.to_thread(sheets.ensure)
    bot = Bot(config.BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username or ""
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await web_api.start(bot, sys.modules[__name__])  # вход для сайта (виджет на Тильде)
    except Exception:
        log.exception("Вход для сайта не запустился — бот в Telegram работает дальше")
    job = asyncio.create_task(background(bot))  # noqa: F841 (держим ссылку)
    pay_job = asyncio.create_task(payments_loop(bot))  # noqa: F841
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
