# EN: Unit tests of the fixes listed in CHANGES.md (no real network).
"""Test unitari delle correzioni (nessuna rete reale)."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

from src.base_fetcher import BaseFetcher, HttpSession, NonRetryableHTTPError, _cell_to_text
from src.fetchers.generic import ApiJsonFetcher, BulkCSVFetcher, SiteCrawlFetcher, _decode_with_fallback
from src.schema import FIELDNAMES, Record, atomic_write_csv
from src.urlutil import (
    canonicalize_url, dedupe_key_for_url, host_of, looks_like_trap, normalize_url_value,
    normalize_url_values, url_pattern_signature,
)
from src.web_signals import detect_challenge, detect_default_page, detect_parked, is_infrastructure, looks_js_rendered
from src import website_finder as wf


# ---------------------------------------------------------------- (2) TLD
@pytest.mark.parametrize("dom,tlds,expected", [
    ("de.wikipedia.org", {"de", "gov"}, False),
    ("be.linkedin.com", {"be", "gov"}, False),
    ("gov.evil-site.com", {"be", "gov"}, False),
    ("evil.gov.com", {"be", "gov"}, False),
    ("stadt-x.de", {"de", "gov"}, True),
    ("vlaanderen.be", {"be"}, True),
    ("revenue.gov.ie", {"ie", "gov"}, True),      # sarebbe vero anche per 'ie'
    ("sede.gob.es", {"gob"}, True),
    ("gov.pl", {"gov"}, True),
    ("x.gouv.fr", {"gouv"}, True),
    ("x.brussels", {"brussels"}, True),
    ("foo.co.uk", {"gov.uk"}, False),
    ("foo.gov.uk", {"gov.uk"}, True),
])
def test_tld_matches(dom, tlds, expected):
    assert SiteCrawlFetcher._tld_matches(dom, tlds) is expected


# ---------------------------------------------------------- (7)(8) URL utils
def test_canonicalize_collapses_variants():
    base = canonicalize_url("https://www.example.be/p")
    for v in ["https://www.example.be/p#a", "https://www.example.be/p/", "https://www.example.be/p?utm_source=x&fbclid=1",
              "HTTPS://WWW.EXAMPLE.BE:443/p", "https://www.example.be/p;jsessionid=ABC123", "https://www.example.be//p"]:
        assert canonicalize_url(v) == base, v
    assert canonicalize_url("https://a.be/x?b=2&a=1") == canonicalize_url("https://a.be/x?a=1&b=2")


def test_host_of_edge_cases():
    assert host_of("https://user:pw@host.example.be:8080/x") == "host.example.be"
    assert host_of("https://www.münchen.de/") == "xn--mnchen-3ya.de".replace("xn--mnchen-3ya.de", "www.xn--mnchen-3ya.de")
    assert host_of("http://[bad-ipv6/x") == ""
    assert host_of("https://Example.BE./") == "example.be"


def test_trap_guards():
    assert looks_like_trap("https://a.be/" + "x" * 2100)
    assert looks_like_trap("https://a.be/a/b/a/b/a/b/a/b")
    assert not looks_like_trap("https://a.be/news/2026/10/titolo")
    assert url_pattern_signature("https://a.be/cal/2026/10/03?month=10") == url_pattern_signature("https://a.be/cal/2027/01/15?month=1")


# ------------------------------------------------------------ (22) valori
def test_normalize_url_values():
    assert normalize_url_values("www.y.es; www.z.es") == ["https://www.y.es", "https://www.z.es"]
    assert normalize_url_values("HTTP://WWW.X.ES/") == ["http://www.x.es"]
    for junk in ["nan", "n/d", "-", "info@comune.it", "mailto:a@b.it", "testo libero senza dominio", None, ""]:
        assert normalize_url_values(junk) == [], junk
    assert dedupe_key_for_url("http://www.x.es/") == dedupe_key_for_url("https://x.es")


def test_cell_to_text_excel_numbers():
    assert _cell_to_text(12345.0) == "12345"
    assert _cell_to_text(12.5) == "12.5"
    assert _cell_to_text(float("nan")) is None
    assert _cell_to_text(None) is None
    assert _cell_to_text(["a", "b"]) == "a|b"


def test_run_postprocess_and_case_insensitive_dedupe(tmp_path):
    class F(BaseFetcher):
        country_code = "ES"; source_name = "t"
        def download(self): return tmp_path
        def parse(self, p): return [
            {"h": "http://www.X.es/", "c": 12345.0}, {"h": "HTTPS://x.es", "c": 1}, {"h": "nan", "c": 2},
            {"h": "www.a.es; www.b.es", "c": 3},
        ]
    f = F({"field_mapping": {"hostname": "h", "codice_ipa": "c"}, "dedupe_by": "hostname"}, tmp_path / "i", tmp_path / "o")
    recs = f.run()
    assert [r.hostname for r in recs if r.hostname] == ["http://www.x.es", "https://www.a.es"]
    assert recs[0].codice_ipa == "12345" and recs[0].dominio_radice == "x.es"
    a = [r for r in recs if r.hostname == "https://www.a.es"][0]
    assert a.host_osservati == "https://www.b.es"
    assert any(r.hostname == "" for r in recs)   # 'nan' -> vuoto, non "nan"


# ------------------------------------------------- HttpSession (1)(11)(12)(18)
class _Handler(BaseHTTPRequestHandler):
    hits: list[str] = []
    def log_message(self, *a): pass
    def do_GET(self):
        _Handler.hits.append(self.path)
        if self.path == "/404":
            self.send_response(404); self.end_headers(); return
        if self.path == "/503":
            self.send_response(503); self.send_header("Retry-After", "0"); self.end_headers(); return
        if self.path == "/big":
            body = b"<html>" + b"a" * 500_000 + b"</html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body); return
        if self.path == "/pdf":
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.end_headers(); self.wfile.write(b"%PDF" + b"x" * 1000); return
        if self.path == "/latin1":
            self.send_response(200); self.send_header("Content-Type", "text/html")  # nessun charset dichiarato
            self.end_headers(); self.wfile.write(("<html><body>" + "Città però Ægir, più università e caffè. " * 40 + "</body></html>").encode("iso-8859-1")); return
        if self.path == "/metacs":
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.end_headers(); self.wfile.write('<html><head><meta charset="windows-1250"></head><body>Łódź ŻÓŁW</body></html>'.encode("cp1250")); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b"<html>ok</html>")


@pytest.fixture
def plain_server():
    _Handler.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_get_accepts_crawler_kwargs_and_does_not_retry_404(plain_server, monkeypatch):
    slept = []
    monkeypatch.setattr("src.base_fetcher.time.sleep", lambda s: slept.append(s))
    h = HttpSession()
    r = h.get(plain_server + "/", max_bytes=3_000_000, skip_non_html=True, read_timeout=8.0)   # (1) firma
    assert r.status_code == 200
    with pytest.raises(NonRetryableHTTPError):
        h.get(plain_server + "/404")
    assert _Handler.hits.count("/404") == 1 and slept == []                                         # (11)


def test_get_retries_5xx_without_trailing_sleep(plain_server, monkeypatch):
    slept = []
    monkeypatch.setattr("src.base_fetcher.time.sleep", lambda s: slept.append(s))
    h = HttpSession()
    with pytest.raises(requests.HTTPError):
        h.get(plain_server + "/503")
    assert _Handler.hits.count("/503") == 3
    assert len(slept) == 2                    # niente sleep DOPO l'ultimo tentativo


def test_dns_error_is_not_retried(monkeypatch):
    """Un host che non risolve (DNS) fallisce SUBITO, senza consumare i retry né dormire tra un
    tentativo e l'altro: un NXDOMAIN non è transitorio, ritentarlo è solo tempo perso e log in più."""
    slept = []
    monkeypatch.setattr("src.base_fetcher.time.sleep", lambda s: slept.append(s))
    calls = []

    def fake_get(self, url, timeout=None, stream=False, verify=True, **kwargs):
        calls.append(url)
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='nonexistent.invalid', port=443): Max retries exceeded "
            "(Caused by NameResolutionError(\"Failed to resolve 'nonexistent.invalid' "
            "([Errno 11001] getaddrinfo failed)\"))"
        )

    monkeypatch.setattr(requests.Session, "get", fake_get)
    h = HttpSession()
    with pytest.raises(requests.exceptions.ConnectionError):
        h.get("https://nonexistent.invalid/", retries=3)
    assert len(calls) == 1 and slept == []


def test_max_bytes_and_skip_non_html(plain_server):
    h = HttpSession()
    r = h.get(plain_server + "/big", max_bytes=100_000)
    assert len(r.content) == 100_000 and r.truncated is True
    r = h.get(plain_server + "/pdf", skip_non_html=True)
    assert r.content == b"" and "pdf" in r.headers["Content-Type"]


def test_encoding_fixed_when_charset_missing(plain_server):
    h = HttpSession()
    assert "Città però Ægir, più università e caffè." in h.get(plain_server + "/latin1").text   # niente mojibake
    assert "Łódź ŻÓŁW" in h.get(plain_server + "/metacs").text                # <meta charset> rispettato


def test_download_to_file_streaming(plain_server, tmp_path):
    h = HttpSession()
    n = h.download_to_file(plain_server + "/big", tmp_path / "d" / "f.bin")
    assert n == 500_013 and (tmp_path / "d" / "f.bin").stat().st_size == n
    assert not list((tmp_path / "d").glob("*.part"))


def test_tls_fallback_flags_error(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{srv.server_address[1]}/"
    try:
        h = HttpSession()
        with pytest.raises(requests.exceptions.SSLError):
            h.get(url)                                          # dati ufficiali: verifica SEMPRE attiva
        r = h.get(url, allow_insecure_fallback=True)            # discovery: fallback + flag
        assert r.status_code == 200 and r.tls_error is True
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- (18) CSV
def test_decode_with_fallback_cp1250():
    raw = "nazwa;miasto\nUrząd Miejski;Łódź\n".encode("cp1250")
    text, used = _decode_with_fallback(raw, "utf-8-sig", "test")
    assert "Łódź" in text and "\ufffd" not in text


# ---------------------------------------------------------- (21) ApiJson
def _api_fetcher(tmp_path, cfg, responder):
    class A(ApiJsonFetcher):
        country_code = "XX"; source_name = "t"
    a = A({"source_url": "http://x", **cfg}, tmp_path / "i", tmp_path / "o")
    class FakeHttp:
        def get(self, url, params=None, **kw):
            class R:
                def json(_): return responder(params)
            return R()
    a.http = FakeHttp()
    return a


def test_api_pagination_not_truncated_when_server_page_size_is_smaller(tmp_path):
    data = list(range(55))
    def responder(params):
        p = params["page"]; return {"items": [{"id": i} for i in data[(p - 1) * 20: p * 20]]}
    a = _api_fetcher(tmp_path, {"paginate": True, "records_path": "items", "page_size": 100}, responder)
    out = a.download()
    assert len(json.loads(out.read_text())) == 55         # prima: si fermava a 20


def test_api_ignoring_page_param_stops(tmp_path, caplog):
    a = _api_fetcher(tmp_path, {"paginate": True, "records_path": "items", "max_pages": 50}, lambda p: {"items": [{"id": 1}, {"id": 2}]})
    with caplog.at_level("WARNING"):
        out = a.download()
    assert len(json.loads(out.read_text())) == 2 and "identica" in caplog.text


# ------------------------------------------------------------- (25) atomic csv
def test_atomic_csv_survives_locked_destination(tmp_path, monkeypatch):
    dest = tmp_path / "siti.csv"
    dest.write_text("vecchio", encoding="utf-8")
    real_replace = os.replace
    def locked(src, dst, *a, **k):
        if str(dst) == str(dest):
            raise PermissionError("file aperto in Excel")
        return real_replace(src, dst)
    monkeypatch.setattr("src.schema.os.replace", locked)
    monkeypatch.setattr("src.schema.time.sleep", lambda s: None)
    n = atomic_write_csv([{"hostname": "https://a.be"}], FIELDNAMES, dest)
    assert n == 1 and dest.read_text(encoding="utf-8") == "vecchio"     # l'originale non è stato toccato
    assert list(tmp_path.glob("siti.*.csv")), "deve esistere il salvataggio alternativo"


# --------------------------------------------------------- web_signals (14)(15)
def test_web_signals():
    assert detect_challenge("<html><head><title>Just a moment...</title></head>") is not None
    assert detect_challenge("<html><title>Comune di X</title>" + "testo " * 2000 + "captcha nel form</html>") is None
    assert looks_js_rendered("<html><body><div id='root'></div><script src=a.js></script></body></html>")
    assert not looks_js_rendered("<html><body>" + "contenuto " * 100 + "<div id='root'></div></body></html>")
    assert detect_parked("<html>This domain is for sale</html>")
    assert detect_default_page("<html><title>Welcome to nginx!</title></html>")
    assert is_infrastructure("www.google.de") and is_infrastructure("bit.ly") and is_infrastructure("cdn.jsdelivr.net")
    assert not is_infrastructure("www.comune.milano.it")


# ------------------------------------------------------------ (24) DDG/wikidata
def test_ddg_unwrap_and_name_match():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.sevilla.org%2F&rut=abc"
    assert wf._ddg_unwrap(href) == "https://www.sevilla.org/"
    assert wf.is_plausible_official(wf._ddg_unwrap(href))
    assert wf.name_match_score("Ayuntamiento de Sevilla", "https://www.sevilla.org/") == 1.0
    assert wf.name_match_score("Junta de Andalucía", "https://www.juntadeandalucia.es/") == 1.0
    assert wf.name_match_score("Ayuntamiento de Sevilla", "https://www.madrid.es/") == 0.0
    assert wf.name_match_score("Secretaría General", "https://www.qualcosa.es/") == 1.0     # nessun token distintivo


def test_find_official_website_prefers_matching_candidate(monkeypatch):
    monkeypatch.setitem(wf.SEARCH_BACKENDS, "fake", lambda q, s, max_results=5: [
        "https://www.linkedin.com/x", "https://www.madrid.es/", "https://www.sevilla.org/"])
    assert wf.find_official_website("Ayuntamiento de Sevilla", None, backend="fake") == "https://www.sevilla.org/"


def test_search_blocked_is_detected_and_aborts(monkeypatch, tmp_path):
    class R:
        status_code = 200; text = "<html>anomaly-modal: unfortunately, bots use DuckDuckGo too</html>"
        def raise_for_status(self): pass
    class S:
        def get(self, *a, **k): return R()
    with pytest.raises(wf.SearchBlocked):
        wf.search_duckduckgo("q", S())
    recs = [Record(nome_ente=f"Ente {i}") for i in range(20)]
    monkeypatch.setattr(wf.time, "sleep", lambda s: None)
    monkeypatch.setattr(wf, "find_official_website", lambda *a, **k: (_ for _ in ()).throw(wf.SearchBlocked("x")))
    _, n = wf.enrich_records_with_website(recs, max_consecutive_blocks=3)
    assert n == 3                                          # si ferma, non consuma tutte le 20 ricerche


def test_enrich_cache_and_checkpoint(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(wf.time, "sleep", lambda s: None)
    monkeypatch.setattr(wf, "find_official_website", lambda name, *a, **k: calls.append(name) or f"https://{name.lower()}.es")
    saves = []
    recs = [Record(nome_ente="Uno"), Record(nome_ente="Due"), Record(nome_ente="Uno")]
    cache = tmp_path / "c.json"
    wf.enrich_records_with_website(recs, cache_path=cache, checkpoint_cb=lambda r: saves.append(len(r)), checkpoint_every=1, sleep_seconds=0)
    assert calls == ["Uno", "Due"]                        # il nome ripetuto usa la cache
    assert recs[2].hostname == "https://uno.es" and saves
    calls.clear()
    recs2 = [Record(nome_ente="Uno"), Record(nome_ente="Due")]
    wf.enrich_records_with_website(recs2, cache_path=cache, sleep_seconds=0)
    assert calls == [] and recs2[1].hostname == "https://due.es"      # run successivo: zero ricerche


# ------------------------------------------------------------- (28) redact
def test_redact_key_in_error_messages():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import threat_check
    msg = "403 Client Error: Forbidden for url: https://safebrowsing.googleapis.com/v4/threatMatches:find?key=AIzaSECRET123&x=1"
    red = threat_check._redact(msg)
    assert "AIzaSECRET123" not in red and "key=***" in red and "x=1" in red


def test_decode_uses_country_hint_on_tiny_files():
    raw = "Nome;Web\nUrząd Miejski w Łodzi;www.uml.lodz.pl\n".encode("cp1250")
    text, used = _decode_with_fallback(raw, "utf-8-sig", "test", "PL")
    assert "Urząd Miejski w Łodzi" in text and used == "cp1250"
