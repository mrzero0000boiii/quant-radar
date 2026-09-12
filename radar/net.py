"""Thin HTTP layer: one shared session, retries, timeouts, polite concurrency."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("radar.net")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 quant-radar/1.0"
)

DEFAULT_TIMEOUT = 20

_local = threading.local()
_host_lock = threading.Lock()
_host_last: dict[str, float] = {}
MIN_HOST_INTERVAL = 0.35  # seconds between hits on the same host


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is not None:
        return s
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    _local.session = s
    return s


def _throttle(url: str) -> None:
    try:
        host = url.split("/")[2]
    except IndexError:
        return
    with _host_lock:
        last = _host_last.get(host, 0.0)
        wait = MIN_HOST_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.1))
        _host_last[host] = time.time()


def get_json(url: str, *, timeout: int = DEFAULT_TIMEOUT, **kw: Any) -> Any | None:
    """GET and parse JSON. Returns None on any failure (never raises)."""
    _throttle(url)
    try:
        r = _session().get(url, timeout=timeout, **kw)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None
    if r.status_code != 200:
        log.debug("GET %s -> HTTP %s", url, r.status_code)
        return None
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype and not r.text.lstrip()[:1] in ("{", "["):
        log.debug("GET %s -> non-JSON (%s)", url, ctype)
        return None
    try:
        return r.json()
    except ValueError:
        log.debug("GET %s -> bad JSON", url)
        return None


def get_text(url: str, *, timeout: int = DEFAULT_TIMEOUT, max_bytes: int = 3_000_000,
             **kw: Any) -> str | None:
    """GET raw text/HTML. Returns None on any failure (never raises)."""
    _throttle(url)
    headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    headers.update(kw.pop("headers", {}))
    try:
        r = _session().get(url, timeout=timeout, headers=headers,
                           allow_redirects=True, stream=True, **kw)
    except requests.RequestException as exc:
        log.debug("GET(text) %s failed: %s", url, exc)
        return None
    if r.status_code != 200:
        log.debug("GET(text) %s -> HTTP %s", url, r.status_code)
        return None
    try:
        chunks, total = [], 0
        for chunk in r.iter_content(65536, decode_unicode=False):
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        raw = b"".join(chunks)
    except requests.RequestException:
        return None
    finally:
        r.close()
    return raw.decode(r.encoding or "utf-8", errors="replace")


def post_json(url: str, payload: dict, *, timeout: int = DEFAULT_TIMEOUT, **kw: Any) -> Any | None:
    """POST JSON and parse JSON. Returns None on any failure (never raises)."""
    _throttle(url)
    headers = {"Content-Type": "application/json"}
    headers.update(kw.pop("headers", {}))
    try:
        r = _session().post(url, json=payload, timeout=timeout, headers=headers, **kw)
    except requests.RequestException as exc:
        log.debug("POST %s failed: %s", url, exc)
        return None
    if r.status_code != 200:
        log.debug("POST %s -> HTTP %s", url, r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        return None
