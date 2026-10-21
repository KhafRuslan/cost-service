import json
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Текущее время в UTC без tzinfo — в таком виде даты хранятся в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_is_sqlite = settings.db_url.startswith("sqlite")
engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401  (регистрация моделей)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _add_column("users", "currency", "VARCHAR(3) NOT NULL DEFAULT 'USD'")
    _migrate_shares()
    _migrate_components()


def _add_column(table: str, column: str, ddl: str) -> None:
    """Новая колонка в существующей таблице (create_all добавляет только таблицы)."""
    if column not in {c["name"] for c in inspect(engine).get_columns(table)}:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        log.info("Добавлена колонка %s.%s", table, column)


def _migrate_shares() -> None:
    """Доля сервера в сервисе (share_percent) → выделенные ресурсы: та же доля каждого ресурса, 100% — весь сервер."""
    from types import SimpleNamespace

    from .costs import server_capacities

    cols = {c["name"] for c in inspect(engine).get_columns("service_servers")}
    if "share_percent" not in cols:
        return
    part_cols = "category, capacity, disk_type, quantity, price_mode, price, amortization_months"
    with engine.begin() as conn:
        if "whole" not in cols:
            conn.execute(text("ALTER TABLE service_servers ADD COLUMN whole BOOLEAN NOT NULL DEFAULT 0"))
        if "amounts" not in cols:
            conn.execute(text("ALTER TABLE service_servers ADD COLUMN amounts JSON NOT NULL DEFAULT '{}'"))
        rows = conn.execute(text("SELECT id, server_id, share_percent FROM service_servers")).all()
        for link_id, server_id, share in rows:
            whole, amounts = share >= 100 - 1e-6, {}
            if not whole:
                parts = conn.execute(text(f"SELECT {part_cols} FROM server_parts WHERE server_id = :id"),
                                     {"id": server_id}).mappings().all()
                caps = server_capacities(SimpleNamespace(parts=[SimpleNamespace(**p) for p in parts]))
                amounts = {k: round(c.total * share / 100, 6) for k, c in caps.items()}
            conn.execute(text("UPDATE service_servers SET whole = :whole, amounts = :amounts WHERE id = :id"),
                         {"whole": whole, "amounts": json.dumps(amounts), "id": link_id})
        conn.execute(text("ALTER TABLE service_servers DROP COLUMN share_percent"))
    log.info("Доли серверов переведены в выделенные ресурсы: %d", len(rows))


def _migrate_components() -> None:
    """Компоненты сервисов (старая схема) → серверы: компонент × N становится N серверами в сервисе на 100%."""
    if "components" not in inspect(engine).get_table_names():
        return
    part_cols = ("position", "category", "name", "spec", "capacity", "disk_type", "quantity", "price_mode", "price",
                 "amortization_months")
    with engine.begin() as conn:
        taken = {r[0] for r in conn.execute(text("SELECT name FROM servers"))}
        components = conn.execute(text(
            "SELECT id, service_id, name, kind, quantity, description FROM components ORDER BY id")).mappings().all()
        for c in components:
            parts = conn.execute(text(f"SELECT {', '.join(part_cols)} FROM component_parts WHERE component_id = :id"),
                                 {"id": c["id"]}).mappings().all()
            pools = [r[0] for r in conn.execute(text("SELECT pool_id FROM pool_components WHERE component_id = :id"),
                                                {"id": c["id"]})]
            count = max(1, c["quantity"] or 1)
            for n in range(1, count + 1):
                name = c["name"] if count == 1 else f"{c['name']}-{n:02d}"
                base, k = name, 2
                while name in taken:
                    name, k = f"{base} ({k})", k + 1
                taken.add(name)
                now = utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")  # формат DateTime SQLAlchemy для SQLite
                server_id = conn.execute(text(
                    "INSERT INTO servers (name, kind, host, description, created_at, updated_at) "
                    "VALUES (:name, :kind, '', :description, :now, :now)"),
                    {"name": name, "kind": c["kind"], "description": c["description"], "now": now}).lastrowid
                for p in parts:
                    conn.execute(text(f"INSERT INTO server_parts (server_id, {', '.join(part_cols)}) "
                                      f"VALUES (:server_id, {', '.join(':' + col for col in part_cols)})"),
                                 {"server_id": server_id, **p})
                conn.execute(text("INSERT INTO service_servers (service_id, server_id, whole, amounts, created_at) "
                                  "VALUES (:svc, :srv, 1, '{}', :now)"), {"svc": c["service_id"], "srv": server_id,
                                                                          "now": now})
                for pool_id in pools:
                    conn.execute(text("INSERT INTO pool_servers (pool_id, server_id) VALUES (:p, :s)"),
                                 {"p": pool_id, "s": server_id})
        for table in ("pool_components", "component_parts", "components"):
            conn.execute(text(f"DROP TABLE {table}"))
    if components:
        log.info("Компоненты сервисов перенесены в серверы: %d", len(components))
