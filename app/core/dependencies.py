"""Shared synchronization primitives used by mutating services."""

from functools import wraps
from threading import RLock

STATE_LOCK = RLock()


def synchronized_mutation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with STATE_LOCK:
            return function(*args, **kwargs)

    return wrapped
