"""Задачи 2 и 4. На входе — таблица компаний (CSV/XLSX или Google Sheet), на выходе —
та же таблица + колонки «Персонализация», «Источник персонализации», «Цитата-подтверждение»
и (для чужих баз) «Проверка строки» с найденными нестыковками.

    python -m outreach.personalize --in base.csv --out base_personalized.csv
    python -m outreach.personalize --sheet <ID> --worksheet "Лист1" --out audit.xlsx --audit

Как защищаемся от выдумок: модель обязана вернуть дословную цитату (evidence) с сайта,
скрипт проверяет, что цитата реально есть в скачанном тексте. Нет цитаты — нет факта,
в колонку пишем «не подтверждено», а не красивую выдумку."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import fetch, fetch_site, html_to_text, internal_links, norm_domain, page_meta, root_domain
from .llm import ask_json
from .validate import check_email

FACT_KEYS = ("about", "o-kompanii", "o_kompanii", "о компании", "о нас", "news", "новост", "blog", "блог",
             "press", "пресс", "case", "кейс", "project", "проект", "portfolio", "client", "клиент",
             "product", "продукт", "catalog", "каталог", "uslugi", "услуг", "service")
MAX_PAGES, PAGE_CHARS, CORPUS_CHARS = 6, 3500, 14000

SYSTEM = """Ты — ресёрчер в B2B-аутрич агентстве Polza Agency (запуск холодных email-кампаний для B2B-компаний).
Тебе дают текст с сайта компании-адресата. Нужно написать ПЕРСОНАЛИЗАЦИЮ — 1–2 предложения на русском,
которые встанут в первое холодное письмо после приветствия и покажут, что мы реально смотрели компанию.

Правила:
1. Только факты из текста: продукт/ниша, кому продают, свежие новости, кейсы, регионы, партнёрская программа,
   вакансии продажников, запуск нового направления. Не выдумывай цифры и события.
2. Факт должен быть полезен как зацепка про продажи/клиентов/рост (нам продавать лидогенерацию), но без лести
   и без слов «впечатляет», «уникальный», «лидер рынка».
3. Пиши от лица «мы» к «вы», естественно, как человек в письме: «Увидел, что вы …», «Заметил у вас …».
   Без тире, без восклицаний, без канцелярита.
4. Обращайся к компании на «вы» (строчная). Не повторяй название компании, если это не нужно.
5. Первое предложение — конкретный факт (что, когда, для кого). Второе, если нужно, — короткий мост к теме
   новых клиентов/продаж. Запрещены оценочные связки «это показывает», «это говорит о том», «видно, что вы».
   Не начинай каждый раз одинаково («Видим, что…»): чередуй «Заметил…», «Увидел на сайте…», «У вас в новостях…»,
   «Судя по кейсу…», «Обратил внимание…». Свежие даты (месяц, год) из текста — оставляй, они ценны.
6. Пиши только кириллицей и латиницей: иероглифы и слова на китайском в персонализацию не переносить,
   переводи смысл на русский. Названия моделей и брендов латиницей оставляй.
7. Не продавай внутри персонализации: без «мы помогаем», «мы специализируемся», «можем помочь», без упоминания
   Polza и лидогенерации. Это факт о компании плюс, при необходимости, короткий мост к теме новых клиентов,
   оффер стоит в самом письме.
8. evidence — ДОСЛОВНАЯ цитата из данного текста (30–200 символов), на которую опирается факт. Копируй символ в символ.

Ответ строго JSON:
{"personalization": "...", "evidence": "...", "source_url": "URL страницы, где цитата", "confidence": "high|medium|low"}
Если текст пустой/нерелевантный (заглушка, парковка домена, чужая компания) — personalization пустая, confidence "low"."""

AUDIT_SYSTEM = """Тебе дают название компании из чужой базы, домен сайта, e-mail и то, что реально открывается по
домену сайта и по домену e-mail (title и начало текста). Определи, нет ли путаницы.
Ответ строго JSON: {"site_matches_name": true|false|null, "email_domain_matches_name": true|false|null,
"real_company_on_site": "как называется компания на сайте (кратко)", "real_company_on_email_domain": "…",
"country": "страна компании на сайте, если понятна", "note": "одна фраза: что не так или 'ок'"}"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", (s or "").lower()).strip()


# ---------- чтение/запись таблиц (CSV / XLSX / Google Sheet) ----------

def read_table(path: str | None, sheet: str | None, worksheet: str | None) -> tuple[list[str], list[dict]]:
    if sheet:
        from .sheets import read_rows
        raw = read_rows(sheet, worksheet)
    elif path and path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True).active
        raw = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            raw = list(csv.reader(f))
    raw = [r for r in raw if any(c.strip() for c in r)]
    if not raw:
        return [], []
    # Заголовка может не быть (как в базе Polza): если в первой строке есть '@' или домен — это данные
    first = " ".join(raw[0])
    if "@" in first or re.search(r"\.[a-z]{2,}(\s|$)", first):
        header = _guess_header(raw[0])
        body = raw
    else:
        header, body = raw[0], raw[1:]
    rows = [dict(zip(header, r + [""] * (len(header) - len(r)), strict=False)) for r in body]
    return header, rows


def _guess_header(sample: list[str]) -> list[str]:
    h = []
    for i, v in enumerate(sample):
        v = v.strip()
        h.append("Email" if "@" in v else "Сайт" if re.search(r"\.[a-z]{2,}$", v) else "Компания" if i == 0 else f"col{i}")
    return h


def col(row: dict, *names: str) -> str:
    """Берём значение по любому из синонимов колонки, без учёта регистра."""
    low = {k.strip().lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low and str(low[n.lower()]).strip():
            return str(low[n.lower()]).strip()
    return ""


def write_table(path: str, header: list[str], rows: list[dict]):
    if path.lower().endswith(".xlsx"):
        import openpyxl
        from openpyxl.styles import Alignment, Font
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Персонализация"
        ws.append(header)
        for r in rows:
            ws.append([r.get(h, "") for h in header])
        for c in ws[1]:
            c.font = Font(bold=True)
        for i, h in enumerate(header, 1):
            width = 60 if h in ("Персонализация", "Проверка строки", "Цитата-подтверждение") else 28
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        wb.save(path)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


# ---------- сбор фактов и персонализация ----------

def collect_corpus(domain: str) -> list[dict]:
    """Главная + до MAX_PAGES внутренних страниц «о компании / новости / кейсы / продукты»."""
    home = fetch_site(domain)
    if not home["html"]:
        return []
    pages = [{"url": home["final_url"], "text": html_to_text(home["html"])[:PAGE_CHARS], **page_meta(home["html"])}]
    for u in internal_links(home["html"], home["final_url"], FACT_KEYS)[:MAX_PAGES]:
        r = fetch(u)
        if r["html"]:
            t = html_to_text(r["html"])
            if len(t) > 200:
                pages.append({"url": u, "text": t[:PAGE_CHARS], **page_meta(r["html"])})
    return pages


def grounded(evidence: str, pages: list[dict]) -> str | None:
    """URL страницы, где цитата реально встречается (сравниваем без пунктуации/регистра)."""
    ev = _norm(evidence)
    if len(ev) < 15:
        return None
    for p in pages:
        if ev in _norm(p["text"]) or ev[:80] in _norm(p["text"]):
            return p["url"]
    return None


def personalize_one(company: str, domain: str, pages: list[dict], extra: str = "") -> dict:
    if not pages:
        return {"Персонализация": "", "Источник персонализации": "", "Цитата-подтверждение": "",
                "Статус персонализации": "сайт не открылся"}
    corpus = "\n\n".join(f"[{p['url']}] {p.get('title','')}\n{p['text']}" for p in pages)[:CORPUS_CHARS]
    user = f"Компания: {company}\nСайт: {domain}\n{extra}\n\nТЕКСТ СТРАНИЦ:\n{corpus}"
    for attempt in range(2):
        try:
            ans = ask_json(SYSTEM, user if attempt == 0 else user + "\n\nВНИМАНИЕ: прошлая цитата не нашлась в тексте. "
                           "Скопируй evidence дословно, без изменений и сокращений.")
        except RuntimeError as e:
            return {"Персонализация": "", "Источник персонализации": "", "Цитата-подтверждение": "",
                    "Статус персонализации": f"LLM: {e}"[:120]}
        text = (ans.get("personalization") or "").strip()
        src = grounded(ans.get("evidence", ""), pages)
        if text and src:
            return {"Персонализация": text, "Источник персонализации": src,
                    "Цитата-подтверждение": ans.get("evidence", "").strip()[:200],
                    "Статус персонализации": f"подтверждено ({ans.get('confidence','')})"}
        if not text:
            break
    return {"Персонализация": text, "Источник персонализации": pages[0]["url"],
            "Цитата-подтверждение": ans.get("evidence", "").strip()[:200],
            "Статус персонализации": "НЕ ПОДТВЕРЖДЕНО цитатой, проверить вручную" if text else "фактов не найдено"}


def audit_row(company: str, site: str, email: str, pages: list[dict], dupes: set[str]) -> dict:
    """Задача 4: ищем «перепутанное» — e-mail чужого домена, сайт не той компании, дубли, мёртвые адреса."""
    flags: list[str] = []
    site_dom, mail_dom = root_domain(site), root_domain(email.split("@")[-1]) if "@" in email else ""
    if not pages:
        flags.append("сайт не открывается")
    if site_dom in dupes:
        flags.append("такой же сайт у другой строки")
    chk = check_email(email, site) if email else {"verdict": "e-mail пустой", "mx": False}
    if email and not chk["mx"]:
        flags.append("домен e-mail не принимает почту")
    mail_pages: list[dict] = []
    if mail_dom and mail_dom != site_dom:
        flags.append(f"домен e-mail ({mail_dom}) не совпадает с сайтом ({site_dom})")
        mail_pages = collect_corpus(mail_dom)[:1]
    if chk.get("free_mail"):
        flags.append("e-mail на публичной почте")
    # LLM сверяет название с тем, что реально на сайте / на домене e-mail
    verdict = {}
    try:
        desc = lambda ps: (f"title: {ps[0].get('title','')}\n{ps[0]['text'][:1200]}" if ps else "не открывается")  # noqa: E731
        verdict = ask_json(AUDIT_SYSTEM, f"Компания: {company}\nСайт: {site}\nEmail: {email}\n\n"
                           f"ПО ДОМЕНУ САЙТА {site_dom}:\n{desc(pages)}\n\nПО ДОМЕНУ E-MAIL {mail_dom or '—'}:\n{desc(mail_pages)}")
    except RuntimeError:
        pass
    if verdict.get("site_matches_name") is False:
        flags.append(f"на сайте другая компания: {verdict.get('real_company_on_site','?')}")
    if mail_dom and mail_dom != site_dom and verdict.get("email_domain_matches_name"):
        flags.append(f"e-mail принадлежит именно «{company}» → сайт в строке, похоже, чужой")
    country = (verdict.get("country") or "").strip()
    if country and not re.search(r"росси|russia|рф", country, re.I):
        flags.append(f"компания не из России ({country})")
    return {"Проверка e-mail": chk["verdict"], "Проверка строки": "; ".join(flags) if flags else "ок",
            "Что реально на сайте": verdict.get("real_company_on_site", ""),
            "Что на домене e-mail": verdict.get("real_company_on_email_domain", "")}


def process(rows: list[dict], *, audit: bool, workers: int) -> None:
    sites = [root_domain(col(r, "Сайт", "site", "website", "domain", "url")) for r in rows]
    dupes = {d for d in sites if d and sites.count(d) > 1}

    def work(i: int):
        r = rows[i]
        company = col(r, "Компания", "name", "company", "Название")
        site = norm_domain(col(r, "Сайт", "site", "website", "domain", "url"))
        email = col(r, "Email", "e-mail", "почта")
        pages = collect_corpus(site) if site else []
        out = {}
        if audit:
            out.update(audit_row(company, site, email, pages, dupes))
        extra = f"ЛПР: {col(r, 'Имя ЛПР', 'name_lpr', 'contact')}" if col(r, "Имя ЛПР") else ""
        out.update(personalize_one(company, site, pages, extra))
        return i, out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, i) for i in range(len(rows))]
        for n, fut in enumerate(as_completed(futs), 1):
            i, out = fut.result()
            rows[i].update(out)
            print(f"[{n}/{len(rows)}] {col(rows[i], 'Компания', 'name')[:28]:28} {out.get('Статус персонализации','')}"
                  f"{' | ' + out['Проверка строки'][:60] if audit else ''}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", help="CSV/XLSX на входе")
    ap.add_argument("--sheet", help="ID Google Таблицы на входе (вместо --in)")
    ap.add_argument("--worksheet", help="имя листа в Google Таблице")
    ap.add_argument("--out", required=True, help="CSV/XLSX на выходе")
    ap.add_argument("--push-sheet", help="ID Google Таблицы, куда записать результат")
    ap.add_argument("--push-worksheet", default="Персонализация")
    ap.add_argument("--audit", action="store_true", help="режим проверки чужой базы (задача 4)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    if not (a.inp or a.sheet):
        ap.error("нужен --in или --sheet")

    header, rows = read_table(a.inp, a.sheet, a.worksheet)
    if a.limit:
        rows = rows[: a.limit]
    print(f"строк: {len(rows)}; колонки: {header}", file=sys.stderr)
    process(rows, audit=a.audit, workers=a.workers)

    new_cols = (["Проверка e-mail", "Проверка строки", "Что реально на сайте", "Что на домене e-mail"] if a.audit else []) + \
               ["Персонализация", "Источник персонализации", "Цитата-подтверждение", "Статус персонализации"]
    out_header = header + [c for c in new_cols if c not in header]
    write_table(a.out, out_header, rows)
    print(f"записано: {a.out}", file=sys.stderr)
    if a.push_sheet:
        from .sheets import write_rows
        url = write_rows(a.push_sheet, a.push_worksheet, [out_header] + [[r.get(h, "") for h in out_header] for r in rows])
        print(f"Google Sheet: {url}", file=sys.stderr)


if __name__ == "__main__":
    main()
