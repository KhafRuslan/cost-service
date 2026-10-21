"""Администрирование: пользователи и источники Prometheus (только для администраторов)."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, update

from . import currency, prom, security
from .auth_routes import USERNAME_ERROR
from .config import settings
from .db import SessionLocal
from .models import AUTH_BASIC, AUTH_BEARER, AUTH_NONE, AUTH_TYPES, ROLE_ADMIN, ROLE_VIEWER, ROLES, PrometheusSource, \
    Resource, User
from .web import num_text, parse_float, redirect, render, require_admin

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])


def _get(s, model, obj_id: int, what: str):
    obj = s.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{what} не найден")
    return obj


@router.get("/admin")
def admin_home():
    return redirect("/admin/users")


# ================================================================ пользователи

@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, msg: str | None = None):
    with SessionLocal() as s:
        users = list(s.scalars(select(User).order_by(User.username)))
    return render(request, "admin/users.html", users=users, msg=msg, section="users")


def _user_form_page(request: Request, target: User | None, form: dict, status_code: int = 200, error: str = ""):
    return render(request, "admin/user_form.html", status_code, target=target, form=form, error=error,
                  section="users")


@router.get("/admin/users/new", response_class=HTMLResponse)
def user_new(request: Request):
    return _user_form_page(request, None, {"username": "", "display_name": "", "role": ROLE_VIEWER,
                                           "is_active": True})


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
def user_edit(request: Request, user_id: int):
    with SessionLocal() as s:
        target = _get(s, User, user_id, "Пользователь")
    return _user_form_page(request, target, {"username": target.username, "display_name": target.display_name,
                                             "role": target.role, "is_active": target.is_active})


def _other_active_admins(s, user_id: int | None) -> int:
    q = select(func.count(User.id)).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
    if user_id is not None:
        q = q.where(User.id != user_id)
    return s.scalar(q)


@router.post("/admin/users")
async def user_create(request: Request):
    return await _save_user(request, None)


@router.post("/admin/users/{user_id}")
async def user_update(request: Request, user_id: int):
    return await _save_user(request, user_id)


async def _save_user(request: Request, user_id: int | None):
    me = request.state.user
    data = await request.form()
    form = {
        "username": (data.get("username") or "").strip(),
        "display_name": (data.get("display_name") or "").strip()[:200],
        "role": data.get("role") if data.get("role") in ROLES else ROLE_VIEWER,
        "is_active": data.get("is_active") == "on",
    }
    password = data.get("password") or ""
    with SessionLocal() as s:
        target = _get(s, User, user_id, "Пользователь") if user_id else None
        try:
            if not re.match(security.USERNAME_RE, form["username"]):
                raise ValueError(USERNAME_ERROR)
            if s.scalar(select(User.id).where(User.username == form["username"], User.id != (user_id or 0))):
                raise ValueError(f"Пользователь {form['username']} уже существует")
            if target is None or password:
                security.check_password_policy(password)
                if password != data.get("password2"):
                    raise ValueError("Пароли не совпадают")
            keeps_admin = form["role"] == ROLE_ADMIN and form["is_active"]
            if target is not None and target.id == me.id and not keeps_admin:
                raise ValueError("Нельзя снять с себя права администратора или заблокировать себя")
            if target is not None and target.is_admin and target.is_active and not keeps_admin \
                    and not _other_active_admins(s, target.id):
                raise ValueError("Это последний активный администратор")
        except ValueError as e:
            return _user_form_page(request, target, form, 422, str(e))

        user = target or User()
        user.username, user.display_name = form["username"], form["display_name"]
        user.role, user.is_active = form["role"], form["is_active"]
        if password:
            user.password_hash = security.hash_password(password)
        s.add(user)
        s.flush()
        if target is not None and (password or not form["is_active"]):
            security.drop_user_sessions(s, user.id)
        s.commit()
    log.info("Пользователь %s сохранён (%s)", form["username"], me.username)
    return redirect("/admin/users?msg=saved")


@router.post("/admin/users/{user_id}/delete")
def user_delete(request: Request, user_id: int):
    me = request.state.user
    with SessionLocal() as s:
        target = _get(s, User, user_id, "Пользователь")
        if target.id == me.id:
            return redirect("/admin/users?msg=self")
        if target.is_admin and target.is_active and not _other_active_admins(s, target.id):
            return redirect("/admin/users?msg=last_admin")
        s.delete(target)
        s.commit()
    return redirect("/admin/users?msg=deleted")


# ================================================================ источники Prometheus

def bootstrap_source_from_env() -> None:
    """Источник «Основной» из PROMETHEUS_URL, если источников ещё нет."""
    if not settings.prometheus_url:
        return
    with SessionLocal() as s:
        if s.scalar(select(func.count(PrometheusSource.id))):
            return
        try:
            url = prom.normalize_url(settings.prometheus_url)
        except ValueError as e:
            log.warning("PROMETHEUS_URL: %s", e)
            return
        s.add(PrometheusSource(name="Основной", url=url))
        s.commit()
    log.info("Добавлен источник Prometheus «Основной»: %s", url)


@router.get("/admin/prometheus", response_class=HTMLResponse)
def sources_page(request: Request, msg: str | None = None):
    with SessionLocal() as s:
        sources = list(s.scalars(select(PrometheusSource).order_by(PrometheusSource.name)))
        usage = dict(s.execute(select(Resource.source_id, func.count(Resource.id)).group_by(Resource.source_id)).all())
    return render(request, "admin/sources.html", sources=sources, usage=usage, msg=msg, section="prometheus")


def _source_form_page(request: Request, source: PrometheusSource | None, form: dict, status_code: int = 200, **ctx):
    return render(request, "admin/source_form.html", status_code, source=source, form=form, section="prometheus",
                  **ctx)


@router.get("/admin/prometheus/new", response_class=HTMLResponse)
def source_new(request: Request):
    return _source_form_page(request, None, {"name": "", "url": settings.prometheus_url or "http://localhost:9090",
                                             "auth_type": AUTH_NONE, "username": "", "skip_tls": False})


@router.get("/admin/prometheus/{source_id}", response_class=HTMLResponse)
def source_edit(request: Request, source_id: int):
    with SessionLocal() as s:
        src = _get(s, PrometheusSource, source_id, "Источник")
    return _source_form_page(request, src, {"name": src.name, "url": src.url, "auth_type": src.auth_type,
                                            "username": src.username or "", "skip_tls": not src.verify_tls})


@router.post("/admin/prometheus")
async def source_create(request: Request):
    return await _save_source(request, None)


@router.post("/admin/prometheus/{source_id}")
async def source_update(request: Request, source_id: int):
    return await _save_source(request, source_id)


async def _save_source(request: Request, source_id: int | None):
    data = await request.form()
    action = data.get("action", "save")
    form = {
        "name": (data.get("name") or "").strip()[:100],
        "url": (data.get("url") or "").strip(),
        "auth_type": data.get("auth_type") if data.get("auth_type") in AUTH_TYPES else AUTH_NONE,
        "username": (data.get("username") or "").strip()[:200],
        "skip_tls": data.get("skip_tls") == "on",
    }
    new_secret = data.get("secret") or ""
    with SessionLocal() as s:
        src = _get(s, PrometheusSource, source_id, "Источник") if source_id else None
        try:
            url = prom.normalize_url(form["url"])
            if not form["name"]:
                raise ValueError("Укажите название источника")
            if s.scalar(select(PrometheusSource.id).where(PrometheusSource.name == form["name"],
                                                          PrometheusSource.id != (source_id or 0))):
                raise ValueError(f"Источник «{form['name']}» уже есть")
            if form["auth_type"] == AUTH_BASIC and not form["username"]:
                raise ValueError("Укажите логин")
            # Пустое поле секрета при редактировании — оставить прежний (если способ авторизации не менялся)
            secret = new_secret or (src.secret if src and src.auth_type == form["auth_type"] else None)
            if form["auth_type"] in (AUTH_BASIC, AUTH_BEARER) and not secret:
                raise ValueError("Укажите пароль" if form["auth_type"] == AUTH_BASIC else "Укажите токен")
        except ValueError as e:
            return _source_form_page(request, src, form, 422, error=str(e))

    conn = prom.Conn(url, form["auth_type"], form["username"] or None, secret, not form["skip_tls"])
    if action != "save_nocheck":
        try:
            info = await prom.probe(conn)
        except prom.PromError as e:
            return _source_form_page(request, src, form, 422, error=str(e), allow_nocheck=True)
        if action == "test":
            return _source_form_page(request, src, form, info=info)

    with SessionLocal() as s:
        src = _get(s, PrometheusSource, source_id, "Источник") if source_id else PrometheusSource()
        src.name, src.url, src.auth_type = form["name"], url, form["auth_type"]
        src.username = form["username"] if form["auth_type"] == AUTH_BASIC else None
        src.secret = secret if form["auth_type"] != AUTH_NONE else None
        src.verify_tls = not form["skip_tls"]
        s.add(src)
        s.commit()
    return redirect("/admin/prometheus?msg=saved")


@router.post("/admin/prometheus/{source_id}/delete")
def source_delete(source_id: int):
    with SessionLocal() as s:
        s.delete(_get(s, PrometheusSource, source_id, "Источник"))
        s.commit()
    return redirect("/admin/prometheus?msg=deleted")


# ================================================================ валюты

@router.get("/admin/currency", response_class=HTMLResponse)
def currency_page(request: Request, msg: str | None = None):
    form = {code: num_text(rate) for code, rate in currency.rates().items()}
    return render(request, "admin/currency.html", form=form, msg=msg, updated_at=currency.rates_updated_at(),
                  section="currency")


@router.post("/admin/currency")
async def currency_save(request: Request):
    data = await request.form()
    form = {code: (data.get(code) or "").strip() for code in currency.CURRENCIES if code != currency.BASE}
    try:
        new = {}
        for code, raw in form.items():
            value = parse_float(raw, f"Курс {code}", minimum=None)
            if value is not None and value <= 0:
                raise ValueError(f"Курс {code} должен быть больше нуля")
            new[code] = value
    except ValueError as e:
        return render(request, "admin/currency.html", 422, form=form, error=str(e),
                      updated_at=currency.rates_updated_at(), section="currency")
    currency.set_rates(new)
    # Пользователи с валютой, у которой убрали курс, видят доллары
    with SessionLocal() as s:
        s.execute(update(User).where(User.currency.not_in(currency.available())).values(currency=currency.BASE))
        s.commit()
    log.info("Курсы валют: %s (%s)", new, request.state.user.username)
    return redirect("/admin/currency?msg=saved")
