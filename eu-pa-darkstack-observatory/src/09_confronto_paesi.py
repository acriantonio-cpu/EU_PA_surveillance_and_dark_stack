#!/usr/bin/env python3
"""
09_confronto_paesi.py

Unico script della pipeline che lavora su PIÙ Paesi insieme invece che su
uno alla volta: 01-08 sono tutti "per Paese" (--paese IT, --paese FR, ...,
ognuno con il proprio data/results/<paese>/<run_id>/), voluto così perché
ogni Paese ha le sue fonti/tempi/lingua di raccolta e non ha senso doverli
processare tutti insieme per forza. Questo script invece va eseguito DOPO
aver girato 03_analyze.py per ciascun Paese che si vuole confrontare:
scansiona data/results/<PAESE>/latest_run.json per ogni sottocartella di
data/results/, legge la sezione 'tipo_ente' del report.json di quel run
(scritta da 03_analyze.py a partire da 08_classifica_tipo_ente.py) e
costruisce una matrice tipo_ente x Paese — "quante scuole/comuni/ospedali
per Paese", "che percentuale è classificata", "tempo/peso pagina medio per
Paese", "quota di possibili enti non pubblici per Paese".

Un Paese senza 'tipo_ente' nel suo report.json (perché 08 non è stato
eseguito o tipo_ente_classificazione=false in quel run) viene SALTATO con un
avviso, non riempito a zero: zero comuni rilevati per un Paese vorrebbe dire
"il classificatore non ha trovato nulla", diverso da "il dato non è mai
stato raccolto" — confonderli userebbe l'assenza di dato come se fosse un
risultato.

Va eseguito dalla cartella principale del progetto, dopo aver girato
03_analyze.py (via run_pipeline.py o a mano) per ciascun Paese di interesse.

Esempi:
    python src\\09_confronto_paesi.py
    python src\\09_confronto_paesi.py --paesi IT,FR,AT
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

import config as cfgmod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "data" / "results"
OUT_DIR = RESULTS_DIR / "_confronto_UE"


def scopri_paesi() -> list[str]:
    """Ogni sottocartella di data/results/ che non inizia per '_' (riservato
    all'output di QUESTO script, per non provare a leggerlo come se fosse
    un Paese) e ha un latest_run.json è un Paese candidato."""
    if not RESULTS_DIR.exists():
        return []
    trovati = []
    for d in sorted(RESULTS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith("_") and (d / "latest_run.json").exists():
            trovati.append(d.name)
    return trovati


def carica_tipo_ente_paese(paese: str):
    """Ritorna (run_id, sezione_tipo_ente_del_report) o (run_id, None) se il
    Paese ha un run ma senza sezione 'tipo_ente' (08 non eseguito in quel
    run)."""
    run_id = cfgmod.leggi_puntatore_latest(paese)
    if not run_id:
        return None, None
    report_path = RESULTS_DIR / paese / run_id / "report.json"
    if not report_path.exists():
        return run_id, None
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    return run_id, report.get("tipo_ente")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paesi", default=None,
        help="Lista di codici Paese separati da virgola (es. IT,FR,AT). Default: tutte le "
             "sottocartelle di data/results/ con un latest_run.json (scoperta automatica).",
    )
    ap.add_argument("--out-dir", default=str(OUT_DIR), help=f"Cartella di output (default: {OUT_DIR})")
    args = ap.parse_args()

    paesi = [p.strip().upper() for p in args.paesi.split(",")] if args.paesi else scopri_paesi()
    if not paesi:
        print(
            "Nessun Paese trovato sotto data/results/ (nessuna sottocartella con "
            "latest_run.json). Esegui prima run_pipeline.py per almeno un Paese.",
            file=sys.stderr,
        )
        sys.exit(1)

    dati_per_paese = {}
    saltati = []
    for paese in paesi:
        run_id, sezione = carica_tipo_ente_paese(paese)
        if run_id is None:
            saltati.append((paese, "nessun run completato (latest_run.json assente)"))
            continue
        if sezione is None:
            saltati.append((paese, f"run {run_id} trovato ma senza sezione 'tipo_ente' nel report (08_classifica_tipo_ente.py non eseguito o tipo_ente_classificazione=false in quel run)"))
            continue
        dati_per_paese[paese] = {"run_id": run_id, "tipo_ente": sezione}

    for paese, motivo in saltati:
        print(f"[saltato] {paese}: {motivo}", file=sys.stderr)

    if not dati_per_paese:
        print("\nNessun Paese con dati 'tipo_ente' disponibili: niente da confrontare.", file=sys.stderr)
        sys.exit(1)

    # --- Matrice conteggi: righe = categoria, colonne = Paese ---
    tutte_categorie = sorted({
        cat for d in dati_per_paese.values() for cat in d["tipo_ente"]["distribuzione_categorie"]
    })
    righe_conteggi = []
    for categoria in tutte_categorie:
        riga = {"categoria": categoria}
        for paese, d in dati_per_paese.items():
            riga[paese] = d["tipo_ente"]["distribuzione_categorie"].get(categoria, 0)
        righe_conteggi.append(riga)
    df_conteggi = pd.DataFrame(righe_conteggi).set_index("categoria")
    # Riga finale con il totale campione per Paese: senza, chi legge il CSV
    # non può calcolare le percentuali per colonna senza tornare al JSON.
    df_conteggi.loc["TOTALE_CAMPIONE"] = {p: d["tipo_ente"]["dimensione_campione"] for p, d in dati_per_paese.items()}

    # --- Tabella di sintesi: una riga per Paese con le metriche scalari ---
    righe_sintesi = []
    for paese, d in dati_per_paese.items():
        te = d["tipo_ente"]
        tempo = te.get("tempo_download_pagina_completa", {})
        peso = te.get("peso_pagina", {})
        non_pub = te.get("possibile_ente_non_pubblico", {})
        accordo = te.get("accordo_con_sector")
        righe_sintesi.append({
            "paese": paese,
            "run_id": d["run_id"],
            "dimensione_campione": te["dimensione_campione"],
            "pct_classificato": te["pct_classificato"],
            "tempo_download_pagina_mediana_ms": tempo.get("mediana_ms"),
            "peso_pagina_mediana_kb": peso.get("mediana_kb"),
            "pct_possibile_non_pubblico": non_pub.get("pct_del_campione"),
            "pct_accordo_con_sector": accordo.get("pct_accordo") if accordo else None,
        })
    df_sintesi = pd.DataFrame(righe_sintesi).set_index("paese").sort_index()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conteggi_path = out_dir / "confronto_tipo_ente_per_paese.csv"
    sintesi_path = out_dir / "confronto_sintesi_per_paese.csv"
    cfgmod.backup_se_esiste(conteggi_path)
    cfgmod.backup_se_esiste(sintesi_path)
    df_conteggi.to_csv(conteggi_path, encoding="utf-8")
    df_sintesi.to_csv(sintesi_path, encoding="utf-8")

    manifest = {
        "paesi_inclusi": {p: d["run_id"] for p, d in dati_per_paese.items()},
        "paesi_saltati": {p: motivo for p, motivo in saltati},
        "script": "09_confronto_paesi.py",
    }
    with open(out_dir / "confronto_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(
        f"\nFatto. Confronto fra {len(dati_per_paese)} Paesi ({', '.join(sorted(dati_per_paese))}) scritto in:\n"
        f"  {conteggi_path}\n"
        f"  {sintesi_path}\n"
        + (f"\n{len(saltati)} Paese/i saltati, vedi sopra e confronto_manifest.json." if saltati else ""),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
