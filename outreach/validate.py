"""Проверка e-mail без отправки писем: синтаксис, MX-записи домена, совпадение
домена письма с доменом сайта. SMTP-проба (RCPT TO) — опционально, если у хоста
открыт 25-й порт; у большинства VPS он закрыт, тогда честно пишем 'smtp: недоступно'."""
from __future__ import annotations

import re
import smtplib
from functools import lru_cache

import dns.resolver

from .common import root_domain

SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
FREE_MAIL = {"gmail.com", "mail.ru", "yandex.ru", "ya.ru", "bk.ru", "list.ru", "inbox.ru",
             "rambler.ru", "outlook.com", "hotmail.com", "icloud.com", "126.com", "163.com",
             "qq.com", "yahoo.com", "proton.me"}


@lru_cache(maxsize=4096)
def mx_hosts(domain: str) -> tuple[str, ...]:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return tuple(str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference))
    except Exception:  # noqa: BLE001 — NXDOMAIN/NoAnswer/таймаут = нет MX
        try:  # RFC 5321: без MX почта идёт на A-запись
            dns.resolver.resolve(domain, "A", lifetime=8)
            return (domain,)
        except Exception:  # noqa: BLE001
            return ()


def smtp_probe(email: str, helo: str = "verify.local", timeout: int = 10) -> str:
    """'ok' | 'reject' | 'unknown' (greylist/catch-all/порт закрыт)."""
    dom = email.split("@")[1]
    hosts = mx_hosts(dom)
    if not hosts:
        return "reject"
    try:
        with smtplib.SMTP(hosts[0], 25, timeout=timeout) as s:
            s.helo(helo)
            s.mail("check@" + helo)
            code, _ = s.rcpt(email)
            if code == 250:
                return "ok"
            if code in (550, 551, 553):
                return "reject"
            return "unknown"
    except (TimeoutError, OSError, smtplib.SMTPException):
        return "unknown"


def check_email(email: str, site_domain: str = "", *, smtp: bool = False) -> dict:
    """Возвращает словарь с флагами и итоговым вердиктом для колонки таблицы."""
    email = (email or "").strip().lower()
    out = {"email": email, "syntax": False, "mx": False, "same_domain": None,
           "free_mail": False, "smtp": "не проверялось", "verdict": "нет адреса"}
    if not email:
        return out
    out["syntax"] = bool(SYNTAX_RE.match(email))
    if not out["syntax"]:
        out["verdict"] = "битый синтаксис"
        return out
    dom = email.split("@")[1]
    out["mx"] = bool(mx_hosts(dom))
    out["free_mail"] = root_domain(dom) in FREE_MAIL
    if site_domain:
        out["same_domain"] = root_domain(dom) == root_domain(site_domain)
    if smtp:
        out["smtp"] = smtp_probe(email)
    if not out["mx"]:
        out["verdict"] = "домен не принимает почту (нет MX)"
    elif out["smtp"] == "reject":
        out["verdict"] = "сервер отверг адрес"
    elif site_domain and out["same_domain"] is False:
        out["verdict"] = "MX ок, но домен письма ≠ домену сайта — проверить"
    else:
        out["verdict"] = "MX ок" + (", SMTP ок" if out["smtp"] == "ok" else "")
    return out
