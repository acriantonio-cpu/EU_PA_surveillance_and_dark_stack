# EN: Shared test fixtures: local multi-host HTTP server (no real network).
"""Fixture condivise: server HTTP locale multi-host (nessuna rete reale)."""

from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeWeb:
    """Server HTTP che serve contenuti diversi in base all'header Host + path.
    'routes' = {(host, path): (status, headers_dict, body_bytes|str)}."""

    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, dict, bytes]] = {}
        self.hits: list[tuple[str, str]] = []
        self.hit_log: list[tuple[float, str, str, str]] = []     # (timestamp, host, path, user-agent)
        self.latency = 0.0
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):  # silenzio
                pass

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                path = self.path
                with outer.lock:
                    outer.hits.append((host, path))
                    outer.hit_log.append((time.monotonic(), host, path, self.headers.get("User-Agent", "")))
                if outer.latency:
                    time.sleep(outer.latency)
                route = outer.routes.get((host, path)) or outer.routes.get((host, path.split("?")[0]))
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>not found</body></html>")
                    return
                status, headers, body = route
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                hdrs = {"Content-Type": "text/html; charset=utf-8", **headers}
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def add(self, host, path, body="", status=200, **headers):
        self.routes[(host, path)] = (status, headers, body)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()

    def hit_paths(self, host):
        return [p for h, p in self.hits if h == host]

    def url(self, host, path="/"):
        return f"http://{host}:{self.port}{path}"


@pytest.fixture
def web(monkeypatch):
    """Server locale + risoluzione DNS finta: qualunque hostname -> 127.0.0.1."""
    fw = FakeWeb()
    fw.start()
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str) and host not in ("127.0.0.1", "localhost"):
            host = "127.0.0.1"
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield fw
    fw.stop()
