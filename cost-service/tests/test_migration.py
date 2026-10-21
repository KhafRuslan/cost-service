"""Перенос компонентов сервисов (старая схема) в серверы."""

import sqlite3

import pytest

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app import db
from app.costs import allocation_cost
from app.models import ServiceServer

OLD_SCHEMA = """
CREATE TABLE services (id INTEGER PRIMARY KEY, name VARCHAR(200) UNIQUE, description TEXT DEFAULT '',
                       created_at DATETIME, updated_at DATETIME);
CREATE TABLE components (id INTEGER PRIMARY KEY, service_id INTEGER, name VARCHAR(200), kind VARCHAR(100),
                         quantity INTEGER, description TEXT, created_at DATETIME, updated_at DATETIME);
CREATE TABLE component_parts (id INTEGER PRIMARY KEY, component_id INTEGER, position INTEGER, category VARCHAR(20),
                              name VARCHAR(200), spec VARCHAR(500), capacity FLOAT, disk_type VARCHAR(10),
                              quantity FLOAT, price_mode VARCHAR(10), price FLOAT, amortization_months INTEGER);
CREATE TABLE storage_pools (id INTEGER PRIMARY KEY, service_id INTEGER, name VARCHAR(200), disk_type VARCHAR(10),
                            usable_percent FLOAT, manual_price_per_gb FLOAT, description TEXT, created_at DATETIME);
CREATE TABLE pool_components (pool_id INTEGER, component_id INTEGER, PRIMARY KEY (pool_id, component_id));
INSERT INTO services VALUES (1, 'ES', '', '2026-01-01', '2026-01-01');
INSERT INTO components VALUES (1, 1, 'hot', 'Сервер', 3, 'горячие', '2026-01-01', '2026-01-01');
INSERT INTO components VALUES (2, 1, 'master', 'ВМ', 1, '', '2026-01-01', '2026-01-01');
INSERT INTO component_parts VALUES (1, 1, 1, 'disk', 'PM9A3', '', 3840, 'nvme', 4, 'capex', 120000, 60);
INSERT INTO component_parts VALUES (2, 1, 2, 'ram', '', '', 64, NULL, 8, 'monthly', 1000, NULL);
INSERT INTO component_parts VALUES (3, 2, 1, 'cpu', '', '', 4, NULL, 1, 'monthly', 1800, NULL);
INSERT INTO storage_pools VALUES (1, 1, 'hot', 'nvme', 50, NULL, '', '2026-01-01');
INSERT INTO pool_components VALUES (1, 1);
"""


def test_components_become_servers(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(db, "engine", engine)
    db.Base.metadata.create_all(engine)
    db._migrate_components()

    tables = inspect(engine).get_table_names()
    assert "components" not in tables and "component_parts" not in tables and "pool_components" not in tables
    with sqlite3.connect(path) as conn:
        servers = conn.execute("SELECT id, name, kind, description FROM servers ORDER BY name").fetchall()
        assert [s[1] for s in servers] == ["hot-01", "hot-02", "hot-03", "master"]
        assert servers[0][2:] == ("Сервер", "горячие")
        hot_ids = [s[0] for s in servers[:3]]
        assert conn.execute("SELECT COUNT(*) FROM server_parts WHERE server_id = ?", (hot_ids[1],)).fetchone()[0] == 2
        links = conn.execute("SELECT service_id, whole, amounts FROM service_servers").fetchall()
        assert links == [(1, 1, "{}")] * 4
        pool_servers = [r[0] for r in conn.execute("SELECT server_id FROM pool_servers WHERE pool_id = 1")]
        assert sorted(pool_servers) == sorted(hot_ids)
    # Повторный запуск ничего не делает
    db._migrate_components()


SHARES_SCHEMA = """
CREATE TABLE services (id INTEGER PRIMARY KEY, name VARCHAR(200) UNIQUE, description TEXT DEFAULT '',
                       created_at DATETIME, updated_at DATETIME);
CREATE TABLE servers (id INTEGER PRIMARY KEY, name VARCHAR(200) UNIQUE, kind VARCHAR(100), host VARCHAR(300),
                      description TEXT, created_at DATETIME, updated_at DATETIME);
CREATE TABLE server_parts (id INTEGER PRIMARY KEY, server_id INTEGER, position INTEGER, category VARCHAR(20),
                           name VARCHAR(200), spec VARCHAR(500), capacity FLOAT, disk_type VARCHAR(10),
                           quantity FLOAT, price_mode VARCHAR(10), price FLOAT, amortization_months INTEGER);
CREATE TABLE service_servers (id INTEGER PRIMARY KEY, service_id INTEGER, server_id INTEGER,
                              share_percent FLOAT NOT NULL, created_at DATETIME, UNIQUE (service_id, server_id));
INSERT INTO services VALUES (1, 'A', '', '2026-01-01', '2026-01-01');
INSERT INTO services VALUES (2, 'B', '', '2026-01-01', '2026-01-01');
INSERT INTO servers VALUES (1, 'shared', 'Сервер', '', '', '2026-01-01', '2026-01-01');
INSERT INTO servers VALUES (2, 'dedicated', 'Сервер', '', '', '2026-01-01', '2026-01-01');
INSERT INTO server_parts VALUES (1, 1, 1, 'cpu', '', '', 16, NULL, 2, 'monthly', 100, NULL);
INSERT INTO server_parts VALUES (2, 1, 2, 'disk', '', '', 1000, 'ssd', 4, 'monthly', 50, NULL);
INSERT INTO server_parts VALUES (3, 1, 3, 'other', 'стойка', '', NULL, NULL, 1, 'monthly', 300, NULL);
INSERT INTO service_servers VALUES (1, 1, 1, 70, '2026-01-01');
INSERT INTO service_servers VALUES (2, 2, 1, 30, '2026-01-01');
INSERT INTO service_servers VALUES (3, 1, 2, 100, '2026-01-01');
"""


def test_shares_become_resources(tmp_path, monkeypatch):
    """Доля сервера → та же доля каждого его ресурса; стоимость для сервиса не меняется."""
    path = tmp_path / "shares.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SHARES_SCHEMA)
    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(db, "engine", engine)
    db.Base.metadata.create_all(engine)
    db._migrate_shares()

    columns = {c["name"] for c in inspect(engine).get_columns("service_servers")}
    assert "share_percent" not in columns and {"whole", "amounts"} <= columns
    with Session(engine) as s:
        links = {link.id: link for link in s.query(ServiceServer)}
        assert links[1].amounts == {"cpu": 22.4, "disk_ssd": 2800} and not links[1].whole
        assert links[2].amounts == {"cpu": 9.6, "disk_ssd": 1200}
        assert links[3].whole and links[3].amounts == {}
        assert allocation_cost(links[1]) == pytest.approx(700 * 0.7)  # сервер стоит 200 + 200 + 300
        assert allocation_cost(links[2]) == pytest.approx(700 * 0.3)
    db._migrate_shares()  # повторно — ничего не делает
