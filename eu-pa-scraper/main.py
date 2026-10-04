"""
Osservatorio infrastruttura tecnica PA UE — tool unico di scraping/estrazione
per i 26 paesi UE (esclusa Italia, coperta da IndicePA).

USO (da VS Code, terminale integrato, con il virtualenv attivato):

    python main.py --list
    python main.py --country NL
    python main.py --country FR --country HR
    python main.py --all
    python main.py --all --skip-todo
    python main.py --country BE --resume   # riprende un crawl interrotto

Ogni paese ha:
    - config/countries/{ISO}.yaml   -> sorgente ufficiale + mapping campi
    - data/input/{ISO}/             -> file grezzi scaricati (cache)
    - data/output/{ISO}/siti.csv    -> output normalizzato

Schema di output comune (vedi src/schema.py):
    hostname, comune, regione, codice_ipa, sector, country_code,
    source_name, retrieved_at

RIPRENDERE UN'ANALISI INTERROTTA (--resume):
    I fetcher a crawl (source_type: site_crawl, es. BE/DE) possono
    richiedere molti minuti su più livelli — un'interruzione (Ctrl+C, chiusura
    del terminale, errore di rete non gestito) a metà perderebbe altrimenti
    tutto il lavoro fatto fino a quel momento. Questi fetcher salvano quindi
    lo stato su disco (data/input/{ISO}/crawl_state.sqlite, aggiornato in modo
    incrementale dopo ogni pagina; il JSON storico crawl_checkpoint.json è
    esportato a fine livello e all'interruzione). Se un run si interrompe, basta rilanciare LO
    STESSO comando aggiungendo '--resume': il crawl riparte esattamente dalla
    pagina successiva a quella in corso al momento dell'interruzione, invece
    che dal seed iniziale. Senza '--resume' un checkpoint trovato viene
    ignorato (e sovrascritto) e si riparte da zero, come comportamento
    predefinito. Il checkpoint NON viene mai cancellato automaticamente
    (nemmeno a crawl completato: resta come traccia permanente dell'ultimo
    run) — se rilanci con '--resume' un crawl già completato con le stesse
    impostazioni, l'output viene rigenerato all'istante dal checkpoint
    invece di ripetere tutto il crawl da capo. Se nel frattempo cambi i parametri del
    crawl nello YAML del paese (seed, crawl_depth, allowed_domains, ...), il
    checkpoint non corrisponde più e viene ignorato con un avviso, invece di
    riprendere alla cieca con impostazioni diverse da quelle con cui era
    stato salvato.
    NOTA: al momento il checkpoint copre solo il crawl (source_type:
    site_crawl); l'eventuale arricchimento successivo con la ricerca del
    sito ufficiale (website_search.enabled) riparte sempre da zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # carica .env (SCRAPER_CONTACT, chiavi API) se python-dotenv è installato
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # pragma: no cover
    pass

from src.registry import build_fetcher, list_countries, DATA_INPUT_DIR, DATA_OUTPUT_DIR
from src.schema import write_csv
from src.website_finder import enrich_records_with_website

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8"),
        ],
    )


def run_country(country_code: str, resume: bool = False) -> tuple[bool, str]:
    logger = logging.getLogger("main")
    try:
        fetcher = build_fetcher(country_code)
        fetcher.resume = resume
        records = fetcher.run()

        output_path = DATA_OUTPUT_DIR / country_code.upper() / "siti.csv"

        ws_config = fetcher.config.get("website_search", {})
        if ws_config.get("enabled"):
            logger.info("[%s] ricerca sito ufficiale abilitata (website_search.enabled=true)", country_code.upper())
            # Salvataggio incrementale + cache: un crash dopo ore di ricerche non
            # perde più tutto (prima il CSV veniva scritto solo alla fine).
            cache_path = DATA_INPUT_DIR / country_code.upper() / "website_search_cache.json"
            records, n_queries = enrich_records_with_website(
                records,
                backend=ws_config.get("backend", "duckduckgo"),
                query_suffix=ws_config.get("query_suffix", "sitio oficial"),
                max_results=ws_config.get("max_results", 5),
                sleep_seconds=ws_config.get("sleep_seconds", 2.0),
                max_queries=ws_config.get("max_queries"),
                cache_path=cache_path,
                checkpoint_cb=lambda recs: write_csv(recs, output_path),
                language=ws_config.get("language", "en"),
            )
            logger.info("[%s] ricerca sito ufficiale: %d query eseguite", country_code.upper(), n_queries)

        n = write_csv(records, output_path)
        logger.info("[%s] OK — %d record scritti in %s", country_code.upper(), n, output_path)
        return True, f"{n} record -> {output_path}"
    except NotImplementedError as exc:
        logger.warning("[%s] SALTATO: %s", country_code.upper(), exc)
        return False, "config incompleto (status: todo)"
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] ERRORE", country_code.upper())
        return False, f"{exc.__class__.__name__}: {exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Scraper unificato enti PA — UE (no IT)")
    parser.add_argument(
        "--country", "-c", action="append", default=[],
        help="Codice ISO paese (es. NL, FR, HR). Ripetibile.",
    )
    parser.add_argument("--all", action="store_true", help="Esegue tutti i paesi configurati.")
    parser.add_argument("--list", action="store_true", help="Elenca i paesi disponibili ed esce.")
    parser.add_argument("--skip-todo", action="store_true", help="Con --all, salta silenziosamente i config non completati.")
    parser.add_argument(
        "--resume", action="store_true",
        help="Riprende un'analisi a crawl (site_crawl) interrotta, usando il checkpoint "
             "salvato in data/input/{ISO}/crawl_checkpoint.json invece di ripartire da zero. "
             "Ignorato se non esiste un checkpoint valido per il paese.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log dettagliato (DEBUG).")
    args = parser.parse_args()

    setup_logging(args.verbose)

    available = list_countries()

    if args.list:
        print("Paesi configurati:")
        for c in available:
            print(f"  - {c}")
        return

    if args.all:
        targets = available
    elif args.country:
        targets = [c.upper() for c in args.country]
        unknown = [c for c in targets if c not in available]
        if unknown:
            print(f"Paesi non configurati: {unknown}. Usa --list per vedere quelli disponibili.")
            sys.exit(1)
    else:
        parser.print_help()
        return

    results = {}
    for country in targets:
        ok, msg = run_country(country, resume=args.resume)
        if not ok and args.skip_todo and "todo" in msg:
            continue
        results[country] = (ok, msg)

    print("\n=== Riepilogo ===")
    for country, (ok, msg) in results.items():
        status = "OK " if ok else "ERR"
        print(f"[{status}] {country}: {msg}")

    # Riepilogo leggibile da macchina + exit code: prima il processo usciva SEMPRE
    # con 0 anche se tutti i paesi fallivano, quindi uno scheduler/CI non se ne accorgeva.
    try:
        DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "results": {c: {"ok": ok, "message": msg} for c, (ok, msg) in results.items()},
        }
        (DATA_OUTPUT_DIR / "_run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logging.getLogger("main").warning("impossibile scrivere _run_summary.json: %s", exc)
    if any(not ok for ok, _ in results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()
