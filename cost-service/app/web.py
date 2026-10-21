"""Общее для веб-страниц: шаблоны, фильтры, текущий пользователь."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import currency
from .config import settings
from .costs import (allocated_amounts, allocation_cost, expense_monthly, maintenance_monthly, part_monthly,
                    server_monthly)
from .models import AUTH_TYPES, CATEGORIES, DISK_TYPES, EXPENSE_PERIODS, PRICE_MODES, RESOURCES, ROLES, User

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(settings.tz)


def f_dt(dt: datetime | None) -> str:
    dt = to_local(dt)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


def f_num(value: float | None, digits: int = 2) -> str:
    """Число с разделителем разрядов и без лишних нулей: 1 234,5."""
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    if digits:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def f_money(value: float | None, digits: int = 2) -> str:
    """Сумма в долларах → в валюте пользователя, с символом валюты."""
    if value is None:
        return "—"
    display = currency.current()
    text = f"{display.from_base(value):,.{digits}f}".replace(",", "\u202f").replace(".", ",")
    return f"{text}\u00a0{display.symbol}"


def f_price_per_gb(value: float | None) -> str:
    """Цена за ГБ бывает дробной (0,0123 $): маленькие цены — с 4 знаками после запятой."""
    if value is None:
        return "—"
    return f_money(value, 2 if currency.current().from_base(value) >= 10 else 4)


def f_converted(value: float | None) -> float | None:
    """Число в валюте пользователя без форматирования — для расчётов в браузере (data-атрибуты)."""
    return currency.current().from_base(value)


def money_input(value: float | None) -> str:
    """Сумма в долларах → значение поля формы в валюте пользователя (без хвостов от пересчёта)."""
    converted = currency.current().from_base(value)
    return num_text(round(converted, 6) if converted is not None else None)


def parse_money(raw, what: str, *, required: bool = False) -> float | None:
    """Сумма из формы в валюте пользователя → в долларах."""
    return currency.current().to_base(parse_float(raw, what, required=required))


def f_gb(gb: float | None) -> str:
    """Объём в десятичных единицах (как на дисках): 1 ТБ = 1000 ГБ."""
    if gb is None:
        return "—"
    for unit, size in (("ПБ", 1e6), ("ТБ", 1e3), ("ГБ", 1), ("МБ", 1e-3)):
        if abs(gb) >= size or unit == "МБ":
            return f"{f_num(gb / size, 2)} {unit}"
    return str(gb)


def num_text(value) -> str:
    """Число для поля формы: без «.0» у целых, пусто для None."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _static_version() -> str:
    """Хеш содержимого статики: меняется при обновлении сервиса, и браузер не берёт старые CSS/JS из кэша."""
    digest = hashlib.sha256()
    for path in sorted((BASE_DIR / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


templates.env.filters.update(dt=f_dt, num=f_num, money=f_money, gbp=f_price_per_gb, gb=f_gb, conv=f_converted,
                             plain=lambda v: num_text(round(v, 6) if v is not None else None))
templates.env.globals.update(settings=settings, CATEGORIES=CATEGORIES, DISK_TYPES=DISK_TYPES, ROLES=ROLES,
                             RESOURCES=RESOURCES, PRICE_MODES=PRICE_MODES, AUTH_TYPES=AUTH_TYPES,
                             static_version=_static_version(),
                             part_monthly=part_monthly, server_monthly=server_monthly,
                             allocated_amounts=allocated_amounts, allocation_cost=allocation_cost,
                             EXPENSE_PERIODS=EXPENSE_PERIODS, expense_monthly=expense_monthly,
                             maintenance_monthly=maintenance_monthly,
                             CURRENCIES=currency.CURRENCIES, current_currency=currency.current,
                             available_currencies=currency.available)


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Требуется вход")
    return user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(403, "Раздел доступен только администраторам")
    return user


def require_editor(request: Request) -> User:
    user = current_user(request)
    if not user.can_edit:
        raise HTTPException(403, "Недостаточно прав: нужна роль редактора")
    return user


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    """Только относительные адреса этого сервиса (защита от открытого редиректа)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return "/"


def parse_float(raw, what: str, *, required: bool = False, minimum: float | None = 0) -> float | None:
    """Число из формы: допускает запятую и пробелы. Пустое значение — None (или ошибка, если required)."""
    text = str(raw or "").strip().replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        if required:
            raise ValueError(f"{what}: укажите значение")
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{what}: «{raw}» — не число") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{what}: недопустимое значение")
    if minimum is not None and value < minimum:
        raise ValueError(f"{what}: не меньше {f_num(minimum)}")
    return value
