from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    "kyc_network_scout",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["workers.tasks"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Kyiv",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    worker_prefetch_multiplier=1,
)

app.conf.beat_schedule = {
    "demo-heartbeat": {
        "task": "workers.tasks.heartbeat",
        "schedule": crontab(minute="*/10"),
    },
}
