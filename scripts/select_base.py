"""Отбор финальной базы из обогащённого CSV: только строки с e-mail на домене компании и живым MX,
приоритет строкам с найденным ЛПР, ровное распределение по сегментам.

    python scripts/select_base.py --in out/base_all.csv --out out/base.csv --n 60"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from outreach.common import email_rank, is_role_email  # noqa: E402
from outreach.dadata import greeting_name  # noqa: E402
from outreach.validate import check_email  # noqa: E402

SEGMENT_NAMES = {  # приводим метки двух сборщиков к одному словарю
    "saas": "B2B SaaS", "it-аутсорс": "IT-аутсорс / интеграторы", "промышлен": "Промышленные поставщики",
    "логистик": "Логистика / ВЭД", "b2b-услуги": "B2B-услуги", "маркетинг": "B2B-маркетинг / лидген",
    "корп. it": "Корпоративный IT", "ритейл": "Поставщики ритейла / HoReCa", "строитель": "Строительство / инженерия",
    "образование": "Обучение / консалтинг",
}


def segment(label: str) -> str:
    low = label.lower()
    return next((v for k, v in SEGMENT_NAMES.items() if k in low), label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=60)
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.inp, encoding="utf-8")))
    good = []
    for r in rows:
        emails = [e.strip() for e in r["Все email с сайта"].split(",") if "@" in e]
        if not emails:
            continue
        emails.sort(key=lambda e: email_rank(e, r["Сайт"], r["Имя ЛПР"]))  # пересчёт лучшего адреса свежим ранжированием
        best = emails[0]
        chk = check_email(best, r["Сайт"])
        if not chk["mx"] or chk["same_domain"] is False:
            continue
        r["Email"], r["Тип email"], r["Проверка email"] = best, ("ролевой" if is_role_email(best) else "именной"), chk["verdict"]
        r["Сегмент"] = segment(r["Сегмент"])
        if r["Имя ЛПР"].isupper():
            r["Имя ЛПР"] = greeting_name(r["Имя ЛПР"])
        r["Город"] = re.sub(r"\s*\(.*?\)", "", r["Город"]).strip()
        good.append(r)
    # приоритет: есть ЛПР и должность → есть ЛПР → остальное; затем round-robin по сегментам
    good.sort(key=lambda r: (0 if r["Имя ЛПР"] and r["Должность"] else 1 if r["Имя ЛПР"] else 2))
    buckets: dict[str, list] = defaultdict(list)
    for r in good:
        buckets[r["Сегмент"]].append(r)
    picked = []
    while len(picked) < a.n and any(buckets.values()):
        for seg in list(buckets):
            if buckets[seg] and len(picked) < a.n:
                picked.append(buckets[seg].pop(0))
    picked.sort(key=lambda r: (r["Сегмент"], r["Компания"]))
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(picked)
    print(f"всего {len(rows)}, прошли фильтр e-mail {len(good)}, отобрано {len(picked)}, "
          f"с ЛПР {sum(1 for r in picked if r['Имя ЛПР'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
