"""HTTP server for the audit portal (standard-library `http.server`, read-only).

    GET /                                          the single-page UI
    GET /healthz
    GET /api/summary                               build metadata, QC, flag definitions, libraries, trait groups
    GET /api/search?q=TEXT[&limit=]                traits, factors, gene sets and genes matching TEXT
    GET /api/traits                                every trait
    GET /api/trait?id=TRAIT                        trait, ontology mappings, its factors with audit columns
    GET /api/trait_matrix?id=TRAIT[&per_factor=]   factors x their top joint gene sets
    GET /api/factors[?trait=&flag=]                factor audit table
    GET /api/factor?id=FACTOR_ID                   factor, EAGGL metadata, sibling factors
    GET /api/factor_genes?id=FACTOR_ID             ranked nonzero gene loadings
    GET /api/factor_gene_sets?id=FACTOR_ID         the factor's top gene sets with overlap statistics
    GET /api/factor_gene_set?factor=ID&id=GS_ID    per-gene contributions and the trait's other factors
    GET /api/gene_set?id=GS_ID                     gene set, members, factors whose top lists contain it
    GET /api/gene?id=SYMBOL                        every factor loading the gene
    GET /api/hubs[?limit=]                         gene sets in the most joint top lists
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import queries
from .assets import render_page

LOG = logging.getLogger("factor_portal")


class PortalState:
    def __init__(self, db_path, title, cors_origin="*"):
        self.db_path = db_path
        self.title = title
        self.cors_origin = cors_origin
        self._local = threading.local()
        queries.open_database(db_path).close()  # fail at start-up, not on the first request

    def connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = queries.open_database(self.db_path)
        return conn


def _str(params, key, default=""):
    values = params.get(key)
    return values[0] if values else default


def _int(params, key, default, lo=1, hi=10000):
    value = _str(params, key)
    if value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        raise ValueError("query parameter %r must be an integer" % key)
    return max(lo, min(hi, number))


def _required(params, key):
    value = _str(params, key)
    if not value:
        raise ValueError("missing %r parameter" % key)
    return value


def handle_api(state, path, params):
    """Route one API request: (status, body). Bad parameters give 400, unknown ids 404."""
    conn = state.connection()
    try:
        if path == "/api/summary":
            return 200, queries.summary(conn)
        if path == "/api/search":
            return 200, queries.search(conn, _str(params, "q"), limit=_int(params, "limit", 8, hi=50))
        if path == "/api/traits":
            return 200, {"traits": queries.list_traits(conn)}
        if path == "/api/trait":
            return 200, queries.trait_detail(conn, _required(params, "id"))
        if path == "/api/trait_matrix":
            return 200, queries.trait_matrix(conn, _required(params, "id"), per_factor=_int(params, "per_factor", 5, hi=50))
        if path == "/api/factors":
            return 200, {"factors": queries.list_factors(conn, trait=_str(params, "trait"), flag=_str(params, "flag"))}
        if path == "/api/factor":
            return 200, queries.factor_detail(conn, _required(params, "id"))
        if path == "/api/factor_genes":
            return 200, queries.factor_genes(conn, _required(params, "id"))
        if path == "/api/factor_gene_sets":
            return 200, queries.factor_gene_sets(conn, _required(params, "id"))
        if path == "/api/factor_gene_set":
            return 200, queries.factor_gene_set(conn, _required(params, "factor"), _required(params, "id"))
        if path == "/api/gene_set":
            return 200, queries.gene_set_detail(conn, _required(params, "id"))
        if path == "/api/gene":
            return 200, queries.gene_detail(conn, _required(params, "id"))
        if path == "/api/hubs":
            return 200, {"gene_sets": queries.hubs(conn, limit=_int(params, "limit", 100, hi=5000))}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except queries.NotFound as exc:
        return 404, {"error": str(exc)}
    return 404, {"error": "unknown endpoint %s" % path}


def _json_default(value):
    return None if isinstance(value, float) else str(value)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "factor-portal/1"

        def log_message(self, fmt, *args):
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        def _cors(self):
            if state.cors_origin:
                self.send_header("Access-Control-Allow-Origin", state.cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status, payload):
            body = json.dumps(payload, allow_nan=False, default=_json_default, separators=(",", ":"))
            self._send(status, body.encode("utf-8"), "application/json; charset=utf-8")

        def do_OPTIONS(self):  # noqa: N802 (CORS preflight)
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802 (http.server API)
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, render_page(title=state.title, api_base="").encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self._send_json(200, {"ok": True, "db": str(state.db_path)})
                return
            if parsed.path.startswith("/api/"):
                try:
                    status, payload = handle_api(state, parsed.path, parse_qs(parsed.query, keep_blank_values=True))
                except Exception as exc:  # report, never kill the server thread
                    LOG.exception("API error for %s", self.path)
                    status, payload = 500, {"error": "internal error: %s" % exc}
                self._send_json(status, payload)
                return
            self._send_json(404, {"error": "not found"})

    return Handler


def serve(db_path, host="127.0.0.1", port=8766, title="EAGGL x CFDE projection audit", cors_origin="*",
          server_ready=None):
    """Serve until interrupted. `server_ready(httpd)` is called once the socket is bound (tests use port 0)."""
    state = PortalState(db_path, title, cors_origin)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    LOG.info("serving %s at http://%s:%d/", db_path, *httpd.server_address[:2])
    if server_ready is not None:
        server_ready(httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        httpd.server_close()
