"""Фоновые задачи: обновление размеров ресурсов из Prometheus и очистка старой истории."""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import prom, security
from .config import settings

scheduler = AsyncIOScheduler(timezone=settings.tz)


def start() -> None:
    if settings.metrics_refresh_minutes > 0:
        scheduler.add_job(prom.refresh_all, "interval", minutes=settings.metrics_refresh_minutes, id="refresh",
                          next_run_time=datetime.now(settings.tz) + timedelta(seconds=10),
                          max_instances=1, coalesce=True)
    scheduler.add_job(prom.cleanup_samples, "interval", hours=6, id="cleanup_samples")
    scheduler.add_job(security.cleanup_sessions, "interval", hours=6, id="cleanup_sessions")
    scheduler.start()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
