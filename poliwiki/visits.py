"""Visitor counts from GoatCounter (no cookies, no consent banner needed).

The tracking script is added to every page when GOATCOUNTER_CODE is set at
build time. `python -m poliwiki visits` reads totals for all time, the last 30
days and the last 7 days from the GoatCounter API (token in
GOATCOUNTER_API_KEY) and stores them in data/visits.json for the site build.
Without a token, or if the API fails, it keeps the previous file (if any) and
never fails the build.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from .http import USER_AGENT

log = logging.getLogger(__name__)

DEFAULT_FILE = Path("data/visits.json")
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)   # before the site existed, so "all time" covers everything
WINDOWS = (("all", None), ("month", 30), ("week", 7))


def code() -> str:
    """GoatCounter site code (the <code> in <code>.goatcounter.com); empty disables tracking."""
    return os.environ.get("GOATCOUNTER_CODE", "").strip()


def script_tag() -> str:
    c = code()
    if not c:
        return ""
    return (f'<script data-goatcounter="https://{c}.goatcounter.com/count" '
            'async src="https://gc.zgo.at/count.js"></script>')


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _total(c: str, key: str, start: datetime, end: datetime) -> int:
    """Visits in [start, end]; GoatCounter counts a page once per visitor per 8 hours."""
    url = f"https://{c}.goatcounter.com/api/v0/stats/total?" + urlencode({"start": _iso(start), "end": _iso(end)})
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return int(json.load(resp)["total"])


def fetch(c: str, key: str, now: datetime) -> dict:
    """Totals for all time and the last 30 / 7 days (counted in whole UTC days, today included)."""
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for name, days in WINDOWS:
        start = SINCE if days is None else midnight - timedelta(days=days - 1)
        out[name] = _total(c, key, start, end)
    out["fetched_at"] = now.strftime("%Y-%m-%dT%H:%MZ")
    out["code"] = c
    return out


def update(path: Path = DEFAULT_FILE, now: datetime | None = None) -> dict | None:
    c, key = code(), os.environ.get("GOATCOUNTER_API_KEY", "").strip()
    if not c or not key:
        log.warning("visits: GOATCOUNTER_CODE or GOATCOUNTER_API_KEY not set, skipping")
        return load(path)
    try:
        data = fetch(c, key, now or datetime.now(timezone.utc))
    except (urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as ex:
        log.warning("visits: GoatCounter API failed (%s), keeping previous numbers", ex)
        return load(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")
    log.info("visits: all %(all)d, 30 days %(month)d, 7 days %(week)d", data)
    return data


def load(path: Path = DEFAULT_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
