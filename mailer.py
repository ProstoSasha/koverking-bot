"""Отправка писем с почты студии (Gmail) — подарочные сертификаты в PDF.

Нужна переменная окружения SMTP_PASSWORD — «пароль приложения» Google (16 букв),
не обычный пароль от почты. Без неё отправка просто выключена.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("mail")

SMTP_USER = os.environ.get("SMTP_USER", "koverkingtafting@gmail.com").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Ковёркинг — студия тафтинга")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def enabled() -> bool:
    return bool(SMTP_PASSWORD)


def valid(email: str) -> bool:
    return bool(EMAIL_RE.match(str(email or "").strip()))


def _send(msg: EmailMessage):
    """Сначала порт 465 (SSL), если не вышло — 587 (STARTTLS)."""
    ctx = ssl.create_default_context()
    errors = []
    attempts = [(SMTP_PORT, SMTP_PORT == 465)] + ([(587, False)] if SMTP_PORT == 465 else [])
    for port, use_ssl in attempts:
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(SMTP_HOST, port, context=ctx, timeout=30) as s:
                    if SMTP_PASSWORD:
                        s.login(SMTP_USER, SMTP_PASSWORD)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, port, timeout=30) as s:
                    if SMTP_HOST != "localhost" and SMTP_HOST != "127.0.0.1":
                        s.starttls(context=ctx)
                    if SMTP_PASSWORD and SMTP_HOST not in ("localhost", "127.0.0.1"):
                        s.login(SMTP_USER, SMTP_PASSWORD)
                    s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise  # неверный пароль приложения — другой порт не поможет
        except Exception as e:
            errors.append(f"{port}: {e}")
    raise RuntimeError("; ".join(errors))


def send_certificates(to: str, name: str, files: list[tuple[str, bytes]], what: str, until: str,
                      codes: list[str], bot_link: str):
    """files — [(имя файла, содержимое PDF)]. Бросает исключение, если отправить не удалось."""
    many = len(files) > 1
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    numbers = ", ".join(codes)
    book = f"напишите в наш Telegram-бот: {bot_link}" if bot_link else "напишите нам в Telegram"
    text = (
        f"{hello}\n\n"
        f"Спасибо за покупку! Во вложении {'подарочные сертификаты' if many else 'подарочный сертификат'} "
        f"на мастер-класс по тафтингу в студии «Ковёркинг».\n\n"
        f"Сертификат: {what}\n"
        f"Номер: {numbers}\n"
        f"Действует до: {until}\n\n"
        f"Чтобы записаться на мастер-класс, {book} — и при записи нажмите «У меня сертификат».\n\n"
        f"Адрес студии: Калининград, ул. Генерала Озерова, 19, помещение 214 (справа от ТЦ МЕГА).\n\n"
        f"Ждём вас! 🧶\nСтудия «Ковёркинг»"
    )
    msg = EmailMessage()
    msg["Subject"] = "Ваш подарочный сертификат на мастер-класс по тафтингу 🎁"
    msg["From"] = formataddr((FROM_NAME, SMTP_USER))
    msg["To"] = to.strip()
    msg["Reply-To"] = SMTP_USER
    msg.set_content(text)
    for filename, data in files:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    _send(msg)
    log.info("Сертификат %s отправлен на %s", numbers, to)


# фразы для менеджера (в Telegram)
MGR_EMAIL_OK = "\n📧 Сертификат отправлен на почту: {email}"
MGR_EMAIL_FAIL = "\n⚠️ На почту {email} отправить не получилось — перешли файлы клиенту сам."
MGR_SITE_PDF_EMAILED = "✅ Подтверждено. Клиент с сайта получил сертификат на почту и может скачать его в окне на сайте."
