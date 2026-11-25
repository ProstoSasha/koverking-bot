"""Работа с Google Таблицей."""
import json
import os
import threading
import time as _time

import gspread
from gspread.exceptions import WorksheetNotFound

GROUP_HEADER = ["Дата", "Время", "Мест всего", "Занято вручную", "Скрыть"]
CLOSED_HEADER = ["Дата", "Дата до", "С", "До", "Причина"]
BOOK_HEADER = [
    "Создано", "Формат", "Дата", "Время", "Человек", "Имя", "Контакт",
    "Telegram", "Telegram ID", "Статус", "Детали", "Напомнили", "Сертификат", "ID",
    "Размер", "Бронь напомнили", "Отзыв запрошен", "Источник", "Платёж", "Ссылка оплаты", "Для чека",
]
CERT_HEADER = [
    "Создано", "Номера", "Размер", "Ковриков", "Файлов", "Тип", "Сумма", "Статус",
    "Действует до", "Имя", "Контакт", "Telegram", "Telegram ID", "Использован", "Источник",
    "Email", "Отправлено на почту", "Комментарий", "Платёж", "Ссылка оплаты", "Для чека",
]
LEAD_HEADER = ["Telegram ID", "Имя", "Telegram", "Первый визит", "Последний интерес", "Что смотрел", "Напомнили"]
SHEETS = {
    "Групповые": GROUP_HEADER, "Закрытые": CLOSED_HEADER, "Записи": BOOK_HEADER,
    "Сертификаты": CERT_HEADER, "Интерес": LEAD_HEADER,
}

CACHE_SECONDS = 15  # чтобы не упираться в лимиты Google при активных диалогах


class Sheets:
    def __init__(self, creds_file: str, sheet_id: str):
        # На хостинге ключ лежит в переменной окружения GOOGLE_CREDS_JSON (всё содержимое credentials.json),
        # на компьютере — в файле credentials.json рядом с ботом.
        creds_json = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
        if creds_json:
            self.gc = gspread.service_account_from_dict(json.loads(creds_json))
        else:
            self.gc = gspread.service_account(filename=creds_file)
        self.sh = self.gc.open_by_key(sheet_id)
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def ensure(self):
        """Создаёт недостающие листы с заголовками, а в старые листы дописывает новые колонки справа."""
        with self._lock:
            for name, header in SHEETS.items():
                try:
                    ws = self.sh.worksheet(name)
                except WorksheetNotFound:
                    ws = self.sh.add_worksheet(title=name, rows=500, cols=len(header) + 2)
                    ws.update(range_name="A1", values=[header])
                    continue
                current = [h.strip() for h in ws.row_values(1)]
                missing = [h for h in header if h not in current]
                if missing:
                    need = len(current) + len(missing)
                    if ws.col_count < need:
                        ws.add_cols(need - ws.col_count)
                    for i, h in enumerate(missing):
                        ws.update_cell(1, len(current) + i + 1, h)

    def _rows(self, name: str, fresh: bool = False) -> list[dict]:
        with self._lock:
            hit = self._cache.get(name)
            if hit and not fresh and _time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
            values = self.sh.worksheet(name).get_all_values()
            rows: list[dict] = []
            if values:
                header = [h.strip() for h in values[0]]
                for i, row in enumerate(values[1:], start=2):
                    if not any(c.strip() for c in row):
                        continue
                    rec = {header[j]: row[j].strip() for j in range(min(len(header), len(row)))}
                    rec["_row"] = i
                    rows.append(rec)
            self._cache[name] = (_time.time(), rows)
            return rows

    def groups(self, fresh: bool = False):
        return self._rows("Групповые", fresh)

    def closed(self, fresh: bool = False):
        return self._rows("Закрытые", fresh)

    def bookings(self, fresh: bool = False):
        return self._rows("Записи", fresh)

    def certs(self, fresh: bool = False):
        return self._rows("Сертификаты", fresh)

    def add_booking(self, rec: dict, sheet: str = "Записи"):
        with self._lock:
            ws = self.sh.worksheet(sheet)
            header = [h.strip() for h in ws.row_values(1)]
            ws.append_row([rec.get(h, "") for h in header], value_input_option="RAW")
            self._cache.pop(sheet, None)

    def add_cert(self, rec: dict):
        self.add_booking(rec, sheet="Сертификаты")

    def leads(self, fresh: bool = False):
        return self._rows("Интерес", fresh)

    def upsert_lead(self, tg_id: str, name: str, tg: str, what: str, when: str):
        """Отмечает, что человек что-то смотрел (цены, даты, эскизы). Одна строка на человека."""
        for r in self._rows("Интерес", fresh=False):
            if str(r.get("Telegram ID", "")).strip() == tg_id:
                self.set_cell(r["_row"], "Последний интерес", when, sheet="Интерес")
                self.set_cell(r["_row"], "Что смотрел", what, sheet="Интерес")
                return
        self.add_booking({"Telegram ID": tg_id, "Имя": name, "Telegram": tg, "Первый визит": when,
                          "Последний интерес": when, "Что смотрел": what}, sheet="Интерес")

    def set_cell(self, row: int, column: str, value: str, sheet: str = "Записи"):
        with self._lock:
            ws = self.sh.worksheet(sheet)
            header = [h.strip() for h in ws.row_values(1)]
            ws.update_cell(row, header.index(column) + 1, value)
            self._cache.pop(sheet, None)
