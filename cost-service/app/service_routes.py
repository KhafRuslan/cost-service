"""Сервисы, компоненты, дисковые подпространства и ресурсы."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import prom
from .costs import GB, disk_totals, pool_report, service_report
from .db import SessionLocal, utcnow
from .models import DISK_TYPES, PrometheusSource, Resource, ResourceSample, Service, StoragePool
from .web import money_input, num_text, parse_float, parse_money, redirect, render, require_editor, to_local

router = APIRouter()
edit = [Depends(require_editor)]


def _service(s, service_id: int) -> Service:
    service = s.get(Service, service_id)
    if service is None:
        raise HTTPException(404, "Сервис не найден")
    return service


def _child(s, model, obj_id: int, what: str):
    obj = s.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{what} не найден")
    return obj


def _load_services(s) -> list[Service]:
    return list(s.scalars(select(Service).order_by(Service.name).options(
        selectinload(Service.servers), selectinload(Service.pools), selectinload(Service.resources))))


# ================================================================ список и карточка сервиса

@router.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None):
    with SessionLocal() as s:
        reports = [service_report(svc) for svc in _load_services(s)]
        return render(request, "index.html", reports=reports, msg=msg,
                      total_infra=sum(r.infra_cost for r in reports),
                      total_allocated=sum(r.allocated_cost for r in reports),
                      total_unallocated=sum(r.unallocated_cost for r in reports),
                      metric_errors=sum(1 for r in reports for x in r.resources if x.resource.last_error))


@router.get("/services/new", response_class=HTMLResponse, dependencies=edit)
def service_new(request: Request):
    return render(request, "service_form.html", service=None, form={"name": "", "description": ""})


def service_page_response(request: Request, s, service: Service, status_code: int = 200, **ctx):
    return render(request, "service.html", status_code, report=service_report(service), **ctx)


@router.get("/services/{service_id}", response_class=HTMLResponse)
def service_page(request: Request, service_id: int, msg: str | None = None):
    with SessionLocal() as s:
        return service_page_response(request, s, _service(s, service_id), msg=msg)


@router.get("/services/{service_id}/edit", response_class=HTMLResponse, dependencies=edit)
def service_edit(request: Request, service_id: int):
    with SessionLocal() as s:
        svc = _service(s, service_id)
    return render(request, "service_form.html", service=svc, form={"name": svc.name, "description": svc.description})


@router.post("/services", dependencies=edit)
async def service_create(request: Request):
    return await _save_service(request, None)


@router.post("/services/{service_id}", dependencies=edit)
async def service_update(request: Request, service_id: int):
    return await _save_service(request, service_id)


async def _save_service(request: Request, service_id: int | None):
    data = await request.form()
    form = {"name": (data.get("name") or "").strip()[:200], "description": (data.get("description") or "").strip()}
    with SessionLocal() as s:
        svc = _service(s, service_id) if service_id else None
        error = ""
        if not form["name"]:
            error = "Укажите название сервиса"
        elif s.scalar(select(Service.id).where(Service.name == form["name"], Service.id != (service_id or 0))):
            error = f"Сервис «{form['name']}» уже есть"
        if error:
            return render(request, "service_form.html", 422, service=svc, form=form, error=error)
        svc = svc or Service()
        svc.name, svc.description = form["name"], form["description"]
        s.add(svc)
        s.commit()
        return redirect(f"/services/{svc.id}?msg=saved")


@router.post("/services/{service_id}/delete", dependencies=edit)
def service_delete(service_id: int):
    with SessionLocal() as s:
        s.delete(_service(s, service_id))
        s.commit()
    return redirect("/?msg=deleted")


@router.post("/services/{service_id}/refresh", dependencies=edit)
async def service_refresh(service_id: int):
    with SessionLocal() as s:
        _service(s, service_id)
    errors = await prom.refresh_many(prom.metric_resource_ids(service_id))
    failed = sum(1 for e in errors.values() if e)
    return redirect(f"/services/{service_id}?msg={'refresh_failed' if failed else 'refreshed'}")


# ================================================================ дисковые подпространства

def _pool_form(pool: StoragePool | None) -> dict:
    if pool is None:
        return {"name": "", "disk_type": "ssd", "usable_percent": "100", "price_source": "disks", "manual_price": "",
                "server_ids": set(), "description": ""}
    return {"name": pool.name, "disk_type": pool.disk_type, "usable_percent": num_text(pool.usable_percent),
            "price_source": "manual" if pool.manual_price_per_gb is not None else "disks",
            "manual_price": money_input(pool.manual_price_per_gb), "server_ids": {srv.id for srv in pool.servers},
            "description": pool.description}


def _pool_page(request: Request, service: Service, pool: StoragePool | None, form: dict, status_code: int = 200,
               error: str = ""):
    # Объём и стоимость дисков каждого типа на каждом сервере (с учётом доли) — подсказка в форме
    disks = {(a.server_id, t): disk_totals([a], t)[1:] for a in service.servers for t in DISK_TYPES}
    return render(request, "pool_form.html", status_code, service=service, pool=pool, form=form, disks=disks,
                  report=service_report(service).pool(pool.id) if pool else None, error=error)


@router.get("/services/{service_id}/pools/new", response_class=HTMLResponse, dependencies=edit)
def pool_new(request: Request, service_id: int):
    with SessionLocal() as s:
        return _pool_page(request, _service(s, service_id), None, _pool_form(None))


@router.get("/pools/{pool_id}", response_class=HTMLResponse, dependencies=edit)
def pool_edit(request: Request, pool_id: int):
    with SessionLocal() as s:
        pool = _child(s, StoragePool, pool_id, "Подпространство")
        return _pool_page(request, pool.service, pool, _pool_form(pool))


@router.post("/services/{service_id}/pools", dependencies=edit)
async def pool_create(request: Request, service_id: int):
    return await _save_pool(request, service_id, None)


@router.post("/pools/{pool_id}", dependencies=edit)
async def pool_update(request: Request, pool_id: int):
    with SessionLocal() as s:
        service_id = _child(s, StoragePool, pool_id, "Подпространство").service_id
    return await _save_pool(request, service_id, pool_id)


async def _save_pool(request: Request, service_id: int, pool_id: int | None):
    data = await request.form()
    form = {
        "name": (data.get("name") or "").strip()[:200],
        "disk_type": data.get("disk_type") if data.get("disk_type") in DISK_TYPES else "ssd",
        "usable_percent": (data.get("usable_percent") or "").strip(),
        "price_source": "manual" if data.get("price_source") == "manual" else "disks",
        "manual_price": (data.get("manual_price") or "").strip(),
        "server_ids": {int(v) for v in data.getlist("server_ids") if str(v).isdigit()},
        "description": (data.get("description") or "").strip(),
    }
    with SessionLocal() as s:
        service = _service(s, service_id)
        pool = _child(s, StoragePool, pool_id, "Подпространство") if pool_id else None
        try:
            if not form["name"]:
                raise ValueError("Укажите название подпространства")
            usable = parse_float(form["usable_percent"], "Полезный объём", required=True)
            if not 0 < usable <= 100:
                raise ValueError("Полезный объём: от 0 до 100 %")
            manual = parse_money(form["manual_price"], "Цена за ГБ", required=True) \
                if form["price_source"] == "manual" else None
        except ValueError as e:
            return _pool_page(request, service, pool, form, 422, str(e))
        pool = pool or StoragePool(service_id=service_id)
        pool.name, pool.disk_type, pool.description = form["name"], form["disk_type"], form["description"]
        pool.usable_percent, pool.manual_price_per_gb = usable, manual
        pool.servers = [a.server for a in service.sorted_servers if a.server_id in form["server_ids"]]
        s.add(pool)
        s.commit()
    return redirect(f"/services/{service_id}?msg=pool_saved#pools")


@router.post("/pools/{pool_id}/delete", dependencies=edit)
def pool_delete(pool_id: int):
    with SessionLocal() as s:
        pool = _child(s, StoragePool, pool_id, "Подпространство")
        service_id = pool.service_id
        s.delete(pool)
        s.commit()
    return redirect(f"/services/{service_id}?msg=pool_deleted#pools")


# ================================================================ ресурсы

HISTORY_DAYS = (7, 30, 90, 180)


def _chart(samples: list[ResourceSample], width: int = 800, height: int = 160) -> dict | None:
    """Точки линии для SVG: время по X, размер по Y (не больше ~400 точек)."""
    if len(samples) < 2:
        return None
    step = max(1, len(samples) // 400)
    picked = samples[::step]
    if picked[-1] is not samples[-1]:
        picked.append(samples[-1])
    t0, t1 = picked[0].ts, picked[-1].ts
    span = (t1 - t0).total_seconds() or 1
    lo, hi = min(p.bytes for p in picked), max(p.bytes for p in picked)
    pad = (hi - lo) * 0.1 or hi * 0.05 or 1
    lo, hi = max(0.0, lo - pad), hi + pad
    points = " ".join(f"{(p.ts - t0).total_seconds() / span * width:.1f},"
                      f"{height - (p.bytes - lo) / (hi - lo) * height:.1f}" for p in picked)
    return {"points": points, "width": width, "height": height, "min_gb": lo / GB, "max_gb": hi / GB,
            "start": to_local(t0), "end": to_local(t1)}


@router.get("/resources/{resource_id}", response_class=HTMLResponse)
def resource_page(request: Request, resource_id: int, days: int = 30, msg: str | None = None):
    days = days if days in HISTORY_DAYS else 30
    with SessionLocal() as s:
        r = _child(s, Resource, resource_id, "Ресурс")
        report = service_report(r.service)
        samples = list(s.scalars(select(ResourceSample).where(
            ResourceSample.resource_id == r.id, ResourceSample.ts >= utcnow() - timedelta(days=days))
            .order_by(ResourceSample.ts)))
        growth = None
        if len(samples) >= 2:
            growth = (samples[-1].bytes - samples[0].bytes) / GB * r.size_factor
        return render(request, "resource.html", rr=report.resource(r.id), pool=report.pool(r.pool_id),
                      service=r.service, chart=_chart(samples), samples=samples[-10:][::-1], growth=growth,
                      days=days, history_days=HISTORY_DAYS, msg=msg)


def _resource_form(r: Resource | None, pool_id: int | None = None) -> dict:
    if r is None:
        return {"name": "", "kind": "datastream", "description": "", "pool_id": pool_id, "size_mode": "metric",
                "source_id": None, "promql": "", "manual_gb": "", "size_factor": "1"}
    return {"name": r.name, "kind": r.kind, "description": r.description, "pool_id": r.pool_id,
            "size_mode": "metric" if r.uses_metric else "manual", "source_id": r.source_id, "promql": r.promql,
            "manual_gb": num_text(r.manual_gb), "size_factor": num_text(r.size_factor)}


def _resource_page(request: Request, s, service: Service, resource: Resource | None, form: dict,
                   status_code: int = 200, error: str = ""):
    sources = list(s.scalars(select(PrometheusSource).order_by(PrometheusSource.name)))
    if form["source_id"] is None and sources:
        form["source_id"] = sources[0].id
    return render(request, "resource_form.html", status_code, service=service, resource=resource, form=form,
                  sources=sources, pools=[pool_report(p, service) for p in service.pools], error=error)


@router.get("/services/{service_id}/resources/new", response_class=HTMLResponse, dependencies=edit)
def resource_new(request: Request, service_id: int, pool_id: int | None = None):
    with SessionLocal() as s:
        service = _service(s, service_id)
        default_pool = pool_id or (service.pools[0].id if len(service.pools) == 1 else None)
        return _resource_page(request, s, service, None, _resource_form(None, default_pool))


@router.get("/resources/{resource_id}/edit", response_class=HTMLResponse, dependencies=edit)
def resource_edit(request: Request, resource_id: int):
    with SessionLocal() as s:
        r = _child(s, Resource, resource_id, "Ресурс")
        return _resource_page(request, s, r.service, r, _resource_form(r))


@router.post("/services/{service_id}/resources", dependencies=edit)
async def resource_create(request: Request, service_id: int):
    return await _save_resource(request, service_id, None)


@router.post("/resources/{resource_id}", dependencies=edit)
async def resource_update(request: Request, resource_id: int):
    with SessionLocal() as s:
        service_id = _child(s, Resource, resource_id, "Ресурс").service_id
    return await _save_resource(request, service_id, resource_id)


def _int_or_none(raw) -> int | None:
    return int(raw) if str(raw or "").isdigit() else None


async def _save_resource(request: Request, service_id: int, resource_id: int | None):
    data = await request.form()
    form = {
        "name": (data.get("name") or "").strip()[:200],
        "kind": (data.get("kind") or "").strip()[:100],
        "description": (data.get("description") or "").strip(),
        "pool_id": _int_or_none(data.get("pool_id")),
        "size_mode": "manual" if data.get("size_mode") == "manual" else "metric",
        "source_id": _int_or_none(data.get("source_id")),
        "promql": (data.get("promql") or "").strip(),
        "manual_gb": (data.get("manual_gb") or "").strip(),
        "size_factor": (data.get("size_factor") or "").strip(),
    }
    with SessionLocal() as s:
        service = _service(s, service_id)
        r = _child(s, Resource, resource_id, "Ресурс") if resource_id else None
        try:
            if not form["name"]:
                raise ValueError("Укажите название ресурса")
            if form["pool_id"] is not None and form["pool_id"] not in {p.id for p in service.pools}:
                raise ValueError("Подпространство не найдено в этом сервисе")
            factor = parse_float(form["size_factor"] or "1", "Множитель размера", required=True)
            manual_gb = None
            if form["size_mode"] == "metric":
                if not form["promql"]:
                    raise ValueError("Укажите PromQL-запрос, возвращающий размер в байтах")
                if form["source_id"] is None or s.get(PrometheusSource, form["source_id"]) is None:
                    raise ValueError("Выберите источник Prometheus")
            else:
                manual_gb = parse_float(form["manual_gb"], "Размер", required=True)
        except ValueError as e:
            return _resource_page(request, s, service, r, form, 422, str(e))
        metric_changed = r is None or (r.promql, r.source_id) != (form["promql"], form["source_id"])
        r = r or Resource(service_id=service_id)
        r.name, r.kind, r.description = form["name"], form["kind"] or "datastream", form["description"]
        r.pool_id, r.size_factor = form["pool_id"], factor
        if form["size_mode"] == "metric":
            r.promql, r.source_id, r.manual_gb = form["promql"], form["source_id"], None
        else:
            r.promql, r.manual_gb = "", manual_gb
        if metric_changed:  # старые значения относятся к другому запросу
            r.last_bytes = r.last_success_at = r.last_checked_at = r.last_error = None
        s.add(r)
        s.commit()
        rid = r.id
    if form["size_mode"] == "metric" and metric_changed:
        await prom.refresh_resource(rid)
    return redirect(f"/resources/{rid}?msg=saved")


@router.post("/resources/{resource_id}/refresh", dependencies=edit)
async def resource_refresh(resource_id: int):
    with SessionLocal() as s:
        _child(s, Resource, resource_id, "Ресурс")
    error = await prom.refresh_resource(resource_id)
    return redirect(f"/resources/{resource_id}?msg={'refresh_failed' if error else 'refreshed'}")


@router.post("/resources/{resource_id}/delete", dependencies=edit)
def resource_delete(resource_id: int):
    with SessionLocal() as s:
        r = _child(s, Resource, resource_id, "Ресурс")
        service_id = r.service_id
        s.delete(r)
        s.commit()
    return redirect(f"/services/{service_id}?msg=resource_deleted#resources")

