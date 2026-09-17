"""ЕГРЮЛ через DaData (бесплатный тариф: 10 000 запросов/сутки). Даёт юрлицо, ИНН,
ФИО и должность руководителя — резервный источник ЛПР, когда на сайте команды нет."""
from __future__ import annotations

import os
from functools import lru_cache

import requests

BASE = "https://suggestions.dadata.ru/suggestions/api/4_1/rs"


def _headers() -> dict:
    key = os.environ.get("DADATA_API_KEY", "")
    if not key:
        raise RuntimeError("нет DADATA_API_KEY")
    return {"Authorization": f"Token {key}", "Content-Type": "application/json"}


def _pack(s: dict) -> dict:
    d = s.get("data", {})
    mgmt = d.get("management") or {}
    return {"legal_name": s.get("value", ""), "inn": d.get("inn", ""),
            "ceo_name": mgmt.get("name", ""), "ceo_post": mgmt.get("post", ""),
            "status": (d.get("state") or {}).get("status", ""),
            "okved": d.get("okved", ""), "city": ((d.get("address") or {}).get("data") or {}).get("city", "") or ""}


@lru_cache(maxsize=2048)
def by_inn(inn: str) -> dict | None:
    r = requests.post(f"{BASE}/findById/party", headers=_headers(), json={"query": inn}, timeout=20)
    r.raise_for_status()
    sug = r.json().get("suggestions") or []
    return _pack(sug[0]) if sug else None


@lru_cache(maxsize=2048)
def by_name(name: str, count: int = 5) -> list[dict]:
    """Подсказки по названию. Осторожно: одноимённых ООО много, берём только действующие
    и решение о совпадении принимает вызывающий код."""
    r = requests.post(f"{BASE}/suggest/party", headers=_headers(),
                      json={"query": name, "count": count, "status": ["ACTIVE"]}, timeout=20)
    r.raise_for_status()
    return [_pack(s) for s in r.json().get("suggestions") or []]


def greeting_name(fio: str) -> str:
    """'Иванов Иван Иванович' -> 'Иван Иванович'; 'Иван Иванов' -> 'Иван'."""
    parts = [p for p in (fio or "").replace("\xa0", " ").split() if p]
    if len(parts) >= 3:
        return " ".join(parts[1:3])
    if len(parts) == 2:
        # западный порядок 'Иван Иванов' vs ЕГРЮЛ 'Иванов Иван' — ЕГРЮЛ всегда Фамилия Имя
        return parts[1] if parts[0].endswith(("ов", "ев", "ин", "ий", "ова", "ева", "ина", "ая", "ко", "ук", "юк")) else parts[0]
    return fio or ""
