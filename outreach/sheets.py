"""Чтение/запись Google Sheets через gspread. Авторизация: файл authorized_user
(GOOGLE_OAUTH_FILE, по умолчанию ~/.config/gws/credentials.json) либо сервисный
аккаунт (GOOGLE_APPLICATION_CREDENTIALS). Если нет ни того ни другого — работаем с CSV/XLSX."""
from __future__ import annotations

import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


def client():
    import gspread
    sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if sa and Path(sa).exists():
        return gspread.service_account(filename=sa, scopes=SCOPES)
    oauth = Path(os.environ.get("GOOGLE_OAUTH_FILE", "~/.hermes/google_token.json")).expanduser()
    if oauth.exists():
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(oauth), SCOPES)
        return gspread.authorize(creds)
    raise RuntimeError("нет учётных данных Google (GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_OAUTH_FILE)")


def read_rows(spreadsheet_id: str, worksheet: str | None = None) -> list[list[str]]:
    sh = client().open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    return ws.get_all_values()


def write_rows(spreadsheet_id: str, worksheet: str, rows: list[list], *, clear: bool = True):
    sh = client().open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(worksheet)
    except Exception:  # noqa: BLE001 — листа нет, создаём
        ws = sh.add_worksheet(title=worksheet, rows=max(100, len(rows) + 10), cols=max(26, len(rows[0])))
    if clear:
        ws.clear()
    ws.update(range_name="A1", values=[[("" if v is None else str(v)) for v in r] for r in rows],
              value_input_option="RAW")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}, "wrapStrategy": "WRAP"})
    return ws.url
