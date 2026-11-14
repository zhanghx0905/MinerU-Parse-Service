"""Scheduling failures shared by the coordinator and application services."""


class ServiceBusy(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class LeaseLost(Exception):
    pass
