"""Тонкий слой над LLM. По умолчанию — Claude Code CLI (`claude -p`), т.е. подписка,
без отдельного API-ключа. Резерв: Anthropic API, Gemini. Все ответы — строгий JSON."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

PROVIDER = os.environ.get("OUTREACH_LLM", "auto")  # auto | claude-cli | anthropic | gemini
CLAUDE_MODEL = os.environ.get("OUTREACH_CLAUDE_MODEL", "haiku")
ANTHROPIC_MODEL = os.environ.get("OUTREACH_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
GEMINI_MODEL = os.environ.get("OUTREACH_GEMINI_MODEL", "gemini-2.5-flash")


def _extract_json(text: str) -> dict:
    """Достаём первый JSON-объект из ответа модели (модели любят обёртки ```json)."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"нет JSON в ответе: {text[:200]!r}")
    return json.loads(m.group(0))


def _claude_cli(system: str, user: str) -> str:
    cmd = ["claude", "-p", "--model", CLAUDE_MODEL, "--output-format", "json",
           "--permission-mode", "dontAsk", "--no-session-persistence",
           "--append-system-prompt", system, user]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"claude -p rc={p.returncode}: {p.stderr[-300:]}")
    data = json.loads(p.stdout)
    return data.get("result", "")


def _anthropic(system: str, user: str) -> str:
    import requests
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                               "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": ANTHROPIC_MODEL, "max_tokens": 600, "system": system,
                            "messages": [{"role": "user", "content": user}]}, timeout=120)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def _gemini(system: str, user: str) -> str:
    import requests
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}"
           f":generateContent?key={os.environ['GEMINI_API_KEY']}")
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.3, "response_mime_type": "application/json"}}
    r = requests.post(url, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def pick_provider() -> str:
    if PROVIDER != "auto":
        return PROVIDER
    if shutil.which("claude"):
        return "claude-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    raise RuntimeError("нет ни claude CLI, ни ANTHROPIC_API_KEY, ни GEMINI_API_KEY")


def ask_json(system: str, user: str, retries: int = 2) -> dict:
    prov = pick_provider()
    fn = {"claude-cli": _claude_cli, "anthropic": _anthropic, "gemini": _gemini}[prov]
    last = None
    for i in range(retries + 1):
        try:
            return _extract_json(fn(system, user))
        except Exception as e:  # noqa: BLE001 — ретраим любую ошибку провайдера/парсинга
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"LLM ({prov}) не ответил валидным JSON: {last}")
