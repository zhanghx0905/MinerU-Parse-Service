"""Queue read model and completion totals; updates share the task's commit transaction."""

import json

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily


def processing_rows(store):
    with store.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM processing_totals")]


def read_observability(store):
    """Separate execution speed from recent wall-clock throughput across all workers."""
    states = store.stats()
    rows = processing_rows(store)
    totals = {
        key: sum(row[key] for row in rows)
        for key in (
            "completed_jobs",
            "completed_pages",
            "measured_jobs",
            "measured_pages",
            "parse_seconds",
        )
    }
    # Recent throughput uses retained completion timestamps. Keep the denominator
    # within retention so shortening retention cannot silently truncate this window.
    window = min(300, store.config.retention_seconds)
    with store.connect() as connection:
        recent_pages = connection.execute(
            "SELECT coalesce(sum(pages),0) FROM jobs WHERE state='succeeded' AND finished>=?",
            (store.clock() - window,),
        ).fetchone()[0]
    pages, seconds = totals["measured_pages"], totals["parse_seconds"]
    return {
        "queue": {
            "queued": states["queued"],
            "running": states["running"],
            "pending": states["queued"] + states["running"],
            "max_pending": store.settings.max_pending,
            "pending_pages": states["pending_pages"],
            "oldest_wait_seconds": states["oldest_wait_seconds"],
        },
        "parsing": {
            **totals,
            "average_seconds_per_page": seconds / pages if pages else None,
            "average_pages_per_second": pages / seconds if seconds else None,
            "recent_window_seconds": window,
            "recent_completed_pages": recent_pages,
            "recent_pages_per_minute": recent_pages * 60 / window,
        },
        "workers": states["workers"],
    }


class QueueCollector:
    def __init__(self, store):
        self.store = store

    def collect(self):
        from app.queue.store import BUCKETS

        rows, counters = self.store.metric_rows()
        durations = HistogramMetricFamily(
            "mineru_stage_seconds",
            "Stage wall time; nested SDK stages overlap",
            labels=["tier", "stage"],
        )
        for row in rows:
            counts = json.loads(row["buckets"])
            buckets = [
                ["+Inf" if bound == float("inf") else str(bound), count]
                for bound, count in zip(BUCKETS, counts, strict=True)
            ]
            durations.add_metric([row["tier"], row["stage"]], buckets, row["total"])
        yield durations
        events = CounterMetricFamily("mineru_jobs", "Durable coordinator events", labels=["event"])
        for row in counters:
            events.add_metric([row["event"]], row["value"])
        yield events
        stats = self.store.stats()
        queued = GaugeMetricFamily("mineru_tasks", "Tasks by state", labels=["state"])
        for state in ("queued", "running", "succeeded", "failed"):
            queued.add_metric([state], stats[state])
        yield queued
        for name in ("pending_pages", "oldest_wait_seconds", "workers"):
            metric = GaugeMetricFamily("mineru_" + name, "Coordinator " + name)
            metric.add_metric([], stats[name])
            yield metric
        totals = processing_rows(self.store)
        for name, field, description in (
            ("processed_pages", "completed_pages", "Successfully published PDF pages"),
            ("processing_seconds", "parse_seconds", "Execution wall time of measured successes"),
            ("measured_pages", "measured_pages", "Published pages paired with execution timings"),
        ):
            metric = CounterMetricFamily("mineru_" + name, description, labels=["tier"])
            for row in totals:
                metric.add_metric([row["tier"]], row[field])
            yield metric
        speed = GaugeMetricFamily(
            "mineru_parse_seconds_per_page",
            "Cumulative execution seconds per measured page",
            labels=["tier"],
        )
        for row in totals:
            if row["measured_pages"]:
                speed.add_metric([row["tier"]], row["parse_seconds"] / row["measured_pages"])
        yield speed


def render_metrics(store):
    registry = CollectorRegistry()
    registry.register(QueueCollector(store))
    return generate_latest(registry)
