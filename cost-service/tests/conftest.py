import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cost-service-test-")
os.environ["TIMEZONE"] = "Europe/Moscow"
os.environ["METRICS_REFRESH_MINUTES"] = "0"
os.environ["PROMETHEUS_URL"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import auth_routes, prom, security  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuthSession, PrometheusSource, Service, User  # noqa: E402

ADMIN = ("admin", "admin-pass-123")
REAL_ASYNC_CLIENT = httpx.AsyncClient
PROM_REQUESTS: list[httpx.Request] = []
# PromQL-запрос -> ответ data (по умолчанию пустой вектор)
PROM_RESULTS: dict[str, dict] = {}


def vector(*values: float) -> dict:
    return {"resultType": "vector",
            "result": [{"metric": {"index": f"i{n}"}, "value": [1700000000, str(v)]} for n, v in enumerate(values)]}


def fake_prom(request: httpx.Request) -> httpx.Response:
    PROM_REQUESTS.append(request)
    if request.headers.get("authorization") == "Bearer bad-token":
        return httpx.Response(401, text="Unauthorized")
    path = request.url.path
    if path.endswith("/api/v1/status/buildinfo"):
        return httpx.Response(200, json={"status": "success", "data": {"version": "2.55.0"}})
    if path.endswith("/api/v1/query"):
        q = request.url.params.get("query")
        if q == "vector(1)":
            return httpx.Response(200, json={"status": "success", "data": vector(1)})
        if "syntax error" in q:
            return httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"})
        return httpx.Response(200, json={"status": "success", "data": PROM_RESULTS.get(q, vector())})
    return httpx.Response(404, text="not found")


def fake_build_client(conn: prom.Conn) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {conn.secret}"} if conn.auth_type == "bearer" else {}
    return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake_prom), headers=headers)


@pytest.fixture(scope="session")
def app_client():
    prom._build_client = fake_build_client
    with TestClient(app) as c:
        yield c


def login(c, username: str, password: str):
    c.cookies.clear()
    r = c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def ensure_admin(c) -> None:
    if not auth_routes.users_exist():
        c.post("/setup/admin", data={"username": ADMIN[0], "password": ADMIN[1], "password2": ADMIN[1]})
    login(c, *ADMIN)


def reset_all(c) -> None:
    """Пустая БД, как при первом запуске."""
    with SessionLocal() as s:
        for model in (Service, PrometheusSource, AuthSession, User):
            s.query(model).delete()
        s.commit()
    auth_routes._has_users = False
    security._failures.clear()
    PROM_RESULTS.clear()
    c.cookies.clear()


@pytest.fixture
def client(app_client):
    """Администратор вошёл в систему."""
    ensure_admin(app_client)
    return app_client


def last_id(r) -> int:
    return int(r.headers["location"].split("?")[0].split("#")[0].rstrip("/").split("/")[2])


def create_user(c, username: str, role: str, password: str = "user-pass-123") -> int:
    r = c.post("/admin/users", data={"username": username, "password": password, "password2": password,
                                     "role": role, "is_active": "on"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        return s.scalar(select(User.id).where(User.username == username))
