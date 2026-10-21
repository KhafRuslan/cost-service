"""Расчёт стоимости в месяц: комплектующие → серверы → ресурсы серверов в сервисах → подпространства → ресурсы.

Сервер может работать на несколько сервисов: каждому выделяются конкретные ресурсы (ядра, ГБ памяти, ГБ дисков).
Цена единицы ресурса — стоимость комплектующих этого вида ÷ их ёмкость. Комплектующие без ёмкости (лицензии, прочее)
и расходы на обслуживание делятся между сервисами пропорционально стоимости выделенных им ресурсов.
Объёмы — в десятичных гигабайтах (1 ГБ = 10⁹ байт), как в маркировке дисков.
"""

from dataclasses import dataclass, field

from .models import (EXPENSE_CAPEX_PERCENT, EXPENSE_YEARLY, PRICE_CAPEX, RESOURCES, Resource, Server, ServerExpense,
                     ServerPart, Service, ServiceServer, StoragePool)

GB = 1e9


def part_unit_monthly(part: ServerPart) -> float:
    """Стоимость одной штуки комплектующего в месяц."""
    if part.price_mode == PRICE_CAPEX:
        return part.price / part.amortization_months if part.amortization_months else 0.0
    return part.price


def part_monthly(part: ServerPart) -> float:
    """Стоимость комплектующего в месяц (с учётом количества штук)."""
    return part_unit_monthly(part) * part.quantity


def server_capex(server: Server) -> float:
    """Цена покупки комплектующих сервера (только купленные, без аренды)."""
    return sum(p.price * p.quantity for p in server.parts if p.price_mode == PRICE_CAPEX)


def expense_monthly(expense: ServerExpense, server: Server) -> float:
    """Расход на обслуживание в месяц."""
    if expense.period == EXPENSE_YEARLY:
        return expense.amount / 12
    if expense.period == EXPENSE_CAPEX_PERCENT:
        return server_capex(server) * expense.amount / 100 / 12
    return expense.amount


def maintenance_monthly(server: Server) -> float:
    """Все расходы на обслуживание сервера в месяц."""
    return sum(expense_monthly(e, server) for e in server.expenses)


def server_monthly(server: Server) -> float:
    """Стоимость сервера целиком в месяц: комплектующие и обслуживание."""
    return sum(part_monthly(p) for p in server.parts) + maintenance_monthly(server)


def resource_key(part: ServerPart) -> str | None:
    """Какой ресурс сервера даёт комплектующее (None — не выделяется: нет ёмкости или это лицензия, прочее)."""
    if not part.capacity or part.capacity <= 0:
        return None
    key = f"disk_{part.disk_type}" if part.category == "disk" else part.category
    return key if key in RESOURCES else None


@dataclass
class Capacity:
    total: float  # ёмкость сервера: ядер, ГБ ...
    cost: float   # стоимость этих комплектующих в месяц

    @property
    def unit_price(self) -> float:
        return self.cost / self.total


def server_capacities(server: Server) -> dict[str, Capacity]:
    """Ресурсы сервера, которые можно выделять, в порядке RESOURCES."""
    caps: dict[str, Capacity] = {}
    for p in server.parts:
        key = resource_key(p)
        if key:
            cap = caps.setdefault(key, Capacity(0.0, 0.0))
            cap.total += p.capacity * p.quantity
            cap.cost += part_monthly(p)
    return {k: caps[k] for k in RESOURCES if k in caps}


def server_overhead(server: Server) -> float:
    """Комплектующие без ёмкости и обслуживание — делятся между сервисами пропорционально выделенным ресурсам."""
    return sum(part_monthly(p) for p in server.parts if resource_key(p) is None) + maintenance_monthly(server)


def allocated_amounts(a: ServiceServer) -> dict[str, float]:
    """Выделенные сервису ресурсы (для «весь сервер» — вся ёмкость сервера)."""
    caps = server_capacities(a.server)
    if a.whole:
        return {k: c.total for k, c in caps.items()}
    amounts = a.amounts or {}
    return {k: float(amounts[k]) for k in caps if amounts.get(k)}


def allocation_cost(a: ServiceServer) -> float:
    """Стоимость выделенного сервису в месяц: ресурсы по цене единицы + их доля в прочих расходах сервера."""
    if a.whole:
        return server_monthly(a.server)
    caps = server_capacities(a.server)
    resources = sum(amount * caps[k].unit_price for k, amount in allocated_amounts(a).items())
    capacity_cost = sum(c.cost for c in caps.values())
    overhead = server_overhead(a.server) * resources / capacity_cost if capacity_cost else 0.0
    return resources + overhead


@dataclass
class ServerUsage:
    """Сколько ресурсов сервера выделено сервисам."""
    server: Server
    cost: float  # стоимость сервера в месяц
    capacities: dict[str, Capacity]
    allocated: dict[str, float]  # выделено всем сервисам
    allocated_cost: float

    @property
    def free(self) -> dict[str, float]:
        return {k: c.total - self.allocated.get(k, 0.0) for k, c in self.capacities.items()}

    @property
    def unallocated_cost(self) -> float:
        return max(0.0, self.cost - self.allocated_cost)

    @property
    def allocated_percent(self) -> float:
        """Доля стоимости сервера, отнесённая к сервисам."""
        if self.cost:
            return self.allocated_cost / self.cost * 100
        return 100.0 if self.server.services else 0.0

    @property
    def overbooked(self) -> list[str]:
        """Ресурсы, выделенные сверх ёмкости (например, после замены комплектующих)."""
        return [k for k, c in self.capacities.items() if self.allocated.get(k, 0.0) > c.total + 1e-6]


def server_usage(server: Server) -> ServerUsage:
    allocated: dict[str, float] = {}
    for a in server.services:
        for k, amount in allocated_amounts(a).items():
            allocated[k] = allocated.get(k, 0.0) + amount
    return ServerUsage(server=server, cost=server_monthly(server), capacities=server_capacities(server),
                       allocated=allocated, allocated_cost=sum(allocation_cost(a) for a in server.services))


def resource_size_gb(resource: Resource) -> float | None:
    """Размер ресурса с учётом множителя: из последнего значения метрики или заданный вручную."""
    if resource.uses_metric:
        base = resource.last_bytes / GB if resource.last_bytes is not None else None
    else:
        base = resource.manual_gb
    return base * resource.size_factor if base is not None else None


@dataclass
class ResourceReport:
    resource: Resource
    size_gb: float | None
    price_per_gb: float | None

    @property
    def cost(self) -> float | None:
        if self.size_gb is None or self.price_per_gb is None:
            return None
        return self.size_gb * self.price_per_gb


@dataclass
class PoolReport:
    pool: StoragePool
    servers: list[ServiceServer]  # чьи диски входят в подпространство
    disks: list[tuple[ServiceServer, float, float]]  # сервер, выделенные ГБ, их стоимость в месяц
    raw_gb: float
    disk_cost: float  # стоимость дисков подпространства в месяц
    resources: list[ResourceReport] = field(default_factory=list)

    @property
    def usable_gb(self) -> float:
        return self.raw_gb * self.pool.usable_percent / 100

    @property
    def derived_price_per_gb(self) -> float | None:
        return self.disk_cost / self.usable_gb if self.usable_gb else None

    @property
    def price_per_gb(self) -> float | None:
        if self.pool.manual_price_per_gb is not None:
            return self.pool.manual_price_per_gb
        return self.derived_price_per_gb

    @property
    def manual_price(self) -> bool:
        return self.pool.manual_price_per_gb is not None

    @property
    def total_cost(self) -> float | None:
        """Стоимость всего полезного объёма в месяц."""
        if self.manual_price:
            return self.price_per_gb * self.usable_gb if self.usable_gb else None
        return self.disk_cost

    @property
    def used_gb(self) -> float:
        return sum(r.size_gb or 0 for r in self.resources)

    @property
    def used_percent(self) -> float | None:
        return self.used_gb / self.usable_gb * 100 if self.usable_gb else None

    @property
    def allocated_cost(self) -> float:
        return sum(r.cost or 0 for r in self.resources)

    @property
    def unallocated_cost(self) -> float | None:
        """Стоимость свободного места: оплачено, но не занято ресурсами (отрицательная — место перерасходовано)."""
        return self.total_cost - self.allocated_cost if self.total_cost is not None else None


def pool_servers(pool: StoragePool, service: Service) -> list[ServiceServer]:
    """Серверы сервиса, чьи диски входят в подпространство (не выбраны — все серверы сервиса)."""
    selected = {s.id for s in pool.servers}
    return [a for a in service.sorted_servers if not selected or a.server.id in selected]


def disk_totals(assignments: list[ServiceServer], disk_type: str) -> tuple[list[tuple[ServiceServer, float, float]],
                                                                          float, float]:
    """Выделенные сервису диски заданного типа: по серверам (ГБ, стоимость), сырой объём (ГБ) и стоимость в месяц."""
    key = f"disk_{disk_type}"
    disks = []
    for a in assignments:
        amount = allocated_amounts(a).get(key)
        if amount:
            disks.append((a, amount, amount * server_capacities(a.server)[key].unit_price))
    return disks, sum(d[1] for d in disks), sum(d[2] for d in disks)


def pool_report(pool: StoragePool, service: Service) -> PoolReport:
    servers = pool_servers(pool, service)
    disks, raw_gb, disk_cost = disk_totals(servers, pool.disk_type)
    return PoolReport(pool=pool, servers=servers, disks=disks, raw_gb=raw_gb, disk_cost=disk_cost)


@dataclass
class ServerAllocation:
    assignment: ServiceServer
    full_cost: float  # стоимость сервера целиком
    cost: float  # стоимость выделенного сервису
    amounts: dict[str, float]
    capacities: dict[str, Capacity]

    @property
    def server(self) -> Server:
        return self.assignment.server

    @property
    def percent(self) -> float:
        """Доля стоимости сервера, отнесённая к сервису."""
        return self.cost / self.full_cost * 100 if self.full_cost else 100.0


def server_allocation(a: ServiceServer) -> ServerAllocation:
    return ServerAllocation(assignment=a, full_cost=server_monthly(a.server), cost=allocation_cost(a),
                            amounts=allocated_amounts(a), capacities=server_capacities(a.server))


@dataclass
class ServiceReport:
    service: Service
    servers: list[ServerAllocation]
    pools: list[PoolReport]
    resources: list[ResourceReport]

    @property
    def infra_cost(self) -> float:
        """Стоимость выделенных сервису серверов и ресурсов в месяц."""
        return sum(sh.cost for sh in self.servers)

    @property
    def storage_cost(self) -> float:
        return sum(p.total_cost or 0 for p in self.pools)

    @property
    def allocated_cost(self) -> float:
        """Стоимость хранения, распределённая на ресурсы."""
        return sum(r.cost or 0 for r in self.resources)

    @property
    def unallocated_cost(self) -> float:
        return sum(p.unallocated_cost or 0 for p in self.pools)

    @property
    def resources_without_cost(self) -> int:
        return sum(1 for r in self.resources if r.cost is None)

    def pool(self, pool_id: int | None) -> PoolReport | None:
        return next((p for p in self.pools if p.pool.id == pool_id), None)

    def resource(self, resource_id: int) -> ResourceReport | None:
        return next((r for r in self.resources if r.resource.id == resource_id), None)


def service_report(service: Service) -> ServiceReport:
    pools = [pool_report(p, service) for p in service.pools]
    by_id = {p.pool.id: p for p in pools}
    resources = []
    for r in service.resources:
        pr = by_id.get(r.pool_id)
        rep = ResourceReport(resource=r, size_gb=resource_size_gb(r), price_per_gb=pr.price_per_gb if pr else None)
        resources.append(rep)
        if pr:
            pr.resources.append(rep)
    servers = [server_allocation(a) for a in service.sorted_servers]
    return ServiceReport(service=service, servers=servers, pools=pools, resources=resources)
