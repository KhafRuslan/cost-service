from datetime import datetime

from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# Роли пользователей
ROLE_ADMIN = "admin"    # всё + пользователи и источники Prometheus
ROLE_EDITOR = "editor"  # создаёт и меняет сервисы, компоненты, подпространства, ресурсы
ROLE_VIEWER = "viewer"  # только просмотр
ROLES = {ROLE_ADMIN: "администратор", ROLE_EDITOR: "редактор", ROLE_VIEWER: "наблюдатель"}

# Категории комплектующих: название и единица ёмкости (ТТХ)
CATEGORIES = {
    "cpu": ("Процессор", "ядер"),
    "ram": ("Оперативная память", "ГБ"),
    "disk": ("Диск", "ГБ"),
    "gpu": ("GPU", "ГБ"),
    "network": ("Сеть", "Гбит/с"),
    "license": ("Лицензия", ""),
    "other": ("Прочее", ""),
}
DISK_TYPES = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVMe"}

# Ресурсы сервера, которые выделяются сервисам: ключ → (название, единица).
# Комплектующие без ёмкости (лицензии, прочее) не выделяются, а делятся пропорционально выделенному.
RESOURCES = {
    "cpu": ("Процессор", "ядер"),
    "ram": ("Память", "ГБ"),
    "disk_nvme": ("Диск NVMe", "ГБ"),
    "disk_ssd": ("Диск SSD", "ГБ"),
    "disk_hdd": ("Диск HDD", "ГБ"),
    "gpu": ("GPU", "ГБ"),
    "network": ("Сеть", "Гбит/с"),
}

# Как задана цена комплектующего
PRICE_MONTHLY = "monthly"  # стоимость в месяц (аренда, облако)
PRICE_CAPEX = "capex"      # цена покупки, делится на срок амортизации
PRICE_MODES = {PRICE_MONTHLY: "в месяц", PRICE_CAPEX: "покупка"}

# Период расхода на обслуживание сервера
EXPENSE_MONTHLY = "monthly"        # сумма в месяц
EXPENSE_YEARLY = "yearly"          # сумма в год
EXPENSE_CAPEX_PERCENT = "percent"  # % в год от цены покупки комплектующих (вендорская поддержка и т. п.)
EXPENSE_PERIODS = {EXPENSE_MONTHLY: "в месяц", EXPENSE_YEARLY: "в год",
                   EXPENSE_CAPEX_PERCENT: "% от покупки в год"}

AUTH_NONE, AUTH_BASIC, AUTH_BEARER = "none", "basic", "bearer"
AUTH_TYPES = {AUTH_NONE: "без авторизации", AUTH_BASIC: "логин и пароль", AUTH_BEARER: "токен (Bearer)"}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(10), default=ROLE_VIEWER)
    currency: Mapped[str] = mapped_column(String(3), default="USD")  # валюта, в которой пользователь видит суммы
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)

    @property
    def title(self) -> str:
        return self.display_name or self.username

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def can_edit(self) -> bool:
        return self.role in (ROLE_ADMIN, ROLE_EDITOR)


class AppSetting(Base):
    """Настройки сервиса, задаваемые в интерфейсе (например, курсы валют)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class PrometheusSource(Base):
    """Сервер Prometheus (или совместимый: VictoriaMetrics, Thanos), из которого берутся метрики ресурсов."""

    __tablename__ = "prometheus_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    url: Mapped[str] = mapped_column(String(2000))
    auth_type: Mapped[str] = mapped_column(String(10), default=AUTH_NONE)
    username: Mapped[str | None] = mapped_column(String(200), default=None)
    secret: Mapped[str | None] = mapped_column(Text, default=None)  # пароль или токен
    verify_tls: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Service(Base):
    """Приложение (например, Elasticsearch): выделенные серверы, дисковые подпространства и ресурсы."""

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    servers: Mapped[list["ServiceServer"]] = relationship(
        back_populates="service", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin")
    pools: Mapped[list["StoragePool"]] = relationship(
        back_populates="service", cascade="all, delete-orphan", passive_deletes=True, order_by="StoragePool.name")
    resources: Mapped[list["Resource"]] = relationship(
        back_populates="service", cascade="all, delete-orphan", passive_deletes=True, order_by="Resource.name")

    @property
    def sorted_servers(self) -> list["ServiceServer"]:
        return sorted(self.servers, key=lambda a: a.server.name)


class Server(Base):
    """Сервер (или ВМ, СХД ...) из комплектующих. Описывается отдельно, его ресурсы выделяются сервисам."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(100), default="Сервер")
    host: Mapped[str] = mapped_column(String(300), default="")  # имя хоста или IP
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    parts: Mapped[list["ServerPart"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ServerPart.position", lazy="selectin")
    services: Mapped[list["ServiceServer"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin")
    expenses: Mapped[list["ServerExpense"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ServerExpense.position", lazy="selectin")


class ServerPart(Base):
    """Комплектующее сервера: процессор, память, диск ... с ТТХ и ценой."""

    __tablename__ = "server_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200), default="")  # модель
    spec: Mapped[str] = mapped_column(String(500), default="")  # прочие ТТХ: частота, скорость и т.п.
    capacity: Mapped[float | None] = mapped_column(Float, default=None)  # ёмкость одной штуки (ядер, ГБ ...)
    disk_type: Mapped[str | None] = mapped_column(String(10), default=None)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    price_mode: Mapped[str] = mapped_column(String(10), default=PRICE_MONTHLY)
    price: Mapped[float] = mapped_column(Float, default=0)  # за одну штуку
    amortization_months: Mapped[int | None] = mapped_column(Integer, default=None)

    server: Mapped[Server] = relationship(back_populates="parts")


class ServerExpense(Base):
    """Расход на обслуживание сервера: электроэнергия, колокация, техподдержка, администрирование ..."""

    __tablename__ = "server_expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[float] = mapped_column(Float, default=0)  # сумма или процент (для EXPENSE_CAPEX_PERCENT)
    period: Mapped[str] = mapped_column(String(10), default=EXPENSE_MONTHLY)

    server: Mapped["Server"] = relationship(back_populates="expenses")


class ServiceServer(Base):
    """Сервер в сервисе: весь сервер или конкретные ресурсы (ядра, ГБ памяти, ГБ дисков ...), выделенные сервису."""

    __tablename__ = "service_servers"
    __table_args__ = (UniqueConstraint("service_id", "server_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    # Весь сервер, включая будущие изменения комплектующих
    whole: Mapped[bool] = mapped_column(default=False)
    # Выделенные ресурсы: ключ из RESOURCES → количество (ядер, ГБ ...). При whole не используется
    amounts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    service: Mapped[Service] = relationship(back_populates="servers")
    server: Mapped[Server] = relationship(back_populates="services", lazy="joined")


pool_servers = Table(
    "pool_servers", Base.metadata,
    Column("pool_id", ForeignKey("storage_pools.id", ondelete="CASCADE"), primary_key=True),
    Column("server_id", ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True),
)


class StoragePool(Base):
    """Дисковое подпространство: диски одного типа на серверах сервиса и цена гигабайта в месяц."""

    __tablename__ = "storage_pools"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    disk_type: Mapped[str] = mapped_column(String(10))
    # Доля сырого объёма, доступная под данные (RAID, резерв, файловая система), %
    usable_percent: Mapped[float] = mapped_column(Float, default=100)
    # Цена за ГБ в месяц, заданная вручную (None — считается из дисков)
    manual_price_per_gb: Mapped[float | None] = mapped_column(Float, default=None)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    service: Mapped[Service] = relationship(back_populates="pools")
    # Серверы, диски которых входят в подпространство (пусто — все серверы сервиса)
    servers: Mapped[list[Server]] = relationship(secondary=pool_servers, lazy="selectin", order_by=Server.name)


class Resource(Base):
    """Ресурс сервиса (datastream, индекс ...): размер из Prometheus (или вручную) × цена ГБ подпространства."""

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(100), default="datastream")
    description: Mapped[str] = mapped_column(Text, default="")
    pool_id: Mapped[int | None] = mapped_column(ForeignKey("storage_pools.id", ondelete="SET NULL"), default=None)

    # Размер: PromQL-запрос, возвращающий байты, либо значение вручную (ГБ)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("prometheus_sources.id", ondelete="SET NULL"),
                                                  default=None)
    promql: Mapped[str] = mapped_column(Text, default="")
    manual_gb: Mapped[float | None] = mapped_column(Float, default=None)
    # Множитель размера: например, число копий, если метрика показывает только primary-шарды
    size_factor: Mapped[float] = mapped_column(Float, default=1)

    last_bytes: Mapped[float | None] = mapped_column(Float, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    service: Mapped[Service] = relationship(back_populates="resources")
    pool: Mapped[StoragePool | None] = relationship(lazy="joined")
    source: Mapped[PrometheusSource | None] = relationship(lazy="joined")

    @property
    def uses_metric(self) -> bool:
        return bool(self.promql.strip())


class ResourceSample(Base):
    """История размеров ресурса из Prometheus."""

    __tablename__ = "resource_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    bytes: Mapped[float] = mapped_column(Float)
