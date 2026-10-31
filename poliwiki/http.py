"""Small HTTP helper with retries and an on-disk cache for raw responses."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "czech-poliwiki/0.1 (+https://github.com/DShaker/czech-poliwiki)"
CACHE_DIR = Path("data/cache")


class NotFound(Exception):
    pass


def get(url: str, *, retries: int = 4, timeout: int = 120, cache: bool = True) -> bytes:
    path = CACHE_DIR / hashlib.sha1(url.encode()).hexdigest()
    if cache and path.exists():
        return path.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from e
            if attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return body


def get_json(url: str, **kw):
    body = get(url, **kw)
    return json.loads(body) if body.strip() else {}
