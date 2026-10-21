"""Валюты: суммы хранятся в долларах, показываются и вводятся в валюте, выбранной пользователем.

Курсы EUR и RUB к доллару задаёт администратор (таблица app_settings).
Валюта текущего запроса лежит в contextvar: её ставит middleware, а фильтры шаблонов и формы пересчитывают суммы.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from .db import SessionLocal, utcnow
from .models import AppSetting

BASE = "USD"
# Код → (символ, название)
CURRENCIES = {"USD": ("$", "доллар США"), "EUR": ("€", "евро"), "RUB": ("₽", "российский рубль")}


@dataclass(frozen=True)
class Display:
    code: str
    rate: float  # единиц валюты за 1 доллар

    @property
    def symbol(self) -> str:
        return CURRENCIES[self.code][0]

    def from_base(self, usd: float | None) -> float | None:
        return usd * self.rate if usd is not None else None

    def to_base(self, value: float | None) -> float | None:
        return value / self.rate if value is not None else None


BASE_DISPLAY = Display(BASE, 1.0)
_current: ContextVar[Display] = ContextVar("display_currency", default=BASE_DISPLAY)

# ---------------------------------------------------------------- курсы

_RATE_KEY = "rate_{}"  # rate_EUR, rate_RUB — сколько единиц валюты за 1 доллар
_cache: dict[str, float] | None = None


def rates() -> dict[str, float]:
    """Заданные курсы (без доллара); кэшируются до изменения."""
    global _cache
    if _cache is None:
        with SessionLocal() as s:
            rows = {r.key: r.value for r in s.query(AppSetting).filter(AppSetting.key.like("rate_%"))}
        _cache = {}
        for code in CURRENCIES:
            try:
                value = float(rows.get(_RATE_KEY.format(code), ""))
            except ValueError:
                continue
            if value > 0:
                _cache[code] = value
    return _cache


def rates_updated_at() -> datetime | None:
    with SessionLocal() as s:
        row = s.get(AppSetting, _RATE_KEY.format("RUB")) or s.get(AppSetting, _RATE_KEY.format("EUR"))
        return row.updated_at if row else None


def set_rates(new: dict[str, float | None]) -> None:
    """Сохранить курсы; None — курс не задан (валюта недоступна)."""
    global _cache
    with SessionLocal() as s:
        for code, value in new.items():
            key = _RATE_KEY.format(code)
            row = s.get(AppSetting, key)
            if value is None:
                if row:
                    s.delete(row)
                continue
            row = row or AppSetting(key=key)
            row.value, row.updated_at = repr(float(value)), utcnow()
            s.add(row)
        s.commit()
    _cache = None


def available() -> list[str]:
    """Валюты, доступные для выбора: доллар и те, у которых задан курс."""
    return [BASE] + [c for c in CURRENCIES if c != BASE and c in rates()]


def display_for(code: str | None) -> Display:
    """Валюта показа; если курс не задан — доллар."""
    if code and code != BASE and code in rates():
        return Display(code, rates()[code])
    return BASE_DISPLAY


# ---------------------------------------------------------------- валюта текущего запроса

def current() -> Display:
    return _current.get()


def activate(code: str | None) -> None:
    _current.set(display_for(code))
