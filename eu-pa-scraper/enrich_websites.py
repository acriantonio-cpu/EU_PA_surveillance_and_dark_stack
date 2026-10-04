"""
Rilancia SOLO la ricerca del sito ufficiale su un CSV di output già
generato (data/output/{ISO}/siti.csv), senza rifare l'intero import.
Utile per riprendere dopo un'interruzione, aumentare 'max_queries', o
provare un altro backend/query_suffix senza ripartire da zero.

Uso:
    python enrich_websites.py --country ES
    python enrich_websites.py --country ES --max-queries 200 --sleep 3
    python enrich_websites.py --country ES --query-suffix "web oficial ayuntamiento"

Scrive un backup (siti.csv.bak) prima di sovrascrivere l'output.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from src.schema import FIELDNAMES, Record, atomic_write_csv
from src.website_finder import enrich_records_with_website

PROJECT_ROOT = Path(__file__).resolve().parent


def load_records(csv_path: Path) -> list[Record]:
    records = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(Record(**{k: (row.get(k) or "") for k in FIELDNAMES}))
    return records


def write_records(records: list[Record], csv_path: Path) -> None:
    """Scrittura ATOMICA (file temporaneo + rename): un crash o un CSV aperto in
    Excel non corrompono/perdono l'output."""
    atomic_write_csv((r.as_dict() for r in records), FIELDNAMES, csv_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Arricchisce gli hostname mancanti via ricerca web (nome_ente)")
    parser.add_argument("--country", "-c", required=True, help="Codice ISO paese, es. ES")
    parser.add_argument("--backend", default="duckduckgo", help="duckduckgo | wikidata")
    parser.add_argument("--language", default="en", help="Lingua per la ricerca su Wikidata (es. es, fr, de, nl)")
    parser.add_argument("--query-suffix", default="sitio oficial web oficial")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=2.0, help="Pausa in secondi tra una ricerca e l'altra")
    parser.add_argument("--max-queries", type=int, default=None, help="Tetto di sicurezza al numero di ricerche in questo run")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    csv_path = PROJECT_ROOT / "data" / "output" / args.country.upper() / "siti.csv"
    if not csv_path.exists():
        print(f"Non trovato: {csv_path}\nEsegui prima: python main.py --country {args.country.upper()}")
        sys.exit(1)

    records = load_records(csv_path)
    backup_path = csv_path.with_suffix(".csv.bak")
    write_records(records, backup_path)
    print(f"Backup salvato in {backup_path}")

    cache_path = PROJECT_ROOT / "data" / "input" / args.country.upper() / "website_search_cache.json"
    updated, n_queries = enrich_records_with_website(
        records,
        backend=args.backend,
        query_suffix=args.query_suffix,
        max_results=args.max_results,
        sleep_seconds=args.sleep,
        max_queries=args.max_queries,
        cache_path=cache_path,
        checkpoint_cb=lambda recs: write_records(recs, csv_path),
        language=args.language,
    )
    write_records(updated, csv_path)

    filled = sum(1 for r in updated if r.hostname)
    print(f"Fatto: {n_queries} ricerche eseguite in questo run. {filled}/{len(updated)} record hanno ora un hostname.")


if __name__ == "__main__":
    main()
