from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Сервис ---
    data_dir: Path = Path("./data")
    database_url: str | None = None  # по умолчанию sqlite в data_dir
    timezone: str = "UTC"
    # Валюта: только подпись к суммам, пересчёта курсов нет

    # Первый администратор создаётся из этих переменных при старте, если пользователей ещё нет.
    # Если не заданы — сервис предложит создать администратора в веб-интерфейсе.
    admin_username: str | None = None
    admin_password: str | None = None
    session_ttl_hours: int = 12

    # --- Prometheus ---
    # Источники задаются в веб-интерфейсе (Управление → Prometheus) и хранятся в БД.
    # Если задан PROMETHEUS_URL и источников ещё нет, при старте создаётся источник «Основной».
    prometheus_url: str | None = None
    prometheus_timeout: float = 30.0
    # Как часто обновлять размеры ресурсов (0 — только вручную)
    metrics_refresh_minutes: int = 15
    # Сколько хранить историю размеров
    metrics_history_days: int = 180

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'cost.db').resolve()}"


settings = Settings()
