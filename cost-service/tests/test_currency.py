"""Валюты: суммы хранятся в долларах, показываются и вводятся в валюте пользователя по курсу."""

import pytest
from sqlalchemy import select

from app import currency
from app.db import SessionLocal
from app.models import Server, StoragePool, User

from .conftest import ADMIN, create_user, login
from .test_flow import create_service, expense_rows, part_rows


@pytest.fixture
def rates(client):
    """Курсы на время теста; после — снова только доллар."""
    yield client
    currency.set_rates({"EUR": None, "RUB": None})
    with SessionLocal() as s:
        for user in s.scalars(select(User)):
            user.currency = "USD"
        s.commit()
    login(client, *ADMIN)


def set_currency(c, code: str):
    return c.post("/currency", data={"currency": code, "next": "/servers"}, follow_redirects=False)


def test_rates_admin(rates):
    c = rates
    page = c.get("/admin/currency").text
    assert 'name="EUR"' in page and 'name="RUB"' in page
    # Без курса рубль выбрать нельзя
    assert "disabled" in c.get("/servers").text
    set_currency(c, "RUB")
    with SessionLocal() as s:
        assert s.scalar(select(User.currency).where(User.username == ADMIN[0])) == "USD"

    assert c.post("/admin/currency", data={"EUR": "0", "RUB": ""}).status_code == 422
    assert c.post("/admin/currency", data={"EUR": "abc", "RUB": ""}).status_code == 422
    r = c.post("/admin/currency", data={"EUR": "0,92", "RUB": "90"}, follow_redirects=False)
    assert r.status_code == 303
    assert currency.rates() == {"EUR": 0.92, "RUB": 90.0}
    assert currency.available() == ["USD", "EUR", "RUB"]
    assert 'value="0.92"' in c.get("/admin/currency").text

    # Курсы задаёт только администратор, валюту показа выбирает любой
    create_user(c, "cur-viewer", "viewer")
    login(c, "cur-viewer", "user-pass-123")
    assert c.get("/admin/currency").status_code == 403
    assert set_currency(c, "EUR").headers["location"] == "/servers"
    with SessionLocal() as s:
        assert s.scalar(select(User.currency).where(User.username == "cur-viewer")) == "EUR"

    # Курс убрали — пользователи этой валюты возвращаются к доллару
    login(c, *ADMIN)
    c.post("/admin/currency", data={"EUR": "", "RUB": "90"})
    with SessionLocal() as s:
        assert s.scalar(select(User.currency).where(User.username == "cur-viewer")) == "USD"


def test_input_and_display_in_rubles(rates):
    c = rates
    c.post("/admin/currency", data={"EUR": "0.9", "RUB": "90"})
    set_currency(c, "RUB")
    sid = create_service(c, "Currency")

    # Цены вводятся в рублях, хранятся в долларах
    parts = part_rows({"category": "cpu", "capacity": "8", "price": "9 000"},
                      {"category": "disk", "capacity": "1000", "disk_type": "ssd", "price": "4500"})
    expenses = expense_rows({"name": "Электроэнергия", "amount": "1800", "period": "monthly"},
                            {"name": "Поддержка", "amount": "12", "period": "percent"})
    r = c.post("/servers", data={"name": "rub-1", "count": "1", "service_id": str(sid), **parts, **expenses},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        srv = s.scalar(select(Server).where(Server.name == "rub-1"))
        assert [p.price for p in srv.parts] == [100, 50]
        assert [(e.amount, e.period) for e in srv.expenses] == [(20, "monthly"), (12, "percent")]  # % не пересчитан
        server_id = srv.id

    # Форма показывает рубли без хвостов от пересчёта, страницы — суммы в рублях
    form = c.get(f"/servers/{server_id}").text
    assert 'value="9000"' in form and 'value="4500"' in form and 'value="1800"' in form and 'value="12"' in form
    assert "Цена 1 шт., ₽" in form
    page = c.get(f"/services/{sid}").text
    assert "15 300,00 ₽" in page  # 9000 + 4500 + 1800
    assert "1 $ = 90" in page

    # Ручная цена ГБ подпространства — тоже в рублях
    c.post(f"/services/{sid}/pools", data={"name": "ssd", "disk_type": "ssd", "usable_percent": "100",
                                           "price_source": "manual", "manual_price": "4,5"})
    with SessionLocal() as s:
        assert s.scalar(select(StoragePool.manual_price_per_gb).where(StoragePool.service_id == sid)) == 0.05

    # API: по умолчанию доллары, ?currency= — пересчёт
    data = c.get(f"/api/services/{sid}").json()
    assert data["currency"] == "USD" and data["monthly"]["infrastructure"] == 170
    data = c.get(f"/api/services/{sid}?currency=rub").json()
    assert data["currency"] == "RUB" and data["monthly"]["infrastructure"] == 15_300
    assert data["pools"][0]["price_per_gb"] == pytest.approx(4.5)
    assert c.get(f"/api/services/{sid}?currency=GBP").status_code == 400

    # В долларах — исходные суммы
    set_currency(c, "USD")
    assert 'value="100"' in c.get(f"/servers/{server_id}").text
