import os
from collections import deque
from time import time


HOUR_SECONDS = 3600
DAY_SECONDS = 86400


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


SCORE_HOURLY_LIMIT = _env_int("SCORE_HOURLY_LIMIT", 100)
SCORE_DAILY_LIMIT = _env_int("SCORE_DAILY_LIMIT", 100)
REGISTER_DAILY_LIMIT = _env_int("REGISTER_DAILY_LIMIT", 2)
OBSERVATIONS_HOURLY_LIMIT = _env_int("OBSERVATIONS_HOURLY_LIMIT", 500)
OBSERVATIONS_DAILY_LIMIT = _env_int("OBSERVATIONS_DAILY_LIMIT", 2000)


# user_id -> deque of unix timestamps (one per /score call within the past 24h)
_score_hits: dict[str, deque[float]] = {}
# ip -> deque of unix timestamps (one per /register call within the past 24h)
_register_hits: dict[str, deque[float]] = {}
# user_id -> deque of unix timestamps (one per observation record posted)
_observation_hits: dict[str, deque[float]] = {}


class RateLimitExceeded(Exception):
    def __init__(self, window: str, limit: int, retry_after: int) -> None:
        super().__init__(f"{window} score limit of {limit} reached")
        self.window = window
        self.limit = limit
        self.retry_after = retry_after


def check_and_record_score(user_id: str) -> None:
    now = time()
    q = _score_hits.setdefault(user_id, deque())

    while q and q[0] < now - DAY_SECONDS:
        q.popleft()

    daily = len(q)
    hourly = 0
    hour_cutoff = now - HOUR_SECONDS
    for t in reversed(q):
        if t < hour_cutoff:
            break
        hourly += 1

    if hourly >= SCORE_HOURLY_LIMIT:
        oldest_in_hour = next(t for t in q if t >= hour_cutoff)
        retry_after = max(1, int(oldest_in_hour + HOUR_SECONDS - now))
        raise RateLimitExceeded("hourly", SCORE_HOURLY_LIMIT, retry_after)

    if daily >= SCORE_DAILY_LIMIT:
        retry_after = max(1, int(q[0] + DAY_SECONDS - now))
        raise RateLimitExceeded("daily", SCORE_DAILY_LIMIT, retry_after)

    q.append(now)


def check_and_record_register(ip: str) -> None:
    now = time()
    q = _register_hits.setdefault(ip, deque())

    while q and q[0] < now - DAY_SECONDS:
        q.popleft()

    if len(q) >= REGISTER_DAILY_LIMIT:
        retry_after = max(1, int(q[0] + DAY_SECONDS - now))
        raise RateLimitExceeded("daily register", REGISTER_DAILY_LIMIT, retry_after)

    q.append(now)


def check_and_record_observations(user_id: str, count: int) -> None:
    if count <= 0:
        return
    now = time()
    q = _observation_hits.setdefault(user_id, deque())

    while q and q[0] < now - DAY_SECONDS:
        q.popleft()

    daily = len(q)
    hour_cutoff = now - HOUR_SECONDS
    hourly = 0
    for t in reversed(q):
        if t < hour_cutoff:
            break
        hourly += 1

    if hourly + count > OBSERVATIONS_HOURLY_LIMIT:
        oldest_in_hour = next((t for t in q if t >= hour_cutoff), now)
        retry_after = max(1, int(oldest_in_hour + HOUR_SECONDS - now))
        raise RateLimitExceeded("hourly observations", OBSERVATIONS_HOURLY_LIMIT, retry_after)

    if daily + count > OBSERVATIONS_DAILY_LIMIT:
        retry_after = max(1, int(q[0] + DAY_SECONDS - now)) if q else DAY_SECONDS
        raise RateLimitExceeded("daily observations", OBSERVATIONS_DAILY_LIMIT, retry_after)

    for _ in range(count):
        q.append(now)
