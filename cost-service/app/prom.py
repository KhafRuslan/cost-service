"""Запросы к Prometheus (HTTP API v1) и обновление размеров ресурсов."""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal, utcnow
from .models import AUTH_BASIC, AUTH_BEARER, PrometheusSource, Resource, ResourceSample

log = logging.getLogger(__name__)


class PromError(Exception):
    pass


@dataclass(frozen=True)
class Conn:
    """Параметры подключения (отвязаны от сессии БД)."""

    url: str
    auth_type: str = "none"
    username: str | None = None
    secret: str | None = None
    verify_tls: bool = True

    @classmethod
    def of(cls, src: PrometheusSource) -> "Conn":
        return cls(src.url, src.auth_type, src.username, src.secret, src.verify_tls)


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Адрес Prometheus должен начинаться с http:// или https://")
    return url


def _build_client(conn: Conn) -> httpx.AsyncClient:
    headers, auth = {}, None
    if conn.auth_type == AUTH_BASIC:
        auth = httpx.BasicAuth(conn.username or "", conn.secret or "")
    elif conn.auth_type == AUTH_BEARER and conn.secret:
        headers["Authorization"] = f"Bearer {conn.secret}"
    return httpx.AsyncClient(headers=headers, auth=auth, verify=conn.verify_tls, timeout=settings.prometheus_timeout)


async def _get(conn: Conn, path: str, params: dict | None = None) -> dict:
    try:
        async with _build_client(conn) as client:
            resp = await client.get(conn.url + path, params=params)
    except httpx.HTTPError as e:
        raise PromError(f"Prometheus недоступен: {e.__class__.__name__}: {e}") from e
    try:
        data = resp.json()
    except ValueError:
        raise PromError(f"Prometheus вернул не JSON (HTTP {resp.status_code})") from None
    if resp.status_code >= 400 or data.get("status") != "success":
        raise PromError(f"Ошибка Prometheus (HTTP {resp.status_code}): {data.get('error') or resp.text[:300]}")
    return data.get("data") or {}


def _number(raw) -> float:
    value = float(raw)
    if math.isnan(value) or math.isinf(value):
        raise PromError(f"Запрос вернул {raw}")
    return value


async def query_value(conn: Conn, promql: str) -> float:
    """Значение PromQL-запроса в текущий момент. Несколько рядов суммируются."""
    data = await _get(conn, "/api/v1/query", {"query": promql})
    kind, result = data.get("resultType"), data.get("result")
    if kind == "scalar":
        return _number(result[1])
    if kind == "vector":
        if not result:
            raise PromError("Запрос не вернул данных (пустой результат)")
        return sum(_number(item["value"][1]) for item in result)
    raise PromError(f"Запрос должен возвращать число или вектор, а вернул {kind}")


async def probe(conn: Conn) -> str:
    """Проверка подключения: версия Prometheus (или «доступен», если версия не отдаётся)."""
    try:
        return "версия " + (await _get(conn, "/api/v1/status/buildinfo")).get("version", "?")
    except PromError:
        await query_value(conn, "vector(1)")
        return "доступен"


# ---------------------------------------------------------------- обновление размеров

async def refresh_resource(resource_id: int) -> str | None:
    """Запрашивает размер ресурса и сохраняет его. Возвращает текст ошибки (None — успешно)."""
    with SessionLocal() as s:
        r = s.get(Resource, resource_id)
        if r is None or not r.uses_metric:
            return None
        if r.source is None:
            error, value = "Не выбран источник Prometheus", None
        else:
            conn, promql = Conn.of(r.source), r.promql
            error = value = None
    if error is None:
        try:
            value = await query_value(conn, promql)
        except PromError as e:
            error = str(e)
    with SessionLocal() as s:
        r = s.get(Resource, resource_id)
        if r is None:
            return None
        now = utcnow()
        r.last_checked_at, r.last_error = now, error
        if error is None:
            r.last_bytes, r.last_success_at = value, now
            s.add(ResourceSample(resource_id=r.id, ts=now, bytes=value))
        s.commit()
    if error:
        log.warning("Ресурс %s: %s", resource_id, error)
    return error


async def refresh_many(resource_ids: list[int]) -> dict[int, str | None]:
    sem = asyncio.Semaphore(4)

    async def one(rid: int):
        async with sem:
            return rid, await refresh_resource(rid)

    return dict(await asyncio.gather(*(one(rid) for rid in resource_ids)))


def metric_resource_ids(service_id: int | None = None) -> list[int]:
    with SessionLocal() as s:
        q = select(Resource.id).where(Resource.promql != "")
        if service_id is not None:
            q = q.where(Resource.service_id == service_id)
        return list(s.scalars(q))


async def refresh_all() -> None:
    ids = metric_resource_ids()
    if ids:
        errors = sum(1 for e in (await refresh_many(ids)).values() if e)
        log.info("Обновлены размеры ресурсов: %d, ошибок: %d", len(ids), errors)


def cleanup_samples() -> None:
    with SessionLocal() as s:
        s.execute(delete(ResourceSample).where(
            ResourceSample.ts < utcnow() - timedelta(days=settings.metrics_history_days)))
        s.commit()
