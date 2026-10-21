from sqlalchemy import select

from app import auth_routes, security
from app.db import SessionLocal
from app.models import User

from .conftest import ADMIN, create_user, ensure_admin, login, reset_all


def test_first_run(app_client):
    c = app_client
    reset_all(c)
    for path in ("/", "/login", "/admin/users", "/api/services"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup/admin", path
    assert c.get("/health").status_code == 200

    r = c.post("/setup/admin", data={"username": "admin", "password": "short", "password2": "short"})
    assert r.status_code == 422 and "не короче 8" in r.text
    r = c.post("/setup/admin", data={"username": "admin", "password": ADMIN[1], "password2": ADMIN[1]},
               follow_redirects=False)
    assert r.status_code == 303 and security.SESSION_COOKIE in r.headers["set-cookie"]
    with SessionLocal() as s:
        assert s.scalar(select(User).where(User.username == "admin")).is_admin
    assert auth_routes.users_exist()
    assert c.get("/setup/admin", follow_redirects=False).headers["location"] == "/login"


def test_login_required(app_client):
    c = app_client
    ensure_admin(c)
    c.cookies.clear()
    r = c.get("/services/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/services/1"
    assert c.get("/api/services").status_code == 401
    assert c.post("/services", data={"name": "x"}).status_code == 401
    r = c.post("/login", data={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401
    login(c, *ADMIN)
    # CSRF: POST с чужого origin отклоняется
    assert c.post("/services", data={"name": "x"}, headers={"origin": "https://evil.example"}).status_code == 403


def test_roles(app_client):
    c = app_client
    ensure_admin(c)
    create_user(c, "viewer1", "viewer")
    create_user(c, "editor1", "editor")
    r = c.post("/services", data={"name": "Roles"}, follow_redirects=False)
    sid = int(r.headers["location"].split("?")[0].rsplit("/", 1)[1])

    login(c, "viewer1", "user-pass-123")
    assert c.get("/").status_code == 200
    page = c.get(f"/services/{sid}").text
    assert "Roles" in page and "+ Добавить серверы" not in page
    assert c.get("/api/services").status_code == 200
    assert c.post("/services", data={"name": "nope"}).status_code == 403
    assert c.get("/services/new").status_code == 403
    assert c.post(f"/services/{sid}/delete").status_code == 403
    assert c.post("/api/promql/test", json={}).status_code == 403
    assert c.get("/admin/users").status_code == 403
    # Серверы видны, но не редактируются
    assert c.get("/servers").status_code == 200 and "+ Новый сервер" not in c.get("/servers").text
    assert c.get("/servers/new").status_code == 403
    assert c.post("/servers", data={"name": "nope"}).status_code == 403

    login(c, "editor1", "user-pass-123")
    assert "+ Добавить серверы" in c.get(f"/services/{sid}").text
    assert c.post("/services", data={"name": "by editor"}, follow_redirects=False).status_code == 303
    assert c.get("/admin/prometheus").status_code == 403

    # Последнего администратора нельзя понизить
    login(c, *ADMIN)
    with SessionLocal() as s:
        admin_id = s.scalar(select(User.id).where(User.username == ADMIN[0]))
    r = c.post(f"/admin/users/{admin_id}", data={"username": "admin", "role": "editor", "is_active": "on"})
    assert r.status_code == 422
