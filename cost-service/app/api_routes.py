"""JSON API: стоимость сервисов и проверка PromQL-запроса из формы ресурса.

Суммы — в долларах; параметр ?currency=EUR|RUB пересчитывает их по курсу из «Управление → Валюты».
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from . import currency, prom
from .costs import GB, ServiceReport, maintenance_monthly, service_report
from .db import SessionLocal
from .models import PrometheusSource, Service
from .web import f_gb, require_editor

router = APIRouter(prefix="/api")


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _display(code: str | None) -> currency.Display:
    code = (code or currency.BASE).upper()
    if code not in currency.CURRENCIES:
        raise HTTPException(400, f"Неизвестная валюта {code}: допустимы {', '.join(currency.CURRENCIES)}")
    if code not in currency.available():
        raise HTTPException(400, f"Курс {code} не задан")
    return currency.display_for(code)


def report_json(rep: ServiceReport, display: currency.Display = currency.BASE_DISPLAY) -> dict:
    def m(value: float | None, digits: int = 2) -> float | None:
        """Сумма в валюте ответа."""
        return _round(display.from_base(value), digits)

    return {
        "id": rep.service.id,
        "name": rep.service.name,
        "currency": display.code,
        "monthly": {
            "infrastructure": m(rep.infra_cost),
            "storage": m(rep.storage_cost),
            "storage_allocated": m(rep.allocated_cost),
            "storage_unallocated": m(rep.unallocated_cost),
        },
        "servers": [{"id": sa.server.id, "name": sa.server.name, "kind": sa.server.kind, "host": sa.server.host,
                     "whole": sa.assignment.whole, "allocated": {k: _round(v, 3) for k, v in sa.amounts.items()},
                     "capacity": {k: _round(c.total, 3) for k, c in sa.capacities.items()},
                     "server_monthly": m(sa.full_cost),
                     "server_maintenance_monthly": m(maintenance_monthly(sa.server)),
                     "monthly": m(sa.cost)} for sa in rep.servers],
        "pools": [{"id": p.pool.id, "name": p.pool.name, "disk_type": p.pool.disk_type,
                   "raw_gb": _round(p.raw_gb), "usable_gb": _round(p.usable_gb), "used_gb": _round(p.used_gb),
                   "price_per_gb": m(p.price_per_gb, 6), "price_manual": p.manual_price,
                   "monthly": m(p.total_cost), "allocated": m(p.allocated_cost),
                   "unallocated": m(p.unallocated_cost)} for p in rep.pools],
        "resources": [{"id": r.resource.id, "name": r.resource.name, "kind": r.resource.kind,
                       "pool_id": r.resource.pool_id, "size_gb": _round(r.size_gb, 3),
                       "price_per_gb": m(r.price_per_gb, 6), "monthly": m(r.cost),
                       "updated_at": r.resource.last_success_at.isoformat() + "Z"
                       if r.resource.last_success_at else None,
                       "error": r.resource.last_error} for r in rep.resources],
    }


@router.get("/services")
def services(code: str | None = Query(None, alias="currency")):
    display = _display(code)
    with SessionLocal() as s:
        return [report_json(service_report(svc), display)
                for svc in s.scalars(select(Service).order_by(Service.name))]


@router.get("/services/{service_id}")
def service(service_id: int, code: str | None = Query(None, alias="currency")):
    display = _display(code)
    with SessionLocal() as s:
        svc = s.get(Service, service_id)
        if svc is None:
            raise HTTPException(404, "Сервис не найден")
        return report_json(service_report(svc), display)


@router.post("/promql/test", dependencies=[Depends(require_editor)])
async def promql_test(request: Request):
    data = await request.json()
    promql = str(data.get("promql") or "").strip()
    if not promql:
        return {"ok": False, "error": "Введите запрос"}
    with SessionLocal() as s:
        src = s.get(PrometheusSource, int(data.get("source_id") or 0))
        if src is None:
            return {"ok": False, "error": "Выберите источник Prometheus"}
        conn = prom.Conn.of(src)
    try:
        value = await prom.query_value(conn, promql)
    except prom.PromError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "bytes": value, "size": f_gb(value / GB)}
