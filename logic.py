"""Логика расписания и свободных мест.

Тут ничего не знаем про Телеграм и Google: только даты, время и места.
Настройки вверху файла можно менять.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# ---------------- Настройки ----------------
SESSION = timedelta(hours=4)      # сколько студия занята одним МК (клиенту пишем «2–3 часа», остальное — уборка)
HOLD = timedelta(hours=6)         # сколько держим бронь до предоплаты
HORIZON_DAYS = 42                 # показываем даты на 6 недель вперёд
MIN_LEAD = timedelta(hours=3)     # записаться можно не позже, чем за 3 часа до начала
STEP = timedelta(minutes=30)      # шаг времени старта для свиданий и дней рождения

GROUP_SEATS = 6                   # мест на групповом мастер-классе
GROUP_MIN = 2                     # минимум человек, чтобы групповой состоялся
GROUP_TIMES = ("11:00", "17:00")  # время групповых
GROUP_WEEKDAYS = (5, 6)           # 5 = суббота, 6 = воскресенье
PREPAY_PER_PERSON = 1000          # предоплата за каждого человека, ₽
HOLD_REMIND_BEFORE = timedelta(hours=2)  # за сколько до конца брони напомнить про оплату
FOLLOWUP_AFTER = timedelta(hours=24)     # через сколько написать тем, кто смотрел, но не записался
FOLLOWUP_MAX_AGE = timedelta(days=7)     # совсем старым «смотрел» уже не пишем
FOLLOWUP_REPEAT_DAYS = 30                # одному человеку — не чаще раза в 30 дней
QUIET_FROM, QUIET_TO = 10, 21            # такие сообщения и просьбы об отзыве — только с 10:00 до 21:00
REVIEW_HOUR = 12                         # просьба об отзыве — на следующий день после МК, с 12:00
NEW_YEAR_FROM = (12, 1)                  # (месяц, день) — с какого дня показывать новогоднюю кнопку
NEW_YEAR_TO = (12, 31)                   # и по какой
BDAY_MIN = 3                      # своё событие в студии (день рождения, корпоратив…) — от 3 человек
BDAY_COMFORT = 6                  # столько гостей в студии помещается комфортно
BDAY_MAX = 8                      # больше — уже не помещаемся, предлагаем выездной
BDAY_MONTHS_AHEAD = 3             # дни рождения/корпоративы: текущий месяц + ещё 3 (в октябре видно до января)

# Окна, в которые можно НАЧАТЬ мастер-класс (свидание, день рождения, корпоратив)
WEEKDAY_WINDOW = (time(11, 0), time(18, 30))
WEEKEND_WINDOWS = ((time(10, 0), time(12, 0)), (time(16, 0), time(18, 0)))
# -------------------------------------------

FMT_GROUP = "Групповой"
FMT_DATE = "Свидание"
FMT_BDAY = "День рождения/корпоратив"
FMT_OUT = "Выездной"
STUDIO_FORMATS = (FMT_GROUP, FMT_DATE, FMT_BDAY)

STATUS_WAIT = "Ждёт предоплаты"
STATUS_PAID = "Оплачено"
STATUS_NEW = "Новая заявка"
STATUS_EXPIRED = "Истекло"

YES = {"да", "yes", "y", "1", "+", "x", "х", "скрыть"}
INACTIVE = {"отменено", "отмена", "отменили", "истекло", "не состоялось"}


def norm(s) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


def parse_date(s) -> date | None:
    s = str(s or "").strip().rstrip(".")
    for f in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    # Дата без года: «24.10», «1.11», «22/11» — берём ближайшую: этот год,
    # а если такая дата прошла больше полугода назад — следующий (декабрь → январь).
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", s)
    if m:
        today = date.today()
        for year in (today.year, today.year + 1):
            try:
                d = date(year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
            if d >= today - timedelta(days=180):
                return d
    return None


def parse_time(s) -> time | None:
    s = str(s or "").strip().replace(".", ":")
    for f in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, f).time()
        except ValueError:
            pass
    return None


def parse_created(s) -> datetime | None:
    try:
        return datetime.strptime(str(s or "").strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def to_int(s, default=0) -> int:
    digits = re.sub(r"\D", "", str(s or ""))
    return int(digits) if digits else default


@dataclass
class Booking:
    row: int
    fmt: str
    day: date | None
    start: time | None
    people: int
    status: str
    created: datetime | None
    name: str
    contact: str
    tg_id: str
    reminded: bool
    bid: str = ""             # ID записи (для ссылок с сайта и кнопок оплаты)
    size: str = ""
    tg: str = ""              # @ник в Telegram
    hold_reminded: bool = False
    review_asked: bool = False
    source: str = ""

    @property
    def start_dt(self) -> datetime | None:
        return datetime.combine(self.day, self.start) if self.day and self.start else None

    @property
    def active(self) -> bool:
        return norm(self.status) not in INACTIVE

    @property
    def waiting(self) -> bool:
        return norm(self.status) == norm(STATUS_WAIT)

    @property
    def paid(self) -> bool:
        return norm(self.status) == norm(STATUS_PAID)


def parse_booking(rec: dict) -> Booking:
    return Booking(
        row=rec.get("_row", 0),
        fmt=str(rec.get("Формат", "")).strip(),
        day=parse_date(rec.get("Дата")),
        start=parse_time(rec.get("Время")),
        people=to_int(rec.get("Человек")),
        status=str(rec.get("Статус", "")),
        created=parse_created(rec.get("Создано")),
        name=str(rec.get("Имя", "")),
        contact=str(rec.get("Контакт", "")),
        tg_id=str(rec.get("Telegram ID", "")).strip(),
        reminded=bool(str(rec.get("Напомнили", "")).strip()),
        bid=str(rec.get("ID", "")).strip(),
        size=str(rec.get("Размер", "")).strip(),
        tg=str(rec.get("Telegram", "")).strip(),
        hold_reminded=bool(str(rec.get("Бронь напомнили", "")).strip()),
        review_asked=bool(str(rec.get("Отзыв запрошен", "")).strip()),
        source=str(rec.get("Источник", "")).strip(),
    )


@dataclass
class GroupSlot:
    day: date
    start: time
    total: int = GROUP_SEATS
    manual: int = 0     # места, занятые через другие каналы (вы вписываете в таблицу)
    booked: int = 0     # места, занятые через бота
    hidden: bool = False

    @property
    def taken(self) -> int:
        return self.manual + self.booked

    @property
    def free(self) -> int:
        return max(self.total - self.taken, 0)

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.day, self.start)

    @property
    def end_dt(self) -> datetime:
        return self.start_dt + SESSION


def _overlap(a1, a2, b1, b2) -> bool:
    return a1 < b2 and b1 < a2


class Studio:
    """Снимок занятости студии на данный момент."""

    def __init__(self, bookings_raw, groups_raw, closed_raw, now: datetime, horizon_end: date | None = None):
        self.now = now
        self.horizon_end = horizon_end or now.date() + timedelta(days=HORIZON_DAYS)
        self.bookings = [parse_booking(r) for r in bookings_raw]

        # Групповые: по умолчанию каждые сб и вс в 11:00 и 17:00
        self.group_slots: dict[tuple[date, time], GroupSlot] = {}
        d = now.date()
        while d <= self.horizon_end:
            if d.weekday() in GROUP_WEEKDAYS:
                for t in GROUP_TIMES:
                    st = parse_time(t)
                    self.group_slots[(d, st)] = GroupSlot(d, st)
            d += timedelta(days=1)
        # Правки из листа «Групповые»: другие места, занято вручную, скрыть, лишние даты
        for r in groups_raw:
            day, st = parse_date(r.get("Дата")), parse_time(r.get("Время"))
            if not day or not st:
                continue
            slot = self.group_slots.get((day, st))
            if slot is None:
                slot = self.group_slots[(day, st)] = GroupSlot(day, st)
            if str(r.get("Мест всего", "")).strip():
                slot.total = to_int(r.get("Мест всего"), GROUP_SEATS)
            slot.manual = to_int(r.get("Занято вручную"))
            slot.hidden = norm(r.get("Скрыть")) in YES
        # Записи через бота
        for b in self.bookings:
            if b.fmt == FMT_GROUP and b.active and b.day and b.start:
                slot = self.group_slots.get((b.day, b.start))
                if slot is None:
                    slot = self.group_slots[(b.day, b.start)] = GroupSlot(b.day, b.start)
                slot.booked += b.people

        # Закрытое время
        self.closed: list[tuple[datetime, datetime]] = []
        for r in closed_raw:
            d1 = parse_date(r.get("Дата"))
            if not d1:
                continue
            d2 = parse_date(r.get("Дата до")) or d1
            t1, t2 = parse_time(r.get("С")), parse_time(r.get("До"))
            d = d1
            while d <= d2 and (d - d1).days < 400:
                s = datetime.combine(d, t1 or time(0, 0))
                e = datetime.combine(d, t2) if t2 else datetime.combine(d + timedelta(days=1), time(0, 0))
                self.closed.append((s, e))
                d += timedelta(days=1)

        # Индивидуальные мастер-классы, которые уже заняли студию
        self.individual = [
            (b.start_dt, b.start_dt + SESSION)
            for b in self.bookings
            if b.active and b.fmt in (FMT_DATE, FMT_BDAY) and b.start_dt
        ]

    # ---------- внутренние проверки ----------
    def _busy(self, s: datetime, e: datetime, skip_slot: GroupSlot | None = None) -> bool:
        if any(_overlap(s, e, a, b) for a, b in self.closed):
            return True
        if any(_overlap(s, e, a, b) for a, b in self.individual):
            return True
        for slot in self.group_slots.values():
            if slot is skip_slot or slot.taken <= 0:
                continue
            if _overlap(s, e, slot.start_dt, slot.end_dt):
                return True
        return False

    def _in_range(self, start_dt: datetime) -> bool:
        return (
            start_dt - self.now >= MIN_LEAD
            and start_dt.date() <= self.horizon_end
        )

    # ---------- групповые ----------
    def group_options(self) -> list[GroupSlot]:
        res = []
        for slot in sorted(self.group_slots.values(), key=lambda s: s.start_dt):
            if slot.hidden or slot.free <= 0 or not self._in_range(slot.start_dt):
                continue
            if self._busy(slot.start_dt, slot.end_dt, skip_slot=slot):
                continue
            res.append(slot)
        return res

    def group_slot(self, day: date, start: time) -> GroupSlot | None:
        for s in self.group_options():
            if s.day == day and s.start == start:
                return s
        return None

    def group_can_book(self, day: date, start: time, people: int) -> bool:
        s = self.group_slot(day, start)
        return bool(s and 1 <= people <= s.free)

    # ---------- свидания, дни рождения, корпоративы ----------
    def individual_times(self, day: date) -> list[time]:
        if day < self.now.date() or day > self.horizon_end:
            return []
        windows = WEEKEND_WINDOWS if day.weekday() in GROUP_WEEKDAYS else (WEEKDAY_WINDOW,)
        res = []
        for a, b in windows:
            cur, end = datetime.combine(day, a), datetime.combine(day, b)
            while cur <= end:
                if self._in_range(cur) and not self._busy(cur, cur + SESSION):
                    res.append(cur.time())
                cur += STEP
        return res

    def individual_days(self, first: date, last: date) -> list[date]:
        res, d = [], first
        while d <= last:
            if self.individual_times(d):
                res.append(d)
            d += timedelta(days=1)
        return res

    def individual_ok(self, day: date, start: time) -> bool:
        return start in self.individual_times(day)


def month_end(year: int, month: int) -> date:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def add_months(d: date, n: int) -> tuple[int, int]:
    """(год, месяц) через n месяцев после месяца даты d."""
    m = d.month - 1 + n
    return d.year + m // 12, m % 12 + 1


def bday_horizon_end(now: datetime) -> date:
    """До какого дня показываем даты для дней рождения и корпоративов."""
    return month_end(*add_months(now.date(), BDAY_MONTHS_AHEAD))


def bday_months(studio: "Studio") -> list[tuple[int, int]]:
    """Месяцы, в которых есть хоть один свободный день."""
    res, today = [], studio.now.date()
    for i in range(BDAY_MONTHS_AHEAD + 1):
        y, m = add_months(today, i)
        first, last = max(date(y, m, 1), today), min(month_end(y, m), studio.horizon_end)
        if first <= last and studio.individual_days(first, last):
            res.append((y, m))
    return res


def bday_weeks(studio: "Studio", y: int, m: int) -> list[tuple[date, date]]:
    """Недели месяца (пн–вс, обрезанные по границам месяца) со свободными днями: [(первый день, последний день)]."""
    today = studio.now.date()
    first, last = max(date(y, m, 1), today), min(month_end(y, m), studio.horizon_end)
    res, monday = [], first - timedelta(days=first.weekday())
    while monday <= last:
        a, b = max(monday, first), min(monday + timedelta(days=6), last)
        free = studio.individual_days(a, b)
        if free:
            res.append((free[0], b))  # начинаем подпись с первого свободного дня
        monday += timedelta(days=7)
    return res


def nearest_weekend(today: date) -> set[date]:
    """Ближайшие выходные: если сегодня сб/вс — эти, иначе следующие."""
    if today.weekday() == 6:
        return {today}
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    return {sat, sat + timedelta(days=1)}


def is_new_year_season(today: date) -> bool:
    return NEW_YEAR_FROM <= (today.month, today.day) <= NEW_YEAR_TO


def is_quiet_ok(now: datetime) -> bool:
    return QUIET_FROM <= now.hour < QUIET_TO


def hold_reminder_due(b: Booking, now: datetime) -> bool:
    """Бронь ждёт предоплаты и до её конца осталось меньше 2 часов (но она ещё не сгорела)."""
    if not b.waiting or not b.created:
        return False
    end = b.created + HOLD
    return end - HOLD_REMIND_BEFORE <= now < end


def review_due(b: Booking, now: datetime) -> bool:
    """На следующий день после МК (с 12:00), в течение 3 дней — если запись была оплачена."""
    if not b.paid or b.fmt not in STUDIO_FORMATS or not b.day:
        return False
    days_after = (now.date() - b.day).days
    return (days_after == 1 and now.hour >= REVIEW_HOUR) or 2 <= days_after <= 3


def expired_waiting(bookings: list[Booking], now: datetime) -> list[Booking]:
    """Брони, которые ждут предоплаты дольше HOLD."""
    return [b for b in bookings if b.waiting and b.created and b.created + HOLD <= now]


def due_reminders(bookings: list[Booking], now: datetime) -> list[Booking]:
    """Оплаченные записи, которым пора отправить напоминание (за 24 часа до начала)."""
    res = []
    for b in bookings:
        if not b.paid or b.reminded or b.fmt not in STUDIO_FORMATS or not b.start_dt or not b.tg_id:
            continue
        day_before = b.start_dt - timedelta(hours=24)
        if day_before <= now < b.start_dt and (b.created is None or b.created < day_before):
            res.append(b)
    return res
