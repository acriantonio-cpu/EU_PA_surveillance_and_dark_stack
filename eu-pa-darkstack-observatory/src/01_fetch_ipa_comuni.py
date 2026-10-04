#!/usr/bin/env python3
"""
01_fetch_ipa_comuni.py

Scarica (o carica da file locale) il dataset "Enti" di IndicePA (CC-BY 4.0),
normalizza il campo Sito_istituzionale e produce un elenco di siti pronto
per la sonda infrastrutturale (02_probe_infra.py).

Copre TUTTI i tipi di ente (comuni, scuole, ASL, università, ecc. — vedi
colonna Codice_Categoria), non solo i comuni. Filtro opzionale per
categoria/regione via --categoria/--regione.

Il join ente -> comune/provincia/regione usa il codice catastale ufficiale
(Codice_catastale_comune in enti.xlsx, presente per ogni ente perché indica
il comune di sede legale) invece di un match euristico sul nome: funziona
per qualsiasi tipologia di ente, non solo per i comuni.

Va eseguito dalla cartella principale del progetto (project root), così i
percorsi di default puntano correttamente dentro data/.
"""

import argparse
import io
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import requests

import config as cfgmod

# --- Percorsi di progetto ---
# Organizzati per paese (data/<tipo>/<CODICE_PAESE>/...): per ora l'unica
# logica di estrazione implementata è quella italiana (IndicePA), ma la
# struttura è pronta per affiancare in futuro altri paesi con i loro script
# di fetch dedicati (l'estrazione IPA è specifica dell'Italia, non
# generalizzabile a un altro paese passando solo --paese).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def dati_raw(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "raw" / paese


def dati_processed(paese: str) -> Path:
    return PROJECT_ROOT / "data" / "processed" / paese


ENTI_URL = (
    "https://indicepa.gov.it/ipa-dati/dataset/5baa3eb8-266e-455a-8de8-b1f434c279b2/"
    "resource/d09adf99-dc10-4349-8c53-27b1e5aa97b6/download/enti.xlsx"
)
COMUNI_JSON_URL = (
    "https://raw.githubusercontent.com/matteocontrini/comuni-json/master/comuni.json"
)

# Tassonomia omogenea per confronti futuri fra paesi UE (vedi idea "estendibilità
# europea"): mappa il Codice_Categoria di IndicePA (specifico italiano) su un
# tag di settore neutro. Elenco parziale sui codici più numerosi; tutto il
# resto finisce in "altro" — va arricchita quando servirà granularità maggiore,
# senza rompere l'output (colonna sempre presente).
CATEGORIA_TO_SECTOR = {
    "L6": "comune",
    "L7": "sanita",       # ASL/Aziende sanitarie/ospedaliere
    "L17": "universita",
    "L33": "scuola",
    "L18": "ministero",
    "L34": "provincia",
    "L4": "regione",
    "L37": "camera_commercio",
}


def sector_from_categoria(cod: str) -> str:
    return CATEGORIA_TO_SECTOR.get(str(cod).strip().upper(), "altro")


def normalize_name(name: str) -> str:
    """Normalizza una stringa per confronti case/accent-insensitive (usata solo per --regione).
    Tronca al primo '/' prima di normalizzare: alcuni nomi ufficiali ISTAT
    sono bilingui (es. "Valle d'Aosta/Vallée d'Aoste", "Trentino-Alto
    Adige/Südtirol") e il nome comune con cui un utente cerca la regione
    non include la parte dopo lo slash — senza il troncamento, --regione
    "Valle d'Aosta" non troverebbe mai corrispondenza."""
    if not isinstance(name, str):
        return ""
    name = name.split("/")[0].strip().lower()
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_url(raw: str):
    """Ritorna (url_normalizzato, hostname) oppure (None, None) se non valorizzabile."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    url = raw.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    host_bare = host[4:] if host.startswith("www.") else host
    # host_bare deve contenere un punto E almeno un carattere alfanumerico:
    # esclude scarti tipo "." o "--" che altrimenti supererebbero il solo
    # controllo "c'è un punto" (visti in produzione: 5 enti con
    # Sito_istituzionale=".").
    if not host_bare or "." not in host_bare or not re.search(r"[a-z0-9]", host_bare):
        return None, None
    return url, host_bare


def load_enti(enti_file: str | None, default_path: Path) -> pd.DataFrame:
    if enti_file:
        path = Path(enti_file)
        print(f"[1/4] Carico dataset Enti da file locale: {path}", file=sys.stderr)
        return pd.read_excel(path)

    if default_path.exists():
        print(f"[1/4] Trovato {default_path}, lo uso senza scaricare nulla.", file=sys.stderr)
        return pd.read_excel(default_path)

    print(f"[1/4] Nessun file locale trovato, scarico da IndicePA: {ENTI_URL}", file=sys.stderr)
    try:
        resp = requests.get(ENTI_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(
            f"\nERRORE: impossibile scaricare {ENTI_URL} ({exc}).\n"
            "Segui la guida in docs/GUIDA_DOWNLOAD_IPA.md per scaricare il file a mano\n"
            f"e salvarlo come {default_path}, oppure passa --enti-file percorso\\enti.xlsx\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return pd.read_excel(io.BytesIO(resp.content))


def load_comuni_catastale(comuni_file: str | None, default_path: Path) -> dict:
    """Anagrafica comuni chiave = codice catastale (es. 'H501'), non nome:
    è la stessa chiave presente in enti.xlsx (Codice_catastale_comune) per
    OGNI ente, quale che sia la sua tipologia."""
    if comuni_file:
        path = Path(comuni_file)
        print(f"[2/4] Carico anagrafica comuni da file locale: {path}", file=sys.stderr)
        data = json.loads(path.read_text(encoding="utf-8"))
    elif default_path.exists():
        print(f"[2/4] Trovato {default_path}, lo uso senza scaricare nulla.", file=sys.stderr)
        data = json.loads(default_path.read_text(encoding="utf-8"))
    else:
        print("[2/4] Scarico anagrafica comuni da GitHub", file=sys.stderr)
        resp = requests.get(COMUNI_JSON_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        default_path.parent.mkdir(parents=True, exist_ok=True)
        default_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    lookup = {}
    for c in data:
        cat = (c.get("codiceCatastale") or "").strip().upper()
        if not cat:
            continue
        lookup[cat] = {
            "comune": c.get("nome", ""),
            "provincia": (c.get("provincia") or {}).get("nome", ""),
            "regione": (c.get("regione") or {}).get("nome", ""),
        }
    return lookup


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paese", default="IT",
        help="Codice paese ISO a due lettere, usato per organizzare data/raw/<paese> e "
             "data/processed/<paese> (default: IT — per ora l'unica logica di estrazione "
             "implementata è quella italiana di IndicePA).",
    )
    ap.add_argument("--regione", required=False, default=None, help="Nome regione, es. 'Lombardia'")
    ap.add_argument(
        "--categoria", required=False, default=None,
        help="Filtra per Codice_Categoria IPA, uno o più separati da virgola "
             "(es. 'L6' comuni, 'L7' ASL, 'L17' università, 'L33' scuole). "
             "Default: nessun filtro, tutti gli enti.",
    )
    ap.add_argument("--out", default=None, help="File CSV di output (default: data/processed/<paese>/siti.csv)")
    ap.add_argument("--enti-file", default=None, help="Percorso locale a enti.xlsx (se già scaricato altrove)")
    ap.add_argument("--comuni-file", default=None, help="Percorso locale a comuni.json (se già scaricato altrove)")
    args = ap.parse_args()

    paese = args.paese.strip().upper()
    out_path = Path(args.out) if args.out else dati_processed(paese) / "siti.csv"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    enti = load_enti(args.enti_file, dati_raw(paese) / "enti.xlsx")
    lookup = load_comuni_catastale(args.comuni_file, dati_raw(paese) / "comuni.json")

    print("[3/4] Joino ogni ente con comune/provincia/regione via codice catastale", file=sys.stderr)
    required_cols = {"Codice_IPA", "Denominazione_ente", "Codice_catastale_comune"}
    missing_cols = required_cols - set(enti.columns)
    if missing_cols:
        print(f"ERRORE: colonne mancanti nel file: {missing_cols}. Colonne trovate: {list(enti.columns)}", file=sys.stderr)
        sys.exit(1)

    enti = enti.copy()
    enti["_cat"] = enti["Codice_catastale_comune"].astype(str).str.strip().str.upper()
    geo = enti["_cat"].map(lookup)
    enti["comune"] = geo.apply(lambda d: d["comune"] if isinstance(d, dict) else "")
    enti["provincia"] = geo.apply(lambda d: d["provincia"] if isinstance(d, dict) else "")
    enti["regione"] = geo.apply(lambda d: d["regione"] if isinstance(d, dict) else "")
    n_no_geo = int((geo.isna()).sum())
    if n_no_geo:
        print(f"    ({n_no_geo} enti senza corrispondenza nell'anagrafica comuni: codice catastale mancante/non trovato)", file=sys.stderr)

    subset = enti
    if args.categoria:
        wanted = {c.strip().upper() for c in args.categoria.split(",") if c.strip()}
        subset = subset[subset["Codice_Categoria"].astype(str).str.upper().isin(wanted)].copy()
        if subset.empty:
            print(f"ERRORE: nessun ente trovato per categoria={sorted(wanted)}.", file=sys.stderr)
            sys.exit(1)
        print(f"    Filtrato per categoria {sorted(wanted)}: {len(subset)} enti", file=sys.stderr)

    if args.regione:
        target_norm = normalize_name(args.regione)
        regione_mask = subset["regione"].apply(normalize_name) == target_norm
        subset = subset[regione_mask].copy()
        if subset.empty:
            regioni_disponibili = sorted(set(v["regione"] for v in lookup.values() if v.get("regione")))
            print(
                f"ERRORE: nessun ente trovato per regione='{args.regione}'.\n"
                f"Regioni disponibili nell'anagrafica: {regioni_disponibili}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[4/4] Normalizzo URL per {len(subset)} enti in {args.regione}", file=sys.stderr)
    else:
        print(f"[4/4] Normalizzo URL per tutti i {len(subset)} enti d'Italia", file=sys.stderr)

    rows = []
    n_missing = 0
    for _, r in subset.iterrows():
        url_norm, host = normalize_url(r.get("Sito_istituzionale", ""))
        if not host:
            n_missing += 1
            continue
        rows.append(
            {
                "codice_ipa": r.get("Codice_IPA", ""),
                "denominazione_ente": r.get("Denominazione_ente", ""),
                "codice_categoria": r.get("Codice_Categoria", ""),
                "sector": sector_from_categoria(r.get("Codice_Categoria", "")),
                "country_code": paese,
                "comune": r.get("comune", ""),
                "provincia": r.get("provincia", ""),
                "regione": r.get("regione", ""),
                "url_originale": r.get("Sito_istituzionale", ""),
                "url_normalizzato": url_norm,
                "hostname": host,
            }
        )

    out_df = pd.DataFrame(rows)

    if out_df.empty:
        print(
            f"\nERRORE: Trovati {len(subset)} enti, ma nessun URL valido nella colonna 'Sito_istituzionale'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # NON deduplichiamo per hostname: enti diversi che condividono lo stesso
    # sito (unioni di comuni, portali scolastici comuni, consorzi) devono
    # comparire ciascuno con la propria riga, altrimenti spariscono dal
    # dataset finale (04_export_enti_esteso.py) come se non avessero un sito.
    # La sonda (02_probe_infra.py) si occupa di non interrogare due volte lo
    # stesso hostname, riusando il risultato per le righe che lo condividono.
    dup_counts = out_df["hostname"].value_counts()
    out_df["hostname_condiviso_con_altri_enti"] = out_df["hostname"].map(lambda h: bool(dup_counts[h] > 1))
    n_hostname_condivisi = int((dup_counts > 1).sum())
    n_enti_su_hostname_condivisi = int(out_df["hostname_condiviso_con_altri_enti"].sum())

    cfgmod.backup_se_esiste(out_path)  # non perdere siti.csv precedente se lo script viene rilanciato per lo stesso paese
    out_df.to_csv(out_path, index=False, encoding="utf-8")

    print(
        f"\nFatto. {len(out_df)} righe scritte in {out_path} "
        f"({n_missing} enti scartati per Sito_istituzionale mancante/non valorizzabile; "
        f"{out_df['hostname'].nunique()} hostname distinti, di cui {n_hostname_condivisi} "
        f"condivisi da più enti per un totale di {n_enti_su_hostname_condivisi} righe).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()