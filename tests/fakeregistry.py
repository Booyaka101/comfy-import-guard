"""A stand-in for api.comfy.org and its CDN, on localhost.

Everything ``crawl`` reads over HTTP is served from here, so the crawl tests
never touch the real registry: the node listing with its paging, the per-version
records that carry ``downloadUrl``, and the ``node.zip`` archives themselves.

The failure paths are the point of the thing. A route can be told to return a
run of 429s or 503s before it succeeds, to 404, to omit Content-Length, to cut
the body off half way, or to drop the connection outright, which is what a
crawl has to survive.
"""

import io
import json
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACTIVE = "NodeVersionStatusActive"


def make_zip(files):
    """``{"nodes.py": "import comfy.utils\n"}`` -> zip bytes, members at the root."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body.encode("utf-8") if isinstance(body, str) else body)
    return buf.getvalue()


class FakeRegistry:
    """Serves a registry you assemble pack by pack."""

    def __init__(self, page_size=100):
        self.page_size = page_size
        self.nodes = []
        self.versions = {}      # (id, version) -> record
        self.archives = {}      # "/cdn/<name>.zip" -> bytes
        self.requests = []      # every path served, in order
        self.failures = {}      # path -> [status, ...] popped one per request
        self.drop = set()       # paths whose connection is closed unanswered
        self.headless = set()   # paths served without a Content-Length
        self.truncate = set()   # paths cut off mid-body, length header and all
        self._server = None
        self._thread = None

    # ------------------------------------------------------------- fixtures

    def add_pack(self, node_id, version="1.0.0", files=None, declared="",
                 publisher="pub", downloads=0, status=ACTIVE, deprecated=False,
                 archive=None, download_url=None, repository=""):
        """Add one pack. ``version=None`` means the registry has no release for it."""
        node = {
            "id": node_id,
            "name": node_id,
            "publisher": {"id": publisher},
            "downloads": downloads,
            "repository": repository,
        }
        if version is not None:
            node["latest_version"] = {
                "version": version,
                "supported_comfyui_version": declared,
                # The real listing always carries an empty one here.
                "downloadUrl": "",
            }
            path = "/cdn/%s-%s.zip" % (node_id, version)
            self.archives[path] = archive if archive is not None else make_zip(
                files if files is not None else {"nodes.py": "import comfy.utils\n"})
            self.versions[(node_id, version)] = {
                "version": version,
                "supported_comfyui_version": declared,
                "status": status,
                "deprecated": deprecated,
                "downloadUrl": download_url if download_url is not None else path,
            }
        self.nodes.append(node)
        return node

    def fail(self, path, statuses):
        """Return each of ``statuses`` once, in order, before serving ``path``."""
        self.failures.setdefault(path, []).extend(statuses)

    # --------------------------------------------------------------- server

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.registry = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def absolute(self, path):
        return self.url + path

    def requests_for(self, needle):
        return [r for r in self.requests if needle in r]

    # -------------------------------------------------------------- routing

    def respond(self, handler):
        split = urllib.parse.urlsplit(handler.path)
        path = split.path
        self.requests.append(handler.path)

        if path in self.drop:
            handler.close_connection = True
            handler.connection.close()
            return

        pending = self.failures.get(path)
        if pending:
            status = pending.pop(0)
            body = json.dumps({"error": "injected %d" % status}).encode("utf-8")
            headers = {"Retry-After": "0"} if status == 429 else {}
            return _send(handler, status, body, "application/json", headers)

        if path == "/nodes":
            return _send(handler, 200, self._page(split.query), "application/json",
                         truncate=path in self.truncate)
        if path.startswith("/nodes/"):
            return self._version(handler, path)
        if path in self.archives:
            return _send(handler, 200, self.archives[path], "application/zip",
                         omit_length=path in self.headless,
                         truncate=path in self.truncate)
        _send(handler, 404, b"{}", "application/json")

    def _page(self, query):
        params = urllib.parse.parse_qs(query)
        page = max(1, int((params.get("page") or ["1"])[0]))
        limit = min(int((params.get("limit") or [self.page_size])[0]), self.page_size)
        start = (page - 1) * limit
        chunk = self.nodes[start:start + limit]
        total_pages = max(1, -(-len(self.nodes) // limit))
        return json.dumps({
            "nodes": chunk,
            "total": len(self.nodes),
            "page": page,
            "limit": limit,
            "totalPages": total_pages,
        }).encode("utf-8")

    def _version(self, handler, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[2] != "versions":
            return _send(handler, 404, b"{}", "application/json")
        key = (urllib.parse.unquote(parts[1]), urllib.parse.unquote(parts[3]))
        record = self.versions.get(key)
        if record is None:
            return _send(handler, 404, b'{"error":"no such version"}', "application/json")
        payload = dict(record)
        if payload.get("downloadUrl", "").startswith("/"):
            payload["downloadUrl"] = self.absolute(payload["downloadUrl"])
        return _send(handler, 200, json.dumps(payload).encode("utf-8"), "application/json")


def _send(handler, status, body, content_type, headers=None, omit_length=False,
          truncate=False):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    if truncate:
        # A promised length the body never reaches, which is what a connection
        # dropped mid-transfer looks like to the client.
        handler.send_header("Content-Length", str(len(body)))
        handler.close_connection = True
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body[:max(1, len(body) // 2)])
        handler.connection.close()
        return
    if omit_length:
        # No length and no chunking: the client reads to EOF, which is the
        # path where only the running byte count can refuse an oversize file.
        handler.close_connection = True
    else:
        handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.registry.respond(self)

    def log_message(self, *args):
        pass
