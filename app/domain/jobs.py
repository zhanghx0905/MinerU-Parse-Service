"""Scheduling rules and failures shared by the coordinator and application services."""

TIER_WEIGHTS = {"flash": 1, "basic": 2, "standard": 3, "advanced": 6}
SHORT_JOB_MAX_PAGES = 8
AGING_SECONDS = 60


class ServiceBusy(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class LeaseLost(Exception):
    pass


class WorkBudgetExceeded(ValueError):
    pass
