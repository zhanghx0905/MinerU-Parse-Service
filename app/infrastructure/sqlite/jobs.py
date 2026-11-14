"""SQLite WAL coordinator. Transactions contain metadata only, never model execution."""

import json
import math
import time
from uuid import uuid4

from app.domain.jobs import (
    IdempotencyConflict,
    LeaseLost,
    ServiceBusy,
)
from app.domain.parsing import ParseOptions
from app.infrastructure.sqlite.database import QueueDatabase
from app.infrastructure.sqlite.observability import record_completion

BUCKETS = (0.01, 0.1, 0.5, 1, 5, 20, 60, 300, math.inf)


class QueueStore(QueueDatabase):
    def __init__(self, settings, queue_settings, clock=time.time):
        self.settings = settings
        self.config = queue_settings
        self.clock = clock
        super().__init__(queue_settings.database)

    @staticmethod
    def _garbage(connection, key):
        if key:
            connection.execute("INSERT OR IGNORE INTO garbage VALUES (?)", (key,))

    @staticmethod
    def _counter(connection, event):
        connection.execute(
            "INSERT INTO counters VALUES (?,1) ON CONFLICT(event) DO UPDATE SET value=value+1",
            (event,),
        )

    def _observe(self, connection, tier, timings):
        for stage, value in timings.items():
            if not math.isfinite(value) or value < 0:
                continue
            old = connection.execute(
                "SELECT * FROM metrics WHERE stage=? AND tier=?", (stage, tier)
            ).fetchone()
            counts = json.loads(old["buckets"]) if old else [0] * len(BUCKETS)
            counts = [
                count + int(value <= bound) for count, bound in zip(counts, BUCKETS, strict=True)
            ]
            connection.execute(
                "INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?)",
                (
                    stage,
                    tier,
                    (old["count"] if old else 0) + 1,
                    (old["total"] if old else 0) + value,
                    json.dumps(counts),
                ),
            )

    def observe(self, tier, timings):
        with self.transaction() as connection:
            self._observe(connection, tier, timings)

    def _terminal(self, connection, job, error, now):
        # Failed inputs stay available for diagnosis/recovery until retention
        # expires. A transient infrastructure failure must not erase the source.
        if job["lease_token"]:
            self._garbage(connection, f"results/{job['lease_token']}.md")
        connection.execute(
            "UPDATE jobs SET state='failed',finished=?,error=?,dedup_key=NULL,"
            "lease_token=NULL,lease_until=NULL WHERE id=?",
            (now, error, job["id"]),
        )
        self._counter(connection, "failed")

    def _sweep(self, connection, now):
        for upload in connection.execute("SELECT id FROM uploads WHERE expires<=?", (now,)):
            key = f"inputs/{upload['id']}.pdf"
            if not connection.execute("SELECT 1 FROM jobs WHERE input_key=?", (key,)).fetchone():
                self._garbage(connection, key)
        connection.execute("DELETE FROM uploads WHERE expires<=?", (now,))
        connection.execute("DELETE FROM waiters WHERE expires<=?", (now,))
        connection.execute("DELETE FROM transfers WHERE expires<=?", (now,))
        for job in connection.execute(
            "SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (now,)
        ).fetchall():
            self._retry(connection, job, "lease_expired", now)
        if self.config.max_queue_seconds:
            for job in connection.execute(
                "SELECT * FROM jobs WHERE state='queued' AND queued<?",
                (now - self.config.max_queue_seconds,),
            ).fetchall():
                self._terminal(connection, job, "queue_timeout", now)
        connection.execute(
            "UPDATE jobs SET dedup_key=NULL WHERE state='succeeded' "
            "AND dedup_key IS NOT NULL AND cache_until<=?",
            (now,),
        )
        for job in connection.execute(
            "SELECT * FROM jobs WHERE state IN ('succeeded','failed') AND finished<?",
            (now - self.config.retention_seconds,),
        ).fetchall():
            self._garbage(connection, job["input_key"])
            self._garbage(connection, job["output_key"])
            connection.execute("DELETE FROM idempotency WHERE job_id=?", (job["id"],))
            connection.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        connection.execute(
            "DELETE FROM workers WHERE seen<?", (now - self.config.lease_seconds * 2,)
        )

    def maintenance(self):
        with self.transaction() as connection:
            self._sweep(connection, self.clock())

    def reserve_upload(self, wait_for_result=False):
        now = self.clock()
        with self.transaction() as connection:
            self._sweep(connection, now)
            count = connection.execute("SELECT count(*) FROM uploads").fetchone()[0]
            if count >= self.config.max_uploads:
                raise ServiceBusy("Upload capacity is full; retry later")
            if wait_for_result:
                waiting = connection.execute("SELECT count(*) FROM waiters").fetchone()[0]
                if waiting >= self.config.max_sync_waiters:
                    raise ServiceBusy("Synchronous wait capacity is full")
            token = uuid4().hex
            if wait_for_result:
                connection.execute(
                    "INSERT INTO waiters VALUES (?,?)",
                    (token, now + self.config.upload_seconds + self.settings.request_timeout + 15),
                )
            connection.execute(
                "INSERT INTO uploads VALUES (?,?)", (token, now + self.config.upload_seconds + 10)
            )
            return token

    def reserve_transfer(self):
        with self.transaction() as connection:
            now = self.clock()
            connection.execute("DELETE FROM transfers WHERE expires<=?", (now,))
            if (
                connection.execute("SELECT count(*) FROM transfers").fetchone()[0]
                >= self.config.max_transfers
            ):
                raise ServiceBusy("Transfer capacity is full")
            token = uuid4().hex
            connection.execute(
                "INSERT INTO transfers VALUES (?,?)",
                (token, now + self.config.transfer_seconds + 10),
            )
            return token

    def release_transfer(self, token):
        with self.transaction() as connection:
            connection.execute("DELETE FROM transfers WHERE id=?", (token,))

    def renew_upload(self, token):
        with self.transaction() as connection:
            now = self.clock()
            return (
                connection.execute(
                    "UPDATE uploads SET expires=? WHERE id=? AND expires>?",
                    (now + self.config.upload_seconds + 10, token, now),
                ).rowcount
                == 1
            )

    def release_waiter(self, token):
        with self.transaction() as connection:
            connection.execute("DELETE FROM waiters WHERE id=?", (token,))

    def release_upload(self, token):
        with self.transaction() as connection:
            key = f"inputs/{token}.pdf"
            if not connection.execute("SELECT 1 FROM jobs WHERE input_key=?", (key,)).fetchone():
                self._garbage(connection, key)
            connection.execute("DELETE FROM uploads WHERE id=?", (token,))

    def submit(
        self,
        *,
        fingerprint,
        input_key,
        pages,
        tier,
        encode_img,
        disable_cache=False,
        idempotency_key=None,
        upload_token=None,
        options=None,
        priority=0,
    ):
        now = self.clock()
        options = options or ParseOptions()
        if priority not in {0, 1}:
            raise ValueError("priority must be 0 or 1")
        with self.transaction() as connection:
            self._sweep(connection, now)
            if (
                upload_token
                and not connection.execute(
                    "SELECT 1 FROM uploads WHERE id=? AND expires>?", (upload_token, now)
                ).fetchone()
            ):
                raise ServiceBusy("Upload reservation expired")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT jobs.* FROM idempotency JOIN jobs ON jobs.id=idempotency.job_id "
                    "WHERE idempotency.key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise IdempotencyConflict("Idempotency key was used for different input")
                    self._counter(connection, "idempotency_hit")
                    return self._promote(connection, existing, priority), True
            if not disable_cache:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE dedup_key=?", (fingerprint,)
                ).fetchone()
                if existing:
                    if idempotency_key:
                        connection.execute(
                            "INSERT INTO idempotency VALUES (?,?,?)",
                            (idempotency_key, fingerprint, existing["id"]),
                        )
                    self._counter(
                        connection, "cache_hit" if existing["state"] == "succeeded" else "coalesced"
                    )
                    return self._promote(connection, existing, priority), True
            count = connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if count >= self.settings.max_pending:
                raise ServiceBusy("Parse queue is full; retry later")
            job_id = uuid4().hex
            # Explicit independent keys may intentionally represent distinct jobs.
            dedup = fingerprint if not disable_cache else None
            connection.execute(
                "INSERT INTO jobs (id,fingerprint,dedup_key,idempotency_key,input_key,"
                "tier,encode_img,pages,priority,state,created,queued,available,parse_options) "
                "VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
                (
                    job_id,
                    fingerprint,
                    dedup,
                    None,
                    input_key,
                    tier,
                    int(encode_img),
                    pages,
                    priority,
                    now,
                    now,
                    now,
                    options.to_json(),
                ),
            )
            if idempotency_key:
                connection.execute(
                    "INSERT INTO idempotency VALUES (?,?,?)", (idempotency_key, fingerprint, job_id)
                )
            self._counter(connection, "submitted")
            return dict(
                connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            ), False

    @staticmethod
    def _promote(connection, job, priority):
        job = dict(job)
        if priority > job["priority"] and job["state"] in {"queued", "running"}:
            connection.execute("UPDATE jobs SET priority=? WHERE id=?", (priority, job["id"]))
            job["priority"] = priority
        return job

    def claim(self, worker_id, min_priority=0):
        if min_priority not in {0, 1}:
            raise ValueError("min_priority must be 0 or 1")
        now = self.clock()
        # Idle polling is read-only except for periodic liveness refreshes.
        with self.connect() as reader:
            seen = reader.execute("SELECT seen FROM workers WHERE id=?", (worker_id,)).fetchone()
            pending = reader.execute(
                "SELECT 1 FROM jobs WHERE state='queued' AND available<=? AND priority>=? LIMIT 1",
                (now, min_priority),
            ).fetchone()
            if seen and seen[0] > now - min(5, self.config.lease_seconds / 3) and not pending:
                return None
        with self.transaction() as connection:
            self._sweep(connection, now)
            connection.execute("INSERT OR REPLACE INTO workers VALUES (?,?)", (worker_id, now))
            job = connection.execute(
                "SELECT * FROM jobs WHERE state='queued' AND available<=? "
                "AND priority>=? ORDER BY priority DESC,queued,id "
                "LIMIT 1",
                (now, min_priority),
            ).fetchone()
            if not job:
                return None
            token = uuid4().hex
            connection.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?,"
                "lease_until=?,started=?,worker_id=? WHERE id=?",
                (token, now + self.config.lease_seconds, now, worker_id, job["id"]),
            )
            self._observe(connection, job["tier"], {"queue.wait": now - job["queued"]})
            return dict(
                connection.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            )

    def check_lease(self, job_id, token, connection=None):
        if connection is None:
            with self.connect() as owned:
                return self.check_lease(job_id, token, owned)
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["state"] == "succeeded" and row["completed_token"] == token:
            return dict(row)
        if (
            not row
            or row["state"] != "running"
            or row["lease_token"] != token
            or row["lease_until"] <= self.clock()
        ):
            raise LeaseLost("Task lease is no longer valid")
        return dict(row)

    def heartbeat(self, job_id, token):
        with self.transaction() as connection:
            job = self.check_lease(job_id, token, connection)
            if job["state"] == "running":
                connection.execute(
                    "UPDATE workers SET seen=? WHERE id=?", (self.clock(), job["worker_id"])
                )
                connection.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?",
                    (self.clock() + self.config.lease_seconds, job_id),
                )
            return job

    def complete(self, job_id, token, pages, timings, result_bytes=0):
        key = f"results/{token}.md"
        with self.transaction() as connection:
            try:
                job = self.check_lease(job_id, token, connection)
            except LeaseLost:
                self._garbage(connection, key)
                return False
            if job["state"] == "succeeded":
                return True
            if pages != job["pages"]:
                self._garbage(connection, key)
                self._retry(connection, job, "page_mismatch", self.clock())
                return False
            now = self.clock()
            connection.execute(
                "UPDATE jobs SET state='succeeded',output_key=?,finished=?,cache_until=?,"
                "completed_token=?,lease_token=NULL,lease_until=NULL,error=NULL,timings=?,"
                "result_bytes=? WHERE id=?",
                (
                    key,
                    now,
                    now + self.settings.cache_ttl,
                    token,
                    json.dumps(timings),
                    result_bytes,
                    job_id,
                ),
            )
            cached = connection.execute(
                "SELECT id,result_bytes FROM jobs WHERE dedup_key IS NOT NULL "
                "AND state='succeeded' ORDER BY finished DESC"
            ).fetchall()
            cache_size = 0
            for row in cached:
                if (
                    not self.settings.cache_bytes
                    or cache_size + row["result_bytes"] > self.settings.cache_bytes
                ):
                    connection.execute("UPDATE jobs SET dedup_key=NULL WHERE id=?", (row["id"],))
                else:
                    cache_size += row["result_bytes"]
            self._observe(connection, job["tier"], timings)
            self._observe(connection, job["tier"], {"job.total": now - job["created"]})
            self._counter(connection, "succeeded")
            record_completion(connection, job["tier"], pages, timings)
            self._garbage(connection, job["input_key"])
            return True

    def _retry(self, connection, job, error, now):
        if job["lease_token"]:
            self._garbage(connection, f"results/{job['lease_token']}.md")
        if job["attempts"] >= self.config.max_attempts:
            self._terminal(connection, job, error, now)
        else:
            connection.execute(
                "UPDATE jobs SET state='queued',lease_token=NULL,lease_until=NULL,"
                "queued=?,available=?,error=? WHERE id=?",
                (now, now + min(2 ** job["attempts"], 30), error, job["id"]),
            )
            self._counter(connection, "retried")

    def fail(self, job_id, token):
        with self.transaction() as connection:
            job = self.check_lease(job_id, token, connection)
            if job["state"] == "running":
                self._retry(connection, job, "parse_error", self.clock())

    def get(self, job_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def stats(self):
        now = self.clock()
        with self.connect() as connection:
            states = {
                row[0]: row[1]
                for row in connection.execute("SELECT state,count(*) FROM jobs GROUP BY state")
            }
            pages = connection.execute(
                "SELECT coalesce(sum(pages),0) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            oldest = connection.execute(
                "SELECT min(queued) FROM jobs WHERE state='queued'"
            ).fetchone()[0]
            workers = connection.execute(
                "SELECT count(*) FROM workers WHERE seen>?", (now - self.config.lease_seconds * 2,)
            ).fetchone()[0]
            return {
                **{
                    state: states.get(state, 0)
                    for state in ("queued", "running", "succeeded", "failed")
                },
                "pending_pages": pages,
                "oldest_wait_seconds": max(0, now - oldest) if oldest else 0,
                "workers": workers,
            }

    def metric_rows(self):
        with self.connect() as connection:
            return (
                [dict(row) for row in connection.execute("SELECT * FROM metrics")],
                [dict(row) for row in connection.execute("SELECT * FROM counters")],
            )

    def garbage(self):
        with self.connect() as connection:
            return [row[0] for row in connection.execute("SELECT key FROM garbage LIMIT 100")]

    def forget_garbage(self, key):
        with self.transaction() as connection:
            connection.execute("DELETE FROM garbage WHERE key=?", (key,))
