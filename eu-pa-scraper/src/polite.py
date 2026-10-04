# EN: Polite crawling: robots.txt (RFC 9309) and Crawl-delay compliance, identifiable User-Agent;
# controlled by the respect_robots flag (default true).
"""
Comportamento "educato" verso i siti visitati: robots.txt, Crawl-delay, User-Agent identificabile.

Usato dal crawler (SiteCrawlFetcher) e dal classificatore (stadio 3). Il rispetto di robots.txt è
CONTROLLATO DA UN FLAG (`respect_robots`, default True nel config di ciascun paese e in
config_classify.yaml): con `respect_robots: false` nessun robots.txt viene letto.

Regole applicate (RFC 9309):
  - robots.txt assente o "non disponibile" (qualunque 4xx)  -> tutto consentito;
  - robots.txt con errore del server (5xx dopo i retry)      -> tutto VIETATO per questo host
    (per il resto del run: meglio saltare che insistere su un server in difficoltà);
  - robots.txt irraggiungibile per rete/DNS/timeout          -> consentito (se il sito non risponde
    fallirà comunque anche la pagina);
  - le direttive valgono per il "product token" `ROBOTS_AGENT` e, in mancanza, per `*`;
  - `Crawl-delay` viene rispettato (con tetto `max_crawl_delay`, default 30 s);
  - i caratteri jolly (`*`, `$`) sono supportati dalla libreria `protego`; senza di essa si ripiega su
    urllib.robotparser, che NON li capisce (avviso nel log).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

ROBOTS_AGENT = "eu-pa-scraper"
VERSION = "1.0"

try:  # parser con supporto a wildcard e '$'
    from protego import Protego
except ImportError:  # pragma: no cover
    Protego = None


def identified_user_agent(contact: str | None = None) -> str:
    """User-Agent onesto: dice chi siamo e dove contattarci (contatto da config `contact` o dalla
    variabile d'ambiente SCRAPER_CONTACT: email o URL)."""
    contact = (contact or os.environ.get("SCRAPER_CONTACT") or "").strip()
    # Un header HTTP deve essere ASCII e senza a-capo (altrimenti requests solleva o si apre un'iniezione di header).
    contact = "".join(ch for ch in contact if 32 <= ord(ch) < 127).strip()
    if contact:
        return f"{ROBOTS_AGENT}/{VERSION} (+{contact})"
    return f"{ROBOTS_AGENT}/{VERSION} (research crawler; contact not configured)"


def warn_if_no_contact(contact: str | None) -> None:
    if not (contact or os.environ.get("SCRAPER_CONTACT")):
        logger.warning(
            "Nessun contatto per lo User-Agent: imposta SCRAPER_CONTACT nel file .env (una email o un URL) o "
            "'contact:' nel config. Un crawler identificabile è più corretto e i gestori dei siti possono "
            "scrivervi invece di bannare l'IP."
        )


@dataclass
class RobotsDecision:
    allowed: bool
    crawl_delay: float = 0.0
    reason: str = ""


class RobotsCache:
    """robots.txt per origine (schema+host), in cache per la durata del processo, thread-safe.

    `fetch_text(url) -> (status, text)` scarica un URL con il client HTTP del chiamante (così usa la sua
    sessione/UA/timeout) e ritorna (status HTTP, testo) oppure (None, "") per errori di rete. Se `fetch_text`
    solleva, viene trattato come errore di rete.
    """

    def __init__(
        self,
        fetch_text: Callable[[str], tuple[int | None, str]],
        agent: str = ROBOTS_AGENT,
        max_crawl_delay: float = 30.0,
    ):
        self.fetch_text = fetch_text
        self.agent = agent
        self.max_crawl_delay = max_crawl_delay
        self._lock = threading.Lock()
        self._per_origin_locks: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[Any, str]] = {}   # origin -> (parser|None|"DENY", nota)
        if Protego is None:
            logger.warning(
                "Libreria 'protego' non installata: robots.txt letto con urllib.robotparser, che NON "
                "supporta i caratteri jolly (*, $). Installa: pip install protego"
            )

    # -- interno --------------------------------------------------------------
    def _load(self, origin: str) -> tuple[Any, str]:
        with self._lock:
            hit = self._cache.get(origin)
            if hit is not None:
                return hit
            lock = self._per_origin_locks.setdefault(origin, threading.Lock())
        with lock:                                   # una sola richiesta per origine, anche con più thread
            with self._lock:
                hit = self._cache.get(origin)
            if hit is not None:
                return hit
            try:
                status, text = self.fetch_text(origin + "/robots.txt")
            except Exception as exc:  # noqa: BLE001
                status, text = None, ""
                logger.debug("robots.txt di %s non raggiungibile: %s", origin, exc)
            if status is None:
                result: tuple[Any, str] = (None, "robots.txt irraggiungibile (rete): consentito")
            elif 200 <= status < 300:
                result = (self._parse(text), "")
            elif 500 <= status < 600:
                result = ("DENY", f"robots.txt: errore del server ({status}): host escluso per prudenza")
            else:
                result = (None, f"robots.txt non disponibile ({status}): consentito")
            with self._lock:
                self._cache[origin] = result
            return result

    def _parse(self, text: str) -> Any:
        if Protego is not None:
            return Protego.parse(text)
        import urllib.robotparser
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        return rp

    # -- API -------------------------------------------------------------------
    def check(self, url: str) -> RobotsDecision:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return RobotsDecision(True)
        origin = f"{p.scheme}://{p.netloc}"
        parser, note = self._load(origin)
        if parser == "DENY":
            return RobotsDecision(False, 0.0, note)
        if parser is None:
            return RobotsDecision(True, 0.0, note)
        try:
            if Protego is not None:
                allowed = bool(parser.can_fetch(url, self.agent))
                delay = parser.crawl_delay(self.agent)
            else:
                allowed = bool(parser.can_fetch(self.agent, url))
                delay = parser.crawl_delay(self.agent)
        except Exception as exc:  # noqa: BLE001 - un robots.txt malformato non deve fermare tutto
            logger.debug("robots.txt di %s non interpretabile: %s", origin, exc)
            return RobotsDecision(True)
        delay = float(delay or 0.0)
        delay = min(max(delay, 0.0), self.max_crawl_delay)
        return RobotsDecision(allowed, delay, "" if allowed else f"vietato da robots.txt di {origin}")


def make_fetch_text(http_getter: Callable[[], Any], timeout: float = 8.0) -> Callable[[str], tuple[int | None, str]]:
    """Crea `fetch_text` a partire da una funzione che restituisce l'HttpSession del thread corrente."""

    def fetch(url: str) -> tuple[int | None, str]:
        http = http_getter()
        try:
            r = http.get(url, max_bytes=500_000, read_timeout=timeout, allow_insecure_fallback=True, retries=2)
            return r.status_code, r.text
        except requests.HTTPError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            return (code if code else None), ""
        except requests.RequestException:
            return None, ""

    return fetch
