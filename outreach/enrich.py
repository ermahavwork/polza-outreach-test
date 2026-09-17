"""Задача 1. Из списка компаний (название + сайт) собираем базу для аутрича:
контактный e-mail с сайта, имя ЛПР (страница команды → ЕГРЮЛ по ИНН из подвала сайта),
проверка e-mail (синтаксис, MX, домен). Всё — параллельно, с кешем страниц на диске.

    python -m outreach.enrich --in seeds.csv --out base.csv [--workers 8] [--smtp]

Вход: CSV с колонками name,site[,segment,city,why_b2b,email_seen,source_url].
Выход: CSV с колонками, готовыми для Google Таблицы (см. COLUMNS)."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import dadata
from .common import (
    email_rank,
    extract_emails,
    fetch,
    fetch_site,
    html_to_text,
    internal_links,
    is_role_email,
    norm_domain,
    page_meta,
    root_domain,
)
from .llm import ask_json
from .validate import check_email

CONTACT_KEYS = ("contact", "kontakt", "контакт", "svyaz", "связ", "rekvizit", "реквизит")
ABOUT_KEYS = ("about", "o-kompanii", "o_kompanii", "о компании", "о нас", "team", "команда",
              "rukovodstvo", "руководств", "management", "leadership", "sotrudniki")
LEGAL_KEYS = ("policy", "politika", "privacy", "oferta", "оферт", "конфиденциальн", "agreement", "soglashenie")
OOO_RE = re.compile(r"\b(?:ООО|АО|ПАО|ЗАО|ИП)\s*[«\"“]([^»\"”]{2,60})[»\"”]")
INN_RE = re.compile(r"ИНН\W{0,5}(\d{10}|\d{12})(?!\d)")
NAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:\s[А-ЯЁ][а-яё]+){1,2}$")

COLUMNS = ["Компания", "Сайт", "Сегмент", "Город", "Имя ЛПР", "Должность", "Откуда ЛПР",
           "Email", "Тип email", "Проверка email", "Все email с сайта", "Юрлицо / ИНН",
           "Признак отдела продаж", "Где нашли компанию", "Заметки сборки"]

LPR_SYSTEM = ("Ты извлекаешь из текста страницы сайта компании имя человека, которому логично писать "
              "холодное B2B-письмо о лидогенерации: в порядке приоритета — коммерческий директор / "
              "руководитель отдела продаж / директор по развитию / основатель / генеральный директор. "
              "Отвечай ТОЛЬКО JSON: {\"name\": \"Имя Отчество Фамилия как в тексте или пусто\", "
              "\"role\": \"должность как в тексте\", \"evidence\": \"дословная цитата из текста (до 150 символов), "
              "где названы имя и должность\"}. Если в тексте нет людей с должностями — name пустой. "
              "Ничего не выдумывай.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", (s or "").lower()).strip()


def find_person_on_site(pages: list[dict]) -> dict:
    """LLM ищет ЛПР на страницах «о компании/команда»; принимаем ответ, только если цитата
    действительно есть в тексте (защита от выдумок)."""
    for p in pages:
        text = p["text"]
        if len(text) < 300 or not re.search(r"директор|руководител|основател|founder|CEO|команда", text, re.I):
            continue
        try:
            ans = ask_json(LPR_SYSTEM, f"URL: {p['url']}\n\nТЕКСТ:\n{text[:9000]}")
        except RuntimeError:
            continue
        name, ev = (ans.get("name") or "").strip(), (ans.get("evidence") or "").strip()
        if name and ev and _norm(ev)[:60] in _norm(text) and _norm(name.split()[0]) in _norm(text):
            return {"name": name, "role": (ans.get("role") or "").strip()[:80], "source": f"сайт: {p['url']}"}
    return {}


def enrich_one(seed: dict, *, smtp: bool = False) -> dict:
    site = norm_domain(seed.get("site", ""))
    row = {c: "" for c in COLUMNS}
    row.update({"Компания": seed.get("name", "").strip(), "Сайт": site,
                "Сегмент": seed.get("segment", ""), "Город": seed.get("city", ""),
                "Признак отдела продаж": seed.get("why_b2b", ""), "Где нашли компанию": seed.get("source_url", "")})
    notes: list[str] = []

    home = fetch_site(site)
    if not home["html"]:
        row["Заметки сборки"] = f"сайт не открылся ({home['status'] or home['error']})"
        return row
    base_url = home["final_url"]
    meta = page_meta(home["html"])
    pages = [{"url": base_url, "html": home["html"], "text": html_to_text(home["html"])}]
    # страницы контактов и «о компании» — до 3 каждого типа
    for keys, limit in ((CONTACT_KEYS, 3), (ABOUT_KEYS, 3), (LEGAL_KEYS, 1)):
        for u in internal_links(home["html"], base_url, keys)[:limit]:
            r = fetch(u)
            if r["html"]:
                pages.append({"url": u, "html": r["html"], "text": html_to_text(r["html"])})

    # --- e-mail: со всех страниц, лучший по рангу (свой домен > именной > sales@ > info@) ---
    emails: list[str] = []
    for p in pages:
        for e in extract_emails(p["html"]):
            if e not in emails:
                emails.append(e)
    seen = (seed.get("email_seen") or "").strip().lower()
    if seen and seen not in emails and "@" in seen:
        emails.append(seen)
    emails.sort(key=lambda e: email_rank(e, site))
    row["Все email с сайта"] = ", ".join(emails[:6])
    if emails:
        best = emails[0]
        row["Email"] = best
        row["Тип email"] = "ролевой" if is_role_email(best) else "именной"
        row["Проверка email"] = check_email(best, site, smtp=smtp)["verdict"]
    else:
        notes.append("e-mail на сайте не найден")

    # --- юрлицо по ИНН из подвала → ФИО руководителя из ЕГРЮЛ ---
    inn = next((m.group(1) for p in pages for m in [INN_RE.search(p["text"])] if m), "")
    legal = None
    try:
        if inn:
            legal = dadata.by_inn(inn)
        else:
            # ИНН нет — ищем по названию юрлица из подвала (ООО «…»), затем по бренду;
            # принимаем только если все слова бренда входят в название юрлица
            brand = row["Компания"]
            ooo = next((m.group(1) for p in pages for m in [OOO_RE.search(p["text"])] if m), "")
            for q in [x for x in (ooo, brand) if x]:
                cands = dadata.by_name(q)
                toks = [t for t in _norm(q).split() if len(t) > 2]
                hit = next((c for c in cands if toks and all(t in _norm(c["legal_name"]) for t in toks)), None)
                if hit:
                    legal = hit
                    notes.append(f"юрлицо подобрано по названию «{q}» — сверить")
                    break
    except Exception as e:  # noqa: BLE001
        notes.append(f"DaData: {e}"[:80])
    if legal:
        row["Юрлицо / ИНН"] = f"{legal['legal_name']} / {legal['inn']}" + \
            ("" if legal["status"] == "ACTIVE" else f" ({legal['status']})")
        if not row["Город"]:
            row["Город"] = legal["city"]

    # --- ЛПР: сначала сайт (команда/руководство), потом директор из ЕГРЮЛ ---
    person = find_person_on_site([p for p in pages[1:] if any(k in p["url"].lower() for k in ABOUT_KEYS)] or pages[:1])
    if person:
        full = person["name"]
        # ФИО в формате «Фамилия Имя Отчество» сокращаем до обращения, иначе оставляем как на сайте
        row["Имя ЛПР"] = dadata.greeting_name(full) if NAME_RE.match(full) and len(full.split()) == 3 else full
        row["Должность"], row["Откуда ЛПР"] = person["role"], person["source"]
    elif legal and legal["ceo_name"]:
        row["Имя ЛПР"] = dadata.greeting_name(legal["ceo_name"])
        row["Должность"] = legal["ceo_post"].capitalize() or "Руководитель"
        row["Откуда ЛПР"] = f"ЕГРЮЛ (DaData) по ИНН {legal['inn']}"
    else:
        notes.append("ЛПР не найден ни на сайте, ни по ИНН")

    if meta["title"]:
        notes.append(f"title: {meta['title'][:70]}")
    row["Заметки сборки"] = "; ".join(notes)
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="CSV с семенами (name,site,...)")
    ap.add_argument("--out", required=True, help="куда писать базу (CSV)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--smtp", action="store_true", help="пробовать SMTP RCPT (нужен открытый 25 порт)")
    a = ap.parse_args(argv)

    with open(a.inp, newline="", encoding="utf-8") as f:
        seeds = [r for r in csv.DictReader(f) if (r.get("site") or "").strip()]
    # дедуп по регистрируемому домену — один домен = одна компания
    uniq, seen = [], set()
    for s in seeds:
        d = root_domain(s["site"])
        if d and d not in seen:
            seen.add(d)
            uniq.append(s)
    print(f"семян: {len(seeds)}, уникальных доменов: {len(uniq)}", file=sys.stderr)

    rows = [None] * len(uniq)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(enrich_one, s, smtp=a.smtp): i for i, s in enumerate(uniq)}
        for n, fut in enumerate(as_completed(futs), 1):
            i = futs[fut]
            try:
                rows[i] = fut.result()
            except Exception as e:  # noqa: BLE001 — одна упавшая компания не валит прогон
                rows[i] = {**{c: "" for c in COLUMNS}, "Компания": uniq[i].get("name", ""),
                           "Сайт": norm_domain(uniq[i]["site"]), "Заметки сборки": f"ошибка: {e}"[:200]}
            r = rows[i]
            print(f"[{n}/{len(uniq)}] {r['Компания'][:30]:30} {r['Email'] or '-':35} {r['Имя ЛПР'] or '-'}", file=sys.stderr)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    ok = sum(1 for r in rows if r["Email"])
    print(json.dumps({"rows": len(rows), "with_email": ok, "with_lpr": sum(1 for r in rows if r["Имя ЛПР"])},
                     ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
