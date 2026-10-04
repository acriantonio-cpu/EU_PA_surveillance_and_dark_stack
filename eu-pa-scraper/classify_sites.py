"""
Classifica i siti di un paese: natura (pubblico / privato / terzo_settore / incerto / infrastruttura) e tipo di
ente (comune, regione_provincia, governo_centrale, polizia_sicurezza, scuola, universita, sanita,
giustizia, parlamento, agenzia_authority, emergenza, cultura, trasporti, ambiente, altro).

TUTTI gli stadi sono disattivati per default: vanno attivati in config_classify.yaml oppure con
--stages. Esempi:

    python classify_sites.py --country BE --stages 0            # solo regole sull'hostname (zero rete)
    python classify_sites.py --country BE --stages 0,2          # + Wikidata
    python classify_sites.py --country BE --stages 0,2,3        # + homepage e regole multilingue
    python classify_sites.py --country BE --stages 0,2,3,4      # + LLM locale (Ollama) sul residuo
    python classify_sites.py --country HR --input data/input/HR/crawl_service_bund.json --stages 0,3
    python classify_sites.py --country BE --stages 0,3 --limit 200 -v     # prova su 200 siti

Input: data/output/{ISO}/siti.csv (se non ha hostname, ripiega sul JSON del crawl in
data/input/{ISO}/crawl_*.json) oppure --input. Output: data/output/{ISO}/siti_classificati.csv
(le colonne originali + natura, tipo, confidenze, stadi, evidenza, homepage_status, da_rivedere...).
Le evidenze/risposte sono in cache (data/input/{ISO}/classify_cache.jsonl): rilanciare non rifà
le richieste già fatte, e cambiando il lessico le homepage vengono RIVALUTATE senza riscaricarle.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

try:  # carica .env (SCRAPER_CONTACT, chiavi API) se python-dotenv è installato
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:  # pragma: no cover
    pass

from src.classify.lexicon import load_lexicon
from src.classify.pipeline import Cache, Pipeline, load_config
from src.classify.sites import load_rows, sites_from_rows

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"


def main() -> int:
    ap = argparse.ArgumentParser(description="Classificazione natura/tipo dei siti (stadi opzionali, tutti OFF di default)")
    ap.add_argument("--country", "-c", action="append", required=True,
                     help="Codice ISO (ripetibile). Abilita anche le voci di lessico specifiche per "
                          "quel paese (config/classify_lexicon.yaml: country_overrides) — parole utili "
                          "ma troppo ambigue a livello globale, sicure quando si sa già che il batch è "
                          "di un solo paese.")
    ap.add_argument("--stages", default="", help="Stadi da attivare, es. 0,2,3,4 (in aggiunta a quelli abilitati nel config)")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config_classify.yaml"))
    ap.add_argument("--input", help="CSV di output o JSON del crawl da usare al posto di siti.csv")
    ap.add_argument("--limit", type=int, help="Classifica solo i primi N siti (prova)")
    ap.add_argument("--workers", type=int, help="Thread per l'apertura delle homepage (stadio 3)")
    ap.add_argument("--refresh", action="store_true", help="Ignora la cache (rifà tutte le richieste)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "classify.log", encoding="utf-8")],
    )
    log = logging.getLogger("classify")

    overrides = {}
    for tok in [t.strip() for t in args.stages.split(",") if t.strip()]:
        if tok not in {"0", "2", "3", "4"}:
            ap.error(f"stadio '{tok}' non valido: gli stadi implementati sono 0, 2, 3, 4")
        overrides[f"stage{tok}"] = True
    cfg = load_config(Path(args.config), overrides)
    if args.workers:
        cfg["stages"]["stage3"]["workers"] = args.workers
    if not any(cfg["stages"].get(f"stage{n}", {}).get("enabled") for n in (0, 2, 3, 4)):
        print("Nessuno stadio attivo (sono tutti disattivati di default). Usa --stages 0,2,3,4 oppure imposta "
              "'enabled: true' in config_classify.yaml.")
        return 2

    lex_path = Path(cfg["lexicon"])
    lex = load_lexicon(lex_path if lex_path.is_absolute() else PROJECT_ROOT / lex_path)

    exit_code = 0
    for country in [c.upper() for c in args.country]:
        rows, source = load_rows(country, PROJECT_ROOT, Path(args.input) if args.input else None)
        sites = sites_from_rows(rows)
        if args.limit:
            sites = sites[: args.limit]
            rows = [r for s in sites for r in s.rows]      # l'output contiene solo le righe dei siti provati
        log.info("[%s] %d righe da %s -> %d siti distinti da classificare", country, len(rows), source, len(sites))
        if not sites:
            log.error("[%s] nessun host valido nell'input: niente da classificare", country)
            exit_code = 1
            continue
        cache_path = PROJECT_ROOT / "data" / "input" / country / "classify_cache.jsonl"
        if args.refresh and cache_path.exists():
            cache_path.rename(cache_path.with_suffix(".jsonl.old"))
        cache = Cache(cache_path, int(cfg["cache_ttl_days"]))
        pipe = Pipeline(cfg, lex, cache, country=country)
        out_path = PROJECT_ROOT / "data" / "output" / country / "siti_classificati.csv"
        verdicts = {}
        try:
            verdicts = pipe.run(sites)
        except KeyboardInterrupt:
            log.warning("[%s] interrotto: scrivo i risultati parziali (la cache conserva il lavoro fatto)", country)
            exit_code = 130
            verdicts = {s.key: pipe._verdict(s.key) for s in sites}  # noqa: SLF001
        counts = pipe.write_output(rows, sites, verdicts, out_path)
        by_nat: dict[str, int] = {}
        for (nat, _tipo), n in counts.items():
            by_nat[nat] = by_nat.get(nat, 0) + n
        log.info("[%s] scritto %s — per natura: %s", country, out_path, dict(sorted(by_nat.items())))
        if exit_code == 130:
            break
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
