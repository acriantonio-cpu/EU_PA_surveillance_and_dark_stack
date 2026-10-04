# EN: Heuristic search of an entity's official website from its name (used for Spain/DIR3);
# pluggable search backend, intended for modest volumes.
"""
Ricerca euristica del sito ufficiale di un ente a partire dal suo nome
(usato per Spagna/DIR3, ma riutilizzabile per qualunque paese in cui il
campo 'nome_ente' sia valorizzato ma 'hostname' sia vuoto).

ATTENZIONE — LIMITI DEL BACKEND DI DEFAULT:
- Il backend 'duckduckgo' fa scraping best-effort della pagina HTML dei
  risultati (html.duckduckgo.com/html/), che NON è un'API pubblica
  supportata da DuckDuckGo: può rallentare, cambiare struttura, o bloccare
  richieste troppo frequenti. È pensato per volumi MODESTI (decine/centinaia
  di query, con pausa tra una richiesta e l'altra — parametro
  'sleep_seconds'), non per un uso massivo o commerciale.
- Verificare i Termini di Servizio del motore scelto prima di un uso
  sistematico. Per un uso intensivo o professionale, sostituire questo
  backend con un vero servizio di ricerca (Bing Web Search API, Google
  Programmable Search Engine, SerpApi, ecc.): basta scrivere una nuova
  funzione con la stessa firma di 'search_duckduckgo' e registrarla in
  SEARCH_BACKENDS.
- Il filtro 'is_plausible_official' è euristico: scarta i domini più
  ovviamente NON ufficiali (social, enciclopedie, elenchi commerciali,
  portali di lavoro) ma non garantisce che il primo risultato buono sia
  davvero il sito ufficiale dell'ente — è consigliata una verifica a
  campione dei risultati prima di considerarli definitivi.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) eu-pa-scraper-website-finder/1.0",
}

# Domini che (quasi) certamente NON sono il sito ufficiale dell'ente:
# social network, enciclopedie, portali di lavoro/elenchi commerciali,
# motori di ricerca stessi. Estendere liberamente per paese/dominio.
DEFAULT_BLOCKLIST: set[str] = {
    "wikipedia.org", "es.wikipedia.org", "en.wikipedia.org",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "paginasamarillas.es", "einforma.com", "axesor.es",
    "empresite.eleconomista.es", "infoempresa.com", "opencorporates.com",
    "indeed.com", "glassdoor.com", "infojobs.net", "tripadvisor.com",
    "maps.google.com", "amazon.com", "booking.com",
}


def _domain(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def is_plausible_official(url: str, blocklist: set[str] | None = None) -> bool:
    """True se il dominio non è in blocklist (euristica, non una garanzia)."""
    dom = _domain(url)
    if not dom:
        return False
    bl = blocklist if blocklist is not None else DEFAULT_BLOCKLIST
    return not any(dom == b or dom.endswith("." + b) for b in bl)


class SearchBlocked(RuntimeError):
    """Il motore di ricerca ha risposto con una pagina anti-bot/captcha (non con risultati)."""


def _ddg_unwrap(href: str) -> str:
    """I risultati di html.duckduckgo.com sono link di REDIRECT del tipo
    '//duckduckgo.com/l/?uddg=<URL codificato>&rut=...': l'URL vero è nel parametro
    'uddg'. Senza questo passaggio il dominio di ogni risultato era 'duckduckgo.com'
    (in blocklist) e TUTTI i risultati venivano scartati."""
    if not href:
        return ""
    h = href.strip()
    if h.startswith("//"):
        h = "https:" + h
    try:
        p = urlparse(h)
    except ValueError:
        return ""
    if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l"):
        q = parse_qs(p.query)
        target = (q.get("uddg") or [""])[0]
        return unquote(target)
    return h


def search_duckduckgo(query: str, session: requests.Session, max_results: int = 5, timeout: int = 15) -> list[str]:
    """Scraping best-effort dei risultati HTML di DuckDuckGo. Vedi il
    disclaimer in cima al file prima di un uso intensivo."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 non installato: 'pip install beautifulsoup4'") from exc

    resp = session.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers=DEFAULT_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.text
    low = text[:20000].lower()
    # DDG risponde spesso 202/200 con una pagina "anomaly"/captcha invece dei risultati:
    # prima era indistinguibile da "nessun risultato" e passava in silenzio.
    if resp.status_code == 202 or "anomaly" in low or "unfortunately, bots use duckduckgo" in low or "challenge-form" in low:
        raise SearchBlocked("DuckDuckGo ha risposto con una pagina anti-bot/captcha")
    soup = BeautifulSoup(text, "html.parser")

    urls: list[str] = []
    for a in soup.select("a.result__a"):
        href = _ddg_unwrap(a.get("href") or "")
        if href.startswith(("http://", "https://")):
            urls.append(href)
        if len(urls) >= max_results:
            break
    return urls


WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def search_wikidata(query: str, session: requests.Session, max_results: int = 5, timeout: int = 15, language: str = "en") -> list[str]:
    """Cerca il NOME dell'ente su Wikidata (wbsearchentities) e ritorna i siti
    ufficiali (P856) delle entità trovate. Stabile e permesso (API pubblica), a
    differenza dello scraping di un motore di ricerca. Copertura ottima per
    comuni, università, ospedali, ministeri; debole per scuole e piccole agenzie."""
    headers = {"User-Agent": "eu-pa-scraper/1.0 (osservatorio infrastruttura PA; contatto nel README)"}
    r = session.get(
        WIKIDATA_API,
        params={"action": "wbsearchentities", "search": query, "language": language, "uselang": language,
                "type": "item", "limit": max_results, "format": "json"},
        headers=headers, timeout=timeout,
    )
    r.raise_for_status()
    ids = [it["id"] for it in r.json().get("search", []) if it.get("id")]
    if not ids:
        return []
    r2 = session.get(
        WIKIDATA_API,
        params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims", "format": "json"},
        headers=headers, timeout=timeout,
    )
    r2.raise_for_status()
    entities = r2.json().get("entities", {})
    urls: list[str] = []
    for qid in ids:  # rispetta l'ordine di rilevanza della ricerca
        for claim in entities.get(qid, {}).get("claims", {}).get("P856", []):
            val = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                urls.append(val)
    return urls[:max_results]


SEARCH_BACKENDS = {
    "duckduckgo": search_duckduckgo,
    "wikidata": search_wikidata,
}
# Backend che cercano il NOME dell'ente così com'è: il 'query_suffix' (es. "sitio
# oficial") non va aggiunto.
BACKENDS_WITHOUT_SUFFIX = {"wikidata"}


# --------------------------------------------------------------------------
# Corrispondenza nome ente <-> risultato
# --------------------------------------------------------------------------

_GENERIC_NAME_WORDS = frozenset({
    # parole istituzionali multilingue che non identificano UN ente
    "ayuntamiento", "concello", "ajuntament", "diputacion", "consejeria", "ministerio", "junta",
    "comunidad", "gobierno", "servicio", "servei", "secretaria", "general", "direccion", "consorcio",
    "universidad", "instituto", "fundacion", "agencia", "oficina", "delegacion", "unidad",
    "gemeente", "stad", "ville", "commune", "mairie", "municipality", "council", "county", "city",
    "gemeinde", "stadt", "landkreis", "amt", "verwaltung", "ministry", "ministere", "ministerie",
    "comune", "provincia", "regione", "agenzia", "ufficio", "camara", "municipal", "municipio",
    "office", "department", "agency", "authority", "institute", "national", "public", "state",
    "de", "del", "la", "las", "los", "el", "los", "the", "der", "die", "das", "van", "von", "of", "and",
})


def _fold(text: str) -> str:
    """minuscolo, senza accenti/segni diacritici, solo alfanumerici e spazi."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^\w\s]", " ", t)


def distinctive_tokens(entity_name: str) -> list[str]:
    """Parole del nome che identificano l'ente (>=4 lettere, non generiche)."""
    return [w for w in _fold(entity_name).split() if len(w) >= 4 and w not in _GENERIC_NAME_WORDS]


def name_match_score(entity_name: str, url: str) -> float:
    """Frazione (0..1) dei token distintivi del nome presenti nell'host o nel path
    dell'URL (confronto per sottostringa, senza accenti: 'juntadeandalucia.es' per
    'Junta de Andalucía'). 1.0 se il nome non ha token distintivi (nessuna info)."""
    toks = distinctive_tokens(entity_name)
    if not toks:
        return 1.0
    try:
        p = urlparse(url)
    except ValueError:
        return 0.0
    hay = _fold((p.hostname or "") + " " + (p.path or "")).replace(" ", "")
    hits = sum(1 for t in toks if t in hay)
    return hits / len(toks)


def find_official_website(
    entity_name: str,
    session: requests.Session,
    backend: str = "duckduckgo",
    query_suffix: str = "sitio oficial",
    max_results: int = 5,
    blocklist: set[str] | None = None,
    min_name_score: float = 0.5,
    language: str = "en",
) -> str | None:
    """Ritorna l'URL 'plausibile' migliore trovato per 'entity_name', o None.
    Tra i candidati non in blocklist sceglie quello che meglio corrisponde al NOME
    dell'ente (host/path); a parità, il più in alto nei risultati. Scarta chi ha
    punteggio < min_name_score (se il nome ha token distintivi). Se il motore è
    bloccato solleva SearchBlocked (chi chiama decide se fermarsi)."""
    if not entity_name or not entity_name.strip():
        return None

    search_fn = SEARCH_BACKENDS.get(backend)
    if search_fn is None:
        raise ValueError(f"Backend di ricerca '{backend}' non riconosciuto. Disponibili: {list(SEARCH_BACKENDS)}")

    query = entity_name.strip() if backend in BACKENDS_WITHOUT_SUFFIX else f"{entity_name} {query_suffix}".strip()
    try:
        if backend == "wikidata":
            candidates = search_fn(query, session, max_results=max_results, language=language)
        else:
            candidates = search_fn(query, session, max_results=max_results)
    except SearchBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ricerca fallita per '%s': %s", entity_name, exc)
        return None

    best: tuple[float, int, str] | None = None
    for rank, url in enumerate(candidates):
        if not is_plausible_official(url, blocklist):
            continue
        score = name_match_score(entity_name, url)
        if score < min_name_score:
            continue
        key = (score, -rank)
        if best is None or key > (best[0], -best[1]):
            best = (score, rank, url)
    return best[2] if best else None


def enrich_records_with_website(
    records: list,
    backend: str = "duckduckgo",
    query_suffix: str = "sitio oficial",
    max_results: int = 5,
    sleep_seconds: float = 2.0,
    max_queries: int | None = None,
    cache_path: Path | None = None,
    checkpoint_cb: Callable[[list], None] | None = None,
    checkpoint_every: int = 25,
    max_consecutive_blocks: int = 5,
    language: str = "en",
    min_name_score: float = 0.5,
) -> tuple[list, int]:
    """
    Riempie 'hostname' per i record con 'nome_ente' valorizzato e 'hostname'
    vuoto. Modifica i record IN PLACE e li ritorna assieme al numero di
    ricerche effettivamente eseguite (utile per rispettare 'max_queries').

    Robustezza (prima un crash dopo ore perdeva tutto):
      - 'cache_path': file JSON {nome_ente: url|null} aggiornato durante il lavoro. Un
        nome già cercato non viene cercato di nuovo (né in un run successivo).
      - 'checkpoint_cb(records)': chiamata ogni 'checkpoint_every' ricerche (e alla
        fine) per salvare il CSV in modo atomico.
      - se il motore risponde 'max_consecutive_blocks' volte di fila con un captcha/anti-bot
        ci si ferma con un avviso invece di consumare ore su risposte vuote.
    """
    session = requests.Session()
    n_queries = 0
    cache: dict[str, str | None] = {}
    if cache_path and Path(cache_path).exists():
        try:
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cache ricerca sito %s illeggibile (%s): riparto vuota", cache_path, exc)

    def _save_cache() -> None:
        if not cache_path:
            return
        tmp = Path(cache_path).with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(cache_path)
        except OSError as exc:
            logger.warning("impossibile salvare la cache ricerca sito (%s): non fatale", exc)

    n_total_candidates = sum(1 for r in records if not r.hostname and r.nome_ente)
    logger.info("Ricerca sito ufficiale: %d record candidati (hostname vuoto + nome_ente presente)", n_total_candidates)
    consecutive_blocks = 0

    try:
        for rec in records:
            if rec.hostname or not rec.nome_ente:
                continue
            key = rec.nome_ente.strip()
            if key in cache:
                if cache[key]:
                    rec.hostname = cache[key]  # type: ignore[assignment]
                continue
            if max_queries is not None and n_queries >= max_queries:
                logger.info("Raggiunto max_queries=%d: interrompo la ricerca (i restanti record restano senza hostname).", max_queries)
                break

            try:
                found = find_official_website(
                    rec.nome_ente, session, backend=backend, query_suffix=query_suffix,
                    max_results=max_results, language=language, min_name_score=min_name_score,
                )
                consecutive_blocks = 0
            except SearchBlocked as exc:
                consecutive_blocks += 1
                n_queries += 1
                logger.warning("Ricerca bloccata (%d/%d di fila) per '%s': %s", consecutive_blocks, max_consecutive_blocks, rec.nome_ente, exc)
                if consecutive_blocks >= max_consecutive_blocks:
                    logger.error(
                        "Il motore di ricerca continua a bloccare le richieste: mi fermo qui. Riprova più tardi, "
                        "con 'sleep_seconds' più alto o con backend 'wikidata'. I record già trovati sono salvati."
                    )
                    break
                time.sleep(max(sleep_seconds, 10.0))
                continue
            n_queries += 1
            cache[key] = found
            if found:
                rec.hostname = found
                logger.info("[%d/%d] '%s' -> %s", n_queries, n_total_candidates, rec.nome_ente, found)
            else:
                logger.info("[%d/%d] '%s' -> nessun sito plausibile trovato", n_queries, n_total_candidates, rec.nome_ente)

            if n_queries % max(1, checkpoint_every) == 0:
                _save_cache()
                if checkpoint_cb:
                    checkpoint_cb(records)
            time.sleep(sleep_seconds)
    finally:
        _save_cache()
        if checkpoint_cb:
            checkpoint_cb(records)

    return records, n_queries
