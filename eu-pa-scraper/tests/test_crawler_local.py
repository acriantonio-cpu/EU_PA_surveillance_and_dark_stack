"""Test end-to-end del crawler contro un web finto locale (nessuna rete reale)."""

from __future__ import annotations

import json
from pathlib import Path

from src.fetchers.generic import SiteCrawlFetcher


def make_fetcher(tmp_path: Path, config: dict, country="BE"):
    class F(SiteCrawlFetcher):
        pass

    F.country_code = country
    F.source_name = "test"
    return F(config, tmp_path / "in", tmp_path / "out")


BASE_CFG = {
    "crawl_depth": 3,
    "max_pages_per_level": 100,
    "sleep_seconds": 0,
    "record_internal_links": True,
    "exclude_domains": [],
    "allowed_domains": ["alpha.be"],
    "allowed_tlds": ["be", "gov"],
    "domain_cooldown_calls": 3,
    "js_sitemap_fallback": True,
}


def test_crawl_end_to_end(web, tmp_path):
    P = web.port
    home = f"""<html><head><title>Alpha</title></head><body>
    <a href="/p#a">1</a><a href="/p#b">2</a><a href="/p/">3</a><a href="/p?utm_source=x">4</a>
    <a href="/dead">dead</a><a href="/challenge">c</a><a href="/js">js</a><a href="/redir">r</a>
    <a href="/doc.pdf">pdf</a><a href="mailto:x@y.be">m</a>
    <a href="http://www.beta.com:{P}/">beta</a>
    <a href="http://de.example.com:{P}/">de.example</a>
    <a href="http://gov.evil.com:{P}/">gov.evil</a>
    <a href="https://bit.ly/abc">short</a>
    <a href="https://www.facebook.com/x">fb</a>
    <a href="https://www.google.de/maps">google.de</a>
    <a href="http://city.brussels:{P}/">brussels</a>
    <a href="http://[bad-ipv6/x">bad</a>
    <a href="http://127.0.0.1:{P}/ip">ip</a>
    </body></html>"""
    web.add("alpha.be", "/", home)
    web.add("alpha.be", "/p", "<html><body><a href='/p2'>x</a></body></html>")
    web.add("alpha.be", "/challenge",
            "<html><head><title>Just a moment...</title></head><body>"
            "<a href='https://www.cloudflare.com/x'>cf</a><script src='/cdn-cgi/challenge-platform/x'></script></body></html>")
    web.add("alpha.be", "/js", "<html><body><div id='app'></div><noscript>Please enable JavaScript</noscript></body></html>")
    web.add("alpha.be", "/sitemap.xml",
            f"<urlset><url><loc>http://alpha.be:{P}/from-sitemap</loc></url></urlset>", **{"Content-Type": "application/xml"})
    web.add("alpha.be", "/from-sitemap", "<html><body>ok <a href='http://sitemap-found.be:%d/'>s</a></body></html>" % P)
    web.add("alpha.be", "/redir", "", status=302, Location=f"http://gamma.be:{P}/landing")
    web.add("gamma.be", "/landing", f"<html><body><a href='rel-page'>rel</a><a href='http://delta.be:{P}/'>d</a></body></html>")
    web.add("gamma.be", "/rel-page", "<html><body>rel</body></html>")
    web.add("city.brussels", "/", "<html><body>brussels</body></html>")

    f = make_fetcher(tmp_path, {**BASE_CFG, "seed_urls": [web.url("alpha.be")]})
    out = f.download()
    rows = {r["external_domain"]: r for r in json.loads(out.read_text(encoding="utf-8"))}
    hits_alpha = web.hit_paths("alpha.be")

    # (7) /p raggiunto una volta sola, nonostante #a/#b, slash finale e utm
    assert sum(1 for p in hits_alpha if p.split("?")[0].rstrip("/") == "/p") == 1
    # (11) un link morto NON viene ritentato 3 volte
    assert hits_alpha.count("/dead") == 1
    # (14) la pagina challenge non registra cloudflare.com né la usa come pagina valida
    assert "cloudflare.com" not in rows
    # (15) sito JS: usa la sitemap e ne segue i link
    assert "/from-sitemap" in hits_alpha
    assert "sitemap-found.be" in rows
    # (16) redirect: dominio finale registrato/esplorato, link relativi risolti sull'URL FINALE
    assert "gamma.be" in rows
    assert "/rel-page" in web.hit_paths("gamma.be")
    assert "delta.be" in rows
    # (2) label ccTLD/gov in posizione qualsiasi NON rende interno un dominio
    assert "example.com" in rows and "evil.com" in rows          # registrati come esterni
    assert web.hit_paths("de.example.com") == []                 # ma NON esplorati
    assert web.hit_paths("gov.evil.com") == []
    # (17) TLD regionale .brussels considerato interno (esplorato)
    assert "/" in web.hit_paths("city.brussels")
    # blocklist infrastrutturale / social / google.<tld>
    for junk in ("bit.ly", "facebook.com", "google.de"):
        assert junk not in rows
    # (8) IP letterale mai registrato come dominio; link malformato non rompe nulla
    assert not any(k.replace(".", "").isdigit() for k in rows)
    # (9) FQDN osservati conservati
    assert "www.beta.com" in rows["beta.com"]["observed_hosts"]
    assert rows["alpha.be"]["scope"] == "seed"
    # PDF mai scaricato
    assert "/doc.pdf" not in hits_alpha
    # flag di supporto
    assert rows["alpha.be"].get("needs_js") in (False, True)


def test_parse_flattens_lists(web, tmp_path):
    web.add("alpha.be", "/", "<html><body><a href='http://b.example.be:%d/'>x</a><a href='http://c.example.be:%d/'>y</a></body></html>" % (web.port, web.port))
    f = make_fetcher(tmp_path, {**BASE_CFG, "crawl_depth": 1, "seed_urls": [web.url("alpha.be")]})
    rows = f.parse(f.download())
    ex = [r for r in rows if r["external_domain"] == "example.be"][0]
    assert isinstance(ex["observed_hosts"], str)
    assert set(ex["observed_hosts"].split("|")) == {"b.example.be", "c.example.be"}
    assert ex["tls_error"] == ""


def test_internal_bug_is_not_masked_and_aborts(web, tmp_path, monkeypatch):
    """Un bug di programmazione (non un errore di rete) non deve più essere scambiato per
    'errore di rete' su ogni pagina: dopo N errori interni di fila il crawl si ferma."""
    import pytest
    from src.base_fetcher import HttpSession

    web.add("alpha.be", "/", "<html><body>" + "".join(f"<a href='/p{i}'>x</a>" for i in range(30)) + "</body></html>")
    for i in range(30):
        web.add("alpha.be", f"/p{i}", "<html><body>ok</body></html>")
    calls = {"n": 0}
    orig = HttpSession.get

    def broken(self, url, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise TypeError("firma cambiata")
        return orig(self, url, **kw)

    monkeypatch.setattr(HttpSession, "get", broken)
    f = make_fetcher(tmp_path, {**BASE_CFG, "seed_urls": [web.url("alpha.be")], "max_internal_errors_in_a_row": 5, "respect_robots": False})
    with pytest.raises(RuntimeError, match="errori interni consecutivi"):
        f.download()


def test_round_robin_selection_and_logging(caplog, tmp_path):
    f = make_fetcher(tmp_path, {**BASE_CFG, "seed_urls": ["http://x.be"]})
    urls = [f"http://a.be/{i}" for i in range(50)] + [f"http://b.be/{i}" for i in range(5)] + [f"http://c.be/{i}" for i in range(5)]
    with caplog.at_level("WARNING"):
        sel = f._select_for_level(urls, 12, 2)
    hosts = [u.split("/")[2] for u in sel]
    assert len(sel) == 12
    assert hosts.count("a.be") == hosts.count("b.be") == hosts.count("c.be") == 4   # nessun host monopolizza il livello
    assert "scartate" in caplog.text


def test_interrupt_and_resume(web, tmp_path, monkeypatch):
    """Ctrl+C a metà, poi --resume: nessuna pagina già scaricata viene riscaricata."""
    import pytest
    from src.base_fetcher import HttpSession

    web.add("alpha.be", "/", "<html><body>" + "".join(f"<a href='/p{i}'>x</a>" for i in range(6)) + "<a href='http://ext.example.com:%d/'>e</a></body></html>" % web.port)
    for i in range(6):
        web.add("alpha.be", f"/p{i}", f"<html><body>page {i} <a href='http://n{i}.example.be:{web.port}/'>n</a></body></html>")
    cfg = {**BASE_CFG, "crawl_depth": 2, "seed_urls": [web.url("alpha.be")]}

    calls = {"n": 0}
    orig = HttpSession.get

    def flaky(self, url, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt
        return orig(self, url, **kw)

    monkeypatch.setattr(HttpSession, "get", flaky)
    f1 = make_fetcher(tmp_path, cfg)
    with pytest.raises(KeyboardInterrupt):
        f1.download()
    assert (tmp_path / "in" / "crawl_checkpoint.json").exists()
    fetched_before = set(web.hit_paths("alpha.be"))
    monkeypatch.setattr(HttpSession, "get", orig)
    web.hits.clear()

    f2 = make_fetcher(tmp_path, cfg)
    f2.resume = True
    out = f2.download()
    rows = {r["external_domain"] for r in json.loads(out.read_text(encoding="utf-8"))}
    assert {f"example.be", "example.com"} <= rows
    refetched = [p for p in web.hit_paths("alpha.be") if p in fetched_before and p != "/"]
    assert refetched == [] or len(refetched) <= 1      # al più la pagina in corso al momento dell'interruzione
