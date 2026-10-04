# EN: Stage 3 - homepage: collect an evidence bundle (title, meta, h1, menu, footer, text) and
# score it with weighted multilingual rules; explicit page states (ok, needs_js, blocked,
# parked, ...).
"""Stadio 3 - homepage: raccolta di un "pacchetto di evidenza" + regole pesate multilingue.

La raccolta (collect_evidence) è separata dalla valutazione (score_evidence): le evidenze
vengono messe in cache e RIVALUTATE a ogni run, così si può migliorare il lessico senza
riscaricare nulla. Lo stadio 4 (LLM) legge lo stesso pacchetto.

Stati della homepage (mai indovinare quando il sito non è leggibile):
  ok | needs_js | blocked | parked | default_page | timeout | dns_error | tls_error |
  http_error | non_html | empty | robots   (robots = vietata da robots.txt, se respect_robots=true)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin

import requests

from ..base_fetcher import HttpSession
from ..polite import ROBOTS_AGENT, RobotsCache, identified_user_agent, make_fetch_text, warn_if_no_contact
from ..urlutil import host_of
from ..web_signals import detect_challenge, detect_default_page, detect_parked, looks_js_rendered, visible_text_length
from .lexicon import Lexicon
from .taxonomy import PUBLIC_BY_NATURE, Vote, squash
from .textnorm import distinct_matches, fold

logger = logging.getLogger(__name__)

SCORABLE_STATUS = frozenset({"ok", "needs_js"})
_COOKIE_RE = re.compile(
    r"cookie|gdpr|onetrust|cookiebot|didomi|usercentrics|trustarc|iubenda|cc-window|cc_banner|consent-banner|consentbanner",
    re.IGNORECASE,
)
_STATUS_PRIORITY = ["robots", "blocked", "timeout", "tls_error", "http_error", "dns_error"]

DEFAULT_CFG = {
    "max_bytes": 400_000,
    "read_timeout": 10.0,
    "max_attempts": 5,
    "per_host_delay": 1.5,
    "fetch_legal_page": True,
    "legal_page_below": 0.6,
    "respect_robots": True,          # legge e rispetta robots.txt (e Crawl-delay) prima di aprire la homepage
    "identify_crawler": True,        # User-Agent identificabile (eu-pa-scraper/1.0 (+contatto)); false = header da browser
    "contact": None,                 # email/URL nell'User-Agent (o variabile d'ambiente SCRAPER_CONTACT)
    "max_crawl_delay": 30.0,
    "body_chars": 1200,
    "user_agent": None,              # se valorizzato e identify_crawler=false, User-Agent personalizzato
}


# --------------------------------------------------------------------------
# Rete: sessione per thread, limite per host, robots (opzionale)
# --------------------------------------------------------------------------
_local = threading.local()


def _thread_http(cfg: dict) -> HttpSession:
    h = getattr(_local, "http", None)
    if h is None:
        headers = None
        if cfg.get("identify_crawler", True):
            headers = {"User-Agent": identified_user_agent(cfg.get("contact"))}
        elif cfg.get("user_agent"):
            headers = {"User-Agent": cfg["user_agent"]}
        h = _local.http = HttpSession(headers=headers, timeout=15)
    return h


class HostLimiter:
    """Al più una richiesta ogni 'delay' secondi verso lo stesso host, anche con più thread. Il ritardo
    di un host può essere alzato (Crawl-delay di robots.txt) con set_min_delay()."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}

    def set_min_delay(self, host: str, delay: float) -> None:
        if delay and delay > self.delay:
            with self._lock:
                self._host_delay[host] = delay

    def wait(self, host: str) -> None:
        with self._lock:
            d = max(self.delay, self._host_delay.get(host, 0.0))
            if d <= 0:
                return
            now = time.monotonic()
            at = max(now, self._next.get(host, 0.0))
            self._next[host] = at + d
        sleep = at - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)


def make_robots(cfg: dict) -> RobotsCache | None:
    """RobotsCache condivisa tra i thread se 'respect_robots' (default true), altrimenti None."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    if not cfg.get("respect_robots"):
        return None
    if cfg.get("identify_crawler", True):
        warn_if_no_contact(cfg.get("contact"))
    return RobotsCache(make_fetch_text(lambda: _thread_http(cfg)), agent=cfg.get("robots_agent", ROBOTS_AGENT),
                       max_crawl_delay=float(cfg["max_crawl_delay"]))


# --------------------------------------------------------------------------
# Estrazione
# --------------------------------------------------------------------------
def _soup(html: str):
    from bs4 import BeautifulSoup
    for parser in ("lxml", "html.parser", "html5lib"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:  # noqa: BLE001
            continue
    return None


def _jsonld_items(soup) -> tuple[list[str], list[str]]:
    types: list[str] = []
    names: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            t = node.get("@type")
            for x in ([t] if isinstance(t, str) else (t if isinstance(t, list) else [])):
                if isinstance(x, str) and x not in types:
                    types.append(x.rsplit("/", 1)[-1])
            n = node.get("name")
            if isinstance(n, str) and n.strip() and n not in names and len(names) < 5:
                names.append(n.strip()[:120])
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            walk(json.loads(tag.string or tag.get_text() or "null"))
        except (ValueError, TypeError):
            continue
    return types[:12], names


def extract_evidence(html: str, page_url: str, lex: Lexicon, body_chars: int = 1200) -> dict[str, Any]:
    soup = _soup(html)
    if soup is None:
        return {}
    ev: dict[str, Any] = {}
    root = soup.find("html")
    ev["lang"] = ((root.get("lang") if root else "") or "").strip()[:12]
    ev["title"] = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]

    def meta(**attrs) -> str:
        m = soup.find("meta", attrs=attrs)
        return (m.get("content") or "").strip() if m else ""

    ev["description"] = (meta(name="description") or meta(property="og:description"))[:400]
    ev["site_name"] = (meta(property="og:site_name") or meta(name="application-name") or meta(property="og:title"))[:150]
    ev["jsonld_types"], ev["jsonld_names"] = _jsonld_items(soup)
    ev["h1"] = [h.get_text(" ", strip=True)[:150] for h in soup.find_all("h1")[:3]]

    # link "legale/chi siamo" e menu (prima di togliere elementi)
    legal_link = ""
    nav_items: list[str] = []
    container = soup.find("nav") or soup.find("header") or soup
    for a in (container.find_all("a") if container else [])[:80]:
        txt = a.get_text(" ", strip=True)[:40]
        if txt and txt not in nav_items and len(nav_items) < 25:
            nav_items.append(txt)
    for a in soup.find_all("a", href=True):
        if distinct_matches(lex.legal_pattern, fold(a.get_text(" ", strip=True) + " " + (a.get("href") or ""))):
            try:
                legal_link = urljoin(page_url, a["href"])
            except ValueError:
                legal_link = ""
            break
    ev["nav"] = nav_items
    ev["legal_link"] = legal_link

    for t in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        t.decompose()
    body = soup.body or soup
    total = len(body.get_text(" ", strip=True)) or 1
    # banner cookie: rimossi, ma mai un contenitore che regge la pagina intera
    for el in list(body.find_all(True, attrs={"id": _COOKIE_RE})) + list(body.find_all(True, attrs={"class": _COOKIE_RE})):
        try:
            if len(el.get_text(" ", strip=True)) < 0.5 * total:
                el.decompose()
        except Exception:  # noqa: BLE001
            continue
    footer = soup.find("footer")
    text = body.get_text(" ", strip=True)
    ev["footer"] = (footer.get_text(" ", strip=True) if footer else text[-600:])[:600]
    ev["body"] = text[:body_chars]
    ev["text_len"] = len(text)
    return ev


# --------------------------------------------------------------------------
# Raccolta
# --------------------------------------------------------------------------
def _status_from_exception(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        msg = str(exc).lower()
        if any(s in msg for s in ("name or service not known", "getaddrinfo", "nodename nor servname", "no address associated", "temporary failure in name")):
            return "dns_error"
        return "timeout" if "timed out" in msg else "http_error"
    if isinstance(exc, requests.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", 0)
        return "blocked" if code in (401, 403, 429) else "http_error"
    return "http_error"


def _candidate_urls(hosts: list[str], max_attempts: int) -> list[str]:
    urls: list[str] = []
    ordered: list[str] = []
    for h in hosts[:3]:
        for v in (h, (h[4:] if h.startswith("www.") else "www." + h)):
            if v not in ordered:
                ordered.append(v)
    for h in ordered:
        urls.append(f"https://{h}/")
    for h in ordered[:2]:
        urls.append(f"http://{h}/")
    return urls[:max_attempts]


def collect_evidence(hosts: list[str], lex: Lexicon, cfg: dict, limiter: HostLimiter, robots: RobotsCache | None = None) -> dict[str, Any]:
    """Apre la homepage (host osservati e varianti www/apex, https poi http) e ritorna il
    pacchetto di evidenza, con 'status' sempre valorizzato."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    http = _thread_http(cfg)
    statuses: list[str] = []
    last_err = ""
    for url in _candidate_urls(hosts, int(cfg["max_attempts"])):
        host = host_of(url)
        if robots is not None:
            dec = robots.check(url)
            limiter.set_min_delay(host, dec.crawl_delay)
            if not dec.allowed:
                statuses.append("robots")
                last_err = dec.reason or "vietata da robots.txt"
                continue
        limiter.wait(host)
        try:
            resp = http.get(url, max_bytes=int(cfg["max_bytes"]), skip_non_html=True,
                            read_timeout=float(cfg["read_timeout"]), allow_insecure_fallback=True, retries=1)
        except Exception as exc:  # noqa: BLE001 - qualunque errore = homepage non leggibile
            statuses.append(_status_from_exception(exc))
            last_err = f"{exc.__class__.__name__}: {str(exc)[:120]}"
            continue

        final_url = getattr(resp, "url", url)
        ctype = resp.headers.get("Content-Type", "").lower()
        base = {"fetched_url": url, "final_url": final_url, "final_host": host_of(final_url),
                "tls_error": bool(getattr(resp, "tls_error", False))}
        if not resp.content or (ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype):
            statuses.append("non_html")
            continue
        html = resp.text
        why = detect_challenge(html, resp.headers, resp.status_code)
        if why:
            statuses.append("blocked")
            last_err = why
            continue
        if detect_parked(html):
            return {**base, "status": "parked"}
        if detect_default_page(html):
            return {**base, "status": "default_page"}
        ev = extract_evidence(html, final_url, lex, int(cfg["body_chars"]))
        if not ev:
            statuses.append("empty")
            continue
        n_links = html.lower().count("<a ")
        status = "needs_js" if looks_js_rendered(html, n_links) else "ok"
        if status == "ok" and visible_text_length(html) < 40 and not ev.get("title"):
            status = "empty"
        ev.update(base)
        ev["status"] = status

        # Pagina "legale/chi siamo": un solo salto, stesso host, solo se la homepage non basta.
        if cfg["fetch_legal_page"] and ev.get("legal_link") and host_of(ev["legal_link"]) == ev["final_host"]:
            first = score_evidence(ev, lex)
            best_tipo = max((v.conf for v in first if v.field == "tipo"), default=0.0)
            if best_tipo < float(cfg["legal_page_below"]):
                limiter.wait(ev["final_host"])
                try:
                    r2 = http.get(ev["legal_link"], max_bytes=int(cfg["max_bytes"]), skip_non_html=True,
                                  read_timeout=float(cfg["read_timeout"]), allow_insecure_fallback=True, retries=1)
                    ev2 = extract_evidence(r2.text, ev["legal_link"], lex, int(cfg["body_chars"]))
                    ev["legal"] = ((ev2.get("title", "") + " " + ev2.get("body", ""))[:int(cfg["body_chars"])]).strip()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pagina legale non raggiungibile %s: %s", ev["legal_link"], exc)
        return ev

    status = next((s for s in _STATUS_PRIORITY if s in statuses), (statuses[-1] if statuses else "http_error"))
    return {"status": status, "detail": last_err}


# --------------------------------------------------------------------------
# Valutazione (regole pesate)
# --------------------------------------------------------------------------
def _fields_text(ev: dict[str, Any]) -> dict[str, str]:
    return {
        "title": fold(ev.get("title", "")),
        "site_name": fold(ev.get("site_name", "")),
        "h1": fold(" . ".join(ev.get("h1", []))),
        "description": fold(ev.get("description", "")),
        "jsonld": fold(" . ".join(ev.get("jsonld_names", []))),
        "legal": fold(ev.get("legal", "")),
        "footer": fold(ev.get("footer", "")),
        "nav": fold(" . ".join(ev.get("nav", []))),
        "body": fold(ev.get("body", "")),
    }


def _weighted(pattern, fields: dict[str, str], lex: Lexicon) -> tuple[float, list[str]]:
    total = 0.0
    hits: list[str] = []
    for fname, text in fields.items():
        w = lex.field_weights.get(fname, 1.0)
        found = distinct_matches(pattern, text)
        if found:
            total += w * min(len(found), lex.max_distinct)
            hits.extend(sorted(found)[:2])
    return total, hits


def score_evidence(ev: dict[str, Any], lex: Lexicon, country: str | None = None) -> list[Vote]:
    """Evidenza -> voti (stadio3). Se la homepage non è leggibile: nessun voto.
    'country' (facoltativo): se valorizzato, applica anche le voci di country_overrides valide
    SOLO per quel paese E SOLO se la lingua dichiarata dalla pagina (ev['lang'], sottotag BCP-47
    primario: 'nl-NL'/'NL' -> 'nl') combacia con quella attesa per quel country — vedi commento a
    country_overrides nel lessico. Pagina senza 'lang' dichiarato: l'override non si applica (si
    torna al solo lessico globale, per prudenza)."""
    if not ev or ev.get("status") not in SCORABLE_STATUS:
        return []
    votes: list[Vote] = []
    fields = _fields_text(ev)
    page_lang = ev.get("lang", "")

    # 1) dati strutturati schema.org (segnale forte, indipendente dalla lingua)
    for t in ev.get("jsonld_types", []):
        spec = lex.jsonld_types.get(t)
        if not spec:
            continue
        if spec.get("tipo"):
            votes.append(Vote("tipo", spec["tipo"], 0.85, "stadio3", f"JSON-LD {t}"))
        if spec.get("natura"):
            votes.append(Vote("natura", spec["natura"], 0.8 if spec["natura"] == "pubblico" else 0.6, "stadio3", f"JSON-LD {t}"))

    # 2) tipo: lessico multilingue pesato per campo
    scores: dict[str, tuple[float, list[str]]] = {}
    for tipo in lex.tipo_patterns:
        pat = lex.tipo_pattern_for(tipo, country, page_lang)
        s, hits = _weighted(pat, fields, lex)
        if s > 0:
            scores[tipo] = (s, hits)
    if scores:
        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        best_t, (best_s, best_hits) = ranked[0]
        second_s = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if best_s >= lex.min_type_score:
            dom = (best_s - second_s) / best_s
            conf = 0.95 * squash(best_s, 4.0) * (0.5 + 0.5 * dom)
            votes.append(Vote("tipo", best_t, round(conf, 3), "stadio3", f"parole: {', '.join(best_hits)} (score {best_s:.1f})"))
            if best_t in PUBLIC_BY_NATURE:
                votes.append(Vote("natura", "pubblico", round(0.85 * conf, 3), "stadio3", f"tipo {best_t}"))

    # 3) natura: marcatori pubblico / privato / terzo settore (associazioni, fondazioni, ODV/APS,
    # ONG... enti che non sono organi statali ma possono comunque ricevere fondi pubblici: natura
    # a sé, non "incerto" — "incerto" resta per i casi in cui davvero non si riesce a distinguere).
    p_s, p_hits = _weighted(lex.public_pattern_for(country, page_lang), fields, lex)
    q_s, q_hits = _weighted(lex.private_pattern_for(country, page_lang), fields, lex)
    t_s, t_hits = _weighted(lex.terzo_settore_pattern_for(country, page_lang), fields, lex)
    candidates = {"pubblico": (p_s, p_hits), "privato": (q_s, q_hits), "terzo_settore": (t_s, t_hits)}
    best_nat, (best_s, best_hits) = max(candidates.items(), key=lambda kv: kv[1][0])
    runner_up_s = max(s for nat, (s, _) in candidates.items() if nat != best_nat)
    if best_s >= lex.min_nature_score and best_s > 1.2 * runner_up_s:
        label = {"pubblico": "marcatori pubblici", "privato": "marcatori privati",
                  "terzo_settore": "marcatori di terzo settore"}[best_nat]
        votes.append(Vote("natura", best_nat, round(0.85 * squash(best_s - runner_up_s), 3), "stadio3",
                           f"{label}: {', '.join(best_hits)}"))
    return votes
