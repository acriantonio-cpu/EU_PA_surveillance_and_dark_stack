# EN: Crawler engine tests: robots.txt, per-host concurrency, User-Agent, incremental SQLite
# checkpoint.
"""Motore del crawler: robots.txt, concorrenza per host, User-Agent, checkpoint incrementale SQLite."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.crawl_store import CrawlStore
from src.fetchers.generic import HostScheduler, SiteCrawlFetcher
from src.polite import RobotsCache, identified_user_agent

from test_crawler_local import BASE_CFG, make_fetcher


def cfg(web, **extra):
    return {**BASE_CFG, "crawl_depth": 2, "seed_urls": [web.url("alpha.be")], **extra}


def site(web, pages=("p1", "p2")):
    web.add("alpha.be", "/", "<html><body>" + "".join(f"<a href='/{p}'>x</a>" for p in pages) +
            "<a href='/private/secret'>s</a><a href='/files/doc.pdf'>d</a></body></html>")
    for p in pages:
        web.add("alpha.be", f"/{p}", f"<html><body>{p}</body></html>")
    web.add("alpha.be", "/private/secret", "<html><body>segreto <a href='http://leak.example.com'>l</a></body></html>")


# ------------------------------------------------------------------ robots.txt
def test_robots_respected_by_default_and_flag_disables(web, tmp_path):
    site(web)
    web.add("alpha.be", "/robots.txt", "User-agent: *\nDisallow: /private\n", **{"Content-Type": "text/plain"})
    f = make_fetcher(tmp_path / "a", cfg(web))
    rows = {r["external_domain"] for r in json.loads(f.download().read_text(encoding="utf-8"))}
    assert "/private/secret" not in web.hit_paths("alpha.be") and "example.com" not in rows
    assert "/robots.txt" in web.hit_paths("alpha.be")
    errs = json.loads((tmp_path / "a" / "in" / "crawl_checkpoint.json").read_text(encoding="utf-8"))["error_log"]
    assert any(e["error_type"] == "RobotsDisallowed" for e in errs)

    web.hits.clear()
    f2 = make_fetcher(tmp_path / "b", cfg(web, respect_robots=False))
    rows2 = {r["external_domain"] for r in json.loads(f2.download().read_text(encoding="utf-8"))}
    assert "/private/secret" in web.hit_paths("alpha.be") and "example.com" in rows2
    assert "/robots.txt" not in web.hit_paths("alpha.be")            # con il flag OFF non viene nemmeno letto


def test_robots_wildcards_and_missing_robots(web, tmp_path):
    site(web)
    web.add("alpha.be", "/robots.txt", "User-agent: *\nDisallow: /*.pdf$\nDisallow: /p2\n", **{"Content-Type": "text/plain"})
    web.add("alpha.be", "/files/doc.pdf", "%PDF", **{"Content-Type": "application/pdf"})
    make_fetcher(tmp_path, cfg(web)).download()
    paths = web.hit_paths("alpha.be")
    assert "/p1" in paths and "/p2" not in paths


def test_robots_server_error_blocks_host_but_404_allows(web, tmp_path):
    site(web)
    web.add("alpha.be", "/robots.txt", "boom", status=503)
    make_fetcher(tmp_path / "a", cfg(web)).download()
    assert web.hit_paths("alpha.be").count("/p1") == 0                # 5xx: host escluso per prudenza (RFC 9309)
    web.hits.clear()
    web.routes.pop(("alpha.be", "/robots.txt"))                       # 404: consentito
    make_fetcher(tmp_path / "b", cfg(web)).download()
    assert "/p1" in web.hit_paths("alpha.be")


def test_crawl_delay_is_honoured(web, tmp_path):
    site(web, pages=("p1", "p2"))
    web.add("alpha.be", "/robots.txt", "User-agent: *\nCrawl-delay: 1\n", **{"Content-Type": "text/plain"})
    make_fetcher(tmp_path, cfg(web, sleep_seconds=0, crawl_depth=2)).download()
    times = [t for t, h, p, _ in web.hit_log if h == "alpha.be" and p != "/robots.txt"]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 0.85, gaps


def test_robots_cache_unit():
    calls = []
    def fetch(url):
        calls.append(url)
        return 200, "User-agent: *\nDisallow: /a/*/x$\nCrawl-delay: 99\nUser-agent: eu-pa-scraper\nDisallow: /only-us\n"
    rc = RobotsCache(fetch, max_crawl_delay=10)
    assert not rc.check("https://x.be/only-us").allowed and rc.check("https://x.be/a/b/c").allowed
    assert rc.check("https://x.be/ok").crawl_delay == 0 or rc.check("https://x.be/ok").crawl_delay <= 10
    rc.check("https://x.be/again")
    assert calls == ["https://x.be/robots.txt"]                        # una sola richiesta per origine
    assert RobotsCache(lambda u: (404, ""), ).check("https://y.be/anything").allowed
    assert RobotsCache(lambda u: (None, "")).check("https://y.be/anything").allowed          # rete giù: consentito
    assert not RobotsCache(lambda u: (500, "")).check("https://y.be/anything").allowed


# ------------------------------------------------------------ User-Agent
def test_user_agent_identified_or_browser(web, tmp_path, monkeypatch):
    site(web)
    monkeypatch.setenv("SCRAPER_CONTACT", "osservatorio@example.org")
    make_fetcher(tmp_path / "a", cfg(web, crawl_depth=1)).download()
    uas = {ua for *_, ua in web.hit_log}
    assert uas == {"eu-pa-scraper/1.0 (+osservatorio@example.org)"}
    web.hit_log.clear()
    make_fetcher(tmp_path / "b", cfg(web, crawl_depth=1, identify_crawler=False, respect_robots=False)).download()
    assert all("Mozilla" in ua for *_, ua in web.hit_log)
    assert identified_user_agent("a@b.it") == "eu-pa-scraper/1.0 (+a@b.it)"


# --------------------------------------------------------- concorrenza (#20)
def multi_host_web(web, n_hosts=6, pages=3):
    web.add("alpha.be", "/", "<html><body>" + "".join(f"<a href='http://h{i}.be:{web.port}/'>h</a>" for i in range(n_hosts)) + "</body></html>")
    for i in range(n_hosts):
        web.add(f"h{i}.be", "/", "<html><body>" + "".join(f"<a href='/q{j}'>q</a>" for j in range(pages)) + "</body></html>")
        for j in range(pages):
            web.add(f"h{i}.be", f"/q{j}", f"<html><body>page {i}-{j}</body></html>")


def test_workers_speed_up_the_crawl_without_changing_the_result(web, tmp_path):
    multi_host_web(web)
    web.latency = 0.1
    t0 = time.monotonic()
    r1 = json.loads(make_fetcher(tmp_path / "s", cfg(web, workers=1, sleep_seconds=0, crawl_depth=3)).download().read_text(encoding="utf-8"))
    t_seq = time.monotonic() - t0
    web.hits.clear()
    t0 = time.monotonic()
    r6 = json.loads(make_fetcher(tmp_path / "p", cfg(web, workers=6, sleep_seconds=0, crawl_depth=3)).download().read_text(encoding="utf-8"))
    t_par = time.monotonic() - t0
    assert {r["external_domain"] for r in r1} == {r["external_domain"] for r in r6}
    assert t_par < t_seq * 0.6, (t_seq, t_par)
    for i in range(6):                                                 # ogni pagina scaricata UNA sola volta
        paths = [p for p in web.hit_paths(f"h{i}.be") if p != "/robots.txt"]
        assert len(paths) == len(set(paths)) == 4


def test_per_host_politeness_is_kept_with_many_workers(web, tmp_path):
    site(web, pages=("p1", "p2", "p3"))
    make_fetcher(tmp_path, cfg(web, workers=8, sleep_seconds=0.3, respect_robots=False)).download()
    times = [t for t, h, p, _ in web.hit_log if h == "alpha.be"]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 4 and min(gaps) >= 0.25, gaps                 # mai due richieste ravvicinate allo stesso host


def test_host_scheduler():
    s = HostScheduler(["http://a.be/1", "http://a.be/2", "http://b.be/1"], lambda u: u.split("/")[2])
    assert len(s) == 3
    order = [s.pop_ready(lambda h: True) for _ in range(3)]
    assert order == ["http://a.be/1", "http://b.be/1", "http://a.be/2"]        # round-robin tra host
    assert s.pop_ready(lambda h: True) is None and len(s) == 0
    s.push("http://a.be/9")
    assert s.pop_ready(lambda h: False) is None and len(s) == 1                # host non pronto: resta in coda
    assert s.all_urls() == ["http://a.be/9"]


# ------------------------------------------------------- checkpoint (#19)
def test_checkpoint_is_incremental_not_rewritten_per_page(web, tmp_path, monkeypatch):
    multi_host_web(web, n_hosts=4, pages=4)
    exports = []
    orig = CrawlStore.export_json
    monkeypatch.setattr(CrawlStore, "export_json", lambda self, path: (exports.append(1), orig(self, path))[1])
    f = make_fetcher(tmp_path, cfg(web, crawl_depth=3, sleep_seconds=0))
    f.download()
    n_pages = len({(h, p) for h, p in web.hits if p != "/robots.txt"})
    assert n_pages >= 20
    assert len(exports) <= 4 + 1                                          # a fine livello/fine crawl, NON una per pagina
    state = tmp_path / "in" / "crawl_state.sqlite"
    assert state.exists()
    legacy = json.loads((tmp_path / "in" / "crawl_checkpoint.json").read_text(encoding="utf-8"))
    assert legacy["completed"] is True and len(legacy["visited"]) >= 20 and "domain_registry" in legacy and "resource_index" in legacy


def test_crawl_store_roundtrip(tmp_path):
    st = CrawlStore(tmp_path / "s.sqlite")
    st.set_meta(fingerprint="fp", fingerprint_fields={"a": 1}, completed=False, level=1)
    st.begin_level(2, ["u1", "u2", "u3"])
    st.add_visited("u2"); st.add_next_level("n1"); st.add_next_level("n1"); st.add_origin("u1", "seed")
    st.put_registry("x.be", {"external_domain": "x.be"}); st.add_resource("cdn.com", {"pagina": "p", "tipo": "script"})
    st.bump_pages_seed("seed", 2); st.bump_pages_seed("seed", 2); st.add_error({"url": "e"}); st.add_template("t")
    st.commit(); st.close()
    st2 = CrawlStore(tmp_path / "s.sqlite")
    d = st2.load()
    assert d["fingerprint"] == "fp" and d["level"] == 2
    assert d["queue"] == ["u1", "u3"]                                     # coda del livello MENO i visitati, in ordine
    assert d["next_level"] == ["n1"] and d["domain_registry"]["x.be"]["external_domain"] == "x.be"
    assert d["pages_by_seed_level"] == {"seed": {"2": 2}} and d["resource_index"]["cdn.com"][0]["tipo"] == "script"
    assert d["page_origin_seed"] == {"u1": "seed"} and d["error_log"] == [{"url": "e"}] and d["pagination_templates_seen"] == ["t"]
    st2.close()


def test_resume_from_legacy_json_checkpoint_migrates(web, tmp_path):
    site(web)
    f = make_fetcher(tmp_path, cfg(web))
    seeds = f._load_seeds()
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    legacy = {
        "fingerprint": f._checkpoint_fingerprint(seeds), "fingerprint_fields": f._checkpoint_fingerprint_fields(seeds),
        "level": 1, "queue": seeds, "visited": [], "next_level": [], "pagination_templates_seen": [],
        "page_origin_seed": {seeds[0]: seeds[0]}, "pages_by_seed_level": {}, "domain_consecutive_errors": {},
        "domain_cooldown_until_call": {}, "domain_cooldown_retries_used": {}, "global_call_index": 0, "error_log": [],
        "domain_registry": {"alpha.be": {"external_url": "https://alpha.be", "external_domain": "alpha.be", "found_on": seeds[0],
                                        "level": 0, "scope": "seed", "source_seed": seeds[0]}},
        "completed": False, "saved_at": "2026-01-01T00:00:00+00:00",
    }
    (tmp_path / "in" / "crawl_checkpoint.json").write_text(json.dumps(legacy), encoding="utf-8")
    f2 = make_fetcher(tmp_path, cfg(web))
    f2.resume = True
    rows = json.loads(f2.download().read_text(encoding="utf-8"))
    assert {r["external_domain"] for r in rows} >= {"alpha.be", "example.com"} or "/p1" in web.hit_paths("alpha.be")
    assert (tmp_path / "in" / "crawl_state.sqlite").exists()


# ---------------------------------------------------- classificatore + robots
def test_classifier_respects_robots_by_default(web, monkeypatch):
    from src.classify import stage3_homepage as s3
    from src.classify.lexicon import load_lexicon
    monkeypatch.setattr(s3, "_candidate_urls", lambda hosts, n: [f"http://{h}:{web.port}/" for h in hosts[:2]])
    web.add("gemeente-z.nl", "/", "<html><head><title>Gemeente Z</title></head><body>gemeente</body></html>")
    web.add("gemeente-z.nl", "/robots.txt", "User-agent: *\nDisallow: /\n", **{"Content-Type": "text/plain"})
    lex = load_lexicon()
    assert s3.DEFAULT_CFG["respect_robots"] is True
    cfgd = {"per_host_delay": 0}
    ev = s3.collect_evidence(["gemeente-z.nl"], lex, cfgd, s3.HostLimiter(0), s3.make_robots(cfgd))
    assert ev["status"] == "robots" and "/" not in [p for p in web.hit_paths("gemeente-z.nl") if p != "/robots.txt"]
    web.hits.clear()
    cfgd2 = {"per_host_delay": 0, "respect_robots": False}
    ev2 = s3.collect_evidence(["gemeente-z.nl"], lex, cfgd2, s3.HostLimiter(0), s3.make_robots(cfgd2))
    assert ev2["status"] == "ok" and "/robots.txt" not in web.hit_paths("gemeente-z.nl")


def test_all_seeds_blocked_by_robots_is_reported_loudly(web, tmp_path, caplog):
    site(web)
    web.add("alpha.be", "/robots.txt", "User-agent: *\nDisallow: /\n", **{"Content-Type": "text/plain"})
    with caplog.at_level("ERROR"):
        rows = json.loads(make_fetcher(tmp_path, cfg(web)).download().read_text(encoding="utf-8"))
    assert [r["external_domain"] for r in rows] == ["alpha.be"]           # solo il seed registrato, nessuna scoperta
    assert "TUTTI i seed sono vietati da robots.txt" in caplog.text and "respect_robots: false" in caplog.text
    assert web.hit_paths("alpha.be") == ["/robots.txt"]                    # nessuna pagina del sito è stata richiesta


def test_corrupt_state_file_is_set_aside_not_fatal(web, tmp_path, caplog):
    site(web)
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    (tmp_path / "in" / "crawl_state.sqlite").write_bytes(b"questo non e' un database sqlite" * 50)
    with caplog.at_level("ERROR"):
        rows = json.loads(make_fetcher(tmp_path, cfg(web)).download().read_text(encoding="utf-8"))
    assert "corrotto" in caplog.text and list((tmp_path / "in").glob("crawl_state.sqlite.corrupt-*"))
    assert "/p1" in web.hit_paths("alpha.be") and rows


def test_contact_is_sanitised_for_headers():
    ua = identified_user_agent("mario@rossi.it\r\nX-Evil: 1 — è")
    assert "\r" not in ua and "\n" not in ua and ua.isascii()


def test_belgium_config_end_to_end_to_csv(web, tmp_path, monkeypatch):
    """Percorso completo come da main.py: config reale BE.yaml -> crawl -> parse -> normalize -> CSV."""
    import csv
    from src import registry
    from src.schema import write_csv

    monkeypatch.setattr(registry, "DATA_INPUT_DIR", tmp_path / "input")
    monkeypatch.setattr(registry, "DATA_OUTPUT_DIR", tmp_path / "output")
    (tmp_path / "input" / "BE").mkdir(parents=True)
    (tmp_path / "input" / "BE" / "seeds.txt").write_text(web.url("belgium.be") + "\n", encoding="utf-8")
    web.add("belgium.be", "/", f"<html><body><a href='http://finances.belgium.be:{web.port}/'>f</a><a href='http://ext.example.com:{web.port}/'>e</a></body></html>")
    web.add("finances.belgium.be", "/", "<html><body>fin</body></html>")
    fetcher = registry.build_fetcher("BE")
    fetcher.config.update({"crawl_depth": 2, "sleep_seconds": 0, "allowed_domains": ["belgium.be"]})
    records = fetcher.run()
    out = tmp_path / "output" / "BE" / "siti.csv"
    write_csv(records, out)
    rows = {r["dominio_radice"]: r for r in csv.DictReader(out.open(encoding="utf-8-sig"))}
    assert set(rows) >= {"belgium.be", "example.com"}
    assert rows["belgium.be"]["tipo_link"] == "seed" and rows["example.com"]["tipo_link"] == "esterno"
    assert "finances.belgium.be" in rows["belgium.be"]["host_osservati"]
    assert rows["example.com"]["found_on"].startswith("http://belgium.be") and rows["example.com"]["hostname"] == "https://example.com"
    assert all(r["sector"] == "" and r["codice_ipa"] == "" for r in rows.values())        # colonne non più riusate dal crawler
