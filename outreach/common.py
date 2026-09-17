"""Общие утилиты: HTTP с кешем и ретраями, HTML → текст, домены, e-mail."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
CACHE_DIR = Path(os.environ.get("OUTREACH_CACHE", ".cache"))
TIMEOUT = 20

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Явно не адреса: картинки, домены-примеры, служебные
EMAIL_JUNK = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|example\.|sentry|wixpress|"
                        r"@(2x|3x)\b|noreply|no-reply|mailer-daemon", re.I)
# Порядок = приоритет для холодного письма: продажи/партнёрка → общий ящик → всё остальное.
ROLE_PRIORITY = ["sales", "sale", "b2b", "corp", "partner", "dealer", "opt", "export", "commerce", "kp",
                 "info", "office", "mail", "hello", "hi", "contact", "zakaz", "order", "request", "welcome",
                 "manager", "director", "general", "reception", "secretary", "market", "shop", "service",
                 "advertising", "reklama", "marketing", "media", "press", "pr", "edi", "app", "feedback",
                 "tender", "buh", "hr", "job", "career", "admin", "webmaster", "support", "help", "tech", "it"]
FIRST_TIER = 10  # индексы ROLE_PRIORITY до этого числа считаем «продажными» ящиками
# Транслит частых имён: одиночное слово в local-part считаем именным ящиком только из этого списка
FIRST_NAMES = set("""ivan petr pavel pasha sergey sergei andrey andrei alexey aleksey alexei alex dmitry dmitriy dima mikhail misha
nikolay nikolai kolya vladimir vova oleg igor anton artem artyom maxim maksim max roman kirill denis evgeny evgeniy zhenya ilya
stepan stanislav stas vitaly vitaliy vadim yury yuriy yuri konstantin kostya viktor victor vyacheslav slava boris egor timur
ruslan rustam ravil marat arkady arkadiy anatoly anatoliy grigory grigoriy leonid semen semyon fedor fyodor gleb daniil danil
anna anya olga olya elena lena natalia natalya maria masha irina ira svetlana sveta ekaterina katya tatiana tatyana anastasia
nastya yulia julia ksenia daria dasha alina marina inna elvira galina lyudmila larisa oksana polina vera nadezhda
angelika""".split())
TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
            "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def translit(word: str) -> str:
    return "".join(TRANSLIT.get(ch, ch) for ch in (word or "").lower())
# Ящики, куда холодное письмо о продажах писать бессмысленно — в самый конец списка
ROLE_LOW = ("buh", "buch", "account", "career", "vacan", "rabota", "press", "smi", "legal", "abuse",
            "admin", "webmaster", "noc", "security", "tender", "sklad", "dostavka", "delivery", "reklama")


def norm_domain(url_or_domain: str) -> str:
    """'https://www.Site.ru/path' -> 'site.ru'. Субдомены сохраняем, www убираем."""
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    return host[4:] if host.startswith("www.") else host


def root_domain(url_or_domain: str) -> str:
    """'shop.site.co.uk' -> 'site.co.uk' (регистрируемый домен)."""
    ext = tldextract.extract(norm_domain(url_or_domain))
    return ".".join(p for p in (ext.domain, ext.suffix) if p)


def _cache_path(url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def fetch(url: str, *, use_cache: bool = True, retries: int = 2) -> dict:
    """GET с кешем на диске. Возвращает {url, final_url, status, html, error}."""
    cp = _cache_path(url)
    if use_cache and cp.exists():
        return json.loads(cp.read_text())
    res = {"url": url, "final_url": url, "status": 0, "html": "", "error": ""}
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
                             timeout=TIMEOUT, allow_redirects=True)
            res.update(final_url=r.url, status=r.status_code)
            ctype = r.headers.get("content-type", "")
            if "text" in ctype or "html" in ctype or "xml" in ctype:
                # requests иногда ошибается с кодировкой на старых RU-сайтах
                if r.encoding in (None, "ISO-8859-1"):
                    r.encoding = r.apparent_encoding or "utf-8"
                res["html"] = r.text[:1_500_000]
            break
        except requests.RequestException as e:  # noqa: PERF203
            res["error"] = f"{type(e).__name__}: {e}"[:200]
            time.sleep(1.5 * (attempt + 1))
    if use_cache:
        cp.write_text(json.dumps(res, ensure_ascii=False))
    return res


def fetch_site(domain: str) -> dict:
    """Пробуем https → http, домен → www.домен. Возвращаем первый удачный ответ."""
    last = {}
    for scheme in ("https", "http"):
        for host in (domain, "www." + domain):
            r = fetch(f"{scheme}://{host}/")
            last = r
            if r["status"] and r["status"] < 400 and r["html"]:
                return r
    return last


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def page_meta(html: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    desc = ""
    m = soup.find("meta", attrs={"name": re.compile("^description$", re.I)}) or \
        soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        desc = m["content"].strip()
    return {"title": re.sub(r"\s+", " ", title)[:200], "description": desc[:400]}


def internal_links(html: str, base_url: str, keywords: tuple[str, ...]) -> list[str]:
    """Внутренние ссылки, чей href/текст содержит одно из ключевых слов."""
    soup = BeautifulSoup(html or "", "lxml")
    base_dom = root_domain(base_url)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href)
        if root_domain(full) != base_dom:
            continue
        hay = (full + " " + a.get_text(" ", strip=True)).lower()
        if any(k in hay for k in keywords):
            clean = full.split("#")[0]
            if clean not in out:
                out.append(clean)
    return out


def extract_emails(html: str) -> list[str]:
    """E-mail из текста, mailto:, а также простые обфускации «name [at] site [dot] ru»."""
    src = html or ""
    src = re.sub(r"\s*\[\s*(at|собака|@)\s*\]\s*", "@", src, flags=re.I)
    src = re.sub(r"\s*\(\s*(at|собака)\s*\)\s*", "@", src, flags=re.I)
    src = re.sub(r"\s*\[\s*(dot|точка)\s*\]\s*", ".", src, flags=re.I)
    src = src.replace("&#64;", "@").replace("%40", "@")
    found = []
    for m in EMAIL_RE.findall(src):
        e = m.strip(".").lower()
        if EMAIL_JUNK.search(e) or len(e) > 60:
            continue
        if e not in found:
            found.append(e)
    return found


def email_rank(email: str, site_domain: str, person: str = "") -> tuple:
    """Чем меньше кортеж — тем лучше адрес для холодного письма.
    Порядок: свой домен > ящик самого ЛПР > именной > sales@ > info@ > непонятное слово (snab@, msk@) > support@/hr@."""
    local, _, dom = email.partition("@")
    same = 0 if root_domain(dom) == root_domain(site_domain) else 1
    if person:  # ящик совпадает с фамилией/именем ЛПР: stepan@ для Степана, dibina@ для Дибиной
        for tok in person.split():
            plain = local.replace(".", "").replace("_", "")
            for t in {translit(tok), translit(tok).replace("ks", "x")}:  # Алексей → aleksey / alexey
                if len(t) >= 4 and (local.startswith(t[:5]) or t in plain):
                    return (same, 0, 0)
    if local.startswith(ROLE_LOW):
        return (same, 3, 0)
    role = next((i for i, k in enumerate(ROLE_PRIORITY) if local.startswith(k)), None)
    if role is None:
        # именной ящик только если похоже на имя: i.petrov / ivanov_ii / известное имя; иначе «непонятное слово»
        named = bool(re.fullmatch(r"[a-z]{1,15}[._-][a-z]{2,15}", local)) or local in FIRST_NAMES
        return (same, 1 if named else 2.5, 0)
    return (same, 1 if role < FIRST_TIER else 2 if role < 20 else 3, role)


def is_role_email(email: str) -> bool:
    local = email.split("@")[0]
    return any(local.startswith(k) for k in ROLE_PRIORITY)
