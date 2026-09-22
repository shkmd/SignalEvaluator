"""In-memory rate limiting for auth endpoints -- brute-force/credential-stuffing protection.

This service runs a single Railway replica (see railway.json's multiRegionConfig, numReplicas:
1), so in-memory state is consistent across every request without needing a shared store like
Redis. State resets on every deploy/restart, which is an acceptable tradeoff for a
defense-in-depth layer -- passwords are still PBKDF2-hashed and sessions still expire
regardless of whether this survives a restart.
"""
import time
from collections import defaultdict

_attempts: dict = defaultdict(list)


def check(key: str, max_attempts: int, window_seconds: int) -> tuple:
    """Returns (allowed, retry_after_seconds). Read-only -- call record() separately so a
    caller can choose to only count failed attempts, not successful ones."""
    now = time.time()
    attempts = [t for t in _attempts[key] if now - t < window_seconds]
    _attempts[key] = attempts
    if len(attempts) >= max_attempts:
        retry_after = int(window_seconds - (now - attempts[0])) + 1
        return False, max(retry_after, 1)
    return True, 0


def record(key: str) -> None:
    _attempts[key].append(time.time())


def reset(key: str) -> None:
    _attempts.pop(key, None)
