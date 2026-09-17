"""Юнит-тесты чистых функций — без сети и без LLM."""
from outreach.common import email_rank, extract_emails, is_role_email, norm_domain, root_domain
from outreach.dadata import greeting_name
from outreach.personalize import _guess_header, grounded


def test_norm_domain_strips_scheme_and_www():
    assert norm_domain("https://www.Site.RU/path?x=1") == "site.ru"
    assert norm_domain("shop.site.ru") == "shop.site.ru"
    assert norm_domain("") == ""


def test_root_domain():
    assert root_domain("https://shop.site.co.uk/x") == "site.co.uk"
    assert root_domain("mail.company.ru") == "company.ru"


def test_extract_emails_handles_obfuscation_and_junk():
    html = 'Пишите: sales [at] site [dot] ru; <a href="mailto:Info@Site.ru">почта</a>; logo@2x.png; a@b.jpg'
    assert extract_emails(html) == ["sales@site.ru", "info@site.ru"]


def test_email_rank_prefers_own_domain_then_named_then_sales_then_info():
    ranked = sorted(["info@site.ru", "support@site.ru", "ivanov@site.ru", "sales@site.ru", "office@gmail.com"],
                    key=lambda e: email_rank(e, "site.ru"))
    assert ranked[0] in ("ivanov@site.ru", "sales@site.ru")
    assert ranked[-1] == "office@gmail.com"
    assert ranked.index("info@site.ru") < ranked.index("support@site.ru")


def test_is_role_email():
    assert is_role_email("sales@x.ru") and is_role_email("info@x.ru")
    assert not is_role_email("d.petrov@x.ru")


def test_greeting_name_from_egrul_format():
    assert greeting_name("Калашников Дмитрий Сергеевич") == "Дмитрий Сергеевич"
    assert greeting_name("Иванов Иван") == "Иван"
    assert greeting_name("") == ""


def test_headerless_table_is_detected():
    assert _guess_header(["Rogen Technologies", "sales@jatcarbide.com", "jat-carbide.com"]) == ["Компания", "Email", "Сайт"]


def test_grounded_requires_quote_in_corpus():
    pages = [{"url": "https://a.ru/news", "text": "В июле 2026 компания открыла склад в Казани для дилеров."}]
    assert grounded("открыла склад в Казани для дилеров", pages) == "https://a.ru/news"
    assert grounded("открыла склад в Новосибирске для дилеров", pages) is None
    assert grounded("короткая", pages) is None
