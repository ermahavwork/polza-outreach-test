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
ROLE_PRIORITY = ["sales", "sale", "b2b", "corp", "partner", "info", "office", "mail",
                 "hello", "contact", "zakaz", "order", "welcome", "manager", "pr",
                 "hr", "job", "support", "help"]
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


def email_rank(email: str, site_domain: str) -> tuple:
    """Чем меньше кортеж — тем лучше адрес для холодного письма."""
    local, _, dom = email.partition("@")
    same = 0 if root_domain(dom) == root_domain(site_domain) else 1
    if local.startswith(ROLE_LOW):
        return (same, 3, 0)
    role = next((i for i, k in enumerate(ROLE_PRIORITY) if local.startswith(k)), None)
    # именной ящик (ivanov, i.petrov) лучше sales@, sales@ лучше info@, info@ лучше support@
    if role is None:
        kind = 1 if re.fullmatch(r"[a-z]+([._-][a-z]+)?", local) else 2
        return (same, kind, 0)
    return (same, 1 if role <= 4 else 2, role)


def is_role_email(email: str) -> bool:
    local = email.split("@")[0]
    return any(local.startswith(k) for k in ROLE_PRIORITY)
