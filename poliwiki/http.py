"""Small HTTP helper with retries and an on-disk cache for raw responses."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

USER_AGENT = "czech-poliwiki/0.1 (+https://github.com/DShaker/czech-poliwiki)"
CACHE_DIR = Path("data/cache")
MIN_INTERVAL = 0.25          # seconds between requests to the same host
MAX_WAIT = 300.0             # longest single wait on 429/503
_last: dict[str, float] = {}


log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


def _throttle(host: str) -> None:
    wait = _last.get(host, 0.0) + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.monotonic()


def _retry_after(e: urllib.error.HTTPError, default: float) -> float:
    try:
        return min(MAX_WAIT, max(default, float(e.headers.get("Retry-After", ""))))
    except ValueError:
        return min(MAX_WAIT, default)


def get(url: str, *, retries: int = 8, timeout: int = 120, cache: bool = True) -> bytes:
    path = CACHE_DIR / hashlib.sha1(url.encode()).hexdigest()
    if cache and path.exists():
        return path.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    host = urlsplit(url).netloc
    delay = 2.0
    for attempt in range(retries + 1):
        _throttle(host)
        wait = delay
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from e
            if attempt == retries:
                raise
            if e.code in (429, 503):  # rate limited: back off as the server asks
                wait = _retry_after(e, delay)
                log.warning("%s %s, waiting %.0f s", e.code, host, wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(wait)
        delay = min(delay * 2, MAX_WAIT)
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return body


def get_json(url: str, **kw):
    body = get(url, **kw)
    return json.loads(body) if body.strip() else {}
