"""Queue read model and completion totals; updates share the task's commit transaction."""

import math


def record_completion(connection, tier, pages, timings):
    """Count a published execution once; pair measured pages with their own execution time."""
    seconds = timings.get("worker.parse")
    measured = seconds is not None and math.isfinite(seconds) and seconds > 0
    connection.execute(
        "INSERT INTO processing_totals VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(tier) DO UPDATE SET "
        "completed_jobs=completed_jobs+excluded.completed_jobs,"
        "completed_pages=completed_pages+excluded.completed_pages,"
        "measured_jobs=measured_jobs+excluded.measured_jobs,"
        "measured_pages=measured_pages+excluded.measured_pages,"
        "parse_seconds=parse_seconds+excluded.parse_seconds",
        (tier, 1, pages, int(measured), pages if measured else 0, seconds if measured else 0),
    )


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
