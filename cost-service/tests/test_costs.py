"""Расчёт стоимости без БД и веба."""

import pytest

from app.costs import (allocation_cost, maintenance_monthly, part_monthly, server_capacities, server_monthly,
                       server_usage, service_report)
from app.models import Resource, Server, ServerExpense, ServerPart, Service, ServiceServer, StoragePool


def disk(capacity, qty, price, disk_type="nvme", mode="monthly", months=None):
    return ServerPart(category="disk", capacity=capacity, quantity=qty, price=price, disk_type=disk_type,
                      price_mode=mode, amortization_months=months)


def test_part_and_server_monthly():
    cpu = ServerPart(category="cpu", capacity=32, quantity=2, price=300_000, price_mode="capex",
                     amortization_months=60)
    ram = ServerPart(category="ram", capacity=64, quantity=8, price=1_000, price_mode="monthly")
    assert part_monthly(cpu) == 10_000
    assert part_monthly(ram) == 8_000
    assert server_monthly(Server(name="s", parts=[cpu, ram])) == 18_000
    # Покупка без срока амортизации не даёт деления на ноль
    assert part_monthly(ServerPart(category="other", quantity=1, price=100, price_mode="capex")) == 0


def make_service():
    hot = [Server(id=n, name=f"hot-{n}", parts=[disk(3840, 4, 120_000, mode="capex", months=60)])  # 8000 в мес
           for n in (1, 2, 3)]
    warm = [Server(id=n, name=f"warm-{n}", parts=[disk(16_000, 10, 500, disk_type="hdd")]) for n in (4, 5)]
    svc = Service(name="es", servers=[ServiceServer(server=s, whole=True) for s in hot + warm])
    nvme_pool = StoragePool(id=10, name="nvme", disk_type="nvme", usable_percent=50, servers=[])
    hdd_pool = StoragePool(id=11, name="hdd", disk_type="hdd", usable_percent=100, manual_price_per_gb=0.01,
                           servers=warm)
    svc.pools = [nvme_pool, hdd_pool]
    ds = Resource(id=100, name="logs", promql="x", last_bytes=1.5e12, size_factor=2, pool_id=10)
    manual = Resource(id=101, name="archive", promql="", manual_gb=50_000, size_factor=1, pool_id=11)
    orphan = Resource(id=102, name="nopool", promql="", manual_gb=10, size_factor=1, pool_id=None)
    svc.resources = [ds, manual, orphan]
    return svc


def test_service_report():
    rep = service_report(make_service())
    assert rep.infra_cost == pytest.approx(3 * 8_000 + 2 * 5_000)
    assert [sh.server.name for sh in rep.servers] == ["hot-1", "hot-2", "hot-3", "warm-4", "warm-5"]

    nvme = rep.pool(10)
    assert [a.server.name for a in nvme.servers] == ["hot-1", "hot-2", "hot-3", "warm-4", "warm-5"]
    assert nvme.raw_gb == 4 * 3840 * 3
    assert nvme.usable_gb == 4 * 3840 * 3 / 2
    assert nvme.disk_cost == pytest.approx(24_000)
    assert nvme.price_per_gb == pytest.approx(24_000 / 23_040)
    assert not nvme.manual_price

    logs = rep.resource(100)
    assert logs.size_gb == 3_000  # 1,5 ТБ × 2
    assert logs.cost == pytest.approx(3_000 * 24_000 / 23_040)
    assert nvme.used_gb == 3_000
    assert nvme.unallocated_cost == pytest.approx(24_000 - logs.cost)

    hdd = rep.pool(11)
    assert [a.server.name for a in hdd.servers] == ["warm-4", "warm-5"]
    assert hdd.price_per_gb == 0.01 and hdd.manual_price
    assert hdd.usable_gb == 320_000
    assert hdd.total_cost == pytest.approx(3_200)  # ручная цена × полезный объём
    assert rep.resource(101).cost == pytest.approx(500)

    assert rep.resource(102).cost is None
    assert rep.resources_without_cost == 1
    assert rep.allocated_cost == pytest.approx(logs.cost + 500)


def test_resources_allocated_to_services():
    """Сервер делят два сервиса: каждому — конкретные ядра, память и диски по цене единицы, прочее — пропорционально."""
    srv = Server(id=1, name="shared", parts=[
        ServerPart(category="cpu", capacity=16, quantity=2, price=1_600),           # 32 ядра за 3200: 100 за ядро
        ServerPart(category="ram", capacity=64, quantity=4, price=640),             # 256 ГБ за 2560: 10 за ГБ
        disk(1_000, 4, 500, disk_type="ssd"),                                       # 4000 ГБ за 2000: 0,5 за ГБ
        ServerPart(category="other", name="стойка", quantity=1, price=1_480),       # без ёмкости
        ServerPart(category="ram", capacity=None, quantity=1, price=0)])            # без ёмкости и цены
    caps = server_capacities(srv)
    assert list(caps) == ["cpu", "ram", "disk_ssd"]
    assert caps["cpu"].unit_price == 100 and caps["ram"].unit_price == 10 and caps["disk_ssd"].unit_price == 0.5
    assert server_monthly(srv) == 3_200 + 2_560 + 2_000 + 1_480

    a = ServiceServer(server=srv, amounts={"cpu": 8, "ram": 64, "disk_ssd": 1_000})
    b = ServiceServer(server=srv, amounts={"cpu": 24, "ram": 192})
    svc_a = Service(name="a", servers=[a], resources=[])
    svc_b = Service(name="b", servers=[b], resources=[])
    # a: 800 + 640 + 500 = 1940 из 7760 стоимости ресурсов → та же доля «стойки»
    assert allocation_cost(a) == pytest.approx(1_940 + 1_480 * 1_940 / 7_760)
    assert allocation_cost(b) == pytest.approx(4_320 + 1_480 * 4_320 / 7_760)

    for svc in (svc_a, svc_b):
        svc.pools = [StoragePool(id=1, name="ssd", disk_type="ssd", usable_percent=100, servers=[])]
    ra, rb = service_report(svc_a), service_report(svc_b)
    assert ra.infra_cost == pytest.approx(allocation_cost(a))
    assert ra.pool(1).raw_gb == 1_000 and ra.pool(1).disk_cost == 500 and ra.pool(1).price_per_gb == 0.5
    assert rb.pool(1).raw_gb == 0 and rb.pool(1).price_per_gb is None

    usage = server_usage(srv)
    assert usage.allocated == {"cpu": 32, "ram": 256, "disk_ssd": 1_000}
    assert usage.free == {"cpu": 0, "ram": 0, "disk_ssd": 3_000}
    assert usage.unallocated_cost == pytest.approx(1_500 * (1 + 1_480 / 7_760))  # свободные 3000 ГБ SSD
    assert not usage.overbooked
    a.amounts = {"cpu": 16}
    assert server_usage(srv).overbooked == ["cpu"]


def test_whole_server():
    srv = Server(id=1, name="s", parts=[ServerPart(category="cpu", capacity=8, quantity=1, price=800),
                                        ServerPart(category="license", quantity=1, price=200)])
    a = ServiceServer(server=srv, whole=True, amounts={"cpu": 1})  # amounts при whole не используются
    assert allocation_cost(a) == 1_000
    assert server_usage(srv).allocated == {"cpu": 8} and server_usage(srv).unallocated_cost == 0


def test_maintenance_expenses():
    """Обслуживание: в месяц, в год и % от цены покупки; делится между сервисами как прочие расходы."""
    srv = Server(id=1, name="s", parts=[
        ServerPart(category="cpu", capacity=10, quantity=1, price=120_000, price_mode="capex",
                   amortization_months=60),                                            # 2000 в мес, покупка 120 000
        ServerPart(category="ram", capacity=100, quantity=1, price=1_000)],            # аренда: не входит в покупку
        expenses=[ServerExpense(name="электричество", amount=500, period="monthly"),
                  ServerExpense(name="колокация", amount=12_000, period="yearly"),     # 1000 в мес
                  ServerExpense(name="поддержка", amount=10, period="percent")])       # 10% от 120 000 в год
    assert maintenance_monthly(srv) == pytest.approx(500 + 1_000 + 1_000)
    assert server_monthly(srv) == pytest.approx(3_000 + 2_500)

    whole = ServiceServer(server=srv, whole=True)
    assert allocation_cost(whole) == pytest.approx(5_500)
    whole.whole, whole.amounts = False, {"cpu": 5, "ram": 50}  # половина ресурсов → половина обслуживания
    assert allocation_cost(whole) == pytest.approx(2_750)
    assert server_usage(srv).unallocated_cost == pytest.approx(2_750)


def test_metric_without_value_has_no_cost():
    svc = make_service()
    svc.resources[0].last_bytes = None
    rep = service_report(svc)
    assert rep.resource(100).size_gb is None and rep.resource(100).cost is None


def test_pool_without_disks():
    svc = Service(name="x", servers=[], resources=[])
    svc.pools = [StoragePool(id=1, name="p", disk_type="ssd", usable_percent=100, servers=[])]
    p = service_report(svc).pool(1)
    assert p.raw_gb == 0 and p.price_per_gb is None and p.used_percent is None
