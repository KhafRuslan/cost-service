"""Полный сценарий через веб-интерфейс: серверы → сервис → подпространство → datastream из Prometheus."""

import pytest
from sqlalchemy import func, select

from app import prom
from app.db import SessionLocal
from app.models import (PrometheusSource, Resource, ResourceSample, Server, ServerExpense, Service, ServiceServer,
                        StoragePool)

from .conftest import PROM_RESULTS, last_id, vector

DS_QUERY = 'sum(elasticsearch_indices_store_size_bytes_total{index=~".ds-logs-.*"})'


def part_rows(*rows: dict) -> dict:
    """Строки комплектующих в виде параллельных списков part_*."""
    fields = ("category", "name", "spec", "capacity", "disk_type", "quantity", "price", "price_mode",
              "amortization_months")
    defaults = {"name": "", "spec": "", "capacity": "", "disk_type": "ssd", "quantity": "1", "price": "",
                "price_mode": "monthly", "amortization_months": "60"}
    return {f"part_{f}": [str({**defaults, **r}[f]) for r in rows] for f in fields}


SERVER_PARTS = part_rows(
    {"category": "cpu", "name": "Xeon Gold 6338", "spec": "2,0 ГГц", "capacity": "32", "quantity": "2",
     "price": "300 000", "price_mode": "capex", "amortization_months": "60"},
    {"category": "ram", "capacity": "64", "quantity": "8", "price": "1000"},
    {"category": "disk", "name": "PM9A3", "capacity": "3840", "disk_type": "nvme", "quantity": "4",
     "price": "120000", "price_mode": "capex", "amortization_months": "60"},
    {"category": "other"},  # пустая строка пропускается
)


def add_source(c, name="prom") -> int:
    r = c.post("/admin/prometheus", data={"name": name, "url": "http://prom:9090/", "auth_type": "none",
                                          "action": "save"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        return s.scalar(select(PrometheusSource.id).where(PrometheusSource.name == name))


def create_service(c, name: str) -> int:
    r = c.post("/services", data={"name": name, "description": "кластер логов"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return last_id(r)


def test_full_flow(client):
    c = client
    PROM_RESULTS[DS_QUERY] = vector(1e12, 0.5e12)
    source_id = add_source(c)
    with SessionLocal() as s:
        assert s.get(PrometheusSource, source_id).url == "http://prom:9090"

    sid = create_service(c, "Elasticsearch")
    assert c.post("/services", data={"name": "Elasticsearch"}).status_code == 422  # имя занято

    # Серверы: 3 одинаковых, сразу в сервис целиком; комплектующие с ценой в месяц и покупкой
    r = c.post("/servers", data={"name": "es-data", "kind": "Сервер", "count": "3", "service_id": str(sid),
                                 **SERVER_PARTS}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        servers = list(s.scalars(select(Server).where(Server.name.like("es-data-%")).order_by(Server.name)))
        assert [srv.name for srv in servers] == ["es-data-01", "es-data-02", "es-data-03"]
        srv = servers[0]
        assert [p.category for p in srv.parts] == ["cpu", "ram", "disk"]
        assert srv.parts[0].price == 300_000 and srv.parts[0].amortization_months == 60
        assert srv.parts[2].disk_type == "nvme" and srv.parts[1].disk_type is None
        assert all(len(x.services) == 1 and x.services[0].service_id == sid and x.services[0].whole
                   for x in servers)
        server_id = srv.id

    # Подпространство NVMe, RAID 10 — полезно 50%
    r = c.post(f"/services/{sid}/pools", data={"name": "hot", "disk_type": "nvme", "usable_percent": "50",
                                               "price_source": "disks"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        pool_id = s.scalar(select(StoragePool.id).where(StoragePool.service_id == sid))

    # Datastream: размер из Prometheus × 2 (реплики)
    r = c.post(f"/services/{sid}/resources", data={
        "name": "logs-app", "kind": "datastream", "pool_id": str(pool_id), "size_mode": "metric",
        "source_id": str(source_id), "promql": DS_QUERY, "size_factor": "2"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    rid = last_id(r)
    with SessionLocal() as s:
        res = s.get(Resource, rid)
        assert res.last_bytes == 1.5e12 and res.last_error is None
        assert s.scalar(select(func.count(ResourceSample.id)).where(ResourceSample.resource_id == rid)) == 1

    # Расчёт в API
    data = c.get(f"/api/services/{sid}").json()
    assert data["monthly"]["infrastructure"] == 3 * (10_000 + 8_000 + 8_000)
    pool = data["pools"][0]
    assert pool["raw_gb"] == 46_080 and pool["usable_gb"] == 23_040
    assert pool["price_per_gb"] == pytest.approx(24_000 / 23_040, abs=1e-6)
    ds = data["resources"][0]
    assert ds["size_gb"] == 3_000
    assert ds["monthly"] == pytest.approx(3_125)
    assert data["monthly"]["storage_unallocated"] == pytest.approx(24_000 - 3_125)
    assert any(x["id"] == sid for x in c.get("/api/services").json())

    # Страницы открываются и показывают суммы
    page = c.get(f"/services/{sid}").text
    assert "es-data-01" in page and "hot" in page and "logs-app" in page and "3 125,00" in page
    assert "78 000,00" in page  # инфраструктура
    assert c.get("/").status_code == 200
    assert c.get(f"/resources/{rid}").status_code == 200
    assert c.get(f"/servers/{server_id}").status_code == 200
    assert "es-data-03" in c.get("/servers").text
    assert c.get(f"/pools/{pool_id}").status_code == 200
    assert c.get(f"/resources/{rid}/edit").status_code == 200

    # Обновление: метрика выросла, появилась история и график
    PROM_RESULTS[DS_QUERY] = vector(2e12)
    r = c.post(f"/resources/{rid}/refresh", follow_redirects=False)
    assert r.headers["location"].endswith("msg=refreshed")
    page = c.get(f"/resources/{rid}").text
    assert "<polyline" in page
    assert c.get(f"/api/services/{sid}").json()["resources"][0]["size_gb"] == 4_000

    # Ошибка метрики сохраняется, прежний размер остаётся
    PROM_RESULTS[DS_QUERY] = vector()
    r = c.post(f"/services/{sid}/refresh", follow_redirects=False)
    assert r.headers["location"].endswith("msg=refresh_failed")
    with SessionLocal() as s:
        res = s.get(Resource, rid)
        assert "пустой результат" in res.last_error and res.last_bytes == 2e12

    # Ручная цена ГБ у подпространства
    r = c.post(f"/pools/{pool_id}", data={"name": "hot", "disk_type": "nvme", "usable_percent": "50",
                                          "price_source": "manual", "manual_price": "0,5"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get(f"/api/services/{sid}").json()["resources"][0]["monthly"] == 2_000  # 4000 ГБ × 0,5

    # Копия сервера — не в сервисе; удаление сервиса удаляет всё, кроме серверов
    r = c.post(f"/servers/{server_id}/copy", follow_redirects=False)
    with SessionLocal() as s:
        copy = s.get(Server, int(r.headers["location"].rsplit("/", 1)[1]))
        assert copy.name == "es-data-01 (копия)" and len(copy.parts) == 3 and not copy.services
    assert c.post(f"/services/{sid}/delete", follow_redirects=False).status_code == 303
    with SessionLocal() as s:
        assert s.get(Service, sid) is None
        assert not s.scalar(select(func.count(ResourceSample.id)).where(ResourceSample.resource_id == rid))
        assert s.get(Server, server_id) is not None and not s.get(Server, server_id).services


def test_validation(client):
    c = client
    sid = create_service(c, "Validation")
    # Диск без ёмкости, покупка без срока, нечисловая цена
    r = c.post("/servers", data={"name": "bad", "count": "1", **part_rows(
        {"category": "disk", "price": "100"},
        {"category": "cpu", "capacity": "8", "price": "1000", "price_mode": "capex", "amortization_months": ""},
        {"category": "ram", "capacity": "8", "price": "abc"})})
    assert r.status_code == 422
    assert "Строка 1 (Диск): ёмкость" in r.text and "Строка 2" in r.text and "«abc» — не число" in r.text
    assert 'value="abc"' in r.text  # введённые значения не теряются

    r = c.post("/servers", data={"name": "x", "count": "1,5", **part_rows()})
    assert r.status_code == 422 and "целое" in r.text

    r = c.post(f"/services/{sid}/pools", data={"name": "p", "disk_type": "ssd", "usable_percent": "150"})
    assert r.status_code == 422 and "от 0 до 100" in r.text
    r = c.post(f"/services/{sid}/pools", data={"name": "p", "disk_type": "ssd", "usable_percent": "100",
                                               "price_source": "manual", "manual_price": ""})
    assert r.status_code == 422

    r = c.post(f"/services/{sid}/resources", data={"name": "r", "size_mode": "metric", "promql": ""})
    assert r.status_code == 422 and "PromQL" in r.text
    r = c.post(f"/services/{sid}/resources", data={"name": "r", "size_mode": "manual", "manual_gb": ""})
    assert r.status_code == 422
    # Подпространство чужого сервиса
    other = create_service(c, "Other")
    c.post(f"/services/{other}/pools", data={"name": "p", "disk_type": "ssd", "usable_percent": "100"})
    with SessionLocal() as s:
        foreign = s.scalar(select(StoragePool.id).where(StoragePool.service_id == other))
    r = c.post(f"/services/{sid}/resources", data={"name": "r", "size_mode": "manual", "manual_gb": "1",
                                                   "pool_id": str(foreign)})
    assert r.status_code == 422 and "не найдено" in r.text


def test_promql_check_and_sources(client):
    c = client
    source_id = add_source(c, "check")
    PROM_RESULTS["up_bytes"] = vector(5e9)
    d = c.post("/api/promql/test", json={"source_id": source_id, "promql": "up_bytes"}).json()
    assert d == {"ok": True, "bytes": 5e9, "size": "5 ГБ"}
    d = c.post("/api/promql/test", json={"source_id": source_id, "promql": "syntax error("}).json()
    assert not d["ok"] and "parse error" in d["error"]
    d = c.post("/api/promql/test", json={"source_id": source_id, "promql": "nothing"}).json()
    assert not d["ok"] and "пустой" in d["error"]

    # Неверный токен: сохранение с проверкой не проходит, можно сохранить без проверки
    data = {"name": "secured", "url": "https://prom.example", "auth_type": "bearer", "secret": "bad-token"}
    r = c.post("/admin/prometheus", data={**data, "action": "save"})
    assert r.status_code == 422 and "Сохранить без проверки" in r.text
    r = c.post("/admin/prometheus", data={**data, "action": "save_nocheck"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        src = s.scalar(select(PrometheusSource).where(PrometheusSource.name == "secured"))
    # Секрет не показывается в форме и сохраняется, если поле оставить пустым
    page = c.get(f"/admin/prometheus/{src.id}").text
    assert "bad-token" not in page
    c.post(f"/admin/prometheus/{src.id}", data={**data, "secret": "", "action": "save_nocheck"})
    with SessionLocal() as s:
        assert s.get(PrometheusSource, src.id).secret == "bad-token"
    r = c.post(f"/admin/prometheus/{src.id}", data={**data, "secret": "good", "action": "test"})
    assert r.status_code == 200 and "версия 2.55.0" in r.text


@pytest.mark.anyio
async def test_query_value_parsing(monkeypatch):
    conn = prom.Conn("http://prom")
    PROM_RESULTS["scalar_q"] = {"resultType": "scalar", "result": [0, "42"]}
    PROM_RESULTS["nan_q"] = {"resultType": "vector", "result": [{"metric": {}, "value": [0, "NaN"]}]}
    PROM_RESULTS["matrix_q"] = {"resultType": "matrix", "result": []}
    from .conftest import fake_build_client
    monkeypatch.setattr(prom, "_build_client", fake_build_client)
    assert await prom.query_value(conn, "scalar_q") == 42
    for q, text in (("nan_q", "NaN"), ("matrix_q", "matrix")):
        with pytest.raises(prom.PromError, match=text):
            await prom.query_value(conn, q)


def test_server_resources_shared_between_services(client):
    c = client
    a, b = create_service(c, "Shared A"), create_service(c, "Shared B")
    parts = part_rows({"category": "cpu", "capacity": "16", "quantity": "2", "price": "1600"},  # 100 за ядро
                      {"category": "ram", "capacity": "64", "quantity": "2", "price": "640"},   # 10 за ГБ
                      {"category": "disk", "capacity": "1000", "disk_type": "ssd", "quantity": "2", "price": "500"},
                      {"category": "other", "name": "стойка", "price": "1000"})
    r = c.post("/servers", data={"name": "shared-1", "count": "1", "host": "10.0.0.1", **parts},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/servers?msg=saved"
    with SessionLocal() as s:
        server_id = s.scalar(select(Server.id).where(Server.name == "shared-1"))
    page = c.get(f"/services/{a}/servers/add").text
    assert "shared-1" in page and f'name="s{server_id}_cpu"' in page and f'name="s{server_id}_whole"' in page
    p = f"s{server_id}_"

    # Сервису A: 8 ядер, 32 ГБ памяти, 500 ГБ SSD
    r = c.post(f"/services/{a}/servers", data={"server_ids": [str(server_id)], p + "cpu": "8", p + "ram": "32",
                                               p + "disk_ssd": "500"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    # B: больше свободного нельзя; весь сервер — тоже
    r = c.post(f"/services/{b}/servers", data={"server_ids": [str(server_id)], p + "cpu": "30"})
    assert r.status_code == 422 and "свободно только 24 ядер" in r.text
    assert 'value="30"' in r.text  # введённое не теряется
    r = c.post(f"/services/{b}/servers", data={"server_ids": [str(server_id)], p + "whole": "on"})
    assert r.status_code == 422 and "весь сервер выделить нельзя" in r.text
    r = c.post(f"/services/{b}/servers", data={"server_ids": [str(server_id)], p + "cpu": "", p + "ram": "0"})
    assert r.status_code == 422 and "укажите, какие ресурсы" in r.text
    r = c.post(f"/services/{b}/servers", data={"server_ids": [str(server_id)], p + "cpu": "24", p + "ram": "96",
                                               p + "disk_ssd": "1500"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.post(f"/services/{a}/servers", data={"server_ids": [str(server_id)], p + "cpu": "1"}).status_code == 422
    assert "shared-1" not in c.get(f"/services/{a}/servers/add").text

    # Стоимость: ресурсы по цене единицы + «стойка» (1000) пропорционально; ресурсы сервера стоят 5480
    ja, jb = c.get(f"/api/services/{a}").json(), c.get(f"/api/services/{b}").json()
    assert ja["servers"][0]["allocated"] == {"cpu": 8, "ram": 32, "disk_ssd": 500}
    assert ja["servers"][0]["capacity"] == {"cpu": 32, "ram": 128, "disk_ssd": 2000}
    assert ja["servers"][0]["server_monthly"] == 6_480 and not ja["servers"][0]["whole"]
    assert ja["monthly"]["infrastructure"] == pytest.approx(1_370 * 6_480 / 5_480, abs=0.01)  # 800 + 320 + 250
    assert jb["monthly"]["infrastructure"] == pytest.approx(4_110 * 6_480 / 5_480, abs=0.01)  # 2400 + 960 + 750

    # Подпространство сервиса A — только выделенные ему 500 ГБ
    c.post(f"/services/{a}/pools", data={"name": "ssd", "disk_type": "ssd", "usable_percent": "100",
                                         "price_source": "disks", "server_ids": [str(server_id)]})
    pool = c.get(f"/api/services/{a}").json()["pools"][0]
    assert pool["raw_gb"] == 500 and pool["price_per_gb"] == pytest.approx(0.5)

    # Изменение выделения: проверяется свободное без учёта своего
    with SessionLocal() as s:
        link_a = s.scalar(select(ServiceServer.id).where(ServiceServer.service_id == a))
    edit = c.get(f"/assignments/{link_a}").text
    assert 'name="cpu" value="8"' in edit and "весь сервер выделить нельзя" not in edit
    r = c.post(f"/assignments/{link_a}", data={"cpu": "9", "ram": "32"})
    assert r.status_code == 422 and "свободно только 8 ядер" in r.text
    r = c.post(f"/assignments/{link_a}", data={"cpu": "4", "ram": "32"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(ServiceServer, link_a).amounts == {"cpu": 4, "ram": 32}
    page = c.get("/servers").text
    assert "не выделено" in page and "shared-1" in page

    # Убрать из сервиса: сервер остаётся, из подпространства сервиса тоже убирается
    r = c.post(f"/assignments/{link_a}/delete", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(Server, server_id) is not None
        assert not s.scalar(select(StoragePool).where(StoragePool.service_id == a)).servers
    # Удалить сервер: исчезает из всех сервисов
    assert c.post(f"/servers/{server_id}/delete", follow_redirects=False).status_code == 303
    assert c.get(f"/api/services/{b}").json()["servers"] == []


def test_whole_server_allocation(client):
    c = client
    sid = create_service(c, "Whole")
    r = c.post("/servers", data={"name": "lic-only", "count": "1", **part_rows(
        {"category": "license", "name": "ПО", "price": "500"})}, follow_redirects=False)
    with SessionLocal() as s:
        server_id = s.scalar(select(Server.id).where(Server.name == "lic-only"))
    p = f"s{server_id}_"
    # Без ёмкости можно выделить только весь сервер
    r = c.post(f"/services/{sid}/servers", data={"server_ids": [str(server_id)]})
    assert r.status_code == 422 and "только весь сервер" in r.text
    r = c.post(f"/services/{sid}/servers", data={"server_ids": [str(server_id)], p + "whole": "on"},
               follow_redirects=False)
    assert r.status_code == 303
    data = c.get(f"/api/services/{sid}").json()
    assert data["servers"][0]["whole"] and data["monthly"]["infrastructure"] == 500
    # Весь сервер занят — другому сервису выделить нечего
    other = create_service(c, "Whole 2")
    assert "всё выделено другим сервисам" in c.get(f"/services/{other}/servers/add").text


def test_server_names(client):
    c = client
    r = c.post("/servers", data={"name": "batch", "count": "12", **part_rows()}, follow_redirects=False)
    assert r.headers["location"] == "/servers?msg=created_many"
    with SessionLocal() as s:
        names = list(s.scalars(select(Server.name).where(Server.name.like("batch-%")).order_by(Server.name)))
    assert names[0] == "batch-01" and names[-1] == "batch-12" and len(names) == 12
    r = c.post("/servers", data={"name": "batch", "count": "3", **part_rows()})
    assert r.status_code == 422 and "batch-01" in r.text
    with SessionLocal() as s:
        sid = s.scalar(select(Server.id).where(Server.name == "batch-02"))
    r = c.post(f"/servers/{sid}", data={"name": "batch-01", **part_rows()})
    assert r.status_code == 422 and "уже есть" in r.text


def expense_rows(*rows: dict) -> dict:
    return {f"exp_{f}": [str(r.get(f, "")) for r in rows] for f in ("name", "amount", "period")}


def test_server_maintenance(client):
    c = client
    parts = part_rows({"category": "cpu", "capacity": "8", "price": "120000", "price_mode": "capex",
                       "amortization_months": "60"})  # 2000 в мес, покупка 120 000
    expenses = expense_rows({"name": "Электроэнергия", "amount": "1 500", "period": "monthly"},
                            {"name": "Колокация", "amount": "24000", "period": "yearly"},
                            {"name": "Техподдержка", "amount": "15", "period": "percent"},
                            {"name": "", "amount": "", "period": "monthly"})  # пустая строка пропускается
    sid = create_service(c, "Maintenance")
    r = c.post("/servers", data={"name": "mnt-1", "count": "2", "service_id": str(sid), **parts, **expenses},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        srv = s.scalar(select(Server).where(Server.name == "mnt-1-01"))
        assert [(e.name, e.amount, e.period) for e in srv.expenses] == [
            ("Электроэнергия", 1_500, "monthly"), ("Колокация", 24_000, "yearly"), ("Техподдержка", 15, "percent")]
        server_id = srv.id
        assert len(s.scalar(select(Server).where(Server.name == "mnt-1-02")).expenses) == 3

    # 2000 + 1500 + 2000 + 1500 (15% от 120 000 в год) — на каждый из двух серверов
    data = c.get(f"/api/services/{sid}").json()
    assert data["servers"][0]["server_monthly"] == 7_000
    assert data["servers"][0]["server_maintenance_monthly"] == 5_000
    assert data["monthly"]["infrastructure"] == 14_000
    page = c.get(f"/servers/{server_id}").text
    assert "Расходы на обслуживание" in page and 'value="Колокация"' in page
    assert "в т. ч. обслуживание 5" in c.get("/servers").text

    # Ошибки: нет названия, нечисловая сумма; введённое сохраняется
    r = c.post(f"/servers/{server_id}", data={"name": "mnt-1-01", **parts, **expense_rows(
        {"name": "", "amount": "100", "period": "monthly"}, {"name": "Связь", "amount": "много", "period": "yearly"})})
    assert r.status_code == 422
    assert "Обслуживание, строка 1: укажите название" in r.text and "строка 2 (Связь)" in r.text
    assert 'value="много"' in r.text

    # Копия сервера копирует и обслуживание; без строк — расходы удаляются
    r = c.post(f"/servers/{server_id}/copy", follow_redirects=False)
    with SessionLocal() as s:
        assert len(s.get(Server, int(r.headers["location"].rsplit("/", 1)[1])).expenses) == 3
    r = c.post(f"/servers/{server_id}", data={"name": "mnt-1-01", **parts}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert not s.get(Server, server_id).expenses
        assert not s.scalar(select(func.count(ServerExpense.id)).where(ServerExpense.server_id == server_id))
