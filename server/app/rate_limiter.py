import threading
import time
from collections import defaultdict
from typing import Dict, List

from .config import config

class DownloadConcurrencyLimiter:
    def __init__(self, max_concurrent: int):
        self.semaphore = threading.BoundedSemaphore(max_concurrent)

    def acquire(self, timeout: float = 2.0) -> bool:
        return self.semaphore.acquire(timeout=timeout)

    def release(self):
        try:
            self.semaphore.release()
        except ValueError:
            pass

class IPRateLimiter:
    """
    Sliding window rate limiter per client IP.
    """
    def __init__(self, max_requests_per_sec: int):
        self.max_rps = max_requests_per_sec
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        if self.max_rps <= 0:
            return True

        now = time.time()
        one_sec_ago = now - 1.0

        with self.lock:
            history = self.requests[ip]
            # Prune old timestamps
            self.requests[ip] = [t for t in history if t > one_sec_ago]

            if len(self.requests[ip]) >= self.max_rps:
                return False

            self.requests[ip].append(now)
            return True

download_concurrency_limiter = DownloadConcurrencyLimiter(config.MAX_CONCURRENT_DOWNLOADS)
ip_rate_limiter = IPRateLimiter(config.RATE_LIMIT_RPS)
