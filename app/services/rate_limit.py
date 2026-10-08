import asyncio
import time
from collections import defaultdict, deque


class InMemoryRateLimiter:
    def __init__(self):
        self._buckets = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._last_cleanup = 0.0

    async def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        async with self._lock:
            if now - self._last_cleanup > max(60, window_seconds):
                cutoff = now - window_seconds
                empty_keys = []
                for bucket_key, existing_bucket in self._buckets.items():
                    while existing_bucket and existing_bucket[0] < cutoff:
                        existing_bucket.popleft()
                    if not existing_bucket:
                        empty_keys.append(bucket_key)
                for bucket_key in empty_keys:
                    self._buckets.pop(bucket_key, None)
                self._last_cleanup = now
            bucket = self._buckets[key]
            cutoff = now - window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_requests:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                return False, retry_after
            bucket.append(now)
            return True, 0


telegram_rate_limiter = InMemoryRateLimiter()
vk_rate_limiter = InMemoryRateLimiter()
