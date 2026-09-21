"""The public Comfy Registry API and its CDN, over ``urllib``.

Unauthenticated, exactly like the ComfyUI clone: no token, no account, no paid
tier. The only thing here beyond a plain GET is the backoff, because a
whole-registry crawl is thousands of requests and the API rate-limits.

The node listing embeds a ``latest_version`` object whose ``downloadUrl`` is
always empty. Only the per-version record carries the real CDN URL, so a crawl
costs one listing page plus one version lookup per pack.
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .errors import NetworkError, RegistryError

API_ROOT = "https://api.comfy.org"
NODES_PATH = "/nodes"

# The API silently clamps `limit` here; asking for 500 returns 100.
MAX_PAGE_LIMIT = 100

USER_AGENT = ("comfy-import-guard/%s (+https://github.com/Booyaka101/comfy-import-guard)"
              % __version__)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BACKOFF = 60.0
CHUNK = 64 * 1024


class RegistryClient:
    """Reads the registry listing, version records and pack archives.

    ``sleep`` is injected so tests exercise the backoff without waiting, and
    ``api_root`` so they can point the whole client at a local fixture server.
    """

    def __init__(self, api_root=API_ROOT, timeout=60, retries=5, backoff=1.0,
                 sleep=time.sleep, log=None):
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self._sleep = sleep
        self._log = log or (lambda msg: None)

    # ------------------------------------------------------------------ HTTP

    def open(self, url):
        """GET ``url``, retrying 429 and 5xx with backoff. Caller closes the response."""
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        })
        for attempt in range(self.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_STATUSES or attempt == self.retries:
                    detail = _short_body(exc)
                    raise RegistryError(
                        "%s returned HTTP %s%s" % (url, exc.code, detail), status=exc.code)
                wait = self._retry_after(exc) or self._backoff_for(attempt)
                self._log("comfy-import-guard: HTTP %s from %s, retrying in %.0fs"
                          % (exc.code, _host(url), wait))
            except (urllib.error.URLError, OSError) as exc:
                if attempt == self.retries:
                    raise NetworkError(
                        "Cannot reach %s: %s\n"
                        "Check the connection, or re-run later: crawl resumes from its "
                        "checkpoint and will not redo finished packs." % (_host(url), exc)
                    )
                wait = self._backoff_for(attempt)
                self._log("comfy-import-guard: %s unreachable (%s), retrying in %.0fs"
                          % (_host(url), exc, wait))
            self._sleep(wait)

    def get_json(self, url):
        with self.open(url) as response:
            raw = _read(response, url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError("%s did not return JSON: %s" % (url, exc))

    def _backoff_for(self, attempt):
        return min(self.backoff * (2 ** attempt), MAX_BACKOFF)

    def _retry_after(self, exc):
        """The ``Retry-After`` seconds form. The HTTP-date form falls back to backoff."""
        value = (exc.headers or {}).get("Retry-After")
        try:
            return min(max(float(value), 0.0), MAX_BACKOFF)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------- registry

    def nodes_page(self, page, limit):
        url = "%s%s?%s" % (self.api_root, NODES_PATH,
                           urllib.parse.urlencode({"page": page, "limit": limit}))
        payload = self.get_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise RegistryError("%s did not return a node page; got %s"
                                % (url, type(payload).__name__))
        return payload

    def version_record(self, node_id, version):
        """The published version record, the only place ``downloadUrl`` is filled in."""
        url = "%s%s/%s/versions/%s" % (
            self.api_root, NODES_PATH,
            urllib.parse.quote(str(node_id), safe=""),
            urllib.parse.quote(str(version), safe=""),
        )
        payload = self.get_json(url)
        if not isinstance(payload, dict):
            raise RegistryError("%s did not return a version record" % url)
        return payload

    # ------------------------------------------------------------- download

    def download(self, url, dest, max_bytes):
        """Fetch ``url`` to ``dest``, refusing anything over ``max_bytes``.

        Content-Length is checked first so an oversized archive costs no
        bandwidth, then again while streaming because the header is optional
        and not binding. The bytes land in a ``.part`` file and are renamed on
        success, so an interrupted crawl never leaves a truncated archive that
        the next run would read as cached.
        """
        dest = str(dest)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        part = dest + ".part"
        with self.open(url) as response:
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                raise RegistryError(_too_large(url, declared, max_bytes))
            written = 0
            try:
                with open(part, "wb") as fh:
                    while True:
                        chunk = _read(response, url, CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise RegistryError(_too_large(url, written, max_bytes, atleast=True))
                        fh.write(chunk)
                if declared is not None and written != declared:
                    # read(amt) returns short on a dropped connection rather than
                    # raising, so the promised length is the only thing that
                    # catches a truncated archive before it is cached as valid.
                    raise RegistryError("%s ended early: %d of %d bytes"
                                        % (url, written, declared))
            except BaseException:
                _unlink(part)
                raise
        os.replace(part, dest)
        return written

    def discard(self, path):
        """Forget a cached artifact. Missing is the wanted state, not an error."""
        _unlink(str(path))


def _read(response, url, size=None):
    """Read a response, turning a mid-stream drop into a per-pack RegistryError."""
    try:
        return response.read() if size is None else response.read(size)
    except (OSError, http.client.HTTPException) as exc:
        raise RegistryError("%s ended early: %s" % (url, exc))


def _too_large(url, size, cap, atleast=False):
    return ("%s is %s%.1f MB, over the %.0f MB archive cap; raise --max-zip-mb to "
            "include it" % (url, "over " if atleast else "", size / 1e6, cap / 1e6))


def _content_length(response):
    try:
        return int(response.headers.get("Content-Length"))
    except (TypeError, ValueError):
        return None


def _short_body(exc):
    try:
        body = exc.read(200).decode("utf-8", "replace").strip().replace("\n", " ")
    except Exception:
        return ""
    return ": %s" % body if body else ""


def _host(url):
    return urllib.parse.urlsplit(url).netloc or url


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
