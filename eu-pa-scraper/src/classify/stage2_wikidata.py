"""Stadio 2 - Wikidata. Per ogni host cerca entità il cui sito ufficiale (P856) coincide e
mappa le etichette delle loro classi (P31) su natura/tipo tramite il lessico.

Copertura: ottima per comuni, università, ospedali, ministeri; debole per scuole e piccole
agenzie. Un host non trovato NON è un voto negativo: semplicemente lo stadio non dice nulla."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from ..urlutil import normalize_host
from .lexicon import Lexicon
from .taxonomy import Vote

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "eu-pa-scraper/1.0 (osservatorio infrastruttura PA; classificazione siti)"


def _variants(host: str) -> list[str]:
    bare = host[4:] if host.startswith("www.") else host
    out = []
    for h in (bare, "www." + bare):
        for scheme in ("https", "http"):
            out.append(f"{scheme}://{h}/")
            out.append(f"{scheme}://{h}")
    return out


def build_query(hosts: list[str], language: str = "en") -> str:
    values = " ".join(f"<{u}>" for h in hosts for u in _variants(h))
    return (
        "SELECT ?url ?item ?itemLabel ?classLabel WHERE {\n"
        f"  VALUES ?url {{ {values} }}\n"
        "  ?item wdt:P856 ?url .\n"
        "  OPTIONAL { ?item wdt:P31 ?class . }\n"
        f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{language}". }}\n'
        "}"
    )


def _bare(host: str) -> str:
    h = normalize_host(host)
    return h[4:] if h.startswith("www.") else h


def map_class_labels(labels: list[str], lex: Lexicon) -> list[Vote]:
    """Etichette di classe -> voti. Per ogni etichetta vale la parola chiave PIÙ LUNGA che combacia."""
    votes: list[Vote] = []
    seen: set[tuple[str, str]] = set()
    for label in labels:
        low = (label or "").lower()
        for entry in lex.wikidata_keywords:      # già ordinate per lunghezza decrescente
            if str(entry["kw"]).lower() in low:
                conf = float(entry.get("conf", 0.7))
                for field_name in ("natura", "tipo"):
                    val = entry.get(field_name)
                    if val and (field_name, val) not in seen:
                        seen.add((field_name, val))
                        votes.append(Vote(field_name, val, conf, "stadio2", f"wikidata classe '{label}'"))
                break
    return votes


def run_stage2(
    hosts: list[str], lex: Lexicon, *, endpoint: str = DEFAULT_ENDPOINT, batch_size: int = 20, language: str = "en",
    timeout: float = 60.0, sleep_seconds: float = 1.0, session: requests.Session | None = None,
    on_result: Callable[[str, list[Vote], dict], None] | None = None,
) -> dict[str, tuple[list[Vote], dict]]:
    """hosts: host chiave. Ritorna {host: (voti, info)} SOLO per gli host interrogati con successo
    (trovati o no); un batch fallito (rete, 429, timeout) viene saltato con un avviso e
    ritentato al prossimo run."""
    session = session or requests.Session()
    results: dict[str, tuple[list[Vote], dict]] = {}
    for i in range(0, len(hosts), batch_size):
        batch = hosts[i:i + batch_size]
        query = build_query(batch, language)
        data: dict[str, Any] | None = None
        for attempt in (1, 2):
            try:
                r = session.post(
                    endpoint, data={"query": query, "format": "json"},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}, timeout=timeout,
                )
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After", "10") or 10)
                    logger.warning("Wikidata: 429, attendo %.0fs", wait)
                    time.sleep(min(wait, 60))
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Wikidata: batch %d-%d fallito (tentativo %d/2): %s", i, i + len(batch), attempt, exc)
                time.sleep(2 * attempt)
        if data is None:
            continue
        by_host: dict[str, dict[str, Any]] = {_bare(h): {"items": set(), "classes": [], "labels": []} for h in batch}
        for b in data.get("results", {}).get("bindings", []):
            url = (b.get("url") or {}).get("value", "")
            host = _bare(url.split("//", 1)[-1].split("/", 1)[0])
            slot = by_host.get(host)
            if slot is None:
                continue
            item = (b.get("item") or {}).get("value", "").rsplit("/", 1)[-1]
            if item:
                slot["items"].add(item)
            label = (b.get("itemLabel") or {}).get("value", "")
            # scarta il caso in cui l'entità non ha etichetta e SPARQL ripiega sul QID grezzo (es. "Q12345")
            if label and label not in slot["labels"] and label != item:
                slot["labels"].append(label)
            cl = (b.get("classLabel") or {}).get("value")
            if cl and cl not in slot["classes"]:
                slot["classes"].append(cl)
        for h in batch:
            slot = by_host[_bare(h)]
            votes = map_class_labels(slot["classes"], lex) if slot["items"] else []
            info = {
                "wikidata_items": sorted(slot["items"])[:3],
                "wikidata_classes": slot["classes"][:8],
                # nome "ufficiale" dell'ente secondo Wikidata (etichetta dell'item, non della classe):
                # è la risposta più diretta a "questo sito a quale ente si riferisce?".
                "wikidata_label": slot["labels"][0] if slot["labels"] else "",
            }
            results[h] = (votes, info)
            if on_result:
                on_result(h, votes, info)
        n_batches = (len(hosts) + batch_size - 1) // batch_size
        logger.info("Wikidata: batch %d/%d (%d/%d host interrogati, %.0f%%)",
                    i // batch_size + 1, n_batches, min(i + batch_size, len(hosts)), len(hosts),
                    100.0 * min(i + batch_size, len(hosts)) / max(len(hosts), 1))
        time.sleep(sleep_seconds)
    return results
