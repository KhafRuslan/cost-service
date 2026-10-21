"""Серверы: каталог с комплектующими и выделение ресурсов серверов сервисам."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .costs import allocated_amounts, allocation_cost, server_capacities, server_monthly, server_usage
from .db import SessionLocal
from .models import (CATEGORIES, DISK_TYPES, EXPENSE_CAPEX_PERCENT, EXPENSE_MONTHLY, EXPENSE_PERIODS, PRICE_CAPEX,
                     PRICE_MODES, PRICE_MONTHLY, RESOURCES, Server, ServerExpense, ServerPart, Service, ServiceServer)
from .web import f_num, money_input, num_text, parse_float, parse_money, redirect, render, require_editor

router = APIRouter()
edit = [Depends(require_editor)]

EMPTY_PART = {"category": "cpu", "name": "", "spec": "", "capacity": "", "disk_type": "ssd", "quantity": "1",
              "price": "", "price_mode": PRICE_MONTHLY, "amortization_months": "60"}
EMPTY_EXPENSE = {"name": "", "amount": "", "period": EXPENSE_MONTHLY}
PART_FIELDS = ("position", "category", "name", "spec", "capacity", "disk_type", "quantity", "price_mode", "price",
               "amortization_months")
MAX_COPIES = 100
EPS = 1e-6  # допуск при сравнении выделенного со свободным


def _get(s, model, obj_id: int, what: str):
    obj = s.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{what} не найден")
    return obj


def free_amounts(server: Server, exclude: ServiceServer | None = None) -> dict[str, float]:
    """Свободные ресурсы сервера (без учёта привязки exclude — её сейчас меняют)."""
    free = {k: c.total for k, c in server_capacities(server).items()}
    for a in server.services:
        if exclude is None or a.id != exclude.id:
            for k, amount in allocated_amounts(a).items():
                free[k] -= amount
    return {k: max(0.0, v) for k, v in free.items()}


def parse_allocation(data, prefix: str, server: Server, exclude: ServiceServer | None = None) -> tuple[bool, dict]:
    """Выделение из формы: поля <prefix>whole и <prefix><ресурс>. Возвращает (весь сервер, ресурсы)."""
    others = [a.service.name for a in server.services if exclude is None or a.id != exclude.id]
    if data.get(f"{prefix}whole") == "on":
        if others:
            raise ValueError(f"Сервер {server.name} уже используется в сервисах: {', '.join(sorted(others))} — "
                             f"весь сервер выделить нельзя, укажите ресурсы")
        return True, {}
    caps, free = server_capacities(server), free_amounts(server, exclude)
    if not caps:
        raise ValueError(f"У сервера {server.name} нет комплектующих с ёмкостью — можно выделить только весь сервер")
    amounts = {}
    for key in caps:
        label, unit = RESOURCES[key]
        value = parse_float(data.get(f"{prefix}{key}"), f"{server.name}: {label}") or 0.0
        if value > free[key] + EPS:
            raise ValueError(f"{server.name}: {label} — свободно только {f_num(free[key])} {unit}")
        if value:
            amounts[key] = value
    if not amounts:
        raise ValueError(f"{server.name}: укажите, какие ресурсы выделить сервису")
    return False, amounts


# ================================================================ каталог серверов

@router.get("/servers", response_class=HTMLResponse)
def servers_page(request: Request, msg: str | None = None):
    with SessionLocal() as s:
        usages = [server_usage(srv) for srv in s.scalars(select(Server).order_by(Server.name))]
        return render(request, "servers.html", usages=usages, msg=msg, total=sum(u.cost for u in usages),
                      unallocated=sum(u.unallocated_cost for u in usages),
                      idle=sum(1 for u in usages if not u.server.services))


def _server_form(server: Server | None) -> dict:
    if server is None:
        return {"name": "", "kind": "Сервер", "host": "", "description": "", "count": "1", "service_id": None,
                "parts": [{**EMPTY_PART, "category": c} for c in ("cpu", "ram", "disk")], "expenses": []}
    return {"name": server.name, "kind": server.kind, "host": server.host, "description": server.description,
            "parts": [{"category": p.category, "name": p.name, "spec": p.spec, "capacity": num_text(p.capacity),
                       "disk_type": p.disk_type or "ssd", "quantity": num_text(p.quantity),
                       "price": money_input(p.price), "price_mode": p.price_mode,
                       "amortization_months": num_text(p.amortization_months) or "60"}
                      for p in server.parts],
            "expenses": [{"name": e.name, "period": e.period,
                          "amount": num_text(e.amount) if e.period == EXPENSE_CAPEX_PERCENT else money_input(e.amount)}
                         for e in server.expenses]}


def _server_page(request: Request, s, server: Server | None, form: dict, status_code: int = 200, **ctx):
    services = list(s.scalars(select(Service).order_by(Service.name))) if server is None else []
    return render(request, "server_form.html", status_code, server=server, form=form, services=services,
                  empty_part=EMPTY_PART, empty_expense=EMPTY_EXPENSE, usage=server_usage(server) if server else None,
                  **ctx)


@router.get("/servers/new", response_class=HTMLResponse, dependencies=edit)
def server_new(request: Request, service_id: int | None = None):
    form = _server_form(None)
    form["service_id"] = service_id
    with SessionLocal() as s:
        return _server_page(request, s, None, form)


@router.get("/servers/{server_id}", response_class=HTMLResponse)
def server_detail(request: Request, server_id: int, msg: str | None = None):
    with SessionLocal() as s:
        server = _get(s, Server, server_id, "Сервер")
        if request.state.user.can_edit:
            return _server_page(request, s, server, _server_form(server), msg=msg)
        return render(request, "server_view.html", server=server, usage=server_usage(server))


def _parse_parts(data) -> tuple[list[dict], list[ServerPart], list[str]]:
    """Строки комплектующих из формы (поля part_* идут параллельными списками): строки, комплектующие, ошибки."""
    fields = ("category", "name", "spec", "capacity", "disk_type", "quantity", "price", "price_mode",
              "amortization_months")
    columns = {f: data.getlist(f"part_{f}") for f in fields}
    count = len(columns["category"])
    if any(len(v) != count for v in columns.values()):
        return [], [], ["Форма комплектующих повреждена — обновите страницу"]
    rows = [{f: (columns[f][i] or "").strip() for f in fields} for i in range(count)]
    rows = [r for r in rows if r["name"] or r["spec"] or r["capacity"] or r["price"]]  # без пустых строк
    parts, errors = [], []
    for n, raw in enumerate(rows, 1):
        try:
            if raw["category"] not in CATEGORIES:
                raise ValueError("неизвестная категория")
            mode = raw["price_mode"] if raw["price_mode"] in PRICE_MODES else PRICE_MONTHLY
            part = ServerPart(
                position=n, category=raw["category"], name=raw["name"][:200], spec=raw["spec"][:500],
                capacity=parse_float(raw["capacity"], "ёмкость", required=raw["category"] == "disk"),
                disk_type=(raw["disk_type"] if raw["disk_type"] in DISK_TYPES else "ssd")
                if raw["category"] == "disk" else None,
                quantity=parse_float(raw["quantity"], "количество", required=True),
                price=parse_money(raw["price"], "цена", required=True), price_mode=mode,
            )
            if mode == PRICE_CAPEX:
                months = parse_float(raw["amortization_months"], "срок амортизации", required=True, minimum=1)
                if not months.is_integer():
                    raise ValueError("срок амортизации — целое число месяцев")
                part.amortization_months = int(months)
            parts.append(part)
        except ValueError as e:
            errors.append(f"Строка {n} ({CATEGORIES.get(raw['category'], ('?',))[0]}): {e}")
    return rows, parts, errors


def _parse_expenses(data) -> tuple[list[dict], list[ServerExpense], list[str]]:
    """Строки расходов на обслуживание (поля exp_* параллельными списками): строки, расходы, ошибки."""
    names, amounts, periods = data.getlist("exp_name"), data.getlist("exp_amount"), data.getlist("exp_period")
    if not len(names) == len(amounts) == len(periods):
        return [], [], ["Форма расходов на обслуживание повреждена — обновите страницу"]
    rows = [{"name": (n or "").strip()[:200], "amount": (a or "").strip(), "period": p}
            for n, a, p in zip(names, amounts, periods)]
    rows = [r for r in rows if r["name"] or r["amount"]]  # без пустых строк
    expenses, errors = [], []
    for n, row in enumerate(rows, 1):
        try:
            if not row["name"]:
                raise ValueError("укажите название")
            period = row["period"] if row["period"] in EXPENSE_PERIODS else EXPENSE_MONTHLY
            if period == EXPENSE_CAPEX_PERCENT:
                amount = parse_float(row["amount"], "процент", required=True)
            else:
                amount = parse_money(row["amount"], "сумма", required=True)
            expenses.append(ServerExpense(position=n, name=row["name"], amount=amount, period=period))
        except ValueError as e:
            errors.append(f"Обслуживание, строка {n}{' (' + row['name'] + ')' if row['name'] else ''}: {e}")
    return rows, expenses, errors


def _copy_parts(parts) -> list[ServerPart]:
    return [ServerPart(**{k: getattr(p, k) for k in PART_FIELDS}) for p in parts]


def _copy_expenses(expenses) -> list[ServerExpense]:
    return [ServerExpense(position=e.position, name=e.name, amount=e.amount, period=e.period) for e in expenses]


def _names(base: str, count: int) -> list[str]:
    if count == 1:
        return [base]
    width = max(2, len(str(count)))
    return [f"{base}-{n:0{width}d}" for n in range(1, count + 1)]


@router.post("/servers", dependencies=edit)
async def server_create(request: Request):
    return await _save_server(request, None)


@router.post("/servers/{server_id}", dependencies=edit)
async def server_update(request: Request, server_id: int):
    return await _save_server(request, server_id)


async def _save_server(request: Request, server_id: int | None):
    data = await request.form()
    form = {"name": (data.get("name") or "").strip()[:190], "kind": (data.get("kind") or "").strip()[:100],
            "host": (data.get("host") or "").strip()[:300], "description": (data.get("description") or "").strip(),
            "count": (data.get("count") or "1").strip(),
            "service_id": int(data["service_id"]) if str(data.get("service_id") or "").isdigit() else None,
            "parts": [], "expenses": []}
    with SessionLocal() as s:
        server = _get(s, Server, server_id, "Сервер") if server_id else None
        try:
            form["parts"], parts, errors = _parse_parts(data)
            form["expenses"], expenses, expense_errors = _parse_expenses(data)
            errors += expense_errors
            if errors:
                raise ValueError("; ".join(errors))
            if not form["name"]:
                raise ValueError("Укажите название сервера")
            count = 1
            if server is None:
                count = parse_float(form["count"], "Количество", required=True, minimum=1)
                if not count.is_integer() or count > MAX_COPIES:
                    raise ValueError(f"Количество — целое число от 1 до {MAX_COPIES}")
                count = int(count)
            names = _names(form["name"], count)
            taken = set(s.scalars(select(Server.name).where(Server.name.in_(names), Server.id != (server_id or 0))))
            if taken:
                raise ValueError(f"Сервер с таким названием уже есть: {', '.join(sorted(taken)[:5])}")
            service = None
            if server is None and form["service_id"] is not None:
                service = _get(s, Service, form["service_id"], "Сервис")
        except ValueError as e:
            if not form["parts"]:
                form["parts"] = [dict(EMPTY_PART)]
            return _server_page(request, s, server, form, 422, error=str(e))

        if server is not None:
            server.name, server.kind, server.host = form["name"], form["kind"] or "Сервер", form["host"]
            server.description, server.parts, server.expenses = form["description"], parts, expenses
            s.commit()
            return redirect(f"/servers/{server.id}?msg=saved")
        for n, name in enumerate(names):
            srv = Server(name=name, kind=form["kind"] or "Сервер", host=form["host"] if count == 1 else "",
                         description=form["description"], parts=parts if n == 0 else _copy_parts(parts),
                         expenses=expenses if n == 0 else _copy_expenses(expenses))
            s.add(srv)
            if service is not None:
                s.add(ServiceServer(service=service, server=srv, whole=True))
        s.commit()
        if service is not None:
            return redirect(f"/services/{service.id}?msg=servers_added#servers")
        return redirect(f"/servers?msg={'created_many' if count > 1 else 'saved'}")


@router.post("/servers/{server_id}/copy", dependencies=edit)
def server_copy(server_id: int):
    with SessionLocal() as s:
        src = _get(s, Server, server_id, "Сервер")
        taken = set(s.scalars(select(Server.name)))
        name, k = f"{src.name} (копия)", 2
        while name in taken:
            name, k = f"{src.name} (копия {k})", k + 1
        copy = Server(name=name[:200], kind=src.kind, description=src.description, parts=_copy_parts(src.parts),
                      expenses=_copy_expenses(src.expenses))
        s.add(copy)
        s.commit()
        return redirect(f"/servers/{copy.id}")


@router.post("/servers/{server_id}/delete", dependencies=edit)
def server_delete(server_id: int):
    with SessionLocal() as s:
        s.delete(_get(s, Server, server_id, "Сервер"))
        s.commit()
    return redirect("/servers?msg=deleted")


# ================================================================ серверы в сервисе

@router.get("/services/{service_id}/servers/add", response_class=HTMLResponse, dependencies=edit)
def servers_add_page(request: Request, service_id: int):
    with SessionLocal() as s:
        service = _get(s, Service, service_id, "Сервис")
        return _add_page(request, s, service, {})


def _add_page(request: Request, s, service: Service, form: dict, status_code: int = 200, error: str = ""):
    in_service = {a.server_id for a in service.servers}
    candidates = [(srv, server_monthly(srv), server_capacities(srv), free_amounts(srv))
                  for srv in s.scalars(select(Server).order_by(Server.name)) if srv.id not in in_service]
    return render(request, "servers_add.html", status_code, service=service, candidates=candidates, form=form,
                  error=error)


@router.post("/services/{service_id}/servers", dependencies=edit)
async def servers_add(request: Request, service_id: int):
    data = await request.form()
    ids = [int(v) for v in data.getlist("server_ids") if str(v).isdigit()]
    form = {**{k: v for k, v in data.items() if k != "server_ids"}, "server_ids": set(ids), "submitted": True}
    with SessionLocal() as s:
        service = _get(s, Service, service_id, "Сервис")
        in_service = {a.server_id for a in service.servers}
        try:
            if not ids:
                raise ValueError("Отметьте серверы, которые нужно добавить")
            links = []
            for server_id in ids:
                server = _get(s, Server, server_id, "Сервер")
                if server.id in in_service:
                    raise ValueError(f"Сервер {server.name} уже в сервисе")
                whole, amounts = parse_allocation(data, f"s{server_id}_", server)
                links.append(ServiceServer(service=service, server=server, whole=whole, amounts=amounts))
        except ValueError as e:
            return _add_page(request, s, service, form, 422, str(e))
        s.add_all(links)
        s.commit()
    return redirect(f"/services/{service_id}?msg=servers_added#servers")


def _assignment_page(request: Request, a: ServiceServer, form: dict, status_code: int = 200, error: str = ""):
    return render(request, "assignment_form.html", status_code, a=a, form=form, error=error,
                  capacities=server_capacities(a.server), free=free_amounts(a.server, exclude=a),
                  others=[o for o in a.server.services if o.id != a.id], cost=allocation_cost(a),
                  full_cost=server_monthly(a.server))


@router.get("/assignments/{assignment_id}", response_class=HTMLResponse, dependencies=edit)
def assignment_edit(request: Request, assignment_id: int):
    with SessionLocal() as s:
        a = _get(s, ServiceServer, assignment_id, "Сервер в сервисе")
        form = {"whole": a.whole, **{k: num_text(v) for k, v in (a.amounts or {}).items()}}
        if a.whole:  # при переключении на ресурсы — подставить всю ёмкость
            form.update({k: num_text(round(c.total, 6)) for k, c in server_capacities(a.server).items()})
        return _assignment_page(request, a, form)


@router.post("/assignments/{assignment_id}", dependencies=edit)
async def assignment_update(request: Request, assignment_id: int):
    data = await request.form()
    with SessionLocal() as s:
        a = _get(s, ServiceServer, assignment_id, "Сервер в сервисе")
        try:
            a.whole, a.amounts = parse_allocation(data, "", a.server, exclude=a)
        except ValueError as e:
            s.rollback()
            a = _get(s, ServiceServer, assignment_id, "Сервер в сервисе")
            return _assignment_page(request, a, {**data, "whole": data.get("whole") == "on"}, 422, str(e))
        service_id = a.service_id
        s.commit()
    return redirect(f"/services/{service_id}?msg=allocation_saved#servers")


@router.post("/assignments/{assignment_id}/delete", dependencies=edit)
def assignment_delete(assignment_id: int):
    with SessionLocal() as s:
        a = _get(s, ServiceServer, assignment_id, "Сервер в сервисе")
        service_id = a.service_id
        for pool in a.service.pools:  # диски сервера больше не входят в подпространства сервиса
            pool.servers = [srv for srv in pool.servers if srv.id != a.server_id]
        s.delete(a)
        s.commit()
    return redirect(f"/services/{service_id}?msg=server_removed#servers")
