"""Вход, выход, создание первого администратора, личный кабинет."""

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from . import currency, security
from .config import settings
from .db import SessionLocal
from .models import ROLE_ADMIN, User
from .web import current_user, redirect, render, safe_next

log = logging.getLogger(__name__)
router = APIRouter()

USERNAME_ERROR = "Логин: 2–64 символа — латиница, цифры, точка, дефис, подчёркивание, @"
_has_users = False


def users_exist() -> bool:
    """Есть ли хотя бы один пользователь (результат кэшируется: последнего администратора удалить нельзя)."""
    global _has_users
    if not _has_users:
        with SessionLocal() as s:
            _has_users = bool(s.scalar(select(func.count(User.id))))
    return _has_users


def create_first_admin(username: str, password: str, display_name: str = "") -> User:
    with SessionLocal() as s:
        user = User(username=username, display_name=display_name, password_hash=security.hash_password(password),
                    role=ROLE_ADMIN)
        s.add(user)
        s.commit()
    log.info("Создан администратор %s", username)
    return user


def bootstrap_admin_from_env() -> None:
    if settings.admin_username and settings.admin_password and not users_exist():
        create_first_admin(settings.admin_username, settings.admin_password)


def _login_response(request: Request, user_id: int, next_url: str):
    token = security.create_session(user_id)
    resp = redirect(safe_next(next_url))
    resp.set_cookie(security.SESSION_COOKIE, token, max_age=settings.session_ttl_hours * 3600, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https", path="/")
    return resp


# ---------------------------------------------------------------- первый запуск

@router.get("/setup/admin", response_class=HTMLResponse)
def setup_admin_page(request: Request):
    if users_exist():
        return redirect("/login")
    return render(request, "setup_admin.html", form={"username": "admin", "display_name": ""})


@router.post("/setup/admin")
async def setup_admin(request: Request):
    if users_exist():
        return redirect("/login")
    data = await request.form()
    form = {"username": (data.get("username") or "").strip(), "display_name": (data.get("display_name") or "").strip()}
    password = data.get("password") or ""
    try:
        if not re.match(security.USERNAME_RE, form["username"]):
            raise ValueError(USERNAME_ERROR)
        security.check_password_policy(password)
        if password != data.get("password2"):
            raise ValueError("Пароли не совпадают")
    except ValueError as e:
        return render(request, "setup_admin.html", 422, form=form, error=str(e))
    user = create_first_admin(form["username"], password, form["display_name"][:200])
    return _login_response(request, user.id, "/")


# ---------------------------------------------------------------- вход и выход

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=safe_next(next), username="")


@router.post("/login")
async def login(request: Request):
    data = await request.form()
    username = (data.get("username") or "").strip()
    next_url = safe_next(data.get("next"))
    if security.login_blocked(username):
        return render(request, "login.html", 429, next=next_url, username=username,
                      error="Слишком много неудачных попыток. Попробуйте через 15 минут.")
    user = security.authenticate(username, data.get("password") or "")
    if user is None:
        return render(request, "login.html", 401, next=next_url, username=username,
                      error="Неверный логин или пароль")
    log.info("Вход: %s", user.username)
    return _login_response(request, user.id, next_url)


@router.post("/logout")
def logout(request: Request):
    security.drop_session(request.cookies.get(security.SESSION_COOKIE))
    resp = redirect("/login")
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- личный кабинет

@router.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str | None = None):
    return render(request, "account.html", msg=msg)


@router.post("/account/password")
async def change_password(request: Request):
    user = current_user(request)
    data = await request.form()
    new = data.get("new_password") or ""
    with SessionLocal() as s:
        db_user = s.get(User, user.id)
        try:
            if not security.verify_password(data.get("current_password") or "", db_user.password_hash):
                raise ValueError("Текущий пароль указан неверно")
            security.check_password_policy(new)
            if new != data.get("new_password2"):
                raise ValueError("Пароли не совпадают")
        except ValueError as e:
            return render(request, "account.html", 422, error=str(e))
        db_user.password_hash = security.hash_password(new)
        security.drop_user_sessions(s, user.id)  # выходим на всех устройствах
        s.commit()
    return _login_response(request, user.id, "/account?msg=password")


@router.post("/currency")
async def switch_currency(request: Request):
    """Валюта, в которой пользователь видит и вводит суммы."""
    user = current_user(request)
    data = await request.form()
    code = data.get("currency")
    if code in currency.available():
        with SessionLocal() as s:
            s.get(User, user.id).currency = code
            s.commit()
    return redirect(safe_next(data.get("next")))
