"""Shared HTTP plumbing for the two OpenStreetMap services.

Nominatim and Overpass share a throttle, a disk cache and a User-Agent, but differ
in transport, rate budget, timeout and how long a response stays useful. The
backoff loop lives here so it isn't written twice.

Three rules from the OSM operations policies that this module exists to honor:

- Identify yourself. Nominatim requires a User-Agent with a contact address as a
  *condition of access*, not as a rate upgrade - unidentified clients get blocked.
  Hence RESTAURANT_CONTACT_EMAIL is required rather than optional.
- Nominatim is a hard 1 request/second, and there is no way to raise it.
- Cache. Results must be cached client-side; repeated identical queries are
  explicitly called out as abuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

CACHE_DIR = Path(".cache")


class MissingContactError(RuntimeError):
    """No contact email configured, so we must not touch the public instances."""


class TransientError(RuntimeError):
    """A failure worth retrying: a queued Overpass slot, a timeout, a 429."""


class OSMError(RuntimeError):
    """A failure not worth retrying."""


def contact_email() -> str:
    email = os.environ.get("RESTAURANT_CONTACT_EMAIL", "").strip()
    if not email:
        raise MissingContactError(
            "OpenStreetMap's usage policy requires a contact address in the "
            "User-Agent, and will block requests without one.\n"
            "Set it once:\n\n"
            "    export RESTAURANT_CONTACT_EMAIL=you@example.com\n"
        )
    return email


def user_agent() -> str:
    return f"placemat/0.1 (personal restaurant library; {contact_email()})"


class Throttle:
    """Minimum-interval gate. Simple, and enough for a single-threaded CLI."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last = time.monotonic()


# Nominatim's 1/sec is a hard policy limit. Overpass is slot-queue based rather
# than rate-limited, so the real constraint is the queue - be generous with it.
_THROTTLES = {
    "nominatim": Throttle(1.0),
    "overpass": Throttle(0.5),
}

_RETRY_STATUS = {429, 502, 503, 504}


def _cache_path(service: str, method: str, url: str, params: Any, data: Any) -> Path:
    raw = f"{method}{url}{sorted((params or {}).items())}{data or ''}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return CACHE_DIR / service / f"{digest}.json"


def _fresh(path: Path, max_age_days: float | None) -> bool:
    if not path.exists():
        return False
    if max_age_days is None:
        return True
    return (time.time() - path.stat().st_mtime) <= max_age_days * 86400


def _parse(response: httpx.Response) -> Any:
    """Return parsed JSON, or raise TransientError if the body isn't JSON.

    This guard is the whole reason this function exists. Overpass answers a
    perfectly valid query with an HTML error page when its slots are busy -
    roughly one request in six during development - and the identical query
    succeeds seconds later. `raise_for_status` does not catch it, because the
    status is 200.

    response.json() is guarded too, not just the content-type: an observed
    failure claimed a JSON content-type and still would not parse.
    """
    ctype = response.headers.get("content-type", "")
    body = response.text
    if "json" not in ctype or body.lstrip()[:1] not in "{[":
        raise TransientError(
            f"non-JSON response ({ctype or 'no content-type'}, {len(body)}B): "
            f"{body[:200].strip()!r}"
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise TransientError(f"malformed JSON: {exc}") from exc


def request(
    service: str,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: str | None = None,
    use_cache: bool = True,
    max_age_days: float | None = None,
    timeout: float = 30.0,
    attempts: int = 6,
) -> Any:
    """Throttled, cached, retrying JSON request."""
    cache_file = _cache_path(service, method, url, params, data)
    if use_cache and _fresh(cache_file, max_age_days):
        return json.loads(cache_file.read_text())

    headers = {"User-Agent": user_agent()}
    throttle = _THROTTLES[service]

    for attempt in range(attempts):
        throttle.wait()
        try:
            if method == "POST":
                response = httpx.post(
                    url, content=data, headers=headers, timeout=timeout,
                    follow_redirects=True,
                )
            else:
                response = httpx.get(
                    url, params=params, headers=headers, timeout=timeout,
                    follow_redirects=True,
                )

            if response.status_code in _RETRY_STATUS:
                raise TransientError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                # A 400 from Overpass means the query itself is malformed. Fail
                # loudly rather than six times slowly.
                raise OSMError(
                    f"HTTP {response.status_code} from {service}: "
                    f"{response.text[:300].strip()}"
                )

            payload = _parse(response)

        except (TransientError, httpx.TransportError) as exc:
            if attempt == attempts - 1:
                raise TransientError(
                    f"{service} failed after {attempts} attempts: {exc}"
                ) from exc
            # Jitter so a batch of failures doesn't retry in lockstep.
            delay = min(60.0, 2**attempt) + random.uniform(0, 1)
            print(
                f"  ({service}: {type(exc).__name__}, retrying in {delay:.1f}s)",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        # Only cache a response we successfully parsed.
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload))
        return payload

    raise RuntimeError("unreachable")
