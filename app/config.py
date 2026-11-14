"""Validated service configuration. Engine options use MinerU's own env vars."""

import math
import os
from dataclasses import dataclass

from app.domain.parsing import TIERS, Tier


@dataclass(frozen=True)
class Settings:
    service_mode: str = "inline"
    tier: Tier = "standard"
    preload_models: bool = True
    parse_workers: int = 2
    priority_workers: int = 0
    max_upload_bytes: int = 50 * 1024 * 1024
    max_pages: int = 1000
    max_pending: int = 10000
    request_timeout: float = 540
    api_key: str = ""
    cache_bytes: int = 128 * 1024 * 1024
    cache_ttl: float = 3600

    def __post_init__(self):
        if self.service_mode not in {"inline", "queue"}:
            raise ValueError("SERVICE_MODE must be inline or queue")
        if self.tier not in TIERS:
            raise ValueError("PARSE_TIER must be flash, basic, standard or advanced")
        for name in (
            "max_upload_bytes",
            "max_pages",
            "max_pending",
            "parse_workers",
            "request_timeout",
            "cache_ttl",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cache_bytes < 0:
            raise ValueError("cache_bytes must be nonnegative")
        if not 0 <= self.priority_workers <= self.parse_workers:
            raise ValueError("PRIORITY_WORKERS must be between 0 and PARSE_WORKERS")

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(
            service_mode=os.getenv("SERVICE_MODE", defaults.service_mode),
            tier=os.getenv("PARSE_TIER", defaults.tier),
            preload_models=os.getenv("PRELOAD_MODELS", "true").lower() in {"1", "true", "yes"},
            parse_workers=int(os.getenv("PARSE_WORKERS", defaults.parse_workers)),
            priority_workers=int(os.getenv("PRIORITY_WORKERS", defaults.priority_workers)),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", defaults.max_upload_bytes)),
            max_pages=int(os.getenv("MAX_PAGES", defaults.max_pages)),
            max_pending=int(os.getenv("MAX_PENDING", defaults.max_pending)),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", defaults.request_timeout)),
            api_key=os.getenv("PDF_API_KEY", ""),
            cache_bytes=int(os.getenv("CACHE_BYTES", defaults.cache_bytes)),
            cache_ttl=float(os.getenv("CACHE_TTL", defaults.cache_ttl)),
        )


@dataclass(frozen=True)
class QueueSettings:
    database: str = "data/queue.sqlite3"
    object_dir: str = "data/objects"
    storage: str = "local"
    s3_bucket: str = ""
    s3_endpoint: str | None = None
    worker_token: str = ""
    worker_api_url: str = "http://127.0.0.1:3888"
    max_uploads: int = 64
    max_sync_waiters: int = 64
    max_transfers: int = 64
    transfer_seconds: float = 120
    upload_seconds: float = 120
    lease_seconds: float = 60
    heartbeat_seconds: float = 10
    poll_seconds: float = 0.25
    max_attempts: int = 3
    max_queue_seconds: float = 0
    retention_seconds: float = 86400
    max_result_bytes: int = 64 * 1024 * 1024
    model_revision: str = "MinerU2.5-Pro-2605-1.2B"

    def __post_init__(self):
        for name in (
            "max_uploads",
            "max_sync_waiters",
            "max_transfers",
            "upload_seconds",
            "transfer_seconds",
            "lease_seconds",
            "heartbeat_seconds",
            "poll_seconds",
            "max_attempts",
            "retention_seconds",
            "max_result_bytes",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.heartbeat_seconds >= self.lease_seconds / 2:
            raise ValueError("Heartbeat interval must be less than half of the lease")
        if not math.isfinite(self.max_queue_seconds) or self.max_queue_seconds < 0:
            raise ValueError("max_queue_seconds must be nonnegative (0 disables queue expiry)")
        if self.storage not in {"local", "s3"}:
            raise ValueError("OBJECT_STORAGE must be local or s3")
        if self.storage == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required for S3 storage")

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        names = {
            "database": "QUEUE_DATABASE",
            "object_dir": "OBJECT_DIR",
            "storage": "OBJECT_STORAGE",
            "s3_bucket": "S3_BUCKET",
            "s3_endpoint": "S3_ENDPOINT_URL",
            "worker_token": "WORKER_API_TOKEN",
            "worker_api_url": "WORKER_API_URL",
            "model_revision": "MODEL_REVISION",
        }
        for name, env in names.items():
            values[name] = os.getenv(env, getattr(defaults, name))
        for name in (
            "max_uploads",
            "max_sync_waiters",
            "max_transfers",
            "max_attempts",
            "max_result_bytes",
        ):
            values[name] = int(os.getenv("QUEUE_" + name.upper(), getattr(defaults, name)))
        for name in (
            "upload_seconds",
            "transfer_seconds",
            "lease_seconds",
            "heartbeat_seconds",
            "poll_seconds",
            "max_queue_seconds",
            "retention_seconds",
        ):
            values[name] = float(os.getenv("QUEUE_" + name.upper(), getattr(defaults, name)))
        values["s3_endpoint"] = values["s3_endpoint"] or None
        return cls(**values)
