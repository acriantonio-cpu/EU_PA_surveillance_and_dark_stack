#!/usr/bin/env python3
"""
04_export_enti_esteso.py

Prende il file Enti di IndicePA COMPLETO (tutte le righe, tutte le colonne
originali, non filtrato) e vi affianca:
- sector/country_code/regione/provincia (da 01_fetch_ipa_comuni.py)
- le colonne misurate da 02_probe_infra.py (NS/MX/hosting/ASN/DNSSEC/TLS/HTTP...)
- il riepilogo Dark Stack + cookie da 05_scrape_dark_stack.py, se disponibile
- il censimento software/licenze da 06_fingerprint_cms.py, se disponibile
- il tipo di ente rilevato dal testo homepage da 08_classifica_tipo_ente.py, se disponibile

Join su Codice_IPA. Output: un unico file, stessa granularità di enti.xlsx
(una riga per ente), pensato per essere importato in Minitab e
filtrato/analizzato lì (es. filtro per 'sector' o 'stato_probe').

Ogni ente di enti.xlsx compare sempre, anche se:
- non aveva un Sito_istituzionale valorizzabile (non è mai entrato in
  data/processed/siti.csv) -> colonna stato_probe = 'sito_mancante'
- aveva un sito ma non è stato ancora sondato, es. run parziale/--limit
  -> stato_probe = 'non_processato'
- è stato sondato -> stato_probe = 'processato' (le colonne di misura
  possono comunque essere vuote se le singole query DNS sono fallite:
  vedi colonna 'errori')

Va eseguito dalla cartella principale del progetto, dopo 02_probe_infra.py.

Esempio:
    python src\\04_export_enti_esteso.py
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

import config as cfgmod

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def dati_raw(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "raw" / paese


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


def dati_results(paese: str, run_id: str) -> Path:
    """Vedi 02_probe_infra.py, stessa funzione."""
    return PROJECT_ROOT / "data" / "results" / paese / run_id

# Colonne di siti.csv/risultati.csv/dark_stack.csv che duplicano informazioni
# già presenti (in forma più affidabile) in enti.xlsx o vengono riportate una
# sola volta: non le ripetiamo per evitare colonne doppie con nomi ambigui.
# 'codice_ipa' NON va qui: serve come chiave di join e viene tolta a parte
# dopo ciascun merge.
SITI_COLS_KEEP = ["codice_ipa", "sector", "country_code", "hostname", "hostname_condiviso_con_altri_enti"]
RISULTATI_COLS_DROP = {"comune", "regione", "hostname"}
DARK_STACK_COLS_DROP = {"comune", "regione", "hostname"}
CMS_FINGERPRINT_COLS_DROP = {"comune", "regione", "hostname"}
TIPO_ENTE_COLS_DROP = {"comune", "regione", "hostname"}

# Colonne il cui contenuto deve restare testo puro (codici con zeri
# iniziali, hostname, ASN): tutto il resto viene lasciato a inferenza
# automatica di pandas, altrimenti in Excel/Minitab arriverebbero come testo
# anche i numeri e i booleani (n_ns, spf_presente, ecc.), costringendo a
# riconvertirli a mano prima di qualunque statistica. Gli ASN vanno SEMPRE
# forzati a testo: se non lo fossero, un ASN come "15169" diventerebbe
# "15169.0" appena una riga ha un ASN mancante (NaN forza pandas a float64) —
# bug scoperto leggendo un export reale, non solo teorico.
COLONNE_SOLO_TESTO = ["codice_ipa", "hostname", "asn_a", "asn_mx", "asn_ns"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere: determina i percorsi di default sotto "
             "data/raw|processed|results/<paese> (default: IT).",
    )
    ap.add_argument("--enti", default=None, help="enti.xlsx completo (default: data/raw/<paese>/enti.xlsx)")
    ap.add_argument("--siti", default=None, help="output fase 1 (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--risultati", default=None, help="output fase 2 (default: data/results/<paese>/risultati.csv)")
    ap.add_argument("--dark-stack", default=None, help="output 05, opzionale (default: data/results/<paese>/dark_stack.csv)")
    ap.add_argument("--cms-fingerprint", default=None, help="output 06, opzionale (default: data/results/<paese>/cms_fingerprint.csv)")
    ap.add_argument("--tipo-ente", default=None, help="output 08, opzionale (default: data/results/<paese>/tipo_ente.csv)")
    ap.add_argument("--out", default=None, help="file di output (default: data/results/<paese>/<run-id>/enti_esteso.xlsx)")
    ap.add_argument("--run-id", default=None, help="Vedi 02_probe_infra.py --help (default: data UTC odierna, o l'ultimo run noto se --risultati non è passato esplicitamente)")
    ap.add_argument(
        "--anche-csv", action="store_true",
        help="Scrive anche una copia .csv accanto all'.xlsx (stesso nome, estensione diversa).",
    )
    args = ap.parse_args()

    paese = args.paese.strip().upper()
    # OTTIMIZZAZIONE (seconda passata): 04 gira tipicamente SUBITO dopo 02/05/06
    # nello stesso run_pipeline.py (stesso --run-id passato esplicitamente).
    # Se invece qualcuno lo lancia da solo senza --run-id, il fallback più
    # utile non è 'oggi' (potrebbe non esistere ancora nessun run di oggi)
    # ma l'ultimo run REALMENTE completato con successo, letto dal puntatore
    # — coerente con l'idea che 04 'esporta l'ultimo dato buono', non
    # necessariamente quello odierno.
    run_id = args.run_id or cfgmod.leggi_puntatore_latest(paese) or cfgmod.run_id_default()
    enti_path = args.enti or str(dati_raw(paese) / "enti.xlsx")
    siti_path = args.siti or str(dati_processed(paese) / "siti.csv")
    risultati_path = args.risultati or str(dati_results(paese, run_id) / "risultati.csv")
    dark_stack_path = args.dark_stack or str(dati_results(paese, run_id) / "dark_stack.csv")
    cms_fingerprint_path = args.cms_fingerprint or str(dati_results(paese, run_id) / "cms_fingerprint.csv")
    tipo_ente_path = args.tipo_ente or str(dati_results(paese, run_id) / "tipo_ente.csv")
    out_arg = args.out or str(dati_results(paese, run_id) / "enti_esteso.xlsx")

    for label, path in [("enti", enti_path), ("siti", siti_path), ("risultati", risultati_path)]:
        if not Path(path).exists():
            print(f"ERRORE: non trovo il file {label}: {path}", file=sys.stderr)
            sys.exit(1)

    print(f"[1/6] Carico enti.xlsx completo: {enti_path}", file=sys.stderr)
    enti = pd.read_excel(enti_path)
    if "Codice_IPA" not in enti.columns:
        print(f"ERRORE: colonna 'Codice_IPA' non trovata in {enti_path}", file=sys.stderr)
        sys.exit(1)
    n_enti_tot = len(enti)

    siti = pd.read_csv(siti_path, dtype=str)
    hostnames_con_sito = set(siti["codice_ipa"].astype(str))
    cols_siti = [c for c in SITI_COLS_KEEP if c in siti.columns]
    merged = enti.merge(siti[cols_siti], left_on="Codice_IPA", right_on="codice_ipa", how="left")
    merged = merged.drop(columns=["codice_ipa"])

    dtype_risultati = {c: str for c in COLONNE_SOLO_TESTO}
    risultati = pd.read_csv(risultati_path, dtype=dtype_risultati)
    if "codice_ipa" not in risultati.columns:
        print(f"ERRORE: {risultati_path} non ha una colonna 'codice_ipa', impossibile fare il join.", file=sys.stderr)
        sys.exit(1)
    hostnames_processati = set(risultati["codice_ipa"].astype(str))
    risultati = risultati.drop(columns=[c for c in RISULTATI_COLS_DROP if c in risultati.columns])

    print(f"[2/6] Joino {len(risultati)} righe misurate su {n_enti_tot} enti (chiave: Codice_IPA)", file=sys.stderr)
    merged = merged.merge(risultati, left_on="Codice_IPA", right_on="codice_ipa", how="left")
    merged = merged.drop(columns=["codice_ipa"])

    if Path(dark_stack_path).exists():
        dark_stack = pd.read_csv(dark_stack_path, dtype=dtype_risultati)
        dark_stack = dark_stack.drop(columns=[c for c in DARK_STACK_COLS_DROP if c in dark_stack.columns])
        print(f"[3/6] Joino {len(dark_stack)} righe di dark stack (05_scrape_dark_stack.py)", file=sys.stderr)
        merged = merged.merge(dark_stack, left_on="Codice_IPA", right_on="codice_ipa", how="left", suffixes=("", "_dark_stack"))
        merged = merged.drop(columns=["codice_ipa"])
    else:
        print(f"[3/6] {dark_stack_path} non trovato, salto il merge dark stack (esegui 05_scrape_dark_stack.py se vuoi includerlo)", file=sys.stderr)

    if Path(cms_fingerprint_path).exists():
        cms_fp = pd.read_csv(cms_fingerprint_path, dtype=dtype_risultati)
        cms_fp = cms_fp.drop(columns=[c for c in CMS_FINGERPRINT_COLS_DROP if c in cms_fp.columns])
        print(f"[4/6] Joino {len(cms_fp)} righe di censimento software (06_fingerprint_cms.py)", file=sys.stderr)
        merged = merged.merge(cms_fp, left_on="Codice_IPA", right_on="codice_ipa", how="left", suffixes=("", "_cms_fingerprint"))
        merged = merged.drop(columns=["codice_ipa"])
    else:
        print(f"[4/6] {cms_fingerprint_path} non trovato, salto il merge software/licenze (esegui 06_fingerprint_cms.py se vuoi includerlo)", file=sys.stderr)

    if Path(tipo_ente_path).exists():
        tipo_ente = pd.read_csv(tipo_ente_path, dtype=dtype_risultati)
        tipo_ente = tipo_ente.drop(columns=[c for c in TIPO_ENTE_COLS_DROP if c in tipo_ente.columns])
        print(f"[5/6] Joino {len(tipo_ente)} righe di classificazione tipo ente (08_classifica_tipo_ente.py)", file=sys.stderr)
        merged = merged.merge(tipo_ente, left_on="Codice_IPA", right_on="codice_ipa", how="left", suffixes=("", "_tipo_ente"))
        merged = merged.drop(columns=["codice_ipa"])
    else:
        print(f"[5/6] {tipo_ente_path} non trovato, salto il merge tipo ente (esegui 08_classifica_tipo_ente.py se vuoi includerlo)", file=sys.stderr)

    codici_ipa = enti["Codice_IPA"].astype(str)

    def stato(codice: str) -> str:
        if codice not in hostnames_con_sito:
            return "sito_mancante"
        if codice not in hostnames_processati:
            return "non_processato"
        return "processato"

    merged.insert(0, "stato_probe", codici_ipa.map(stato))

    print("[6/6] Scrivo output", file=sys.stderr)
    Path(out_arg).parent.mkdir(parents=True, exist_ok=True)
    cfgmod.backup_se_esiste(Path(out_arg))
    merged.to_excel(out_arg, index=False)
    if args.anche_csv:
        csv_path = str(out_arg).rsplit(".", 1)[0] + ".csv"
        cfgmod.backup_se_esiste(Path(csv_path))
        merged.to_csv(csv_path, index=False, encoding="utf-8")

    conteggi = merged["stato_probe"].value_counts().to_dict()
    print(
        f"\nFatto. {len(merged)} righe (= tutti gli enti di enti.xlsx) scritte in {out_arg}.\n"
        f"Stato probe: {conteggi}",
        file=sys.stderr,
    )
    cfgmod.aggiorna_puntatore_latest(paese, run_id)


if __name__ == "__main__":
    main()
