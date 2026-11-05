"""One polite HTTP client for every source: per-host pacing, retries on 429/5xx, optional disk cache.

Tests replace `transport` with a fake, so nothing touches the network.
"""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# SEC allows 10 requests a second but throttles bursts; stay near 4.
MIN_INTERVAL = {"sec.gov": 0.25, "wikidata.org": 1.0}
DEFAULT_INTERVAL = 0.5
_last = {}
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward credentials to another host (Companies House documents redirect to storage
    that rejects them, and leaking a key to a third host is wrong anyway)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _urllib_transport(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return None, str(e).encode()


transport = _urllib_transport


def _pace(host):
    """Reserve this host's next slot under a lock, so threads still respect each host's gap."""
    gap = next((v for k, v in MIN_INTERVAL.items() if host == k or host.endswith("." + k)), DEFAULT_INTERVAL)
    with _lock:
        slot = max(time.monotonic(), _last.get(host, 0) + gap)
        _last[host] = slot
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _backoff(host, status, attempt):
    """Seconds to wait before retrying. An SEC rate limit can last minutes, so wait it out."""
    if status == 429 and (host == "sec.gov" or host.endswith(".sec.gov")):
        return [30, 60, 120, 240][min(attempt, 3)]
    return min(60, 2 ** attempt * (5 if status == 429 else 2))


def get(url, params=None, headers=None, timeout=30, tries=5, cache=True):
    """GET and return bytes; None on 404. Raises FetchError after retries."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    cdir = os.environ.get("BENCHDATA_CACHE")
    cfile = Path(cdir) / hashlib.sha256(url.encode()).hexdigest() if cdir and cache else None
    if cfile and cfile.exists():
        return cfile.read_bytes()
    host = urllib.parse.urlparse(url).hostname or ""
    for attempt in range(tries):
        _pace(host)
        status, body = transport(url, headers or {}, timeout)
        if status == 200:
            if cfile:
                cfile.parent.mkdir(parents=True, exist_ok=True)
                cfile.write_bytes(body)
            return body
        if status == 404:
            return None
        if status in (403, 401):
            raise FetchError(f"{status} from {host}: check the contact header or API key", status)
        if status and 400 <= status < 500 and status != 429:
            raise FetchError(f"{status} from {url}", status)   # a retry won't change a client error
        if attempt < tries - 1:
            time.sleep(_backoff(host, status, attempt))
    raise FetchError(f"gave up on {url} (last status {status})", status)


def get_json(url, **kw):
    body = get(url, **kw)
    return None if body is None else json.loads(body)


def get_text(url, **kw):
    body = get(url, **kw)
    return None if body is None else body.decode("utf-8", "replace")
