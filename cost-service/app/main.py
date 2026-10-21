import logging
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import admin_routes, api_routes, auth_routes, currency, scheduler, security, server_routes, service_routes
from .db import init_db
from .web import BASE_DIR, redirect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    auth_routes.bootstrap_admin_from_env()
    admin_routes.bootstrap_source_from_env()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Cost Service", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(api_routes.router)
app.include_router(server_routes.router)
app.include_router(service_routes.router)

# Без входа: healthcheck и статика
OPEN_PREFIXES = ("/health", "/static/")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    # Защита от CSRF: POST принимаем только со своего origin (дополнительно к SameSite cookie)
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return Response("Запрос с чужого источника отклонён", status_code=403)
    if path.startswith(OPEN_PREFIXES):
        return await call_next(request)

    # Первый запуск: создание администратора
    if not auth_routes.users_exist():
        return await call_next(request) if path == "/setup/admin" else redirect("/setup/admin")
    if path in ("/login", "/setup/admin"):
        return await call_next(request)

    user = security.user_by_session(request.cookies.get(security.SESSION_COOKIE))
    if user is None:
        if path.startswith("/api/") or request.method != "GET":
            return Response("Требуется вход", status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return redirect(f"/login?next={quote(target)}")
    request.state.user = user
    currency.activate(user.currency)  # суммы на страницах и в формах — в валюте пользователя
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}
